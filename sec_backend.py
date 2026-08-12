import os
import re
import secrets
import sqlite3
import subprocess
import time
from difflib import SequenceMatcher, get_close_matches
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from excel_export import export_interval_to_excel

PROGRAM_VERSION = "v13-manual-pdf-recovery"


# -----------------------------------------------------------------------------
# PROJECT SETTINGS
# -----------------------------------------------------------------------------

PROJECT_FOLDER = Path(__file__).resolve().parent
DATABASE_FILE = PROJECT_FOLDER / "sec_releases.db"
KEYWORDS_FILE = PROJECT_FOLDER / "keywords.txt"
BACKUP_FOLDER = PROJECT_FOLDER / "backups"

BASE_RELEASE_URL = (
    "https://www.sec.gov/enforcement-litigation/"
    "litigation-releases/lr-"
)

HEADERS = {
    "User-Agent": (
        "SEC Litigation Manual Review Tool "
        "mattia.tschopp@pqs.ch"
    )
}

REQUEST_DELAY_SECONDS = 0.20
MIN_TOTAL_CHARACTERS = 100
MIN_CHARACTERS_PER_PAGE = 150
END_MARKER = "END"
MANUAL_TOOL_VERSION = "v13 — manual recovery of unreadable Resource PDFs before ChatGPT review"
SOURCE_START_MARKER = ">>> START OF TEXT TO COPY INTO CHATGPT >>>"
SOURCE_END_MARKER = "<<< END OF TEXT TO COPY INTO CHATGPT <<<"
RESPONSE_START_MARKER = "<<< START OF MACHINE-READABLE RESPONSE >>>"
RESPONSE_END_MARKER = "<<< END OF MACHINE-READABLE RESPONSE >>>"

def connect_database():
    """Opens SQLite and returns rows that can be accessed by column name."""

    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def create_database_backup():
    """Creates a consistent timestamped SQLite backup before the run starts."""

    if not DATABASE_FILE.exists():
        return None

    BACKUP_FOLDER.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    backup_path = BACKUP_FOLDER / f"sec_releases_{timestamp}.db"

    # SQLite's backup API creates a consistent snapshot even when the source
    # database contains more than one page.
    with sqlite3.connect(DATABASE_FILE) as source_connection:
        with sqlite3.connect(backup_path) as backup_connection:
            source_connection.backup(backup_connection)

    return backup_path

def create_database():
    """Creates the release and document tables if they do not yet exist."""

    with connect_database() as connection:
        cursor = connection.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS releases (
                date TEXT,
                respondents TEXT,
                release_no TEXT UNIQUE,
                keyword TEXT,
                content TEXT,
                complaint TEXT,
                link_to_release TEXT,
                link_to_complaint TEXT
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                release_no TEXT NOT NULL,
                document_name TEXT NOT NULL,
                document_url TEXT NOT NULL,
                extracted_text TEXT,
                page_count INTEGER,
                character_count INTEGER,
                characters_per_page REAL,
                extraction_successful INTEGER,
                manual_review_required INTEGER,
                review_reason TEXT,
                text_source TEXT DEFAULT 'automatic',
                UNIQUE (release_no, document_url)
            )
            """
        )

        # Backward-compatible schema upgrade for existing databases.
        document_columns = {
            row["name"]
            for row in cursor.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "text_source" not in document_columns:
            cursor.execute(
                "ALTER TABLE documents "
                "ADD COLUMN text_source TEXT DEFAULT 'automatic'"
            )

    print("Database is ready.\n")

def save_release(record):
    """
    Inserts a new release or updates the webpage fields of an existing release.

    Existing Keyword and Complaint values are preserved when the current scrape
    contains blank placeholders.
    """

    with connect_database() as connection:
        cursor = connection.cursor()

        cursor.execute(
            "SELECT 1 FROM releases WHERE release_no = ?",
            (record["Release No."],),
        )
        already_exists = cursor.fetchone() is not None

        cursor.execute(
            """
            INSERT INTO releases (
                date,
                respondents,
                release_no,
                keyword,
                content,
                complaint,
                link_to_release,
                link_to_complaint
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(release_no)
            DO UPDATE SET
                date = excluded.date,
                respondents = CASE
                    WHEN excluded.respondents <> 'Not found'
                    THEN excluded.respondents
                    ELSE releases.respondents
                END,
                content = excluded.content,
                link_to_release = excluded.link_to_release,
                link_to_complaint = CASE
                    WHEN TRIM(COALESCE(excluded.link_to_complaint, '')) <> ''
                    THEN excluded.link_to_complaint
                    ELSE releases.link_to_complaint
                END,

                keyword = CASE
                    WHEN TRIM(COALESCE(excluded.keyword, '')) <> ''
                    THEN excluded.keyword
                    ELSE releases.keyword
                END,

                complaint = CASE
                    WHEN TRIM(COALESCE(excluded.complaint, '')) <> ''
                    THEN excluded.complaint
                    ELSE releases.complaint
                END
            """,
            (
                record["Date"],
                record["Respondents"],
                record["Release No."],
                record["Keyword"],
                record["Content"],
                record["Complaint"],
                record["Link to Release"],
                record["Link to Complaint"],
            ),
        )

    return not already_exists


def save_failed_release_placeholder(release_number):
    """
    Ensures that a requested release number still appears in the final Excel file
    when the SEC page cannot be found or extracted.

    Only the Release No. is stored. All other fields remain blank. INSERT OR
    IGNORE is used so that a temporary failure can never overwrite data already
    stored for the same release.
    """

    release_no = f"LR-{release_number}"

    with connect_database() as connection:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO releases (
                date,
                respondents,
                release_no,
                keyword,
                content,
                complaint,
                link_to_release,
                link_to_complaint
            )
            VALUES ('', '', ?, '', '', '', '', '')
            """,
            (release_no,),
        )

        was_inserted = cursor.rowcount == 1

    return was_inserted


def save_documents(release_no, documents):
    """Inserts new documents and updates documents already in SQLite."""

    inserted_count = 0
    updated_count = 0

    with connect_database() as connection:
        cursor = connection.cursor()

        for document in documents:
            cursor.execute(
                """
                SELECT id
                FROM documents
                WHERE release_no = ?
                  AND document_url = ?
                """,
                (release_no, document["url"]),
            )
            exists = cursor.fetchone() is not None

            cursor.execute(
                """
                INSERT INTO documents (
                    release_no,
                    document_name,
                    document_url,
                    extracted_text,
                    page_count,
                    character_count,
                    characters_per_page,
                    extraction_successful,
                    manual_review_required,
                    review_reason,
                    text_source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(release_no, document_url)
                DO UPDATE SET
                    document_name = excluded.document_name,
                    extracted_text = excluded.extracted_text,
                    page_count = excluded.page_count,
                    character_count = excluded.character_count,
                    characters_per_page = excluded.characters_per_page,
                    extraction_successful = excluded.extraction_successful,
                    manual_review_required = excluded.manual_review_required,
                    review_reason = excluded.review_reason,
                    text_source = excluded.text_source
                """,
                (
                    release_no,
                    document["name"],
                    document["url"],
                    document["text"],
                    document["page_count"],
                    document["character_count"],
                    document["characters_per_page"],
                    int(document["extraction_successful"]),
                    int(document["manual_review_required"]),
                    document["review_reason"],
                    document.get("text_source", "automatic"),
                ),
            )

            if exists:
                updated_count += 1
            else:
                inserted_count += 1

    return inserted_count, updated_count

