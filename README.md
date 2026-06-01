# slo-tech.com/delo monitor

A cron (every 4 hours) that fetches the [slo-tech.com/delo](https://slo-tech.com/delo) RSS feed,
diffs against the last-known set of posting IDs in `state.json`, and emails any new
postings via Gmail SMTP. Runs on GitHub Actions, commits the updated `state.json` back
to the repo. First run seeds state and sends a single initialization email so you
don't get spammed with everything currently live.

## How it works

```
fetch RSS -> parse (ISO-8859-2) -> diff against state.json -> email digest
```

State is a JSON array of integer IDs (the trailing integer in `/delo/<id>` permalinks),
capped at the 200 most-recent.

## Setup

### 1. Generate a Gmail App Password

The script signs in to Gmail with an App Password, not your account password.

1. The Google account you'll send from must have **2-Step Verification** enabled.
   Turn it on at <https://myaccount.google.com/security> if it isn't already.
2. Go to <https://myaccount.google.com/apppasswords>.
3. Create a new app password (name it "slo-tech monitor" or similar).
4. Copy the 16-character password. Spaces are cosmetic -- with or without them works.

### 2. Add the three GitHub secrets

In your fork: **Settings -> Secrets and variables -> Actions -> New repository secret**.

| Name                 | Value                                       |
|----------------------|---------------------------------------------|
| `GMAIL_USER`         | The Gmail address you send from (e.g. `you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | The 16-character App Password from step 1   |
| `NOTIFY_TO`          | Where digests should land (can be the same as `GMAIL_USER`) |

### 3. Confirm the workflow is enabled

Push to `main` and check **Actions -> slo-tech delo monitor**. If a fork shows
"Workflows aren't being run on this forked repository" you'll need to enable
workflows under the Actions tab.

## Run locally

Requires Python 3.10 or newer (the workflow uses 3.11).

```sh
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
python monitor.py --dry-run
```

`--dry-run` fetches the feed, computes the diff, and prints the email subject and
both body parts (text + HTML) to stdout. It will not send mail and will not touch
`state.json`. No env vars are required in dry-run mode.

To actually send mail from your local machine, export the three env vars and drop
the flag:

```sh
export GMAIL_USER=you@gmail.com
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export NOTIFY_TO=you@gmail.com
python monitor.py
```

## Manual trigger

**Actions -> slo-tech delo monitor -> Run workflow**. Pick the branch (usually
`main`) and hit the green button. The workflow only commits to `state.json` if
the contents actually changed.

## Schedule

The workflow runs every 4 hours at 6 minutes past the hour
(`6 */4 * * *` -> 00:06, 04:06, 08:06, 12:06, 16:06, 20:06 UTC). Cron is UTC-only,
so local clock times shift by an hour across DST, but at a 4-hourly cadence that
doesn't matter in practice.

The `:06` offset is deliberate: GitHub's shared cron scheduler is most congested
at the top of the hour, and runs queued there can be delayed by hours -- or, under
heavy load, skipped entirely. Firing at `:06` sidesteps the worst of it, but
scheduling is still best-effort, not guaranteed.

None of this is a bug in the monitor -- it's a documented GitHub limitation. Per
GitHub's [docs on the `schedule` event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
scheduled runs "can be delayed during periods of high loads of GitHub Actions
workflow runs" and, if load is high enough, "some queued jobs may be dropped." The
**first** scheduled run after a cron is added or changed is especially prone to being
missed: GitHub can take a while (sometimes an hour or more) to register a new
schedule, and a skipped run is never retried.

A missed run loses nothing here -- `state.json` is only written on a successful run,
so the next run just catches up on anything new. To force a run immediately, trigger
it manually (see [Manual trigger](#manual-trigger) above).

## Failure behaviour

- Network error fetching the feed, or malformed XML: script logs to stderr,
  exits non-zero, and `state.json` is **not** touched. The workflow's commit
  step is skipped because the job has already failed.
- SMTP failure when sending mail: same -- exit non-zero, state untouched.
- Empty diff on a non-first run: script exits 0 silently (no email, no state
  write, no commit).

## Files

| Path                              | Purpose                                |
|-----------------------------------|----------------------------------------|
| `monitor.py`                      | The script                             |
| `state.json`                      | Seen posting IDs (newest first, cap 200) |
| `requirements.txt`                | `feedparser`, `requests`               |
| `.github/workflows/monitor.yml`   | Cron + commit-back workflow            |
