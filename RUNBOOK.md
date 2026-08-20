# Runbook — SEC Litigation Release Tool

This is the operating manual for the tool. It is written for a colleague who
has never touched the code and does not want to. Everything here is done in a
browser.

**The app:** https://sec-litigation-tool.streamlit.app/
**The repository:** https://github.com/mattiazh/sec-litigation-tool

---

## 1. The normal weekly routine

1. Open the app.
2. It tells you at the top how many new SEC releases are not yet processed,
   for example *"9 new release(s) since LR-26603"*. Click the blue button.
   You do not have to look up release numbers anywhere.
3. The app downloads the releases and their PDFs. This takes a few minutes.
4. For each case the prompt is **already on your clipboard**. Switch to the
   dedicated ChatGPT chat, press Ctrl+V, send.
5. Copy ChatGPT's whole answer, paste it into the box in the app, press
   **Ctrl+Enter**.
   - If the answer is clean it saves itself and the next case opens with its
     prompt already copied. You never have to click "save".
   - If something is wrong the app stops and shows you what. Correct the
     answer and press Ctrl+Enter again.
6. When the queue is finished: **Download Excel report**.
7. **Do not skip this:** on the same screen, click
   **Download updated database (sec_history.db)** and upload it to GitHub as
   described in section 2. Until you do, the tool still thinks those releases
   are outstanding and will keep reminding you about them.

If the clipboard copy does not happen by itself (Safari and Firefox block it),
click the **Copy ChatGPT prompt** button instead. Everything else is identical.

---

## 2. Publishing an updated database

The tool keeps its history in one file, `sec_history.db`, stored on GitHub as
a *release asset*. Replacing it is a drag-and-drop.

1. Go to https://github.com/mattiazh/sec-litigation-tool/releases
2. Click the release named **data-latest**, then **Edit release** (the pencil).
3. Delete the old `sec_history.db` (click the small **x** next to it).
4. Drag the `sec_history.db` you just downloaded from the app into the
   attachment area.
5. Click **Update release**.

That is it. The app and the daily check both pick up the new file within an
hour. Nothing needs to be re-deployed.

**Never delete the `data-latest` release itself** — only the file inside it.
If it is ever deleted, create a new release with exactly the tag
`data-latest` and attach the file to it.

---

## 3. The daily check

Every weekday morning, GitHub looks at SEC.gov by itself and compares it with
the published database.

- If releases are missing, it opens **one** GitHub issue titled
  *"SEC litigation releases pending"* listing them, and updates that same
  issue on later days. Whoever is listed in the workflow gets an email.
- When everything has been processed, it closes the issue again.

To see it: repository → **Issues**.
To run it immediately: repository → **Actions** → *Check SEC for new
litigation releases* → **Run workflow**.

### Who gets notified

Open `.github/workflows/check-new-releases.yml` in GitHub, click the pencil,
and change the line:

```yaml
  NOTIFY_USERS: "mattiazh"
```

to the GitHub username of whoever should be notified (comma-separated for
several people). Commit the change. Nothing else needs to happen.

### When the daily check reports a problem

The issue will say what went wrong.

| What it says | What it means | What to do |
| --- | --- | --- |
| *SEC.gov could not be read* | SEC blocked or was down | Wait a day. If it repeats for a week, SEC may be blocking GitHub's servers — the app itself still works, so keep working manually and see section 6. |
| *No published historical database was found* | The `data-latest` file is missing | Re-upload `sec_history.db` per section 2. |
| The check stops running entirely | GitHub disables scheduled jobs in repositories that see no activity for 60 days | Open Actions → the workflow → **Enable workflow**. The workflow also writes a monthly heartbeat to keep this from happening. |

---

## 4. If the app itself will not open

Streamlit Community Cloud puts apps to sleep when they are unused. The first
visit after a while shows a **"Yes, get this app back up!"** button — click it
and wait about a minute.

If it shows an error instead:

1. Go to https://share.streamlit.io and sign in with the GitHub account that
   owns the app.
2. Find the app, open the **⋮** menu, choose **Reboot**.

---

## 5. Things that must be true for this to keep working

This is the list to check once a year, and immediately if anything breaks.

| Thing | Why it matters | Who owns it now |
| --- | --- | --- |
| GitHub repository `mattiazh/sec-litigation-tool` | Holds the code and the database file | **Personal account — see section 7** |
| Streamlit Community Cloud app | Serves the web app; tied to the GitHub account that created it | **Personal account — see section 7** |
| Contact e-mail in the SEC User-Agent | SEC requires a reachable contact and writes there before blocking a tool | Set in `sec_backend.py`, currently `sec.tool@pqs.ch` |
| The ChatGPT chat used for reviews | The prompt assumes the dedicated chat with its instructions | The reviewer |
| `keywords.txt` | The closed 37-keyword catalogue the validation enforces | Edit in GitHub directly |

---

## 6. Known limitations, stated plainly

- **The ChatGPT step stays manual by design.** No API key is used anywhere.
- **SEC's RSS feed lags.** It has been observed weeks behind the website, so
  the check reads the website first and treats RSS only as a cross-check.
- **SEC may block cloud servers.** Both the app and the daily check run on
  rented servers. If SEC starts refusing them, the daily check will say so and
  the extraction step would have to be run from an office machine instead.
- **The published database is public** if the repository is public. It
  contains release numbers, dates, respondents, keywords and the summaries.
  To publish without the summaries, run
  `python scripts/build_history_db.py --no-summaries` locally instead of using
  the download button in the app — duplicate detection then only works within
  a single run.
- **One run is limited to 75 releases.** More than that, and the app offers
  the first 75; run it again afterwards for the rest.

---

## 7. Handover checklist

Do these before the current owner leaves. Each one is a single point of
failure that cannot be fixed afterwards without rebuilding.

- [ ] **Move the GitHub repository to an account the firm controls.**
      Repository → Settings → *Transfer ownership*. A personal account that
      gets deleted takes the app, the database and the daily check with it.
- [ ] **Re-deploy the Streamlit app from that account** (share.streamlit.io →
      Create app → pick the repository → entrypoint `streamlit_app.py`), and
      delete the old deployment. Then update the app link in this file and in
      the README.
- [ ] **Update `DEFAULT_HISTORY_URL` in `sec_history.py`** to the new
      repository address, and `NOTIFY_USERS` in the workflow file.
- [ ] **Confirm the SEC contact address** in `sec_backend.py` is a mailbox
      someone still reads.
- [ ] **Walk one colleague through a complete run once**, including
      section 2 — publishing the updated database is the only step that is not
      automatic and the only one that gets forgotten.
- [ ] **Keep a copy of the full `sec_releases.db`** (54 MB, the working
      database with the extracted PDF text) somewhere on a firm drive. The
      published `sec_history.db` is a slim copy and cannot reconstruct it.
