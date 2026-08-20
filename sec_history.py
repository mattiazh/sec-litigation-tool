"""
Shared access to the published historical database.

The historical database (`sec_history.db`) is published as a GitHub Release
asset. It is a slim copy of the local `sec_releases.db`: the `releases` table
only, without the large extracted PDF text. Everything that reads history --
the Streamlit app and the scheduled new-release checker -- reads it from here,
so there is exactly one source of truth.

Nothing in this module needs a password, an API key or a GitHub token. The
release asset is a plain public download URL.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# WHERE THE PUBLISHED DATABASE LIVES
#
# If the repository is ever renamed or moved to another GitHub account, this
# is the ONLY line that has to change (or set the SEC_HISTORY_URL environment
# variable / Streamlit secret instead).
# --------------------------------------------------------------------------

DEFAULT_HISTORY_URL = (
    "https://github.com/mattiazh/sec-litigation-tool/"
    "releases/download/data-latest/sec_history.db"
)

HISTORY_FILE_NAME = "sec_history.db"
DOWNLOAD_TIMEOUT_SECONDS = 60
CACHE_MAX_AGE_SECONDS = 3600


class HistoryUnavailable(RuntimeError):
    """Raised when the published history database cannot be downloaded."""


def history_url() -> str:
    """Returns the download URL, allowing an environment override."""

    return os.environ.get("SEC_HISTORY_URL", DEFAULT_HISTORY_URL).strip()


def cache_path() -> Path:
    return Path(tempfile.gettempdir()) / HISTORY_FILE_NAME


def download_history(force: bool = False) -> Path:
    """
    Downloads the published history database and returns the local path.

    The file is cached in the temporary folder for an hour so that a busy
    session does not re-download it on every interaction.
    """

    target = cache_path()

    if not force and target.exists():
        age = time.time() - target.stat().st_mtime
        if age < CACHE_MAX_AGE_SECONDS and target.stat().st_size > 0:
            return target

    url = history_url()

    try:
        response = requests.get(
            url,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            headers={"User-Agent": "SEC Litigation Tool history fetch"},
        )
    except requests.RequestException as error:
        raise HistoryUnavailable(
            f"The historical database could not be downloaded: {error}"
        ) from error

    if response.status_code == 404:
        raise HistoryUnavailable(
            "No published historical database was found at "
            f"{url}. Upload sec_history.db to the 'data-latest' release."
        )

    if response.status_code != 200:
        raise HistoryUnavailable(
            f"The historical database download returned HTTP {response.status_code}."
        )

    temporary = target.with_suffix(".part")
    temporary.write_bytes(response.content)

    # Fail early and clearly if the download is not a usable SQLite file.
    try:
        with sqlite3.connect(temporary) as connection:
            connection.execute("SELECT COUNT(*) FROM releases").fetchone()
    except sqlite3.Error as error:
        temporary.unlink(missing_ok=True)
        raise HistoryUnavailable(
            f"The downloaded file is not a usable history database: {error}"
        ) from error

    temporary.replace(target)
    return target


def release_number(release_no: str) -> int | None:
    """Turns 'LR-26595' into 26595. Returns None for anything unexpected."""

    if not release_no:
        return None

    digits = "".join(character for character in str(release_no) if character.isdigit())
    return int(digits) if digits else None


def read_index(database_path: Path) -> dict:
    """
    Reads the history database and returns what is stored and what is reviewed.

    'stored'   -- the release is present in the database.
    'reviewed' -- the release has both a keyword and a summary, i.e. the
                  ChatGPT review step was completed and saved.
    """

    stored: set[int] = set()
    reviewed: set[int] = set()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT release_no,
                   TRIM(COALESCE(keyword, ''))   AS keyword,
                   TRIM(COALESCE(complaint, '')) AS complaint
            FROM releases
            """
        ).fetchall()

    for row in rows:
        number = release_number(row["release_no"])

        if number is None:
            continue

        stored.add(number)

        if row["keyword"] and row["complaint"]:
            reviewed.add(number)

    return {
        "stored": stored,
        "reviewed": reviewed,
        "highest_stored": max(stored) if stored else None,
        "highest_reviewed": max(reviewed) if reviewed else None,
    }


