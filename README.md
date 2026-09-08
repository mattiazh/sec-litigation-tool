# SEC Litigation Release Tool

A simple browser workflow for reviewing an interval of SEC litigation releases.

**App:** https://sec-litigation-tool.streamlit.app/

**Colleague instructions:** [RUNBOOK.md](RUNBOOK.md)

1. Enter the newest and oldest release numbers.
2. Recover unreadable PDF text when asked.
3. Copy the source into ChatGPT, paste its response back, and press **Enter**.
4. The existing checks validate the response. Clean responses save and advance;
   warnings require a decision and critical failures prevent saving.
5. Download **two files**: the usual Excel report and a **full SQLite database**.

No Python installation, scheduled checker, GitHub issue, history download or
GitHub database upload is required for the person using the app.

## The database archive

Each run has a separate database. Its download preserves all tables, including:

- `releases`: date, respondents, release number, keyword, SEC page text, summary and links.
- `documents`: full stored extracted/recovered PDF text, source URL/name, page and
  character counts, extraction status, manual-review flag/reason and text source.
- `run_metadata`: the selected interval, archive timestamp and format version.

PDF text is not shortened to fit Excel. Original PDF binary files are not
embedded. Unavailable or unreadable sources remain incomplete and flagged;
archiving cannot recover text that was never obtained.

The archive uses SQLite's backup API for a consistent snapshot, including
committed WAL data. It can be queried with SQLite tools or uploaded through the
collapsed sidebar to resume a saved run. It is **not a cumulative history** of
all colleagues' runs. Duplicate-summary checks apply to the current database.

Sessions are temporary. Download both files before closing the app. Progress
can also be downloaded mid-run. No claim of permanent server storage is made.

## Maintenance

Streamlit continues to deploy `streamlit_app.py` from this repository. Keep
`keyboard_input/index.html` alongside the Python modules: it provides explicit
Enter submission, Shift+Enter newlines and an accessible submit button. Merely
leaving the text field never submits it. Clipboard copying has a manual fallback.

The daily-check workflow and its unused history/feed scripts have been removed.
No existing GitHub release assets or archived databases are deleted by the app.

For developer use only:

```sh
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
python -m unittest discover -s tests -v
```