def load_stored_document(release_no, document_url):
    """
    Loads an already extracted PDF from SQLite.

    A complete or manually flagged extraction can be reused. A document that
    failed completely is retried on the next run.
    """

    with connect_database() as connection:
        row = connection.execute(
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
                text_source
            FROM documents
            WHERE release_no = ?
              AND document_url = ?
            """,
            (release_no, document_url),
        ).fetchone()

    if row is None:
        return None

    text = row["extracted_text"] or ""
    page_count = row["page_count"] or 0

    # Retry a previous complete failure, but reuse a scanned/manual-review PDF.
    if not text.strip() and page_count == 0:
        return None

    return {
        "name": row["document_name"],
        "url": row["document_url"],
        "text": text,
        "page_count": page_count,
        "character_count": row["character_count"] or 0,
        "characters_per_page": row["characters_per_page"] or 0,
        "extraction_successful": bool(row["extraction_successful"]),
        "manual_review_required": bool(row["manual_review_required"]),
        "review_reason": row["review_reason"] or "",
        "text_source": row["text_source"] or "automatic",
        "reused_from_database": True,
    }

def count_database_records():
    """Counts unique Litigation Releases in SQLite."""

    with connect_database() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM releases"
        ).fetchone()[0]


def build_interval_completion_report(start_number, end_number, release_numbers):
    """Checks the selected interval before an Excel file is created."""

    with connect_database() as connection:
        release_rows = connection.execute(
            """
            SELECT
                release_no,
                date,
                respondents,
                content,
                keyword,
                complaint
            FROM releases
            WHERE CAST(SUBSTR(release_no, 4) AS INTEGER)
                  BETWEEN ? AND ?
            ORDER BY CAST(SUBSTR(release_no, 4) AS INTEGER) DESC
            """,
            (end_number, start_number),
        ).fetchall()

        manual_review_rows = connection.execute(
            """
            SELECT release_no, document_name, review_reason
            FROM documents
            WHERE CAST(SUBSTR(release_no, 4) AS INTEGER)
                  BETWEEN ? AND ?
              AND manual_review_required = 1
            ORDER BY
                CAST(SUBSTR(release_no, 4) AS INTEGER) DESC,
                id ASC
            """,
            (end_number, start_number),
        ).fetchall()

    stored_release_numbers = {
        row["release_no"]
        for row in release_rows
    }
    requested_release_nos = [
        f"LR-{release_number}"
        for release_number in release_numbers
    ]

    missing_releases = [
        release_no
        for release_no in requested_release_nos
        if release_no not in stored_release_numbers
    ]
    unavailable_releases = [
        row["release_no"]
        for row in release_rows
        if not (row["date"] or "").strip()
        and not (row["respondents"] or "").strip()
        and not (row["content"] or "").strip()
    ]

    missing_keywords = [
        row["release_no"]
        for row in release_rows
        if not (row["keyword"] or "").strip()
    ]
    missing_summaries = [
        row["release_no"]
        for row in release_rows
        if not (row["complaint"] or "").strip()
    ]

    manual_review_documents = [
        {
            "release_no": row["release_no"],
            "document_name": row["document_name"],
            "review_reason": row["review_reason"] or "Manual review required.",
        }
        for row in manual_review_rows
    ]

    return {
        "requested_count": len(requested_release_nos),
        "stored_count": len(release_rows),
        "keyword_completed_count": len(release_rows) - len(missing_keywords),
        "summary_completed_count": len(release_rows) - len(missing_summaries),
        "missing_releases": missing_releases,
        "unavailable_releases": unavailable_releases,
        "missing_keywords": missing_keywords,
        "missing_summaries": missing_summaries,
        "manual_review_documents": manual_review_documents,
    }


def print_interval_completion_report(report):
    """Displays a compact final quality-control report."""

    print("\n" + "=" * 80)
    print("FINAL INTERVAL CHECK")
    print("=" * 80)
    print(f"Requested releases: {report['requested_count']}")
    print(f"Stored releases: {report['stored_count']}")
    print(f"Keyword completed: {report['keyword_completed_count']}")
    print(f"Summary completed: {report['summary_completed_count']}")
    print(
        "PDF documents requiring manual review: "
        f"{len(report['manual_review_documents'])}"
    )

    if report["missing_releases"]:
        print("\nMissing or failed releases:")
        for release_no in report["missing_releases"]:
            print(f"- {release_no}")

    if report["unavailable_releases"]:
        print(
            "\nRelease pages not found or not extractable "
            "(blank placeholder rows will still be exported):"
        )
        for release_no in report["unavailable_releases"]:
            print(f"- {release_no}")

    if report["missing_keywords"]:
        print("\nReleases with a blank Keyword:")
        for release_no in report["missing_keywords"]:
            print(f"- {release_no}")

    if report["missing_summaries"]:
        print("\nReleases with a blank Complaint summary:")
        for release_no in report["missing_summaries"]:
            print(f"- {release_no}")

    if report["manual_review_documents"]:
        print("\nResource documents requiring manual review:")
        for document in report["manual_review_documents"]:
            print(
                f"- {document['release_no']}: {document['document_name']} "
                f"({document['review_reason']})"
            )

    print("=" * 80)


def confirm_excel_export(report):
    """Requires deliberate confirmation when the interval is incomplete."""

    has_warnings = any(
        (
            report["missing_releases"],
            report["unavailable_releases"],
            report["missing_keywords"],
            report["missing_summaries"],
            report["manual_review_documents"],
        )
    )

    if not has_warnings:
        print("All requested releases passed the final interval check.")
        return True

    print(
        "\nThe interval contains incomplete or manually flagged items. "
        "No Excel file has been created yet."
    )

    while True:
        answer = input(
            "Type export to create the Excel file anyway, or cancel to finish "
            "without exporting: "
        ).strip().casefold()

        if answer == "export":
            return True

        if answer == "cancel":
            return False

        print("Please enter exactly export or cancel.")

def get_sec_url(url, timeout):
    """Downloads one SEC URL and applies the configured request pause."""

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=timeout,
    )
    time.sleep(REQUEST_DELAY_SECONDS)
    return response

def format_release_date(date_text):
    """Converts SEC dates into DD.MM.YYYY, including missing spaces."""

    cleaned_date = (date_text or "").replace(".", "")
    cleaned_date = cleaned_date.replace("\u00a0", " ")

    # Fix SEC dates such as "September28, 2023".
    cleaned_date = re.sub(
        r"(?<=[A-Za-z])(?=\d)",
        " ",
        cleaned_date,
    )

    # Normalize inconsistent spacing, including "September 28,2023".
    cleaned_date = re.sub(r"\s+", " ", cleaned_date).strip()
    cleaned_date = re.sub(r",\s*", ", ", cleaned_date)

    for date_format in ("%B %d, %Y", "%b %d, %Y"):
        try:
            date_object = datetime.strptime(cleaned_date, date_format)
            return date_object.strftime("%d.%m.%Y")
        except ValueError:
            continue

    return None

def extract_resource_documents(soup, release_url):
    """Returns every PDF listed under the page's Resources heading."""

    resource_section = None

    for section in soup.select(".rightrail-list"):
        heading = section.find("h2")

        if (
            heading is not None
            and heading.get_text(" ", strip=True).casefold() == "resources"
        ):
            resource_section = section
            break

    if resource_section is None:
        return []

    documents = []
    seen_urls = set()

    for link in resource_section.select("a[href]"):
        relative_url = link.get("href", "").strip()

        if not relative_url:
            continue

        link_type = link.get("type", "").casefold()
        path_without_query = relative_url.casefold().split("?", 1)[0]

        if (
            link_type != "application/pdf"
            and not path_without_query.endswith(".pdf")
        ):
            continue

        full_url = urljoin(release_url, relative_url)

        if full_url in seen_urls:
            continue

        document_name = link.get_text(" ", strip=True) or "Unnamed document"

        documents.append(
            {
                "name": document_name,
                "url": full_url,
            }
        )
        seen_urls.add(full_url)

    return documents

def assess_pdf_quality(extracted_text, page_count):
    """
    Checks both the amount and readability of extracted PDF text.

    Character counts alone are not sufficient: some PDFs return thousands of
    characters consisting mainly of broken font codes such as /i255/1/2/3.
    Those documents must be treated as unusable even though the extraction is
    technically non-empty.
    """

    text = (extracted_text or "").strip()
    character_count = len(text)
    characters_per_page = (
        character_count / page_count
        if page_count > 0
        else 0
    )

    if character_count == 0:
        return (
            characters_per_page,
            True,
            "No readable text was extracted.",
        )

    if character_count < MIN_TOTAL_CHARACTERS:
        return (
            characters_per_page,
            True,
            "The total extracted text is suspiciously short.",
        )

    if (
        page_count >= 2
        and characters_per_page < MIN_CHARACTERS_PER_PAGE
    ):
        return (
            characters_per_page,
            True,
            "The extracted text per page is suspiciously low.",
        )

    # Detect broken embedded-font mappings such as:
    # /i255/1/2/3/4/... or long slash-number sequences.
    font_code_tokens = re.findall(r"/i\d+/", text, flags=re.IGNORECASE)
    slash_number_runs = re.findall(
        r"(?:/[A-Za-z]?\d+){8,}",
        text,
        flags=re.IGNORECASE,
    )

    non_whitespace_characters = [
        character
        for character in text
        if not character.isspace()
    ]
    non_whitespace_count = len(non_whitespace_characters)
    alphabetic_count = sum(
        character.isalpha()
        for character in non_whitespace_characters
    )
    alphabetic_ratio = (
        alphabetic_count / non_whitespace_count
        if non_whitespace_count
        else 0
    )

    normal_words = re.findall(
        r"\b[A-Za-z][A-Za-z'’-]{2,}\b",
        text,
    )
    replacement_character_count = text.count("\ufffd")
    replacement_ratio = (
        replacement_character_count / character_count
        if character_count
        else 0
    )

    garbled_reasons = []

    if len(font_code_tokens) >= 8:
        garbled_reasons.append(
            "repeated broken font-code tokens such as /i255/ were detected"
        )

    if slash_number_runs:
        garbled_reasons.append(
            "long slash-and-number sequences were detected"
        )

    if character_count >= 500 and alphabetic_ratio < 0.25:
        garbled_reasons.append(
            "too little of the extracted content consists of normal letters"
        )

    if character_count >= 1000 and len(normal_words) < 20:
        garbled_reasons.append(
            "too few normal words were detected for the amount of extracted text"
        )

    if replacement_ratio >= 0.01:
        garbled_reasons.append(
            "an excessive number of replacement characters was detected"
        )

    if garbled_reasons:
        return (
            characters_per_page,
            True,
            "The extracted text appears garbled or incorrectly decoded: "
            + "; ".join(garbled_reasons)
            + ".",
        )

    return characters_per_page, False, ""


def should_omit_extracted_text(document):
    """Returns True when corrupted text should not be sent to ChatGPT."""

    if not document.get("manual_review_required"):
        return False

    reason = (document.get("review_reason") or "").casefold()

    return any(
        marker in reason
        for marker in (
            "appears garbled",
            "incorrectly decoded",
            "broken font-code",
            "slash-and-number",
            "replacement characters",
        )
    )

def extract_pdf_documents(release_no, documents):
    """Reuses stored PDFs where possible and extracts only missing PDFs."""

    extracted_documents = []

    for document in documents:
        stored_document = load_stored_document(
            release_no,
            document["url"],
        )

        if stored_document is not None:
            stored_document["name"] = document["name"]

            if stored_document.get("text_source") == "manual":
                stored_document["character_count"] = len(
                    stored_document["text"].strip()
                )
                page_count = stored_document["page_count"] or 0
                stored_document["characters_per_page"] = (
                    stored_document["character_count"] / page_count
                    if page_count > 0
                    else 0
                )
                stored_document["extraction_successful"] = True
                stored_document["manual_review_required"] = False
                stored_document["review_reason"] = ""

                extracted_documents.append(stored_document)
                print(f"Reused manually supplied PDF text: {document['name']}")
                continue

            (
                characters_per_page,
                manual_review_required,
                review_reason,
            ) = assess_pdf_quality(
                stored_document["text"],
                stored_document["page_count"],
            )

            stored_document["character_count"] = len(
                stored_document["text"].strip()
            )
            stored_document["characters_per_page"] = characters_per_page
            stored_document["manual_review_required"] = (
                manual_review_required
            )
            stored_document["review_reason"] = review_reason

            extracted_documents.append(stored_document)
            print(f"Reused stored PDF: {document['name']}")

            if manual_review_required:
                print(f"Manual review required: {review_reason}")
            else:
                print("Stored PDF text appears usable after revalidation.")

            continue

        print(f"Downloading PDF: {document['name']}")

        try:
            response = get_sec_url(document["url"], timeout=60)
            response.raise_for_status()

            reader = PdfReader(
                BytesIO(response.content),
                strict=False,
            )

            page_texts = []

            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    page_texts.append(page_text.strip())

            full_text = "\n\n".join(page_texts)
            page_count = len(reader.pages)
            character_count = len(full_text.strip())

            (
                characters_per_page,
                manual_review_required,
                review_reason,
            ) = assess_pdf_quality(
                full_text,
                page_count,
            )

            extracted_document = {
                "name": document["name"],
                "url": document["url"],
                "text": full_text,
                "page_count": page_count,
                "character_count": character_count,
                "characters_per_page": characters_per_page,
                "extraction_successful": character_count > 0,
                "manual_review_required": manual_review_required,
                "review_reason": review_reason,
                "text_source": "automatic",
                "reused_from_database": False,
            }

            extracted_documents.append(extracted_document)

            print(f"PDF pages: {page_count}")
            print(f"Extracted characters: {character_count}")

            if manual_review_required:
                print(f"Manual review required: {review_reason}")
            else:
                print("PDF text appears usable.")

        except Exception as error:
            print(f"Could not process PDF: {document['name']}")
            print(error)

            extracted_documents.append(
                {
                    "name": document["name"],
                    "url": document["url"],
                    "text": "",
                    "page_count": 0,
                    "character_count": 0,
                    "characters_per_page": 0,
                    "extraction_successful": False,
                    "manual_review_required": True,
                    "review_reason": "The PDF could not be processed.",
                    "text_source": "automatic",
                    "reused_from_database": False,
                }
            )

        print()

    return extracted_documents

def extract_release(release_number):
    """Downloads and extracts one SEC Litigation Release and its PDFs."""

    release_url = f"{BASE_RELEASE_URL}{release_number}"
    release_no = f"LR-{release_number}"

    print(f"\nOpening {release_no}...")

    try:
        response = get_sec_url(release_url, timeout=30)

        if response.status_code == 404:
            print(f"{release_no} was not found.")
            return None

        response.raise_for_status()

    except requests.RequestException as error:
        print(f"Could not download {release_no}: {error}")
        return None

    soup = BeautifulSoup(response.content, "html.parser")
    main_content = soup.select_one(".field--name-body")

    if main_content is None:
        print(f"Could not find the main content for {release_no}.")
        return None

    title_element = soup.select_one("h1.page-title__heading")
    respondents = (
        title_element.get_text(" ", strip=True)
        if title_element is not None
        else "Not found"
    )

    release_heading = None

    # First try the normal SEC structure.
    for heading in main_content.find_all(["h1", "h2", "h3", "p"]):
        heading_text = heading.get_text(" ", strip=True)

        if re.search(
            r"(?:litigation|lit\.)\s+release\s+no\.?",
            heading_text,
            re.IGNORECASE,
        ):
            release_heading = heading_text
            break

    # Some SEC pages split the heading across spans or other HTML elements.
    # Search the complete body text as a fallback.
    if release_heading is None:
        complete_body_text = main_content.get_text("\n", strip=True)
        heading_match = re.search(
            r"litigation\s+release\s+no\.?\s*\d+"
            r"\s*[/\\-]?\s*"
            r"[A-Za-z]+\.?\s*\d{1,2},\s*\d{4}",
            complete_body_text,
            re.IGNORECASE,
        )

        if heading_match is not None:
            release_heading = heading_match.group(0)

    if release_heading is None:
        print(f"Could not find the number and date for {release_no}.")
        page_preview = " ".join(main_content.get_text(" ", strip=True).split())[:500]
        print(f"Page text preview: {page_preview!r}")
        return None

    number_match = re.search(
        r"(?:litigation|lit\.)\s+release\s+no\.?\s*(\d+)",
        release_heading,
        re.IGNORECASE,
    )
    date_match = re.search(
        r"([A-Za-z]+\.?\s*\d{1,2},\s*\d{4})",
        release_heading,
        re.IGNORECASE,
    )

    if number_match is None or date_match is None:
        print("Could not understand this release heading:")
        print(repr(release_heading))
        return None

    heading_number = number_match.group(1)

    if heading_number != str(release_number):
        print(
            "Warning: the release number in the page heading "
            f"({heading_number}) differs from the URL ({release_number})."
        )

    release_date = format_release_date(date_match.group(1))

    if release_date is None:
        print(f"Could not understand the date for {release_no}.")
        return None

    content_texts = []

    for element in main_content.find_all(["p", "li"]):
        pdf_link = element.find(
            "a",
            href=re.compile(r"\.pdf(?:$|\?)", re.IGNORECASE),
        )

        if pdf_link is not None:
            continue

        text = element.get_text(" ", strip=True)
        if text:
            content_texts.append(text)

    start_position = 0

    for position, text in enumerate(content_texts):
        lower_text = text.casefold()

        if (
            lower_text.startswith("on ")
            or lower_text.startswith("the securities and exchange commission")
            or lower_text.startswith("the commission")
            or lower_text.startswith("according to")
        ):
            start_position = position
            break

    content = "\n\n".join(content_texts[start_position:]).strip()

    if not content:
        print(f"No narrative content was found for {release_no}.")
        return None

    resource_documents = extract_resource_documents(soup, release_url)
    extracted_documents = extract_pdf_documents(
        release_no,
        resource_documents,
    )

    document_urls = "\n".join(
        document["url"]
        for document in resource_documents
    )

    manual_review_documents = [
        document["name"]
        for document in extracted_documents
        if document["manual_review_required"]
    ]

    return {
        "Date": release_date,
        "Respondents": respondents,
        "Release No.": release_no,
        "Keyword": "",
        "Content": content,
        "Complaint": "",
        "Link to Release": release_url,
        "Link to Complaint": document_urls,
        "Documents": extracted_documents,
        "Keyword Generation Allowed": bool(content),
        "Complaint Summary Allowed": not manual_review_documents,
        "Manual Review Documents": manual_review_documents,
    }

# -----------------------------------------------------------------------------
# MANUAL CHATGPT WORKFLOW WITH BACKGROUND SAFETY CHECKS
# -----------------------------------------------------------------------------

def load_existing_fields(release_no):
    """Loads the current Keyword and Complaint values for one release."""

    with connect_database() as connection:
        row = connection.execute(
            """
            SELECT keyword, complaint
            FROM releases
            WHERE release_no = ?
            """,
            (release_no,),
        ).fetchone()

    if row is None:
        return {"keyword": "", "complaint": ""}

    return {
        "keyword": row["keyword"] or "",
        "complaint": row["complaint"] or "",
    }


def load_keywords():
    """Loads the approved keyword catalogue from keywords.txt."""

    if not KEYWORDS_FILE.exists():
        raise FileNotFoundError(
            f"Keyword file not found: {KEYWORDS_FILE}"
        )

    keywords = []
    seen = set()

    for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
        keyword = " ".join(line.strip().split())
        normalized = keyword.casefold()

        if keyword and normalized not in seen:
            keywords.append(keyword)
            seen.add(normalized)

    if not keywords:
        raise ValueError("keywords.txt does not contain any keywords.")

    return keywords


def append_new_keyword(keyword, approved_keywords):
    """Adds a deliberately accepted new keyword to keywords.txt."""

    normalized_existing = {
        existing_keyword.casefold()
        for existing_keyword in approved_keywords
    }

    if keyword.casefold() in normalized_existing:
        return False

    existing_text = KEYWORDS_FILE.read_text(encoding="utf-8")

    with KEYWORDS_FILE.open("a", encoding="utf-8") as keyword_file:
        if existing_text and not existing_text.endswith("\n"):
            keyword_file.write("\n")
        keyword_file.write(f"{keyword}\n")

    approved_keywords.append(keyword)
    return True


def save_manual_fields(release_no, keyword, complaint, overwrite=False):
    """
    Saves manually generated Keyword and Complaint values.

    By default, only blank fields are filled. When overwrite=True, existing
    values are replaced deliberately.
    """

    existing = load_existing_fields(release_no)

    keyword_was_written = overwrite or not existing["keyword"].strip()
    complaint_was_written = overwrite or not existing["complaint"].strip()

    final_keyword = keyword if keyword_was_written else existing["keyword"]
    final_complaint = (
        complaint if complaint_was_written else existing["complaint"]
    )

    with connect_database() as connection:
        connection.execute(
            """
            UPDATE releases
            SET keyword = ?, complaint = ?
            WHERE release_no = ?
            """,
            (final_keyword, final_complaint, release_no),
        )

    return {
        "keyword_saved": keyword_was_written,
        "complaint_saved": complaint_was_written,
        "keyword": final_keyword,
        "complaint": final_complaint,
    }


def looks_like_wrong_manual_pdf_paste(text, document_url=""):
    """Detects common accidental pastes before manual PDF text is saved."""

    pasted = (text or "").strip()

    if not pasted:
        return "No text was pasted."

    if document_url and pasted == document_url.strip():
        return "Only the PDF URL was pasted, not the extracted document text."

    forbidden_markers = [
        SOURCE_START_MARKER,
        SOURCE_END_MARKER,
        RESPONSE_START_MARKER,
        RESPONSE_END_MARKER,
        "SEC LITIGATION RELEASE SOURCE MATERIAL",
        "CURRENT APPROVED KEYWORD CATALOGUE",
        "KEYWORD:",
        "SUMMARY:",
    ]

    matches = [
        marker
        for marker in forbidden_markers
        if marker.casefold() in pasted.casefold()
    ]

    if RESPONSE_START_MARKER.casefold() in pasted.casefold():
        return (
            "The pasted text appears to be a ChatGPT machine-readable response, "
            "not PDF source text."
        )

    if SOURCE_START_MARKER.casefold() in pasted.casefold():
        return (
            "The pasted text appears to be the ChatGPT source prompt, "
            "not PDF source text."
        )

    if len(matches) >= 2:
        return (
            "The pasted text contains multiple workflow markers and does not "
            "look like manually extracted PDF text."
        )

    return ""


def save_manual_document_text(release_no, document, manual_text):
    """Replaces one unreliable automatic PDF extraction with manual text."""

    cleaned_text = manual_text.strip()
    character_count = len(cleaned_text)
    page_count = document.get("page_count", 0) or 0
    characters_per_page = (
        character_count / page_count
        if page_count > 0
        else 0
    )

    with connect_database() as connection:
        cursor = connection.execute(
            """
            UPDATE documents
            SET extracted_text = ?,
                character_count = ?,
                characters_per_page = ?,
                extraction_successful = 1,
                manual_review_required = 0,
                review_reason = '',
                text_source = 'manual'
            WHERE release_no = ?
              AND document_url = ?
            """,
            (
                cleaned_text,
                character_count,
                characters_per_page,
                release_no,
                document["url"],
            ),
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                "Could not find the expected document row in SQLite for "
                f"{release_no}: {document['name']}"
            )

    document["text"] = cleaned_text
    document["character_count"] = character_count
    document["characters_per_page"] = characters_per_page
    document["extraction_successful"] = True
    document["manual_review_required"] = False
    document["review_reason"] = ""
    document["text_source"] = "manual"
    document["reused_from_database"] = False


def copy_manual_document_url(document):
    """Copies one Resource PDF URL and shows a fallback when copying fails."""

    copied, clipboard_details = copy_to_clipboard(document["url"])

    if copied:
        print(
            "PDF link copied to clipboard using "
            f"{clipboard_details}."
        )
    else:
        print("Automatic clipboard copying failed.")
        if clipboard_details:
            print(f"Technical details: {clipboard_details}")

    print(f"PDF: {document['url']}")


def recover_unreliable_resource_documents(record):
    """
    Recovers every unreadable Resource PDF before the ChatGPT review starts.

    Returns a dictionary containing status, recovered count and unresolved count.
    A skipped document remains flagged and blocks ChatGPT review for the release.
    """

    release_no = record["Release No."]
    unreliable_documents = [
        document
        for document in record["Documents"]
        if document["manual_review_required"]
    ]

    if not unreliable_documents:
        return {
            "status": "complete",
            "recovered": 0,
            "unresolved": 0,
        }

    print("\n" + "!" * 100)
    print("MANUAL PDF TEXT RECOVERY REQUIRED")
    print("!" * 100)
    print(f"Release: {release_no}")
    print(
        f"Unreadable Resource documents: {len(unreliable_documents)}"
    )
    print(
        "Each affected PDF will be handled one by one before the ChatGPT "
        "summary step."
    )
    print("!" * 100)

    recovered = 0

    for position, document in enumerate(unreliable_documents, start=1):
        while document["manual_review_required"]:
            print("\n" + "=" * 100)
            print(
                f"MANUAL PDF: {position} OF {len(unreliable_documents)}"
            )
            print("=" * 100)
            print(f"Release: {release_no}")
            print(f"Document: {document['name']}")
            print(
                "Automatic extraction problem: "
                + (document["review_reason"] or "Text was not reliable.")
            )

            copy_manual_document_url(document)

            action = input(
                "\nOpen the PDF and extract its text manually. Then press Enter "
                "to paste the text, type recopy to copy the PDF link again, "
                "skip to leave this document unresolved, or quit to stop the run: "
            ).strip().casefold()

            if action == "quit":
                unresolved = sum(
                    1
                    for item in record["Documents"]
                    if item["manual_review_required"]
                )
                return {
                    "status": "quit",
                    "recovered": recovered,
                    "unresolved": unresolved,
                }

            if action == "skip":
                print(
                    f"{document['name']} remains flagged for manual review."
                )
                break

            if action == "recopy":
                continue

            manual_text = read_multiline_input(
                "Paste the manually extracted PDF text below.",
                END_MARKER,
            )

            wrong_paste_reason = looks_like_wrong_manual_pdf_paste(
                manual_text,
                document["url"],
            )

            if wrong_paste_reason:
                print("\nMANUAL PDF TEXT REJECTED — NOTHING WAS SAVED")
                print(wrong_paste_reason)
                print(
                    "Press Enter again to retry, or choose recopy on the next "
                    "screen if you need the PDF link again."
                )
                continue

            character_count = len(manual_text.strip())
            preview = " ".join(manual_text.split())[:300]

            print("\n" + "-" * 100)
            print("MANUAL PDF TEXT READY TO SAVE")
            print("-" * 100)
            print(f"Release: {release_no}")
            print(f"Document: {document['name']}")
            print(f"Characters received: {character_count}")
            print(f"Beginning of text: {preview}")
            print("-" * 100)

            if character_count < 50:
                print(
                    "WARNING: The pasted text is unusually short. Check that "
                    "the complete document text was copied."
                )

            save_choice = input(
                "Press Enter to save this manual PDF text, type retry to paste "
                "again, recopy to copy the PDF link again, skip to leave the "
                "document unresolved, or quit to stop the run: "
            ).strip().casefold()

            if save_choice == "quit":
                unresolved = sum(
                    1
                    for item in record["Documents"]
                    if item["manual_review_required"]
                )
                return {
                    "status": "quit",
                    "recovered": recovered,
                    "unresolved": unresolved,
                }

            if save_choice == "skip":
                print(
                    f"{document['name']} remains flagged for manual review."
                )
                break

            if save_choice == "recopy":
                continue

            if save_choice == "retry":
                continue

            if save_choice != "":
                print("The entered option was not understood. Nothing was saved.")
                continue

            save_manual_document_text(
                release_no,
                document,
                manual_text,
            )
            recovered += 1
            print(
                "Manual PDF text saved to SQLite. This document is now "
                "treated as usable source material."
            )

    unresolved_documents = [
        document
        for document in record["Documents"]
        if document["manual_review_required"]
    ]

    record["Manual Review Documents"] = [
        document["name"]
        for document in unresolved_documents
    ]
    record["Complaint Summary Allowed"] = not unresolved_documents

    if unresolved_documents:
        print("\n" + "!" * 100)
        print("SOURCE MATERIAL STILL INCOMPLETE")
        print("!" * 100)
        print(
            "The following Resource documents are still unresolved, so the "
            "ChatGPT review will be skipped for this release:"
        )
        for document in unresolved_documents:
            print(f"- {document['name']}")
        print("!" * 100)
        return {
            "status": "incomplete",
            "recovered": recovered,
            "unresolved": len(unresolved_documents),
        }

    print("\n" + "=" * 100)
    print("SOURCE COMPLETENESS CHECK")
    print("=" * 100)
    print("Litigation Release content: usable")
    for document in record["Documents"]:
        source_label = (
            "manually supplied"
            if document.get("text_source") == "manual"
            else "automatic extraction"
        )
        print(f"- {document['name']}: usable — {source_label}")
    print("All Resource document text is now available for the ChatGPT step.")
    print("=" * 100)

    return {
        "status": "complete",
        "recovered": recovered,
        "unresolved": 0,
    }


def generate_case_id(release_no):
    """Creates a short unique identifier for one copy/paste round."""

    return f"{release_no}-{secrets.token_hex(3).upper()}"


def build_source_block(record, case_id, approved_keywords):
    """
    Builds one complete source block for ChatGPT.

    The output contract is repeated for every case so the response can be
    parsed and matched safely even if the separate chat has a long history.
    """

    lines = [
        ">>> START OF TEXT TO COPY INTO CHATGPT >>>",
        "SEC LITIGATION RELEASE SOURCE MATERIAL",
        "",
        "IMPORTANT INSTRUCTIONS FOR THIS CASE",
        "Use the permanent keyword and summary instructions already defined ",
        "in this chat. The complete case-specific rules below are repeated ",
        "and override any conflicting output-format instruction.",
        "",
        "Return ONLY one machine-readable response block. Do not add an ",
        "introduction, explanation, closing sentence, quotation marks, or a ",
        "Markdown code fence outside the block.",
        "",
        "Copy CASE-ID, RELEASE-NO, and RESPONDENTS exactly as shown below. ",
        "Do not abbreviate, translate, reorder, or correct them.",
        "",
        "STRICT LINE-BREAK RULES",
        "- Every machine-readable label must be on its own line.",
        "- CASE-ID, RELEASE-NO, and RESPONDENTS must each be separate lines.",
        "- KEYWORD: must be on its own line, followed by exactly one keyword ",
        "  on the next line.",
        "- SUMMARY: must be on its own line. The summary begins on the next line.",
        "- In the summary, Case, Court, and Filed must each be on separate lines.",
        "- Every bold section heading must be on a separate line by itself.",
        "- The paragraph belonging to a heading must begin on the following line.",
        "- Insert a blank line between Filed and Allegations and between all ",
        "  subsequent summary sections.",
        "- Never combine Case, Court, Filed, headings, and body text into one ",
        "  paragraph or one continuous line.",
        "",
        "Use exactly this machine-readable structure and line layout:",
        "",
        RESPONSE_START_MARKER,
        f"CASE-ID: {case_id}",
        f"RELEASE-NO: {record['Release No.']}",
        f"RESPONDENTS: {record['Respondents']}",
        "KEYWORD:",
        "[one keyword only; use the existing catalogue whenever suitable]",
        "SUMMARY:",
        "**Case:** [formal case caption only; no case number]",
        "**Court:** [court stated in the supplied materials]",
        "**Filed:** [relevant filing date]",
        "",
        "**Allegations**",
        ""
        "[summary of the SEC's allegations; if the document is purely procedural, state briefly that no substantive allegations are detailed]",
        "",
        "**Findings and Claims**",
        ""
        "[legal claims, findings, consent terms, judgment terms, or procedural posture]",
        "",
        "**Relief Sought**",
        ""
        "[requested or ordered relief]",
        RESPONSE_END_MARKER,
        "",
        "MANDATORY SUMMARY FORMAT",
        "- The three metadata lines must use exactly **Case:**, **Court:**, and ",
        "  **Filed:** and must remain on three separate lines.",
        "- The Case field must contain only the formal case caption. Do not add ",
        "  a civil action number, case number, court, or filing date.",
        "- The Case field must always begin with **SEC v.**",
        "- Replace **Securities and Exchange Commission v.**, ",
        "  **United States Securities and Exchange Commission v.**, and ",
        "  **U.S. Securities and Exchange Commission v.** with **SEC v.**",
        "- Do not use **SEC versus**, **S.E.C. v.**, or any other variation.",
        "- The summary must always contain **Allegations** as a standalone heading.",
        "- For a procedural filing with no detailed allegations, write a short ",
        "  Allegations paragraph explaining that the supplied document does not ",
        "  detail substantive allegations.",
        "- Use **Findings and Claims** as the normal second section heading. You ",
        "  may instead use separate **Misconduct** and **Violations** headings ",
        "  only when that structure is materially clearer for the case.",
        "- The final heading must be exactly one of: **Relief Sought**, ",
        "  **Relief Sought / Final Judgment**, or **Relief Sought / Outcome**.",
        "- Do not place any section heading and its paragraph on the same line.",
        "- Preserve the double asterisks around every metadata label and heading.",
        "- Use professional English, neutral legal wording, and distinguish SEC ",
        "  allegations from court findings, settlements, consent judgments, and ",
        "  procedural outcomes.",
        "- Do not invent unsupported facts and do not repeat information merely ",
        "  because it appears in several source documents.",
        "",
        "SOURCE-MATERIAL RULES",
        "- Use the Litigation Release content and all readable Resource documents ",
        "  supplied below together when preparing the summary.",
        "- Do not base the summary only on the Litigation Release when readable ",
        "  Resource documents contain additional material information.",
        "- Reconcile overlapping information without repetition.",
        "- Where the documents reflect different procedural stages, clearly ",
        "  distinguish allegations, proposed relief, consent terms, court orders, ",
        "  settlements, and final judgments.",
        "- Do not use any Resource document marked as unreadable, garbled, ",
        "  incomplete, or requiring manual review.",
        "- When a Resource document is marked for manual review, rely only on the ",
        "  other readable source materials and do not infer the missing document's ",
        "  contents.",
        "",
"KEYWORD RULES",
        "- Select exactly one keyword from the CURRENT APPROVED KEYWORD CATALOGUE.",
        "- Do not create a new keyword.",
        "- Select the most specific approved category that describes the central ",
        "  misconduct, regulatory issue, or procedural outcome.",
        "- Do not create distinctions based only on the industry, product, security, ",
        "  issuer, type of investor, communication method, relationship between ",
        "  defendants, or type of MNPI involved.",
        "- Where a more specific approved category applies, do not use a broader ",
        "  fallback category such as Securities Fraud.",
        "- Examples: insider trading involving earnings announcements, clinical-trial ",
        "  data, SPACs, family tipping, or follow-on offerings is Insider Trading.",
        "- Crypto does not automatically mean Crypto Fraud. Classify according to the ",
        "  underlying conduct where a more specific category applies.",
        "- For example: crypto price manipulation = Market Manipulation; ",
        "  unregistered crypto securities = Unregistered Securities Activity; ",
        "  operating as an unregistered crypto dealer = Unregistered Broker / Dealer Activity.",
        "- Use Trading Misconduct only when no more specific trading category such as ",
        "  Cherry-Picking, Churning, Insider Trading, or Market Manipulation applies.",
        "- Return only one approved keyword and do not explain the selection.",
        "",
        "CURRENT APPROVED KEYWORD CATALOGUE",
        f"Catalogue entries: {len(approved_keywords)}",
        "Use one of the following whenever reasonably suitable:",
        *[f"- {keyword}" for keyword in approved_keywords],
        "",
        "CASE IDENTIFICATION",
        f"CASE-ID: {case_id}",
        f"RELEASE-NO: {record['Release No.']}",
        f"DATE: {record['Date']}",
        f"RESPONDENTS: {record['Respondents']}",
        f"LINK TO RELEASE: {record['Link to Release']}",
        "",
        "=== LITIGATION RELEASE CONTENT ===",
        "",
        record["Content"].strip(),
    ]

    documents = record["Documents"]

    if not documents:
        lines.extend(
            [
                "",
                "=== RESOURCE DOCUMENTS ===",
                "",
                "No Resource documents are listed for this release.",
            ]
        )
    else:
        for number, document in enumerate(documents, start=1):
            lines.extend(
                [
                    "",
                    (
                        f"=== RESOURCE DOCUMENT {number}: "
                        f"{document['name']} ==="
                    ),
                    f"URL: {document['url']}",
                    f"Pages: {document['page_count']}",
                    (
                        "Text source: manually supplied"
                        if document.get("text_source") == "manual"
                        else "Text source: automatic extraction"
                    ),
                ]
            )

            if document["manual_review_required"]:
                lines.append("TEXT EXTRACTION STATUS: NOT RELIABLE")
                lines.append(
                    "WARNING: Manual review is required. "
                    + (document["review_reason"] or "Text may be incomplete.")
                )
            else:
                lines.append("Text extraction status: usable")

            if should_omit_extracted_text(document):
                source_document_text = (
                    "[The automatically extracted text was omitted because it "
                    "appears corrupted or incorrectly decoded. Open the PDF "
                    "using the URL above, review it manually, and provide its "
                    "readable contents before relying on this document.]"
                )
            else:
                source_document_text = (
                    document["text"].strip()
                    or (
                        "[No readable text was extracted. Open the document "
                        "using the URL above and add its contents manually.]"
                    )
                )

            lines.extend(
                [
                    "",
                    source_document_text,
                ]
            )

    lines.extend(
        [
            "",
            "END OF SOURCE MATERIAL",
            "<<< END OF TEXT TO COPY INTO CHATGPT <<<",
        ]
    )

    return "\n".join(lines)


def copy_to_clipboard(text):
    """
    Copies Unicode text to the clipboard.

    On Windows, the function first uses the native Windows clipboard. If that
    fails, it tries PowerShell's Set-Clipboard command. Tkinter is retained as
    a final fallback.

    Returns:
        (success, method_or_error)
    """

    errors = []

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.EmptyClipboard.argtypes = []
            user32.EmptyClipboard.restype = wintypes.BOOL
            user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            user32.SetClipboardData.restype = wintypes.HANDLE
            user32.CloseClipboard.argtypes = []
            user32.CloseClipboard.restype = wintypes.BOOL

            kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = wintypes.LPVOID
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalUnlock.restype = wintypes.BOOL
            kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalFree.restype = wintypes.HGLOBAL

            clipboard_opened = False

            for _ in range(10):
                if user32.OpenClipboard(None):
                    clipboard_opened = True
                    break
                time.sleep(0.10)

            if not clipboard_opened:
                raise OSError("The Windows clipboard was busy.")

            memory_handle = None

            try:
                if not user32.EmptyClipboard():
                    raise ctypes.WinError(ctypes.get_last_error())

                encoded_text = text.encode("utf-16-le") + b"\x00\x00"
                memory_handle = kernel32.GlobalAlloc(
                    GMEM_MOVEABLE,
                    len(encoded_text),
                )

                if not memory_handle:
                    raise ctypes.WinError(ctypes.get_last_error())

                memory_pointer = kernel32.GlobalLock(memory_handle)

                if not memory_pointer:
                    raise ctypes.WinError(ctypes.get_last_error())

                try:
                    ctypes.memmove(
                        memory_pointer,
                        encoded_text,
                        len(encoded_text),
                    )
                finally:
                    kernel32.GlobalUnlock(memory_handle)

                if not user32.SetClipboardData(
                    CF_UNICODETEXT,
                    memory_handle,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())

                memory_handle = None

            finally:
                user32.CloseClipboard()

                if memory_handle:
                    kernel32.GlobalFree(memory_handle)

            return True, "native Windows clipboard"

        except Exception as error:
            errors.append(f"Windows clipboard: {error}")

        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
                ],
                input=text,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
                check=False,
            )

            if result.returncode == 0:
                return True, "PowerShell Set-Clipboard"

            error_text = result.stderr.strip() or "unknown PowerShell error"
            errors.append(f"PowerShell clipboard: {error_text}")

        except Exception as error:
            errors.append(f"PowerShell clipboard: {error}")

    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True, "Tkinter clipboard"

    except Exception as error:
        errors.append(f"Tkinter clipboard: {error}")

    return False, "; ".join(errors)


