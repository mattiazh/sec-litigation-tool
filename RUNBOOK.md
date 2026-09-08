# SEC Litigation Tool — quick guide

Open **https://sec-litigation-tool.streamlit.app/** in your browser. No installation is needed.

## Review an interval

1. Enter the **newest** and **oldest** release numbers. For one release, enter
   the same number twice. Use numbers only, without `LR-`.
2. Select **Start review**. The app retrieves the selected releases and PDFs.
3. If PDF text is unreadable, follow the recovery instructions and paste the
   complete readable text. Press **Enter** to submit. Short text requires confirmation.
4. Copy the prepared prompt into your usual ChatGPT chat. Automatic clipboard
   copying is attempted once per case; use **Copy ChatGPT prompt** if it is blocked.
5. Copy ChatGPT's complete machine-readable response and paste it into the app.
   Press **Enter** to validate and save. **Shift+Enter** inserts a newline.
6. Clean responses save and the next case opens. Review warnings before saving;
   correct any critical errors. Clicking away from the box does not submit it.

## Finish: save both files

The finish screen provides:

1. **Excel report** — the usual formatted report.
2. **Full database (.db)** — the run's release data, full stored PDF text,
   recovered text, keywords, summaries, source links and extraction flags.

Keep both files together in your team's archive folder. The database does not
contain the original PDF files themselves. Sources that could not be retrieved
remain incomplete; check the warnings before filing the results.

**Download both before closing the tab.** The app's working session is temporary.
There is no database to publish on GitHub and no daily checker to maintain.
Select “I have saved both files” before starting another interval.

## Pause or resume

Open the collapsed sidebar → **Pause / save progress** to download a full
progress database. On a later visit, use **Resume a saved database** in the sidebar.
A resumed run contains the saved text and completed reviews. It does not combine
separate archived runs automatically.

## If something fails

- SEC download failure: check the extraction log and the unavailable-source
  warnings. The download retains whatever data was obtained.
- Clipboard blocked: use the copy button, or copy from **Prompt / source details**.
- Excel error: the database download remains available independently.
- App session interrupted: resume the most recent database you downloaded.
- An interval may contain at most 75 releases. Use several runs for larger ranges.

For handover, keep the app and repository under an account the firm controls,
retain your archive folder and confirm the contact address used for SEC requests.
