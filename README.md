# SEC Litigation Release Tool — Web Edition

Browser-based edition of the SEC Litigation Release workflow.

**App:** https://sec-litigation-tool.streamlit.app/
**Day-to-day instructions and handover: [RUNBOOK.md](RUNBOOK.md)** — start there.

## What the app does

1. On opening, it compares SEC.gov against the published historical database
   and offers the releases that are not yet processed. No release numbers have
   to be looked up by hand.
2. It extracts the SEC release page and all Resource PDFs.
3. Unreadable or garbled PDFs are shown for manual recovery before the
   ChatGPT review.
4. It builds the full ChatGPT source prompt, including the current keyword
   catalogue, and puts it on the clipboard automatically.
5. The user pastes ChatGPT's machine-readable response back and presses
   Ctrl+Enter. A response that passes every check is saved automatically and
   the next case opens.
6. The safety checks are unchanged: Case ID, release number, respondents,
   summary structure, keyword catalogue, duplicate summaries — the duplicate
   check now runs against the whole history rather than only the current
   session.
7. The user downloads the Excel report, and the updated history database for
   publishing.

## How the pieces fit together

```
SEC.gov ──────────┐
                  ├─► daily GitHub Action ──► one GitHub issue listing what is missing
sec_history.db ───┘        (.github/workflows/check-new-releases.yml)
   ▲   │
   │   └────────────► Streamlit app: knows what is done, offers what is new
   │                                   │
   └── uploaded by hand after a run ◄──┘  (download button on the finish screen)
```

`sec_history.db` is published as an asset on the GitHub release tagged
**data-latest**. It is a slim copy of the working database — the `releases`
table only, without the extracted PDF text — of a few megabytes. It needs no
account, no API key and no token: it is a plain public download.

## Repository files

- `streamlit_app.py` — web interface
- `sec_backend.py` — extraction, database, prompt, manual-PDF and validation logic
- `sec_history.py` — downloads, reads and builds the published history database
- `sec_feed.py` — reads which releases SEC.gov has published (website + RSS)
- `excel_export.py` — Excel export with one SEC Resource hyperlink per column
- `keywords.txt` — the closed 37-keyword catalogue
- `scripts/check_new_releases.py` — the daily comparison, also runnable by hand
- `scripts/build_history_db.py` — builds `sec_history.db` from a local `sec_releases.db`
- `.github/workflows/check-new-releases.yml` — the schedule and the notification
- `RUNBOOK.md` — operating manual and handover checklist

## Data flow and privacy

The historical database published on GitHub contains release numbers, dates,
respondents, keywords and summaries. All of the underlying material is public
SEC information, but the keyword classifications and summaries are the firm's
own work. If the repository is public, so are they. To publish without the
summaries:

```
python scripts/build_history_db.py --no-summaries
```

Duplicate-summary detection then only works within a single run.

## Running locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

To check SEC.gov from the command line without opening the app:

```
python scripts/check_new_releases.py
```

## Deployment

The app runs on Streamlit Community Cloud, deployed from `main` with
entrypoint `streamlit_app.py`. Pushing to `main` redeploys it automatically;
the daily check deliberately writes nothing to `main`, so it never causes a
redeploy in the middle of someone's work.

Transferring ownership of the repository and the deployment is covered in
[RUNBOOK.md](RUNBOOK.md), section 7.
