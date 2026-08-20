"""
Checks SEC.gov for litigation releases that are not yet in the database.

Run by GitHub Actions on a schedule (see .github/workflows/check-new-releases.yml)
and can also be run by hand:

    python scripts/check_new_releases.py                    # uses the published history
    python scripts/check_new_releases.py --database my.db   # uses a local database

It writes a short Markdown report to standard output and, when running inside
GitHub Actions, to `report.md` plus the job summary. It never changes any data.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import sec_feed  # noqa: E402
import sec_history  # noqa: E402

# How far below the newest stored release we still look for gaps. Anything
# older than this is treated as deliberately out of scope, not as missing.
LOOKBACK = 50


def build_report(database: Path | None) -> dict:
    result: dict = {
        "ok": True,
        "problems": [],
        "missing": [],
        "unreviewed": [],
        "sec_highest": None,
        "database_highest": None,
        "sources_ok": [],
        "sources_failed": [],
    }

    # ---- what we already have -------------------------------------------
    try:
        if database:
            index = sec_history.read_index(database)
            index["path"] = database
        else:
            index = sec_history.load_index(force=True)
    except sec_history.HistoryUnavailable as error:
        result["ok"] = False
        result["problems"].append(str(error))
        return result

    result["database_highest"] = index["highest_stored"]

    # ---- what SEC has published -----------------------------------------
    try:
        listing = sec_feed.fetch_published_releases()
    except Exception as error:  # noqa: BLE001
        result["ok"] = False
        result["problems"].append(f"SEC.gov could not be read: {error}")
        return result

    result["sources_ok"] = listing.sources_ok
    result["sources_failed"] = listing.sources_failed
    result["sec_highest"] = listing.highest

    stored = index["stored"]
    reviewed = index["reviewed"]
    highest_stored = index["highest_stored"] or 0

    floor = max(min(listing.numbers), highest_stored - LOOKBACK)

    missing = sorted(
        (number for number in listing.numbers if number >= floor and number not in stored),
        reverse=True,
    )
    unreviewed = sorted(
        (
            number
            for number in listing.numbers
            if number >= floor and number in stored and number not in reviewed
        ),
        reverse=True,
    )

    result["missing"] = [
        {"number": number, **listing.describe(number)} for number in missing
    ]
    result["unreviewed"] = [
        {"number": number, **listing.describe(number)} for number in unreviewed
    ]

    return result


def render_markdown(result: dict) -> str:
    lines: list[str] = []

    if not result["ok"]:
        lines.append("## ⚠️ The release check could not run")
        lines.append("")
        for problem in result["problems"]:
            lines.append(f"- {problem}")
        lines.append("")
        lines.append(
            "Nothing is broken in the app itself. See `RUNBOOK.md`, section "
            "*When the daily check reports a problem*."
        )
        return "\n".join(lines)

    missing = result["missing"]
    unreviewed = result["unreviewed"]

    if missing:
        lines.append(
            f"## {len(missing)} SEC litigation release(s) are not in the database"
        )
    else:
        lines.append("## ✅ The database is up to date")

    lines.append("")
    lines.append(
        f"Newest on SEC.gov: **LR-{result['sec_highest']}** · "
        f"newest in the database: **LR-{result['database_highest']}**"
    )
    lines.append("")

    if not missing and not unreviewed:
        return "\n".join(lines)

    if missing:
        lines.append("### To process")
        lines.append("")
        lines.append("| Release | Date | Respondents |")
        lines.append("| --- | --- | --- |")

        for entry in missing:
            respondents = (entry.get("respondents") or "").replace("|", "/")
            if len(respondents) > 90:
                respondents = respondents[:87] + "…"
            lines.append(
                f"| [LR-{entry['number']}]({entry.get('url', '')}) "
                f"| {entry.get('date', '')} | {respondents} |"
            )

        lines.append("")
        first = missing[0]["number"]
        last = missing[-1]["number"]
        lines.append(
            f"Open the app and start the interval **LR-{first} → LR-{last}**: "
            "https://sec-litigation-tool.streamlit.app/"
        )
        lines.append("")

    if unreviewed:
        numbers = ", ".join(f"LR-{entry['number']}" for entry in unreviewed[:25])
        lines.append("### Already downloaded, review not finished")
        lines.append("")
        lines.append(numbers)
        lines.append("")

    if result["sources_failed"]:
        lines.append("<details><summary>Source notes</summary>")
        lines.append("")
        for note in result["sources_failed"]:
            lines.append(f"- Not available: {note}")
        for note in result["sources_ok"]:
            lines.append(f"- Read: {note}")
        lines.append("")
        lines.append("</details>")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Path to a local database instead of the published history database.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Where report.md and result.json are written.",
    )
    arguments = parser.parse_args()

    result = build_report(arguments.database)
    markdown = render_markdown(result)

    print(markdown)

    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    (arguments.output_dir / "report.md").write_text(markdown, encoding="utf-8")
    (arguments.output_dir / "result.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")

    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        needs_notice = (not result["ok"]) or bool(result["missing"])
        fingerprint = ",".join(str(entry["number"]) for entry in result["missing"])
        with open(output_file, "a", encoding="utf-8") as handle:
            handle.write(f"needs_notice={'true' if needs_notice else 'false'}\n")
            handle.write(f"missing_count={len(result['missing'])}\n")
            handle.write(f"fingerprint={fingerprint}\n")
            handle.write(f"check_ok={'true' if result['ok'] else 'false'}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
