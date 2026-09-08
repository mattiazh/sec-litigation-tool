"""Full, consistent SQLite archives for saving and resuming a review run."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid


def database_snapshot(source, directory, high, low):
    """Copy every table, including full document text; never use a slim history."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"archive_{uuid.uuid4().hex}.db"
    stamp = datetime.now(timezone.utc)
    try:
        with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
            dst.execute("CREATE TABLE IF NOT EXISTS run_metadata (key TEXT PRIMARY KEY, value TEXT)")
            dst.executemany("INSERT OR REPLACE INTO run_metadata VALUES (?, ?)", [
                ('range_start', str(high)), ('range_end', str(low)),
                ('archived_utc', stamp.isoformat()), ('archive_format', 'full-text-v1'),
            ])
            dst.commit()
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('The database archive did not pass its integrity check.')
        name = f"SEC_archive_LR-{high}_to_LR-{low}_{stamp:%Y%m%d_%H%M%S_%f}.db"
        return destination.read_bytes(), name
    finally:
        destination.unlink(missing_ok=True)


def validate_archive(path):
    """Validate before replacing any active session data."""
    required = {
        'releases': {'date', 'respondents', 'release_no', 'keyword', 'content',
                     'complaint', 'link_to_release', 'link_to_complaint'},
        'documents': {'id', 'release_no', 'document_name', 'document_url',
                      'extracted_text', 'page_count', 'character_count',
                      'characters_per_page', 'extraction_successful',
                      'manual_review_required', 'review_reason'},
    }
    with sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed.')
        for table, columns in required.items():
            present = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
            if not columns <= present:
                raise ValueError(f'The database is missing required {table} fields.')
        numbers = [int(row[0][3:]) for row in connection.execute('SELECT release_no FROM releases')
                   if isinstance(row[0], str) and row[0].startswith('LR-') and row[0][3:].isdigit()]
        if not numbers:
            raise ValueError('The database does not contain any releases.')
        has_metadata = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_metadata'").fetchone()
        metadata = dict(connection.execute('SELECT key, value FROM run_metadata')) if has_metadata else {}
        high = int(metadata.get('range_start', max(numbers)))
        low = int(metadata.get('range_end', min(numbers)))
        if not 0 < low <= high:
            raise ValueError('Invalid release interval in archive.')
        return high, low
