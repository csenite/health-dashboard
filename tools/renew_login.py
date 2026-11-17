"""
Renews the Google Health login and updates GitHub for you, in one go:
  1. opens Google sign-in in your browser (you click Allow; Google requires this step),
  2. saves the new refresh token straight into the GOOGLE_REFRESH_TOKEN secret on GitHub,
  3. if a service account key file is lying next to your client secret, saves it as
     GOOGLE_SERVICE_ACCOUNT_JSON too (one-time Drive setup),
  4. starts the "Update health data" workflow so the dashboard refreshes now.

Needs the GitHub CLI (gh). The .bat installs it and signs you in the first time.
Nothing secret is written to disk or shown on screen.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

REPO = "csenite/health-dashboard"
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
]
HERE = Path(__file__).resolve().parent
FOLDERS = (HERE, HERE.parent, HERE.parent.parent)


def gh(*args, data=None):
    exe = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    return subprocess.run([exe, *args], input=data, text=True, capture_output=True)


def set_secret(name, value):
    r = gh("secret", "set", name, "--repo", REPO, data=value)   # value goes in on stdin, never on the command line
    if r.returncode:
        sys.exit(f"Couldn't update {name} on GitHub:\n{r.stderr.strip()}")
    print(f"  Updated GitHub secret {name}")


client_file = next((p for d in FOLDERS for p in sorted(d.glob("client_secret*.json"))), None)
if not client_file:
    sys.exit("Couldn't find your client_secret_....json. Put it in this folder and run again.")

print("Step 1 of 3: Google sign-in. Your browser will open; choose your account and click Allow.")
flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
if not creds.refresh_token:
    sys.exit("Google didn't return a refresh token. Run this again and make sure you click Allow.")

print("\nStep 2 of 3: saving to GitHub")
set_secret("GOOGLE_REFRESH_TOKEN", creds.refresh_token)

key = next((p for d in FOLDERS for pat in ("*service*account*.json", "health-dashboard-*.json")
            for p in sorted(d.glob(pat))), None)
if key:
    try:
        if json.loads(key.read_text()).get("type") == "service_account":
            set_secret("GOOGLE_SERVICE_ACCOUNT_JSON", key.read_text())
    except ValueError:
        pass

print("\nStep 3 of 3: refreshing the dashboard")
r = gh("workflow", "run", "update-data.yml", "--repo", REPO)
print("  Started. The dashboard updates in about a minute." if not r.returncode
      else f"  Couldn't start it ({r.stderr.strip()}). It will run on its own within 3 hours.")
print("\nAll done. You'll need to do this again in about 7 days.")
