# SEC Litigation Release Tool — Web Edition

This is a separate browser-based edition of the SEC Litigation Release workflow.
It does **not** use or update the local historical `sec_releases.db` database.

https://sec-litigation-tool.streamlit.app/

## What the web app does

1. User selects an SEC Litigation Release interval.
2. The app extracts the SEC release page and all Resource PDFs.
3. Unreadable/garbled PDFs are shown for manual recovery before ChatGPT review.
4. The app builds the full ChatGPT source prompt, including the current keyword catalogue.
5. The user pastes ChatGPT's machine-readable response back into the app.
6. Existing safety checks validate Case ID, release number, respondents, summary format, keyword catalogue, and duplicate summaries.
7. The user downloads the formatted Excel report.

Each browser session uses a temporary SQLite database. A progress database can be downloaded and uploaded later to resume work.

## Repository files

- `streamlit_app.py` — web interface
- `sec_backend.py` — extraction, database, prompt, manual-PDF and validation logic based on the current local program
- `excel_export.py` — Excel export with one SEC Resource hyperlink per column
- `keywords.txt` — current closed 37-keyword catalogue
- `requirements.txt` — Python dependencies for Streamlit Community Cloud
- `.streamlit/config.toml` — interface theme/configuration
- `.gitignore` — prevents databases and working files from being committed

## Local test before GitHub

Open PowerShell in this folder and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

A browser window should open automatically. If it does not, open the local URL shown in PowerShell.

## GitHub setup

1. Create a new **private** GitHub repository, for example `sec-litigation-tool`.
2. Upload the contents of this folder to the repository root.
3. Confirm that the repository contains `streamlit_app.py` and `requirements.txt`.
4. Do **not** upload your local `sec_releases.db`. The included `.gitignore` excludes `*.db` files.

## Streamlit Community Cloud deployment

1. Sign in to Streamlit Community Cloud and connect your GitHub account.
2. Give Streamlit access to the private repository if required.
3. Choose **Create app**.
4. Select the GitHub repository and branch (`main`).
5. Set the entrypoint file to:

   `streamlit_app.py`

6. Use the default supported Python version initially (Community Cloud currently defaults to Python 3.12).
7. Deploy the app.
8. Keep the app private and invite colleagues as viewers.

## Important operational points

### Temporary data only
The hosted app does not contain the live local historical database. Each browser session receives its own temporary SQLite database.

### Save progress
For longer intervals, use **Download progress database** in the sidebar. If the browser session is lost, upload that file under **Resume a previous web run**.

### SEC access
The app identifies itself with the existing declared User-Agent and includes the current request pause. The SEC's fair-access rules should still be observed. If SEC.gov blocks the Community Cloud outbound IP or returns rate-limit errors, the extraction step may need to be hosted internally instead.

### ChatGPT remains manual
No OpenAI API key is used. The colleague copies the generated prompt to the dedicated ChatGPT chat and pastes the machine-readable response back into the app.

### Private-app access
Use a private repository/private Streamlit app for the first deployment. Viewer access can then be granted to colleagues through Streamlit's sharing controls.

## Recommended first test

Before giving the app to colleagues, test a small known interval such as two or three releases containing:

- one release with no Resource documents;
- one release with normal readable PDFs;
- one release requiring manual PDF recovery;
- one previously problematic historical SEC heading/date format.

Then compare the downloaded Excel against the local program's Excel for the same interval.
