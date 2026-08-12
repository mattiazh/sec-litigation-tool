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
        max-width: 1500px;
        padding-top: 1.6rem;
        padding-bottom: 4rem;
    }
    [data-testid="stSidebar"] {
        border-right: 1px solid #dfe5ec;
    }
    .sec-hero {
        padding: 1.25rem 1.45rem;
        border-radius: 16px;
        background: linear-gradient(135deg, #173f66 0%, #245f91 100%);
        color: white;
        margin-bottom: 1.1rem;
        box-shadow: 0 6px 20px rgba(25, 75, 122, 0.16);
    }
    .sec-hero h1 {
        margin: 0;
        font-size: 1.7rem;
        font-weight: 700;
        letter-spacing: -0.02em;
    }
    .sec-hero p {
        margin: .35rem 0 0;
        opacity: .88;
        font-size: .98rem;
    }
    .step-pill {
        display: inline-block;
        padding: .22rem .62rem;
        border-radius: 999px;
        background: #e9f1f8;
        color: #194b7a;
        font-weight: 700;
        font-size: .78rem;
        margin-bottom: .45rem;
    }
    .muted {
        color: #607080;
        font-size: .9rem;
    }
    .status-complete { color: #137333; font-weight: 700; }
    .status-review { color: #9a6700; font-weight: 700; }
    .status-unavailable { color: #b3261e; font-weight: 700; }
    .status-ready { color: #194b7a; font-weight: 700; }
    div[data-testid="stMetric"] {
        background: white;
        border: 1px solid #e1e7ee;
        padding: .7rem .9rem;
        border-radius: 12px;
    }
    .small-note {
        font-size: .84rem;
        color: #657586;
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
    statuses = all_statuses()

    for release_no, status, _ in statuses:
        if status in {"manual", "chatgpt"}:
            return release_no

    if statuses:
        return statuses[0][0]

    return None


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
          <p>Extract SEC Litigation Releases, recover unreadable PDFs, validate the manual ChatGPT result, and export the completed interval to Excel.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_metrics():
    if not st.session_state.run_ready:
        return

    counts = count_statuses()
    total = len(release_numbers_for_current_run())
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Releases", total)
    c2.metric("Complete", counts["complete"])
    c3.metric("ChatGPT ready", counts["chatgpt"])
    c4.metric("Manual PDFs", counts["manual"])
    c5.metric("Unavailable", counts["unavailable"] + counts["missing"])


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


def render_manual_pdf_recovery(record):
    bad_documents = [
        document
        for document in record["Documents"]
        if document["manual_review_required"]
    ]

    st.markdown('<span class="step-pill">STEP 2 · MANUAL PDF RECOVERY</span>', unsafe_allow_html=True)
    st.subheader("Readable text is required before the ChatGPT step")
    st.warning(
        f"{len(bad_documents)} Resource document(s) could not be extracted reliably. "
        "Open each SEC PDF, obtain readable text manually, and paste it below."
    )

    for position, document in enumerate(bad_documents, start=1):
        with st.container(border=True):
            top_left, top_right = st.columns([4, 1])
            with top_left:
                st.markdown(f"#### {position}. {document['name']}")
                st.caption(document["review_reason"] or "Automatic text extraction was unreliable.")
            with top_right:
                st.link_button(
                    "Open SEC PDF",
                    document["url"],
                    use_container_width=True,
                )

            text_key = f"manual_pdf_text::{record['Release No.']}::{position}"
            manual_text = st.text_area(
                "Paste the manually extracted PDF text",
                key=text_key,
                height=240,
                placeholder="Paste the complete readable text from this PDF here…",
            )

            wrong_paste_reason = ""
            if manual_text.strip():
                wrong_paste_reason = backend.looks_like_wrong_manual_pdf_paste(
                    manual_text,
                    document["url"],
                )

            if wrong_paste_reason:
                st.error(wrong_paste_reason)

            short_text = manual_text.strip() and len(manual_text.strip()) < 100
            confirmed_short = True

            if short_text and not wrong_paste_reason:
                st.warning(
                    f"Only {len(manual_text.strip())} characters were pasted. "
                    "That may be correct for a very short filing, but please confirm it."
                )
                confirmed_short = st.checkbox(
                    "I checked the PDF and confirm this is the complete readable text.",
                    key=f"confirm_short::{record['Release No.']}::{position}",
                )

            save_disabled = (
                not manual_text.strip()
                or bool(wrong_paste_reason)
                or not confirmed_short
            )

            if st.button(
                "Save manual PDF text",
                key=f"save_manual::{record['Release No.']}::{position}",
                type="primary",
                disabled=save_disabled,
                use_container_width=True,
            ):
                backend.save_manual_document_text(
                    record["Release No."],
                    document,
                    manual_text,
                )
                st.success("Manual text saved to the temporary session database.")
                st.rerun()


def render_chatgpt_review(record):
    release_no = record["Release No."]
    approved_keywords = backend.load_keywords()

    if release_no not in st.session_state.case_ids:
        st.session_state.case_ids[release_no] = backend.generate_case_id(release_no)

    case_id = st.session_state.case_ids[release_no]
    source_block = backend.build_source_block(record, case_id, approved_keywords)

    st.markdown('<span class="step-pill">STEP 3 · CHATGPT REVIEW</span>', unsafe_allow_html=True)
    st.subheader("Generate the keyword and summary in your dedicated ChatGPT chat")
    st.caption(
        "The prompt contains the current keyword catalogue, all readable Resource documents, "
        "the Case ID, and the strict machine-readable output format."
    )

    copy_col, download_col = st.columns([2, 1])
    with copy_col:
        render_copy_button(source_block, key=case_id)
    with download_col:
        st.download_button(
            "Download prompt (.txt)",
            data=source_block.encode("utf-8"),
            file_name=f"{release_no}_ChatGPT_prompt.txt",
            mime="text/plain",
            use_container_width=True,
        )

    with st.expander("Show full ChatGPT source prompt", expanded=False):
        st.code(source_block, language=None, wrap_lines=True)

    response_key = f"chatgpt_response::{release_no}"
    response_text = st.text_area(
        "Paste ChatGPT's complete machine-readable response",
        key=response_key,
        height=330,
        placeholder=(
            "Paste everything from <<< START OF MACHINE-READABLE RESPONSE >>> "
            "through <<< END OF MACHINE-READABLE RESPONSE >>>"
        ),
    )

    validate_col, clear_col = st.columns([2, 1])

    if validate_col.button(
        "Validate response",
        type="primary",
        use_container_width=True,
        disabled=not response_text.strip(),
    ):
        result = validate_chatgpt_submission(
            record,
            case_id,
            source_block,
            response_text,
        )
        st.session_state.validation_results[release_no] = result
        st.rerun()

    if clear_col.button("Clear validation", use_container_width=True):
        st.session_state.validation_results.pop(release_no, None)
        st.rerun()

    validation = st.session_state.validation_results.get(release_no)

    if not validation:
        return

    if validation["parse_error"]:
        st.error(f"The response could not be parsed: {validation['parse_error']}")
        return

    st.markdown("#### Validation result")

    for check in validation["checks"]:
        st.success(check, icon="✅")

    critical_issues = [
        issue for issue in validation["issues"] if issue["severity"] == "critical"
    ]
    warning_issues = [
        issue for issue in validation["issues"] if issue["severity"] != "critical"
    ]

    for issue in critical_issues:
        st.error(issue["message"], icon="⛔")

    for issue in warning_issues:
        st.warning(issue["message"], icon="⚠️")

    if critical_issues:
        st.error("Critical checks failed. Nothing can be saved until the response is corrected.")
        return

    parsed = validation["parsed"]

    with st.container(border=True):
        st.markdown(f"**Keyword:** {parsed['keyword']}")
        summary_preview = parsed["complaint"][:800]
        if len(parsed["complaint"]) > 800:
            summary_preview += "…"
        st.markdown("**Summary preview:**")
        st.text(summary_preview)

    warning_confirmed = True
    if warning_issues:
        warning_confirmed = st.checkbox(
            "I reviewed the warning(s) above and want to save this result.",
            key=f"warning_confirm::{release_no}",
        )

    existing_values = backend.load_existing_fields(release_no)
    already_complete = bool(
        existing_values["keyword"].strip() or existing_values["complaint"].strip()
    )

    replace_existing = False
    if already_complete:
        st.info("This release already contains a saved Keyword and/or Complaint summary.")
        replace_existing = st.checkbox(
            "Replace the existing saved result",
            key=f"replace_existing::{release_no}",
        )

    if st.button(
        "Save validated result",
        type="primary",
        use_container_width=True,
        disabled=(not warning_confirmed or (already_complete and not replace_existing)),
    ):
        backend.save_manual_fields(
            release_no,
            parsed["keyword"],
            parsed["complaint"],
            overwrite=replace_existing,
        )
        st.session_state.validation_results.pop(release_no, None)
        st.session_state.pop(f"force_review::{release_no}", None)
        st.success("Keyword and summary saved to the temporary session database.")
        st.rerun()


def render_release_review(release_no: str):
    record = load_release_record(release_no)

    if record is None:
        st.error(f"{release_no} is not present in the temporary session database.")
        return

    status, detail = get_release_status(release_no)

    with st.container(border=True):
        left, middle, right = st.columns([2, 4, 1.5])
        left.markdown(f"### {release_no}")
        middle.markdown(f"**{record['Respondents'] or 'No respondent data'}**")
        middle.caption(record["Date"] or "No release date extracted")
        right.markdown(status_label(status))

        if record["Link to Release"]:
            st.link_button(
                "Open SEC Litigation Release",
                record["Link to Release"],
            )

    if status == "unavailable":
        st.markdown('<span class="step-pill">RELEASE UNAVAILABLE</span>', unsafe_allow_html=True)
        st.warning(
            "The SEC page could not be found or parsed. The release number is still preserved "
            "in the Excel export, but the remaining fields stay blank."
        )
        return

    if status == "manual":
        render_manual_pdf_recovery(record)
        return

    if status == "complete":
        st.markdown('<span class="step-pill">COMPLETE</span>', unsafe_allow_html=True)
        st.success("This release already has a validated keyword and summary.")
        with st.expander("Review saved result"):
            st.markdown(f"**Keyword:** {record['Keyword']}")
            st.markdown(record["Complaint"])

        if st.button("Re-open ChatGPT review for this release"):
            st.session_state.validation_results.pop(release_no, None)
            # We leave the values intact; the review screen will require explicit replacement.
            st.session_state[f"force_review::{release_no}"] = True
            st.rerun()

        if not st.session_state.get(f"force_review::{release_no}"):
            return

    render_chatgpt_review(record)


# -----------------------------------------------------------------------------
# SIDEBAR
# -----------------------------------------------------------------------------


with st.sidebar:
    st.markdown("### SEC Litigation Tool")
    st.caption("Temporary online session — your local database is never accessed.")

    if st.session_state.run_ready:
        st.markdown("---")
        st.markdown(
            f"**Current interval**  \nLR-{st.session_state.range_start} → LR-{st.session_state.range_end}"
        )

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
            help="Use this file to resume if the browser session is lost.",
        )

    st.markdown("---")
    st.markdown("**Resume a previous web run**")
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
                    st.session_state.selected_release = first_incomplete_release()
                    reset_review_state()
                    st.success("Progress restored.")
                    st.rerun()

    if st.session_state.run_ready:
        st.markdown("---")
        if st.button("Start a new run", use_container_width=True):
            clear_session_database()
            st.session_state.range_start = None
            st.session_state.range_end = None
            st.session_state.run_ready = False
            st.session_state.selected_release = None
            reset_review_state()
            st.rerun()


# -----------------------------------------------------------------------------
# MAIN PAGE
# -----------------------------------------------------------------------------


render_header()

if not st.session_state.run_ready:
    st.markdown('<span class="step-pill">STEP 1 · START A RUN</span>', unsafe_allow_html=True)
    st.subheader("Choose the SEC Litigation Release interval")
    st.write(
        "The online version creates a temporary database only for this browser session. "
        "Nothing is written to your local `sec_releases.db`."
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

        st.caption(f"This run will process {count} release(s): LR-{high} through LR-{low}.")

        if count > MAX_RELEASES_PER_RUN:
            st.error(
                f"For the web version, one run is limited to {MAX_RELEASES_PER_RUN} releases. "
                "Use a smaller interval."
            )

        if st.button(
            "Extract interval",
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
                    label="SEC extraction completed",
                    state="complete",
                    expanded=False,
                )

            st.session_state.run_ready = True
            st.session_state.selected_release = first_incomplete_release()
            st.rerun()

else:
    render_metrics()
    st.markdown("")

    statuses = all_statuses()
    release_options = [release_no for release_no, _, _ in statuses]

    if st.session_state.selected_release not in release_options:
        st.session_state.selected_release = first_incomplete_release()

    left_panel, main_panel = st.columns([1.2, 3.8], gap="large")

    with left_panel:
        st.markdown("### Release queue")
        st.caption("Select any release. Completed work stays in the temporary session database.")

        selected = st.selectbox(
            "Release",
            release_options,
            index=(
                release_options.index(st.session_state.selected_release)
                if st.session_state.selected_release in release_options
                else 0
            ),
            label_visibility="collapsed",
        )
        st.session_state.selected_release = selected

        with st.container(border=True):
            for release_no, status, detail in statuses:
                marker = "→ " if release_no == selected else ""
                st.markdown(f"{marker}**{release_no}** · {status_label(status)}")
                st.caption(detail)

        if st.session_state.last_processing_log:
            with st.expander("Extraction log"):
                for line in st.session_state.last_processing_log:
                    st.text(line)

    with main_panel:
        render_release_review(selected)

    st.markdown("---")
    st.markdown('<span class="step-pill">STEP 4 · EXPORT</span>', unsafe_allow_html=True)
    st.subheader("Excel export")

    counts = count_statuses()
    incomplete_count = (
        counts["manual"]
        + counts["chatgpt"]
        + counts["unavailable"]
        + counts["missing"]
    )

    if incomplete_count:
        st.warning(
            f"{incomplete_count} release(s) are still incomplete, manually flagged, or unavailable. "
            "You can still export the information currently available, exactly like in the local workflow."
        )
    else:
        st.success("All releases in the selected interval are complete.")

    export_col, progress_col = st.columns([2, 1])

    if export_col.button(
        "Generate Excel report",
        type="primary",
        use_container_width=True,
    ):
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            export_path, release_count, document_count = backend.export_interval_to_excel(
                DATABASE_FILE,
                EXPORT_DIR,
                int(st.session_state.range_start),
                int(st.session_state.range_end),
            )
            export_path = Path(export_path)
            st.session_state.excel_bytes = export_path.read_bytes()
            st.session_state.excel_name = export_path.name
            st.success(
                f"Excel created: {release_count} releases and {document_count} Resource documents exported."
            )
        except Exception as error:
            st.error(f"Excel export failed: {error}")

    if st.session_state.excel_bytes:
        progress_col.download_button(
            "Download Excel",
            data=st.session_state.excel_bytes,
            file_name=st.session_state.excel_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True,
        )
    else:
        progress_col.info("Generate the report first.")

    st.caption(
        "Tip: download the progress database before closing a long session. "
        "A browser refresh or Community Cloud restart can reset session state."
    )
