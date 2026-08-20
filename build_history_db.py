"""
Builds the small `sec_history.db` that gets published on GitHub.

The working database `sec_releases.db` is ~54 MB, almost all of it extracted
PDF text that the web app does not need. This script copies only the
`releases` table into a new file of a few megabytes, which is what the app and
the daily check download.

Usage (from the folder containing sec_releases.db):

    python build_history_db.py

    python build_history_db.py --no-summaries   # publish without the summaries

Then upload the resulting sec_history.db to the GitHub release "data-latest"
(see RUNBOOK.md, "Publishing an updated database").
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import sec_history  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("sec_releases.db"))
    parser.add_argument("--output", type=Path, default=Path("sec_history.db"))
    parser.add_argument(
        "--no-summaries",
        action="store_true",
        help="Publish release numbers, dates and keywords but not the summaries.",
    )
    parser.add_argument(
        "--keep-content",
        action="store_true",
        help="Also copy the SEC webpage text (makes the file several MB larger).",
    )
    arguments = parser.parse_args()

    try:
        info = sec_history.build_history_database(
            arguments.source,
            arguments.output,
            include_summaries=not arguments.no_summaries,
            keep_content=arguments.keep_content,
        )
    except FileNotFoundError as error:
        raise SystemExit(str(error)) from error

    print(f"Wrote {arguments.output}")
    print(f"  releases:        {info['releases']}")
    print(f"  fully reviewed:  {info['reviewed']}")
    print(f"  newest release:  LR-{info['highest_release']}")
    print(f"  size:            {info['size_mb']} MB")
    print(f"  built:           {info['built_utc']}")
    print()
    print("Next: upload this file to the GitHub release tagged 'data-latest'.")
    print("See RUNBOOK.md -> Publishing an updated database.")


if __name__ == "__main__":
    main()