def present_source_block_again(source_block):
    """Copies and re-displays the active source prompt for the current case."""

    copied, clipboard_details = copy_to_clipboard(source_block)

    print("\n" + "=" * 100)
    print("COPY BLOCK FOR CHATGPT — ACTIVE CASE COPIED AGAIN")
    print("=" * 100)
    print(source_block)
    print("=" * 100)

    if copied:
        print(
            "\nThe active case prompt was copied again using "
            f"{clipboard_details}."
        )
        print(
            "Return to the separate ChatGPT chat and press Ctrl+V. "
            "After ChatGPT generates a new response, copy that response "
            "before returning to this terminal."
        )
    else:
        print("\nAutomatic clipboard copying failed.")
        if clipboard_details:
            print(f"Technical details: {clipboard_details}")
        print(
            "Manually copy everything beginning with:\n"
            ">>> START OF TEXT TO COPY INTO CHATGPT >>>\n"
            "and ending with:\n"
            "<<< END OF TEXT TO COPY INTO CHATGPT <<<"
        )

    input(
        "\nAfter ChatGPT has generated the new keyword and summary, "
        "return here and press Enter."
    )

    print(
        "\nNow copy ChatGPT's generated machine-readable response and "
        "paste it below. Do not paste the source prompt itself."
    )


