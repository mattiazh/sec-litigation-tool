from __future__ import annotations

import base64
import importlib.util
import sqlite3
import tempfile
import uuid
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components


APP_DIR = Path(__file__).resolve().parent
BACKEND_FILE = APP_DIR / "sec_backend.py"
KEYWORDS_FILE = APP_DIR / "keywords.txt"
MAX_RELEASES_PER_RUN = 75
UI_VERSION = "assembly-line-v2"

st.set_page_config(
    page_title="SEC Litigation Release Tool",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)


# -----------------------------------------------------------------------------
# VISUAL DESIGN
# -----------------------------------------------------------------------------

st.markdown(
    """
    <style>
    .block-container {
        max-width: 1120px;
        padding-top: 1.0rem;
        padding-bottom: 2.2rem;
    }
    [data-testid="stSidebar"] {
        border-right: 1px solid #dfe5ec;
    }
    .sec-hero {
        padding: .82rem 1.05rem;
        border-radius: 13px;
        background: linear-gradient(135deg, #173f66 0%, #245f91 100%);
        color: white;
        margin-bottom: .75rem;
        box-shadow: 0 4px 14px rgba(25, 75, 122, 0.13);
    }
    .sec-hero h1 {
        margin: 0;
        font-size: 1.42rem;
        font-weight: 700;
        letter-spacing: -0.02em;
    }
    .sec-hero p {
        margin: .18rem 0 0;
        opacity: .86;
        font-size: .86rem;
    }
    .work-card {
        padding: .8rem 1rem;
        border: 1px solid #dfe5ec;
        border-radius: 12px;
        background: #ffffff;
        margin: .55rem 0 .8rem;
    }
    .work-card-title {
        font-size: 1.12rem;
        font-weight: 750;
        color: #183b5b;
        margin-bottom: .12rem;
    }
    .work-card-subtitle {
        color: #5d6c7b;
        font-size: .88rem;
    }
    .next-action {
        margin: .45rem 0 .55rem;
        color: #194b7a;
        font-size: .78rem;
        font-weight: 800;
        letter-spacing: .055em;
        text-transform: uppercase;
    }
    .compact-status {
        color: #607080;
        font-size: .84rem;
    }
    .status-complete { color: #137333; font-weight: 700; }
    .status-review { color: #9a6700; font-weight: 700; }
    .status-unavailable { color: #b3261e; font-weight: 700; }
    .status-ready { color: #194b7a; font-weight: 700; }
    .small-note {
        font-size: .82rem;
        color: #657586;
    }
    div[data-testid="stMetric"] {
        background: white;
        border: 1px solid #e1e7ee;
        padding: .45rem .65rem;
        border-radius: 10px;
    }
    div[data-testid="stTextArea"] textarea {
        font-size: .9rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# SESSION / BACKEND SETUP
# -----------------------------------------------------------------------------


def initialize_session_state():
    defaults = {
        "session_id": uuid.uuid4().hex[:12],
        "range_start": None,
        "range_end": None,
        "run_ready": False,
        "selected_release": None,
        "case_ids": {},
        "validation_results": {},
        "excel_bytes": None,
        "excel_name": None,
        "last_processing_log": [],
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    if "session_dir" not in st.session_state:
        session_dir = (
            Path(tempfile.gettempdir())
            / f"sec_litigation_web_{st.session_state.session_id}"
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        st.session_state.session_dir = str(session_dir)
        st.session_state.database_file = str(session_dir / "sec_session.db")
        st.session_state.export_dir = str(session_dir / "exports")


@st.cache_resource(show_spinner=False)
def load_isolated_backend(session_id: str, database_file: str, session_dir: str):
    """Loads one independent backend module per browser session."""

    module_name = f"sec_backend_{session_id}"
    spec = importlib.util.spec_from_file_location(module_name, BACKEND_FILE)

    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load sec_backend.py.")

    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)

    backend.DATABASE_FILE = Path(database_file)
    backend.BACKUP_FOLDER = Path(session_dir) / "backups"
    backend.KEYWORDS_FILE = KEYWORDS_FILE
    backend.create_database()
    return backend


initialize_session_state()
backend = load_isolated_backend(
    st.session_state.session_id,
    st.session_state.database_file,
    st.session_state.session_dir,
)
DATABASE_FILE = Path(st.session_state.database_file)
EXPORT_DIR = Path(st.session_state.export_dir)


# -----------------------------------------------------------------------------
# DATABASE HELPERS FOR THE WEB UI
# -----------------------------------------------------------------------------


def db_connection():
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def clear_session_database():
    if DATABASE_FILE.exists():
        DATABASE_FILE.unlink()
    backend.create_database()


def infer_range_from_database():
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT CAST(SUBSTR(release_no, 4) AS INTEGER) AS release_number
            FROM releases
            WHERE release_no LIKE 'LR-%'
            """
        ).fetchall()

    numbers = [row["release_number"] for row in rows if row["release_number"]]

    if not numbers:
        return None, None

    return max(numbers), min(numbers)