def load_index(force: bool = False) -> dict:
    """Downloads (or reuses) the history database and returns its index."""

    path = download_history(force=force)
    index = read_index(path)
    index["path"] = path
    return index


# --------------------------------------------------------------------------
# BUILDING A NEW HISTORY DATABASE
#
# Used both by scripts/build_history_db.py (from the local working database)
# and by the web app (from the finished session database), so the published
# file is produced by exactly one piece of code.
# --------------------------------------------------------------------------

RELEASE_COLUMNS = [
    "date",
    "respondents",
    "release_no",
    "keyword",
    "content",
    "complaint",
    "link_to_release",
    "link_to_complaint",
]

_RELEASES_SCHEMA = """
CREATE TABLE releases (
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

_DOCUMENTS_SCHEMA = """
CREATE TABLE documents (
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


def build_history_database(
    source: Path,
    output: Path,
    include_summaries: bool = True,
    keep_content: bool = False,
) -> dict:
    """
    Copies the `releases` table of `source` into a small new database.

    The extracted PDF text is deliberately left out: it is what makes the
    working database large, and the app re-downloads the PDFs for the interval
    it is currently processing anyway.
    """

    import datetime

    source = Path(source)
    output = Path(output)

    if not source.exists():
        raise FileNotFoundError(f"Source database not found: {source}")

    output.unlink(missing_ok=True)

    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    source_connection.row_factory = sqlite3.Row
    target_connection = sqlite3.connect(output)

    target_connection.execute(_RELEASES_SCHEMA)
    target_connection.execute(_DOCUMENTS_SCHEMA)
    target_connection.execute(
        "CREATE TABLE history_info (key TEXT PRIMARY KEY, value TEXT)"
    )

    rows = source_connection.execute(
        f"SELECT {', '.join(RELEASE_COLUMNS)} FROM releases"
    ).fetchall()

    reviewed = 0
    payload = []

    for row in rows:
        record = {column: (row[column] or "") for column in RELEASE_COLUMNS}

        if record["keyword"].strip() and record["complaint"].strip():
            reviewed += 1

        if not keep_content:
            record["content"] = ""

        if not include_summaries:
            record["complaint"] = ""

        payload.append(tuple(record[column] for column in RELEASE_COLUMNS))

    target_connection.executemany(
        f"INSERT OR REPLACE INTO releases ({', '.join(RELEASE_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(RELEASE_COLUMNS))})",
        payload,
    )

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    numbers = [
        release_number(row["release_no"])
        for row in rows
        if release_number(row["release_no"]) is not None
    ]

    target_connection.executemany(
        "INSERT OR REPLACE INTO history_info (key, value) VALUES (?, ?)",
        [
            ("built_utc", stamp),
            ("release_count", str(len(payload))),
            ("reviewed_count", str(reviewed)),
            ("highest_release", str(max(numbers)) if numbers else ""),
            ("includes_summaries", "yes" if include_summaries else "no"),
        ],
    )

    target_connection.commit()
    target_connection.execute("VACUUM")
    target_connection.close()
    source_connection.close()

    return {
        "releases": len(payload),
        "reviewed": reviewed,
        "highest_release": max(numbers) if numbers else None,
        "size_mb": round(output.stat().st_size / (1024 * 1024), 2),
        "built_utc": stamp,
    }


def read_info(database_path: Path) -> dict:
    """Reads the small metadata table, if the file has one."""

    try:
        with sqlite3.connect(database_path) as connection:
            rows = connection.execute("SELECT key, value FROM history_info").fetchall()
    except sqlite3.Error:
        return {}

    return {key: value for key, value in rows}