def read_multiline_input(prompt, end_marker=END_MARKER):
    """Reads pasted multiline text until END is entered on its own line."""

    print(prompt)
    print(f"Type {end_marker} on a new line when finished.\n")

    lines = []

    while True:
        try:
            line = input()
        except EOFError:
            break

        if line.strip() == end_marker:
            break

        lines.append(line)

    return "\n".join(lines).strip()


def clean_machine_line(line):
    """Removes harmless Markdown wrappers from a machine-readable label line."""

    return line.strip().replace("**", "").strip("`").strip()


def detect_source_prompt_paste(response_text, expected_source_block=""):
    """
    Detects when the source package was pasted back instead of ChatGPT's answer.

    This check runs before the machine-readable response parser because the
    source package itself contains an example of the required response block.
    """

    pasted = response_text.replace("\r\n", "\n").strip()
    expected = expected_source_block.replace("\r\n", "\n").strip()

    if expected and pasted == expected:
        return (
            "The pasted text is exactly the source block that was copied to "
            "ChatGPT, not ChatGPT's generated response."
        )

    strong_source_markers = [
        SOURCE_START_MARKER,
        SOURCE_END_MARKER,
        "SEC LITIGATION RELEASE SOURCE MATERIAL",
        "IMPORTANT INSTRUCTIONS FOR THIS CASE",
        "CURRENT APPROVED KEYWORD CATALOGUE",
        "=== LITIGATION RELEASE CONTENT ===",
        "END OF SOURCE MATERIAL",
    ]

    matched_markers = [
        marker
        for marker in strong_source_markers
        if marker.casefold() in pasted.casefold()
    ]

    if SOURCE_START_MARKER.casefold() in pasted.casefold():
        return (
            "The pasted text contains the START OF TEXT TO COPY INTO CHATGPT "
            "marker. It is the source prompt, not the generated answer."
        )

    if len(matched_markers) >= 2:
        return (
            "The pasted text contains several source-prompt markers "
            f"({', '.join(matched_markers[:3])}). It was rejected before saving."
        )

    placeholder_markers = [
        "[one keyword only; use the existing catalogue whenever suitable]",
        "[complete summary in the required Excel-ready format]",
    ]

    if any(
        marker.casefold() in pasted.casefold()
        for marker in placeholder_markers
    ):
        return (
            "The pasted text contains the response-template placeholders from "
            "the source prompt rather than a completed ChatGPT answer."
        )

    return ""