def release_numbers_for_current_run():
    if st.session_state.range_start is None or st.session_state.range_end is None:
        return []

    return list(
        range(
            int(st.session_state.range_start),
            int(st.session_state.range_end) - 1,
            -1,
        )
    )


def load_release_record(release_no: str):
    with db_connection() as connection:
        row = connection.execute(
            """
            SELECT
                date,
                respondents,
                release_no,
                keyword,
                content,
                complaint,
                link_to_release,
                link_to_complaint
            FROM releases
            WHERE release_no = ?
            """,
            (release_no,),
        ).fetchone()

        if row is None:
            return None

        document_rows = connection.execute(
            """
            SELECT
                document_name,
                document_url,
                extracted_text,
                page_count,
                character_count,
                characters_per_page,
                extraction_successful,
                manual_review_required,
                review_reason,
                COALESCE(text_source, 'automatic') AS text_source
            FROM documents
            WHERE release_no = ?
            ORDER BY id ASC
            """,
            (release_no,),
        ).fetchall()

    documents = []

    for document in document_rows:
        documents.append(
            {
                "name": document["document_name"],
                "url": document["document_url"],
                "text": document["extracted_text"] or "",
                "page_count": document["page_count"] or 0,
                "character_count": document["character_count"] or 0,
                "characters_per_page": document["characters_per_page"] or 0,
                "extraction_successful": bool(document["extraction_successful"]),
                "manual_review_required": bool(document["manual_review_required"]),
                "review_reason": document["review_reason"] or "",
                "text_source": document["text_source"] or "automatic",
                "reused_from_database": True,
            }
        )

    manual_documents = [
        document["name"]
        for document in documents
        if document["manual_review_required"]
    ]

    return {
        "Date": row["date"] or "",
        "Respondents": row["respondents"] or "",
        "Release No.": row["release_no"],
        "Keyword": row["keyword"] or "",
        "Content": row["content"] or "",
        "Complaint": row["complaint"] or "",
        "Link to Release": row["link_to_release"] or "",
        "Link to Complaint": row["link_to_complaint"] or "",
        "Documents": documents,
        "Keyword Generation Allowed": bool((row["content"] or "").strip()),
        "Complaint Summary Allowed": not manual_documents,
        "Manual Review Documents": manual_documents,
    }


def get_release_status(release_no: str):
    record = load_release_record(release_no)

    if record is None:
        return "missing", "Not stored"

    if not any(
        (
            record["Date"].strip(),
            record["Respondents"].strip(),
            record["Content"].strip(),
        )
    ):
        return "unavailable", "SEC page unavailable / not extractable"

    manual_count = sum(
        1 for document in record["Documents"] if document["manual_review_required"]
    )

    if manual_count:
        return "manual", f"{manual_count} PDF(s) need manual text"

    if record["Keyword"].strip() and record["Complaint"].strip():
        return "complete", "Complete"

    return "chatgpt", "Ready for ChatGPT review"


def all_statuses():
    statuses = []

    for number in release_numbers_for_current_run():
        release_no = f"LR-{number}"
        status, detail = get_release_status(release_no)
        statuses.append((release_no, status, detail))

    return statuses


def count_statuses():
    counts = {
        "complete": 0,
        "manual": 0,
        "chatgpt": 0,
        "unavailable": 0,
        "missing": 0,
    }

    for _, status, _ in all_statuses():
        counts[status] = counts.get(status, 0) + 1

    return counts


def first_incomplete_release():
    """Returns the next release that still requires user work."""

    for release_no, status, _ in all_statuses():
        if status in {"manual", "chatgpt"}:
            return release_no

    return None


def select_release(release_no: str | None):
    """Selects the release shown in the main work area."""

    st.session_state.selected_release = release_no


