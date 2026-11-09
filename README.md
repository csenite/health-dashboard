# Health Dashboard

A private dashboard for your weight, body composition, steps, sleep, heart rate, VO2 max and workouts, in metric units. It runs entirely on GitHub, so your PC doesn't need to be on.

Data comes from **two sources**, and either one keeps working if the other stops:

| Source | Gives | Login |
|---|---|---|
| **Google Health API** (main source) | Samsung steps, sleep, workouts, weight, body fat, resting and lowest heart rate, VO2 max | your Google login; expires every 7 days while the app is in Testing mode |
| **Health Sync → Google Drive** (optional, `Health Sync` folder) | bone mass and BMR, plus a fallback when the Google login has expired | a service account, which never expires |

If the Google Health login lapses, the figures Health Sync also has keep updating from there, and the page tells you.

How it runs:

- **GitHub Actions** fetches both sources every 3 hours.
- The data is **encrypted with your password** before it's saved.
- **GitHub Pages** hosts the page.

```
index.html                         the dashboard (one file, no libraries)
scripts/fetch_health.py            Google Health API + combines everything + encrypts
scripts/drive_source.py            reads the Health Sync CSVs from Drive
.github/workflows/update-data.yml  runs it every 3 hours
tools/login.bat                    Google Health sign-in on your PC; prints the GitHub secrets
tools/preview.bat                  preview with your real data on your PC before uploading
```

## Setup

### 1. Put the code on GitHub

Create a **public** repository, for example `health-dashboard`, and upload everything in this folder.

> **Never upload** `client_secret_*.json`, `token.json`, `github_secrets.txt` or the service account key (`health-dashboard-514202-*.json`). Keep them **outside** this folder. Dragging files onto github.com ignores `.gitignore`.

### 2. Google Health login

Double-click `tools/login.bat` and sign in. It saves `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `GOOGLE_REFRESH_TOKEN` to `github_secrets.txt`, next to your client secret file.

### 3. Health Sync (Drive) access (optional)

1. In Google Cloud, go to **IAM & Admin → Service accounts → health-sync-reader → Keys → Add key → Create new key → JSON**. A file like `health-dashboard-514202-xxxx.json` downloads. Keep it outside the repo folder.
2. In Google Drive, share your **Health Sync** folder with `health-sync-reader@health-dashboard-514202.iam.gserviceaccount.com` as **Viewer**.

### 4. Add GitHub secrets

In your repo, go to **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | from `github_secrets.txt` |
| `GOOGLE_CLIENT_SECRET` | from `github_secrets.txt` |
| `GOOGLE_REFRESH_TOKEN` | from `github_secrets.txt` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | optional: the **entire contents** of the service account `.json` file |
| `DASHBOARD_PASSWORD` | required: a long password you make up, typed to open the dashboard. The workflow stops without it, so your data is never published unencrypted. |

Then delete `github_secrets.txt`.

### 5. Run it and turn on the website

1. Go to **Actions → Update health data → Run workflow**. The first run fetches the past year.
2. Go to **Settings → Pages → Deploy from a branch → main / (root) → Save**.
3. Your dashboard is at `https://<your-username>.github.io/health-dashboard/`.

## When the Google Health login expires (every 7 days in Testing mode)

The workflow still saves the Health Sync data, then fails on purpose so GitHub emails you. To fix it:

1. Run `tools/login.bat`.
2. Paste the new `GOOGLE_REFRESH_TOKEN` into the GitHub secret of the same name.

Until you do, the page shows a note, and only the figures Health Sync provides keep updating.

## Notes

- **Steps:** Samsung Health's own count. Google Health's combined daily total has run about 1.65x higher since May 2026, which looks like double counting, so it's only a fallback for days with no Samsung count.
- **Weight and body fat:** the first morning reading of each day (from 4 am), or the earliest reading if there's no morning one.
- **Workouts:** from Google Health. Health Sync's workout files overstate distance, so they only fill in sessions Google Health doesn't have.
- **Keep Health Sync working:** on your phone, set Health Sync's battery to **Unrestricted** so it keeps uploading in the background.
- **Update frequency:** the `cron` line in `.github/workflows/update-data.yml`.
- **Change your password:** update `DASHBOARD_PASSWORD` and re-run the workflow. It re-fetches the last year.