def parse_chatgpt_response(response_text, expected_source_block=""):
    """
    Parses the strict machine-readable response block.

    The last complete block is used so that the parser still works if ChatGPT
    accidentally quotes part of the request before providing its final answer.
    """

    source_paste_reason = detect_source_prompt_paste(
        response_text,
        expected_source_block,
    )

    if source_paste_reason:
        return None, source_paste_reason

    start_position = response_text.rfind(RESPONSE_START_MARKER)

    if start_position == -1:
        return None, "The start marker is missing."

    end_position = response_text.find(
        RESPONSE_END_MARKER,
        start_position + len(RESPONSE_START_MARKER),
    )

    if end_position == -1:
        return None, "The end marker is missing."

    block = response_text[
        start_position + len(RESPONSE_START_MARKER) : end_position
    ]
    lines = block.splitlines()

    metadata = {}
    keyword_heading = None
    keyword_inline = ""
    summary_heading = None
    summary_inline = ""

    for index, raw_line in enumerate(lines):
        line = clean_machine_line(raw_line)

        metadata_match = re.match(
            r"^(CASE-ID|RELEASE-NO|RESPONDENTS)\s*:\s*(.*)$",
            line,
            re.IGNORECASE,
        )

        if metadata_match:
            metadata[metadata_match.group(1).upper()] = (
                metadata_match.group(2).strip()
            )
            continue

        keyword_match = re.match(
            r"^KEYWORD\s*:\s*(.*)$",
            line,
            re.IGNORECASE,
        )

        if keyword_match and keyword_heading is None:
            keyword_heading = index
            keyword_inline = keyword_match.group(1).strip()
            continue

        summary_match = re.match(
            r"^SUMMARY\s*:\s*(.*)$",
            line,
            re.IGNORECASE,
        )

        if summary_match and summary_heading is None:
            summary_heading = index
            summary_inline = summary_match.group(1).strip()
            continue

    missing_metadata = [
        label
        for label in ("CASE-ID", "RELEASE-NO", "RESPONDENTS")
        if not metadata.get(label, "").strip()
    ]

    if missing_metadata:
        return None, (
            "Missing metadata: " + ", ".join(missing_metadata)
        )

    if keyword_heading is None or summary_heading is None:
        return None, "The KEYWORD or SUMMARY label is missing."

    if summary_heading <= keyword_heading:
        return None, "SUMMARY must appear after KEYWORD."

    keyword_lines = []

    if keyword_inline:
        keyword_lines.append(keyword_inline)

    keyword_lines.extend(
        clean_machine_line(line)
        for line in lines[keyword_heading + 1 : summary_heading]
        if clean_machine_line(line)
    )

    if len(keyword_lines) != 1:
        return None, (
            "The KEYWORD section must contain exactly one non-empty line."
        )

    keyword_value = keyword_lines[0].strip()

    if (
        keyword_value.startswith("[")
        or "one keyword only" in keyword_value.casefold()
    ):
        return None, (
            "The KEYWORD field still contains the source-template placeholder. "
            "The source prompt appears to have been pasted instead of the answer."
        )

    summary_lines = []

    if summary_inline:
        summary_lines.append(summary_inline)

    summary_lines.extend(lines[summary_heading + 1 :])
    summary = "\n".join(summary_lines).strip()

    if not summary:
        return None, "The SUMMARY section is empty."

    if (
        summary.startswith("[")
        or "complete summary in the required excel-ready format" 
        in summary.casefold()
    ):
        return None, (
            "The SUMMARY field still contains the source-template placeholder. "
            "The source prompt appears to have been pasted instead of the answer."
        )

    return {
        "case_id": metadata["CASE-ID"].strip(),
        "release_no": metadata["RELEASE-NO"].strip(),
        "respondents": metadata["RESPONDENTS"].strip(),
        "keyword": keyword_value,
        "complaint": summary,
        "raw_response": response_text,
        "manual_entry": False,
    }, ""