def advance_to_next_task():
    """Moves the assembly line to the next release that still needs work."""

    select_release(first_incomplete_release())


def validate_uploaded_database(uploaded_bytes: bytes):
    temp_path = Path(st.session_state.session_dir) / "resume_validation.db"
    temp_path.write_bytes(uploaded_bytes)

    try:
        connection = sqlite3.connect(temp_path)
        release_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='releases'"
        ).fetchone()
        documents_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
        connection.close()

        if not release_table or not documents_table:
            return False, "The uploaded file is not a valid SEC Litigation progress database."

        return True, ""

    except sqlite3.DatabaseError as error:
        return False, f"SQLite could not read the uploaded file: {error}"

    finally:
        if temp_path.exists():
            temp_path.unlink()


def reset_review_state():
    st.session_state.case_ids = {}
    st.session_state.validation_results = {}
    st.session_state.excel_bytes = None
    st.session_state.excel_name = None


# -----------------------------------------------------------------------------
# UI HELPERS
# -----------------------------------------------------------------------------


def render_copy_button(text: str, key: str, label: str = "Copy prompt to clipboard"):
    """Browser-side clipboard button with a normal text fallback below it."""

    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    html = f"""
    <div style="font-family: Arial, sans-serif;">
      <button id="copy-{key}" style="
          background:#194B7A;color:#fff;border:0;border-radius:8px;
          padding:9px 14px;font-weight:600;cursor:pointer;width:100%;">
          {label}
      </button>
      <div id="msg-{key}" style="font-size:12px;color:#5d6b78;margin-top:5px;"></div>
    </div>
    <script>
    const button = document.getElementById('copy-{key}');
    const message = document.getElementById('msg-{key}');
    button.addEventListener('click', async () => {{
        try {{
            const raw = atob('{encoded}');
            const bytes = Uint8Array.from(raw, c => c.charCodeAt(0));
            const text = new TextDecoder('utf-8').decode(bytes);
            await navigator.clipboard.writeText(text);
            message.textContent = 'Copied. Paste it into your dedicated ChatGPT chat.';
            message.style.color = '#137333';
        }} catch (error) {{
            message.textContent = 'Browser copy was blocked. Use the copy icon on the source block below.';
            message.style.color = '#9a6700';
        }}
    }});
    </script>
    """
    components.html(html, height=66)


def status_label(status: str):
    labels = {
        "complete": "✅ Complete",
        "manual": "⚠️ Manual PDF",
        "chatgpt": "🟦 ChatGPT review",
        "unavailable": "⛔ Unavailable",
        "missing": "⛔ Missing",
    }
    return labels.get(status, status)