def normalize_text(value):
    """Normalizes spacing and punctuation for background comparisons."""

    return " ".join(
        re.sub(r"[^0-9a-z]+", " ", value.casefold()).split()
    )


def meaningful_respondent_tokens(respondents):
    """Returns distinctive words useful for checking the summary identity."""

    ignored = {
        "and", "the", "inc", "llc", "llp", "lp", "ltd", "limited",
        "company", "corporation", "corp", "plc", "group", "holdings",
        "et", "al", "defendant", "defendants", "relief", "sec",
        "securities", "exchange", "commission", "foundation",
    }

    tokens = []

    for token in normalize_text(respondents).split():
        if len(token) >= 4 and token not in ignored:
            tokens.append(token)

    return tokens


def validate_identity(record, case_id, result):
    """Checks whether the pasted response belongs to the active release."""

    checks = []
    issues = []

    if result["case_id"].casefold() == case_id.casefold():
        checks.append("Case ID matches the active copy/paste round.")
    else:
        issues.append(
            {
                "severity": "critical",
                "message": (
                    f"Case ID mismatch: expected {case_id}, received "
                    f"{result['case_id']}."
                ),
            }
        )

    if normalize_text(result["release_no"]) == normalize_text(
        record["Release No."]
    ):
        checks.append("Release number matches the active database record.")
    else:
        issues.append(
            {
                "severity": "critical",
                "message": (
                    f"Release-number mismatch: current case is "
                    f"{record['Release No.']}, but the response states "
                    f"{result['release_no']}."
                ),
            }
        )

    expected_respondents = normalize_text(record["Respondents"])
    received_respondents = normalize_text(result["respondents"])

    if expected_respondents == received_respondents:
        checks.append("Respondents match the SEC release exactly.")
    else:
        similarity = SequenceMatcher(
            None,
            expected_respondents,
            received_respondents,
        ).ratio()
        severity = "warning" if similarity >= 0.82 else "critical"
        issues.append(
            {
                "severity": severity,
                "message": (
                    "Respondent mismatch. Expected: "
                    f"{record['Respondents']} | Received: "
                    f"{result['respondents']}"
                ),
            }
        )

    summary_normalized = normalize_text(result["complaint"])
    respondent_tokens = meaningful_respondent_tokens(record["Respondents"])

    if not respondent_tokens or any(
        token in summary_normalized
        for token in respondent_tokens
    ):
        checks.append("The summary refers to at least one current respondent.")
    else:
        issues.append(
            {
                "severity": "warning",
                "message": (
                    "The summary does not appear to mention any distinctive "
                    "name from the current Respondents field."
                ),
            }
        )

    referenced_release_numbers = set()

    for match in re.finditer(
        r"\bLR[-\s]?(\d{4,6})\b|"
        r"litigation\s+release\s+no\.?\s*(\d{4,6})",
        result["complaint"],
        re.IGNORECASE,
    ):
        referenced_release_numbers.add(match.group(1) or match.group(2))

    current_number = record["Release No."].replace("LR-", "")
    other_numbers = sorted(
        number
        for number in referenced_release_numbers
        if number != current_number
    )

    if other_numbers:
        issues.append(
            {
                "severity": "warning",
                "message": (
                    "The summary refers to another Litigation Release number: "
                    + ", ".join(f"LR-{number}" for number in other_numbers)
                    + ". This may be legitimate, but should be checked."
                ),
            }
        )
    else:
        checks.append("No conflicting Litigation Release number was detected.")

    return checks, issues


def inspect_keyword(keyword, approved_keywords):
    """Matches a pasted keyword to the catalogue and suggests close variants."""

    cleaned_keyword = " ".join(keyword.strip().split())
    lookup = {
        existing.casefold(): existing
        for existing in approved_keywords
    }

    if cleaned_keyword.casefold() in lookup:
        return {
            "keyword": lookup[cleaned_keyword.casefold()],
            "is_new": False,
            "suggestions": [],
        }

    suggestions = get_close_matches(
        cleaned_keyword,
        approved_keywords,
        n=3,
        cutoff=0.72,
    )

    return {
        "keyword": cleaned_keyword,
        "is_new": True,
        "suggestions": suggestions,
    }


def resolve_keyword_interactively(keyword, approved_keywords):
    """Requires deliberate confirmation when a keyword is not in the catalogue."""

    current_keyword = keyword

    while True:
        inspection = inspect_keyword(current_keyword, approved_keywords)

        if not inspection["is_new"]:
            return {
                "action": "accepted",
                "keyword": inspection["keyword"],
                "is_new": False,
            }

        print("\nKEYWORD CATALOGUE WARNING")
        print(f"The pasted keyword is not in keywords.txt: {current_keyword}")

        suggestions = inspection["suggestions"]

        if suggestions:
            print("Possible existing matches:")
            for number, suggestion in enumerate(suggestions, start=1):
                print(f"{number}. {suggestion}")

        options = []

        if suggestions:
            options.append("a suggestion number")

        options.extend(["new", "edit", "retry", "skip"])
        choice = input(
            "Choose " + ", ".join(options) + ": "
        ).strip().casefold()

        if choice.isdigit() and suggestions:
            position = int(choice) - 1

            if 0 <= position < len(suggestions):
                return {
                    "action": "accepted",
                    "keyword": suggestions[position],
                    "is_new": False,
                }

        if choice == "new":
            return {
                "action": "accepted",
                "keyword": inspection["keyword"],
                "is_new": True,
            }

        if choice == "edit":
            edited_keyword = input("Enter the corrected keyword: ").strip()

            if edited_keyword:
                current_keyword = edited_keyword
            else:
                print("The keyword cannot be blank.")

        elif choice == "retry":
            return {"action": "retry"}

        elif choice == "skip":
            return {"action": "skip"}

        else:
            print("The entered option was not understood.")


def validate_summary_structure(summary):
    """Checks the Excel-ready summary headings without rewriting the text."""

    checks = []
    issues = []
    cleaned_lines = [
        line.replace("**", "").strip()
        for line in summary.splitlines()
        if line.strip()
    ]
    normalized_lines = [line.casefold() for line in cleaned_lines]
    normalized_summary = " ".join(summary.replace("**", "").casefold().split())

    for label in ("case:", "court:", "filed:"):
        if any(line.startswith(label) for line in normalized_lines[:12]):
            checks.append(f"The summary contains {label[:-1].title()} metadata.")
        elif label in normalized_summary:
            issues.append(
                {
                    "severity": "critical",
                    "message": (
                        f"The {label[:-1].title()} label exists, but it is inline "
                        "instead of being on its own line near the beginning."
                    ),
                }
            )
        else:
            issues.append(
                {
                    "severity": "critical",
                    "message": (
                        f"The summary is missing the required {label[:-1].title()} "
                        "line near the beginning."
                    ),
                }
            )

    if "allegations" in normalized_lines:
        checks.append("The Allegations heading is present on its own line.")
    elif "allegations:" in normalized_summary or "allegations" in normalized_summary:
        issues.append(
            {
                "severity": "warning",
                "message": (
                    "The Allegations label exists, but it is inline instead of "
                    "being a standalone heading."
                ),
            }
        )
    else:
        issues.append(
            {
                "severity": "warning",
                "message": "The summary does not contain an Allegations heading.",
            }
        )

    relief_headings = [
        line
        for line in normalized_lines
        if line.startswith("relief sought")
    ]

    if relief_headings:
        checks.append("A Relief Sought / outcome heading is present on its own line.")
    elif "relief sought" in normalized_summary:
        issues.append(
            {
                "severity": "critical",
                "message": (
                    "A Relief Sought label exists, but it is inline instead of "
                    "being a standalone heading."
                ),
            }
        )
    else:
        issues.append(
            {
                "severity": "critical",
                "message": (
                    "The summary is missing a Relief Sought, Relief Sought / "
                    "Final Judgment, or Relief Sought / Outcome heading."
                ),
            }
        )

    if len(summary.strip()) >= 120:
        checks.append("The summary contains a plausible amount of text.")
    else:
        issues.append(
            {
                "severity": "warning",
                "message": "The summary is unusually short.",
            }
        )

    return checks, issues


def normalize_summary_for_comparison(summary):
    """Creates a stable comparison form for duplicate-response detection."""

    return " ".join(
        summary.replace("**", "").casefold().split()
    )


def find_duplicate_summaries(release_no, summary):
    """Finds exact or very close summaries already stored for another case."""

    target = normalize_summary_for_comparison(summary)

    if not target:
        return []

    with connect_database() as connection:
        rows = connection.execute(
            """
            SELECT release_no, complaint
            FROM releases
            WHERE release_no <> ?
              AND TRIM(COALESCE(complaint, '')) <> ''
            """,
            (release_no,),
        ).fetchall()

    matches = []

    for row in rows:
        other = normalize_summary_for_comparison(row["complaint"] or "")

        if not other:
            continue

        if target == other:
            matches.append(
                {
                    "release_no": row["release_no"],
                    "kind": "exact",
                    "score": 1.0,
                }
            )
            continue

        if min(len(target), len(other)) < 200:
            continue

        length_difference = abs(len(target) - len(other)) / max(
            len(target),
            len(other),
        )

        if length_difference > 0.08:
            continue

        matcher = SequenceMatcher(None, target[:10000], other[:10000])

        if matcher.quick_ratio() < 0.95:
            continue

        score = matcher.ratio()

        if score >= 0.96:
            matches.append(
                {
                    "release_no": row["release_no"],
                    "kind": "near",
                    "score": score,
                }
            )

        if len(matches) >= 3:
            break

    return matches


def collect_chatgpt_result(record, case_id, source_block):
    """Collects and strictly parses one ChatGPT response."""

    while True:
        response_text = read_multiline_input(
            "Paste the complete ChatGPT response below."
        )

        parsed, error_message = parse_chatgpt_response(
            response_text,
            expected_source_block=source_block,
        )

        if parsed is not None:
            return parsed

        print("\nThe response could not be parsed safely.")
        print(f"Reason: {error_message}")

        if detect_source_prompt_paste(response_text, source_block):
            print(
                "\nSOURCE PROMPT DETECTED — NOTHING WAS SAVED.\n"
                "Copy ChatGPT's generated response, return here, and paste that "
                "response instead."
            )
        print(
            "The expected response must include the exact machine-readable "
            "start/end markers and CASE-ID, RELEASE-NO, RESPONDENTS, KEYWORD, "
            "and SUMMARY labels."
        )

        action = input(
            "Type retry to paste the full response again, recopy to copy the "
            "active case prompt again, manual to enter the two output fields "
            "deliberately, or skip: "
        ).strip().casefold()

        if action == "recopy":
            present_source_block_again(source_block)
            continue

        if action == "manual":
            keyword = input("Paste the keyword: ").strip()
            complaint = read_multiline_input(
                "Paste the complete summary below."
            )

            if keyword and complaint:
                return {
                    "case_id": case_id,
                    "release_no": record["Release No."],
                    "respondents": record["Respondents"],
                    "keyword": keyword,
                    "complaint": complaint,
                    "raw_response": response_text,
                    "manual_entry": True,
                }

            print("Keyword and summary must both contain text.")

        elif action == "skip":
            return None


def edit_result_fields(result):
    """Allows deliberate correction before saving."""

    choice = input(
        "Type keyword, summary, both, or cancel: "
    ).strip().casefold()

    if choice in {"keyword", "both"}:
        keyword = input("Enter the corrected keyword: ").strip()

        if keyword:
            result["keyword"] = keyword
        else:
            print("The keyword was not changed because it was blank.")

    if choice in {"summary", "both"}:
        summary = read_multiline_input(
            "Paste the corrected complete summary below."
        )

        if summary:
            result["complaint"] = summary
        else:
            print("The summary was not changed because it was blank.")

    return result


def print_validation_preview(
    record,
    case_id,
    result,
    checks,
    issues,
    duplicate_matches,
    keyword_is_new,
):
    """Displays a compact identity and quality check before saving."""

    print("\n" + "=" * 100)
    print("READY TO SAVE — BACKGROUND VALIDATION")
    print("=" * 100)
    print(f"Active Case ID: {case_id}")
    print(f"Release: {record['Release No.']}")
    print(f"Respondents: {record['Respondents']}")
    print(f"Keyword: {result['keyword']}")

    preview = " ".join(result["complaint"].split())
    print("Summary preview:")
    print(preview[:350] + ("..." if len(preview) > 350 else ""))

    print("\nValidation checks:")

    for check in checks:
        print(f"[OK] {check}")

    if keyword_is_new:
        print("[WARNING] The keyword was deliberately accepted as new.")
    else:
        print("[OK] The keyword matches the approved catalogue.")

    if duplicate_matches:
        for match in duplicate_matches:
            if match["kind"] == "exact":
                print(
                    "[CRITICAL] The summary is identical to the summary stored "
                    f"for {match['release_no']}."
                )
            else:
                print(
                    "[WARNING] The summary is very similar to the summary "
                    f"stored for {match['release_no']} "
                    f"({match['score']:.1%} similarity)."
                )
    else:
        print("[OK] No duplicate response was detected in another release.")

    if result.get("manual_entry"):
        print(
            "[WARNING] The values were entered manually, so the pasted "
            "machine-readable identity block could not be verified."
        )

    for issue in issues:
        label = "CRITICAL" if issue["severity"] == "critical" else "WARNING"
        print(f"[{label}] {issue['message']}")

    print("=" * 100)


def manual_review_release(record, approved_keywords):
    """Runs the protected one-by-one copy/paste workflow."""

    release_no = record["Release No."]
    existing = load_existing_fields(release_no)

    print("\n" + "#" * 100)
    print(f"MANUAL CHATGPT REVIEW: {release_no}")
    print("#" * 100)
    print(f"Respondents: {record['Respondents']}")
    print(
        "Existing Keyword: "
        + (existing["keyword"] if existing["keyword"].strip() else "blank")
    )
    print(
        "Existing Complaint summary: "
        + ("present" if existing["complaint"].strip() else "blank")
    )

    unreliable_documents = [
        document
        for document in record["Documents"]
        if document["manual_review_required"]
    ]

    if unreliable_documents:
        print("\n" + "!" * 100)
        print("CHATGPT REVIEW BLOCKED — RESOURCE PDF TEXT IS STILL INCOMPLETE")
        print("!" * 100)
        for document in unreliable_documents:
            print(f"- {document['name']}: {document['review_reason'] or 'Manual review required.'}")
        print(
            "The manual PDF recovery step must be completed before this "
            "release can be sent to ChatGPT."
        )
        print("!" * 100)
        return "skipped"

    action = input(
        "Press Enter to prepare this release, type skip to leave it unchanged, "
        "or quit to stop the entire run: "
    ).strip().casefold()

    if action == "quit":
        return "quit"

    if action == "skip":
        return "skipped"

    case_id = generate_case_id(release_no)
    source_block = build_source_block(record, case_id, approved_keywords)
    copied, clipboard_details = copy_to_clipboard(source_block)

    print("\n" + "=" * 100)
    print("COPY BLOCK FOR CHATGPT")
    print("=" * 100)
    print(source_block)
    print("=" * 100)

    if copied:
        print(
            "\nClipboard copy successful using "
            f"{clipboard_details}."
        )
        print(
            "Open the separate ChatGPT chat and press Ctrl+V. "
            "The copied block now contains the exact response format and "
            "case-verification data."
        )
    else:
        print("\nAutomatic clipboard copying failed.")
        if clipboard_details:
            print(f"Technical details: {clipboard_details}")
        print(
            "Manually copy everything beginning with:\n"
            ">>> START OF TEXT TO COPY INTO CHATGPT >>>\n"
            "and ending with:\n"
            "<<< END OF TEXT TO COPY INTO CHATGPT <<<"
        )

    input(
        "\nAfter ChatGPT has generated the keyword and summary, return here "
        "and press Enter."
    )

    print(
        "\nIMPORTANT: The clipboard still contains the SOURCE BLOCK unless "
        "you copied ChatGPT's generated response. Copy the complete ChatGPT "
        "response before pasting below. If the source block is pasted again, "
        "the program will reject it and save nothing."
    )

    while True:
        result = collect_chatgpt_result(record, case_id, source_block)

        if result is None:
            print(f"No manual values were saved for {release_no}.")
            return "skipped"

        keyword_resolution = resolve_keyword_interactively(
            result["keyword"],
            approved_keywords,
        )

        if keyword_resolution["action"] == "retry":
            print("Paste the complete ChatGPT response again.")
            continue

        if keyword_resolution["action"] == "skip":
            print(f"No manual values were saved for {release_no}.")
            return "skipped"

        result["keyword"] = keyword_resolution["keyword"]
        keyword_is_new = keyword_resolution["is_new"]

        while True:
            identity_checks, identity_issues = validate_identity(
                record,
                case_id,
                result,
            )
            structure_checks, structure_issues = validate_summary_structure(
                result["complaint"]
            )
            checks = identity_checks + structure_checks
            issues = identity_issues + structure_issues
            duplicate_matches = find_duplicate_summaries(
                release_no,
                result["complaint"],
            )

            if result.get("manual_entry"):
                issues.append(
                    {
                        "severity": "warning",
                        "message": (
                            "The machine-readable response block was bypassed "
                            "through deliberate manual entry."
                        ),
                    }
                )

            for duplicate in duplicate_matches:
                if duplicate["kind"] == "exact":
                    issues.append(
                        {
                            "severity": "critical",
                            "message": (
                                "An identical summary is already stored for "
                                f"{duplicate['release_no']}."
                            ),
                        }
                    )
                else:
                    issues.append(
                        {
                            "severity": "warning",
                            "message": (
                                "A very similar summary is stored for "
                                f"{duplicate['release_no']}."
                            ),
                        }
                    )

            print_validation_preview(
                record,
                case_id,
                result,
                checks,
                issues,
                duplicate_matches,
                keyword_is_new,
            )

            has_critical = any(
                issue["severity"] == "critical"
                for issue in issues
            )
            has_warning = any(
                issue["severity"] == "warning"
                for issue in issues
            ) or keyword_is_new

            existing_present = (
                bool(existing["keyword"].strip())
                or bool(existing["complaint"].strip())
            )

            if has_critical:
                print(
                    "Critical checks failed. Nothing will be saved unless you "
                    "type override deliberately."
                )
                choice = input(
                    "Type retry, recopy, edit, override, override-replace, "
                    "or skip: "
                ).strip().casefold()
            elif has_warning:
                print(
                    "Warnings were found. Review them before choosing to save."
                )
                options = "save, edit, retry, recopy, or skip"
                if existing_present:
                    options = "save, replace, edit, retry, recopy, or skip"
                choice = input(f"Type {options}: ").strip().casefold()
            else:
                if existing_present:
                    choice = input(
                        "Press Enter to fill blank fields only, type replace to "
                        "overwrite existing values, edit, retry, recopy, "
                        "or skip: "
                    ).strip().casefold()
                else:
                    choice = input(
                        "Press Enter to save, or type edit, retry, recopy, "
                        "or skip: "
                    ).strip().casefold()

            if choice == "retry":
                break

            if choice == "recopy":
                present_source_block_again(source_block)
                break

            if choice == "edit":
                result = edit_result_fields(result)
                keyword_resolution = resolve_keyword_interactively(
                    result["keyword"],
                    approved_keywords,
                )

                if keyword_resolution["action"] == "retry":
                    break

                if keyword_resolution["action"] == "skip":
                    print("Nothing was saved.")
                    return "skipped"

                result["keyword"] = keyword_resolution["keyword"]
                keyword_is_new = keyword_resolution["is_new"]
                continue

            if choice == "skip":
                print("Nothing was saved.")
                return "skipped"

            overwrite = False
            save_approved = False

            if has_critical:
                if choice == "override":
                    save_approved = True
                elif choice == "override-replace":
                    save_approved = True
                    overwrite = True
                else:
                    print("Critical validation was not overridden. Nothing saved.")
                    continue
            elif has_warning:
                if choice == "save":
                    save_approved = True
                elif choice == "replace" and existing_present:
                    save_approved = True
                    overwrite = True
                else:
                    print("The warning was not accepted. Nothing saved.")
                    continue
            else:
                if choice == "":
                    save_approved = True
                elif choice == "replace" and existing_present:
                    save_approved = True
                    overwrite = True
                else:
                    print("The entered option was not understood.")
                    continue

            if not save_approved:
                continue

            saved = save_manual_fields(
                release_no,
                result["keyword"],
                result["complaint"],
                overwrite=overwrite,
            )

            if (
                keyword_is_new
                and saved["keyword_saved"]
                and saved["keyword"].casefold() == result["keyword"].casefold()
            ):
                try:
                    if append_new_keyword(
                        result["keyword"],
                        approved_keywords,
                    ):
                        print("The accepted new keyword was added to keywords.txt.")
                except OSError as error:
                    print(
                        "The keyword was saved to SQLite, but could not be "
                        f"added to keywords.txt: {error}"
                    )

            if saved["keyword_saved"]:
                print(f"Keyword saved: {saved['keyword']}")
            else:
                print(f"Existing Keyword preserved: {saved['keyword']}")

            if saved["complaint_saved"]:
                print("Complaint summary saved to SQLite.")
            else:
                print("Existing Complaint summary preserved.")

            return "saved"

        # A retry from the inner validation loop returns here and requests a
        # complete fresh ChatGPT response for the same protected Case ID.
        continue