def render_header():
    st.markdown(
        """
        <div class="sec-hero">
          <h1>SEC Litigation Release Tool</h1>
          <p>One release at a time. Complete the current task and the next case opens automatically.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_workflow_progress(current_release: str | None):
    """Compact progress indicator that stays above the current task."""

    if not st.session_state.run_ready:
        return

    counts = count_statuses()
    total = len(release_numbers_for_current_run())
    resolved = counts["complete"] + counts["unavailable"] + counts["missing"]
    ratio = resolved / total if total else 0

    left, right = st.columns([4, 1.25])
    with left:
        st.caption(
            f"LR-{st.session_state.range_start} → LR-{st.session_state.range_end}  ·  "
            f"{resolved} of {total} resolved"
        )
        st.progress(ratio)
    with right:
        st.metric("Complete", counts["complete"])

    if counts["manual"] or counts["chatgpt"] or counts["unavailable"]:
        st.caption(
            f"Pending: {counts['manual']} manual PDF · {counts['chatgpt']} ChatGPT · "
            f"{counts['unavailable'] + counts['missing']} unavailable"
        )

    if current_release:
        st.markdown(
            f'<div class="compact-status">Current task: <strong>{current_release}</strong></div>',
            unsafe_allow_html=True,
        )


# -----------------------------------------------------------------------------
# RANGE PROCESSING
# -----------------------------------------------------------------------------


def process_release_interval(start_number: int, end_number: int):
    release_numbers = list(range(start_number, end_number - 1, -1))
    progress = st.progress(0, text="Preparing SEC releases…")
    current = st.empty()
    log = []

    for index, release_number in enumerate(release_numbers, start=1):
        release_no = f"LR-{release_number}"
        current.info(f"Processing {release_no} ({index} of {len(release_numbers)})")

        try:
            record = backend.extract_release(release_number)

            if record is None:
                backend.save_failed_release_placeholder(release_number)
                log.append(f"{release_no}: unavailable / placeholder saved")
            else:
                backend.save_release(record)
                backend.save_documents(record["Release No."], record["Documents"])

                manual_count = sum(
                    1
                    for document in record["Documents"]
                    if document["manual_review_required"]
                )

                if manual_count:
                    log.append(
                        f"{release_no}: extracted; {manual_count} PDF(s) need manual text"
                    )
                else:
                    log.append(f"{release_no}: extracted successfully")

        except Exception as error:
            backend.save_failed_release_placeholder(release_number)
            log.append(f"{release_no}: processing error — {error}")

        progress.progress(
            index / len(release_numbers),
            text=f"Processed {index} of {len(release_numbers)} releases",
        )

    current.empty()
    progress.empty()
    st.session_state.last_processing_log = log


# -----------------------------------------------------------------------------
# REVIEW / VALIDATION
# -----------------------------------------------------------------------------


def validate_chatgpt_submission(record, case_id, source_block, response_text):
    parsed, parse_error = backend.parse_chatgpt_response(
        response_text,
        expected_source_block=source_block,
    )

    if parse_error:
        return {
            "ok": False,
            "parse_error": parse_error,
            "checks": [],
            "issues": [],
            "parsed": None,
            "keyword": None,
        }

    checks = []
    issues = []

    identity_checks, identity_issues = backend.validate_identity(
        record,
        case_id,
        parsed,
    )
    checks.extend(identity_checks)
    issues.extend(identity_issues)

    summary_checks, summary_issues = backend.validate_summary_structure(
        parsed["complaint"]
    )
    checks.extend(summary_checks)
    issues.extend(summary_issues)

    # Enforce the user's standardized case-caption convention in the web version.
    summary_lines = [line.strip() for line in parsed["complaint"].splitlines() if line.strip()]
    case_lines = [line for line in summary_lines[:8] if line.replace("**", "").casefold().startswith("case:")]

    if case_lines:
        case_value = case_lines[0].replace("**", "").split(":", 1)[1].strip()
        if case_value.startswith("SEC v."):
            checks.append("The Case caption uses the required 'SEC v.' format.")
        else:
            issues.append(
                {
                    "severity": "critical",
                    "message": "The Case caption must begin with 'SEC v.'.",
                }
            )

    approved_keywords = backend.load_keywords()
    keyword_inspection = backend.inspect_keyword(parsed["keyword"], approved_keywords)

    if keyword_inspection["is_new"]:
        suggestion_text = ""
        if keyword_inspection["suggestions"]:
            suggestion_text = (
                " Close catalogue matches: "
                + ", ".join(keyword_inspection["suggestions"])
                + "."
            )
        issues.append(
            {
                "severity": "critical",
                "message": (
                    f"Keyword '{parsed['keyword']}' is not in the closed approved catalogue."
                    + suggestion_text
                ),
            }
        )
        canonical_keyword = parsed["keyword"]
    else:
        canonical_keyword = keyword_inspection["keyword"]
        checks.append("The keyword matches the approved catalogue.")

    duplicate_matches = backend.find_duplicate_summaries(
        record["Release No."],
        parsed["complaint"],
    )

    if duplicate_matches:
        for duplicate in duplicate_matches:
            severity = "critical" if duplicate["kind"] == "exact" else "warning"
            issues.append(
                {
                    "severity": severity,
                    "message": (
                        f"The summary is {duplicate['kind']}ly duplicated with "
                        f"{duplicate['release_no']} (similarity {duplicate['score']:.1%})."
                    ),
                }
            )
    else:
        checks.append("No duplicate response was detected in another release.")

    parsed["keyword"] = canonical_keyword

    return {
        "ok": True,
        "parse_error": "",
        "checks": checks,
        "issues": issues,
        "parsed": parsed,
        "keyword": canonical_keyword,
    }


def generate_excel_report():
    """Creates the current interval Excel file and stores it in session state."""

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    export_path, release_count, document_count = backend.export_interval_to_excel(
        DATABASE_FILE,
        EXPORT_DIR,
        int(st.session_state.range_start),
        int(st.session_state.range_end),
    )
    export_path = Path(export_path)
    st.session_state.excel_bytes = export_path.read_bytes()
    st.session_state.excel_name = export_path.name
    return release_count, document_count


def render_case_card(record, status):
    respondent = record["Respondents"] or "No respondent data"
    date_text = record["Date"] or "No date extracted"
    st.markdown(
        f"""
        <div class="work-card">
            <div class="work-card-title">{record['Release No.']} · {respondent}</div>
            <div class="work-card-subtitle">{date_text} · {status_label(status)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_manual_pdf_recovery(record):
    """Shows exactly one unresolved PDF at a time."""

    bad_documents = [
        document
        for document in record["Documents"]
        if document["manual_review_required"]
    ]

    if not bad_documents:
        return

    document = bad_documents[0]
    total_documents = len(record["Documents"])
    resolved_manual = sum(
        1
        for document_item in record["Documents"]
        if document_item.get("text_source") == "manual"
        and not document_item["manual_review_required"]
    )
    document_identifier = uuid.uuid5(uuid.NAMESPACE_URL, document["url"]).hex[:12]
    text_key = f"manual_pdf_text::{record['Release No.']}::{document_identifier}"

    st.markdown('<div class="next-action">Next action · Recover unreadable PDF text</div>', unsafe_allow_html=True)

    top_left, top_right = st.columns([4.2, 1.35])
    with top_left:
        st.subheader(document["name"])
        st.caption(
            f"One Resource PDF requires manual text. "
            f"{len(bad_documents)} unresolved document(s) remain for this release."
        )
    with top_right:
        st.link_button(
            "Open SEC PDF ↗",
            document["url"],
            use_container_width=True,
        )

    st.warning(document["review_reason"] or "Automatic text extraction was unreliable.")

    manual_text = st.text_area(
        "Paste the complete readable text from the PDF",
        key=text_key,
        height=245,
        placeholder="Open the SEC PDF, extract the readable text manually, then paste it here…",
    )

    wrong_paste_reason = ""
    if manual_text.strip():
        wrong_paste_reason = backend.looks_like_wrong_manual_pdf_paste(
            manual_text,
            document["url"],
        )

    if wrong_paste_reason:
        st.error(wrong_paste_reason)

    short_text = bool(manual_text.strip()) and len(manual_text.strip()) < 100
    confirmed_short = True

    if short_text and not wrong_paste_reason:
        st.warning(
            f"Only {len(manual_text.strip())} characters were pasted. "
            "Confirm this really is the complete readable text."
        )
        confirmed_short = st.checkbox(
            "I checked the PDF and confirm the text is complete.",
            key=f"confirm_short::{record['Release No.']}::{document_identifier}",
        )

    save_disabled = (
        not manual_text.strip()
        or bool(wrong_paste_reason)
        or not confirmed_short
    )

    if st.button(
        "Save & continue →",
        key=f"save_manual::{record['Release No.']}::{document_identifier}",
        type="primary",
        disabled=save_disabled,
        use_container_width=True,
    ):
        backend.save_manual_document_text(
            record["Release No."],
            document,
            manual_text,
        )
        st.session_state.excel_bytes = None
        st.session_state.excel_name = None
        st.session_state.pop(text_key, None)
        st.session_state.validation_results.pop(record["Release No."], None)
        st.toast("Manual PDF text saved.", icon="✅")
        st.rerun()

    with st.expander("Source details", expanded=False):
        st.write(f"Resource documents in this release: {total_documents}")
        st.write(f"Manually recovered so far: {resolved_manual}")
        st.code(document["url"], language=None)


def save_chatgpt_result(record, validation, overwrite=False):
    parsed = validation["parsed"]
    backend.save_manual_fields(
        record["Release No."],
        parsed["keyword"],
        parsed["complaint"],
        overwrite=overwrite,
    )
    st.session_state.excel_bytes = None
    st.session_state.excel_name = None
    st.session_state.validation_results.pop(record["Release No."], None)
    st.session_state.pop(f"force_review::{record['Release No.']}", None)
    st.session_state.pop(f"chatgpt_response::{record['Release No.']}", None)
    advance_to_next_task()


def render_validation_details(validation):
    with st.expander("Validation details", expanded=False):
        for check in validation.get("checks", []):
            st.success(check, icon="✅")
        for issue in validation.get("issues", []):
            if issue["severity"] == "critical":
                st.error(issue["message"], icon="⛔")
            else:
                st.warning(issue["message"], icon="⚠️")


def render_chatgpt_review(record):
    """One-click normal path: copy prompt -> paste response -> validate & save."""

    release_no = record["Release No."]
    approved_keywords = backend.load_keywords()

    if release_no not in st.session_state.case_ids:
        st.session_state.case_ids[release_no] = backend.generate_case_id(release_no)

    case_id = st.session_state.case_ids[release_no]
    source_block = backend.build_source_block(record, case_id, approved_keywords)
    force_review = bool(st.session_state.get(f"force_review::{release_no}"))

    st.markdown('<div class="next-action">Next action · ChatGPT keyword & summary</div>', unsafe_allow_html=True)
    st.caption(
        "Copy the prepared source package into the dedicated ChatGPT chat, then paste the complete response below."
    )

    render_copy_button(
        source_block,
        key=case_id,
        label="1 · Copy ChatGPT prompt",
    )

    with st.expander("Prompt / source details", expanded=False):
        st.download_button(
            "Download prompt (.txt)",
            data=source_block.encode("utf-8"),
            file_name=f"{release_no}_ChatGPT_prompt.txt",
            mime="text/plain",
            use_container_width=True,
        )
        st.code(source_block, language=None, wrap_lines=True)

    response_key = f"chatgpt_response::{release_no}"
    response_text = st.text_area(
        "2 · Paste ChatGPT's complete machine-readable response",
        key=response_key,
        height=260,
        placeholder=(
            "Paste everything from <<< START OF MACHINE-READABLE RESPONSE >>> "
            "through <<< END OF MACHINE-READABLE RESPONSE >>>"
        ),
    )

    validation = st.session_state.validation_results.get(release_no)
    if validation and validation.get("submitted_text") != response_text:
        st.session_state.validation_results.pop(release_no, None)
        validation = None

    button_label = "3 · Validate & replace →" if force_review else "3 · Validate & save →"

    if st.button(
        button_label,
        type="primary",
        use_container_width=True,
        disabled=not response_text.strip(),
    ):
        validation = validate_chatgpt_submission(
            record,
            case_id,
            source_block,
            response_text,
        )
        validation["submitted_text"] = response_text
        st.session_state.validation_results[release_no] = validation

        if validation["parse_error"]:
            st.rerun()

        critical_issues = [
            issue for issue in validation["issues"] if issue["severity"] == "critical"
        ]
        warning_issues = [
            issue for issue in validation["issues"] if issue["severity"] != "critical"
        ]

        if not critical_issues and not warning_issues:
            save_chatgpt_result(record, validation, overwrite=force_review)
            st.toast(f"{release_no} saved. Moving to the next case.", icon="✅")
            st.rerun()

        st.rerun()

    validation = st.session_state.validation_results.get(release_no)
    if not validation:
        return

    if validation["parse_error"]:
        st.error(f"The response could not be read: {validation['parse_error']}")
        st.caption("Correct the pasted response above and click Validate & save again.")
        return

    critical_issues = [
        issue for issue in validation["issues"] if issue["severity"] == "critical"
    ]
    warning_issues = [
        issue for issue in validation["issues"] if issue["severity"] != "critical"
    ]

    if critical_issues:
        st.error("This response cannot be saved yet.")
        for issue in critical_issues:
            st.error(issue["message"], icon="⛔")
        for issue in warning_issues:
            st.warning(issue["message"], icon="⚠️")
        render_validation_details(validation)
        st.caption("Correct the response above and click Validate & save again.")
        return

    if warning_issues:
        st.warning("The response passed the critical checks, but needs your review.")
        for issue in warning_issues:
            st.warning(issue["message"], icon="⚠️")

        left, right = st.columns([1.4, 1])
        if left.button(
            "Save anyway & continue →",
            type="primary",
            use_container_width=True,
            key=f"save_warning::{release_no}",
        ):
            save_chatgpt_result(record, validation, overwrite=force_review)
            st.toast(f"{release_no} saved. Moving to the next case.", icon="✅")
            st.rerun()

        if right.button(
            "Use corrected response",
            use_container_width=True,
            key=f"correct_warning::{release_no}",
        ):
            st.session_state.validation_results.pop(release_no, None)
            st.rerun()

        render_validation_details(validation)


def render_release_review(release_no: str):
    record = load_release_record(release_no)

    if record is None:
        st.error(f"{release_no} is not present in the temporary session database.")
        return

    status, _ = get_release_status(release_no)
    render_case_card(record, status)

    if record["Link to Release"]:
        with st.expander("SEC release & source details", expanded=False):
            st.link_button("Open SEC Litigation Release ↗", record["Link to Release"])
            st.write(f"Resource documents: {len(record['Documents'])}")

    if status == "unavailable":
        st.warning(
            "The SEC page could not be found or parsed. The release number will still appear "
            "in Excel and the unavailable fields remain blank."
        )
        next_release = first_incomplete_release()
        if next_release and next_release != release_no:
            if st.button("Return to next task →", type="primary", use_container_width=True):
                select_release(next_release)
                st.rerun()
        elif next_release is None:
            if st.button("Return to export →", type="primary", use_container_width=True):
                select_release(None)
                st.rerun()
        return

    if status == "manual":
        render_manual_pdf_recovery(record)
        return

    if status == "complete" and not st.session_state.get(f"force_review::{release_no}"):
        st.success("This release is complete.")
        with st.expander("Review saved keyword and summary", expanded=False):
            st.markdown(f"**Keyword:** {record['Keyword']}")
            st.markdown(record["Complaint"])

        next_release = first_incomplete_release()
        left, right = st.columns([1.6, 1])
        if next_release and next_release != release_no:
            if left.button("Return to next task →", type="primary", use_container_width=True):
                select_release(next_release)
                st.rerun()
        elif next_release is None:
            if left.button("Return to export →", type="primary", use_container_width=True):
                select_release(None)
                st.rerun()
        if right.button("Re-open this release", use_container_width=True):
            st.session_state.validation_results.pop(release_no, None)
            st.session_state[f"force_review::{release_no}"] = True
            st.rerun()
        return

    render_chatgpt_review(record)


def render_run_complete():
    counts = count_statuses()
    total = len(release_numbers_for_current_run())
    unavailable = counts["unavailable"] + counts["missing"]

    st.markdown('<div class="next-action">Run complete · Export</div>', unsafe_allow_html=True)
    st.subheader("The review queue is finished")

    if unavailable:
        st.warning(
            f"{unavailable} release(s) were unavailable or not extractable. "
            "They will still appear in Excel with the information that is available."
        )
    else:
        st.success(f"All {total} releases are complete.")

    if st.session_state.excel_bytes is None:
        with st.spinner("Preparing Excel report…"):
            try:
                release_count, document_count = generate_excel_report()
                st.caption(
                    f"Prepared {release_count} releases and {document_count} Resource document links."
                )
            except Exception as error:
                st.error(f"Excel export failed: {error}")
                return

    st.download_button(
        "Download Excel report",
        data=st.session_state.excel_bytes,
        file_name=st.session_state.excel_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )

    st.caption(
        "The temporary progress database remains available in the sidebar until you start a new run."
    )


# -----------------------------------------------------------------------------
# SIDEBAR — SECONDARY NAVIGATION / RECOVERY ONLY
# -----------------------------------------------------------------------------


with st.sidebar:
    st.markdown("### SEC Litigation Tool")
    st.caption("Assembly-line mode")

    if st.session_state.run_ready:
        statuses = all_statuses()
        release_options = [release_no for release_no, _, _ in statuses]
        next_task = first_incomplete_release()

        st.markdown(
            f"**LR-{st.session_state.range_start} → LR-{st.session_state.range_end}**"
        )
        counts = count_statuses()
        st.caption(
            f"{counts['complete']} complete · {counts['manual']} manual PDF · "
            f"{counts['chatgpt']} ChatGPT"
        )

        if release_options:
            if (
                st.session_state.selected_release not in release_options
                and st.session_state.selected_release is not None
            ):
                select_release(next_task)

            jump_options = ["— Select a release —", *release_options]
            current_jump_index = (
                release_options.index(st.session_state.selected_release) + 1
                if st.session_state.selected_release in release_options
                else 0
            )
            selected = st.selectbox(
                "Jump to release",
                jump_options,
                index=current_jump_index,
                key=f"jump_release_selector::{st.session_state.selected_release or 'none'}",
                help="Normally you do not need this. Use it only to inspect or revisit another release.",
            )

            if selected != "— Select a release —" and selected != st.session_state.selected_release:
                st.session_state.selected_release = selected

            if next_task and st.session_state.selected_release != next_task:
                if st.button("Return to next task", use_container_width=True):
                    select_release(next_task)
                    st.rerun()

        with st.expander("View all releases", expanded=False):
            for release_no, status, detail in statuses:
                marker = "→" if release_no == st.session_state.selected_release else ""
                st.markdown(f"{marker} **{release_no}** · {status_label(status)}")
                if status in {"manual", "unavailable", "missing"}:
                    st.caption(detail)

        with st.expander("Session & recovery", expanded=False):
            progress_bytes = DATABASE_FILE.read_bytes() if DATABASE_FILE.exists() else b""
            st.download_button(
                "Download progress database",
                data=progress_bytes,
                file_name=(
                    f"SEC_progress_LR-{st.session_state.range_start}_"
                    f"to_LR-{st.session_state.range_end}.db"
                ),
                mime="application/octet-stream",
                use_container_width=True,
            )

            if st.session_state.last_processing_log:
                with st.expander("Extraction log"):
                    for line in st.session_state.last_processing_log:
                        st.text(line)

            if st.button("Start a new run", use_container_width=True):
                clear_session_database()
                st.session_state.range_start = None
                st.session_state.range_end = None
                st.session_state.run_ready = False
                st.session_state.selected_release = None
                reset_review_state()
                st.rerun()

        if first_incomplete_release() is not None:
            with st.expander("Export current results", expanded=False):
                if st.button("Prepare Excel now", use_container_width=True):
                    try:
                        release_count, document_count = generate_excel_report()
                        st.success(
                            f"Prepared {release_count} releases / {document_count} Resource documents."
                        )
                    except Exception as error:
                        st.error(f"Excel export failed: {error}")

                if st.session_state.excel_bytes:
                    st.download_button(
                        "Download current Excel",
                        data=st.session_state.excel_bytes,
                        file_name=st.session_state.excel_name,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                    )

    else:
        with st.expander("Resume previous run", expanded=False):
            resume_file = st.file_uploader(
                "Upload progress database",
                type=["db", "sqlite", "sqlite3"],
                label_visibility="collapsed",
            )

            if resume_file is not None:
                if st.button("Resume uploaded run", use_container_width=True):
                    uploaded_bytes = resume_file.getvalue()
                    valid, error_message = validate_uploaded_database(uploaded_bytes)

                    if not valid:
                        st.error(error_message)
                    else:
                        DATABASE_FILE.write_bytes(uploaded_bytes)
                        backend.create_database()
                        inferred_start, inferred_end = infer_range_from_database()

                        if inferred_start is None:
                            st.error("The uploaded database does not contain any releases.")
                        else:
                            st.session_state.range_start = inferred_start
                            st.session_state.range_end = inferred_end
                            st.session_state.run_ready = True
                            reset_review_state()
                            select_release(first_incomplete_release())
                            st.rerun()


# -----------------------------------------------------------------------------
# MAIN PAGE
# -----------------------------------------------------------------------------


render_header()

if not st.session_state.run_ready:
    st.subheader("Start a release interval")
    st.caption(
        "The web version uses a temporary database for this browser session only. "
        "Your local SEC database is never accessed."
    )

    with st.container(border=True):
        col1, col2 = st.columns(2)

        start_input = col1.number_input(
            "First release number",
            min_value=1,
            value=26585,
            step=1,
        )
        end_input = col2.number_input(
            "Last release number",
            min_value=1,
            value=26576,
            step=1,
        )

        high = max(int(start_input), int(end_input))
        low = min(int(start_input), int(end_input))
        count = high - low + 1

        st.caption(f"{count} release(s): LR-{high} through LR-{low}")

        if count > MAX_RELEASES_PER_RUN:
            st.error(
                f"One web run is limited to {MAX_RELEASES_PER_RUN} releases. "
                "Use a smaller interval."
            )

        if st.button(
            "Start processing →",
            type="primary",
            use_container_width=True,
            disabled=count > MAX_RELEASES_PER_RUN,
        ):
            clear_session_database()
            reset_review_state()
            st.session_state.range_start = high
            st.session_state.range_end = low

            with st.status("Extracting SEC releases and Resource PDFs…", expanded=True) as status_box:
                process_release_interval(high, low)
                status_box.update(
                    label="Extraction complete",
                    state="complete",
                    expanded=False,
                )

            st.session_state.run_ready = True
            select_release(first_incomplete_release())
            st.rerun()

else:
    next_task = first_incomplete_release()

    if st.session_state.selected_release is None and next_task is not None:
        select_release(next_task)

    render_workflow_progress(st.session_state.selected_release)

    if st.session_state.selected_release is None:
        render_run_complete()
    else:
        render_release_review(st.session_state.selected_release)