def print_release_status(record, was_inserted, document_counts):
    """Prints a concise extraction and database status."""

    inserted_documents, updated_documents = document_counts

    print(f"Date: {record['Date']}")
    print(f"Respondents: {record['Respondents']}")
    print(
        "Release database status: "
        + ("newly saved" if was_inserted else "existing row updated")
    )
    print(f"Resource documents: {len(record['Documents'])}")
    print(f"Documents newly saved: {inserted_documents}")
    print(f"Documents updated: {updated_documents}")

    if record["Manual Review Documents"]:
        print("Documents with unreliable or incomplete extracted text:")
        for document in record["Documents"]:
            if document["manual_review_required"]:
                print(f"- {document['name']}")
                print(
                    "  Reason: "
                    + (document["review_reason"] or "Manual review required.")
                )

    print("-" * 80)


def main():
    """Processes a chosen interval and supports manual ChatGPT completion."""

    if DATABASE_FILE.exists():
        try:
            backup_path = create_database_backup()
            print(f"Database backup created: {backup_path}\n")
        except Exception as error:
            print("DATABASE BACKUP FAILED")
            print(f"Reason: {error}")
            continuation = input(
                "Type continue to run without a fresh backup, or press Enter "
                "to stop: "
            ).strip().casefold()

            if continuation != "continue":
                print("Program stopped before any release was processed.")
                return

    create_database()
    print(f"Manual review tool: {MANUAL_TOOL_VERSION}\n")

    try:
        start_number = int(input("Enter the first release number: "))
        end_number = int(input("Enter the last release number: "))
    except ValueError:
        print("Please enter release numbers only.")
        return
    except KeyboardInterrupt:
        print("\nProgram stopped before the import started.")
        return

    if start_number < end_number:
        print("The first number must be larger than the last number.")
        return

    release_numbers = list(range(start_number, end_number - 1, -1))

    manual_answer = input(
        "Review the releases one by one for manual ChatGPT completion? "
        "Enter yes or no: "
    ).strip().casefold()

    if manual_answer not in {"yes", "no"}:
        print("Please enter exactly yes or no.")
        return

    manual_enabled = manual_answer == "yes"

    approved_keywords = []

    if manual_enabled:
        try:
            approved_keywords = load_keywords()
        except (FileNotFoundError, ValueError) as error:
            print(error)
            return

        print(
            f"Loaded {len(approved_keywords)} approved keywords for "
            "background validation.\n"
        )

    stats = {
        "attempted": len(release_numbers),
        "extracted": 0,
        "failed": 0,
        "placeholder_rows": 0,
        "new_releases": 0,
        "updated_releases": 0,
        "documents_inserted": 0,
        "documents_updated": 0,
        "manual_pdf_recovered": 0,
        "manual_pdf_unresolved": 0,
        "manual_saved": 0,
        "manual_skipped": 0,
    }

    for release_number in release_numbers:
        record = extract_release(release_number)

        if record is None:
            stats["failed"] += 1

            placeholder_inserted = save_failed_release_placeholder(
                release_number
            )

            if placeholder_inserted:
                stats["placeholder_rows"] += 1
                print(
                    f"Blank placeholder row saved for LR-{release_number}. "
                    "It will still appear in the Excel report."
                )
            else:
                print(
                    f"LR-{release_number} already has a database row. "
                    "Existing data was preserved."
                )

            print("-" * 80)
            continue

        stats["extracted"] += 1
        was_inserted = save_release(record)

        if was_inserted:
            stats["new_releases"] += 1
        else:
            stats["updated_releases"] += 1

        document_counts = save_documents(
            record["Release No."],
            record["Documents"],
        )
        stats["documents_inserted"] += document_counts[0]
        stats["documents_updated"] += document_counts[1]

        recovery_status = {
            "status": "complete",
            "recovered": 0,
            "unresolved": len(record["Manual Review Documents"]),
        }

        if manual_enabled and record["Manual Review Documents"]:
            recovery_status = recover_unreliable_resource_documents(record)
            stats["manual_pdf_recovered"] += recovery_status["recovered"]
            stats["manual_pdf_unresolved"] += recovery_status["unresolved"]

        print_release_status(record, was_inserted, document_counts)

        if recovery_status["status"] == "quit":
            print("Run stopped. All data completed so far remains saved.")
            break

        if manual_enabled and recovery_status["status"] == "incomplete":
            stats["manual_skipped"] += 1
            print(
                f"ChatGPT review skipped for {record['Release No.']} because "
                "one or more Resource PDFs are still unresolved."
            )
            print("-" * 80)
            continue

        if manual_enabled:
            manual_status = manual_review_release(
                record,
                approved_keywords,
            )

            if manual_status == "saved":
                stats["manual_saved"] += 1
            elif manual_status == "skipped":
                stats["manual_skipped"] += 1
            elif manual_status == "quit":
                print("Run stopped. All data completed so far remains saved.")
                break

    print("\nImport completed.")
    print(f"Releases attempted: {stats['attempted']}")
    print(f"Successfully extracted: {stats['extracted']}")
    print(f"Failed releases: {stats['failed']}")
    print(
        "Blank placeholder rows newly saved: "
        f"{stats['placeholder_rows']}"
    )
    print(f"New releases saved: {stats['new_releases']}")
    print(f"Existing releases updated: {stats['updated_releases']}")
    print(f"Documents newly saved: {stats['documents_inserted']}")
    print(f"Documents updated: {stats['documents_updated']}")

    if manual_enabled:
        print(
            "Unreadable PDFs manually recovered: "
            f"{stats['manual_pdf_recovered']}"
        )
        print(
            "Unreadable PDFs left unresolved: "
            f"{stats['manual_pdf_unresolved']}"
        )

    if manual_enabled:
        print(f"Manual results saved: {stats['manual_saved']}")
        print(f"Manual reviews skipped: {stats['manual_skipped']}")

    print(f"Total releases in database: {count_database_records()}")

    completion_report = build_interval_completion_report(
        start_number,
        end_number,
        release_numbers,
    )
    print_interval_completion_report(completion_report)

    if not confirm_excel_export(completion_report):
        print("Excel export cancelled. All extracted and manually saved data remains in SQLite.")
        return

    try:
        export_path, exported_releases, exported_documents = (
            export_interval_to_excel(
                DATABASE_FILE,
                PROJECT_FOLDER / "exports",
                start_number,
                end_number,
            )
        )

        print("\nExcel interval report created.")
        print(f"Releases exported: {exported_releases}")
        print(f"Resource documents exported: {exported_documents}")
        print(f"File: {export_path}")

    except Exception as error:
        print(f"\nExcel export failed: {error}")


if __name__ == "__main__":
    main()