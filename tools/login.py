"""
One-time (and whenever the login expires) Google sign-in on your PC.
Prints the three values to paste into GitHub > Settings > Secrets and variables > Actions.

Put your client_secret_....json (downloaded from Google Cloud) in this tools/ folder first.
It is git-ignored, so it won't be uploaded to GitHub.
"""
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
]
HERE = Path(__file__).resolve().parent

# Look in tools/, the repo folder, and the folder above it (keep the secret OUTSIDE the repo if you can).
matches = [p for d in (HERE, HERE.parent, HERE.parent.parent) for p in sorted(d.glob("client_secret*.json"))]
if not matches:
    raise SystemExit("Put your client_secret_....json file in the tools folder, then run again.")

flow = InstalledAppFlow.from_client_secrets_file(str(matches[0]), SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
client = json.loads(matches[0].read_text())["installed"]

lines = [
    "Add these as GitHub repository secrets",
    "(repo > Settings > Secrets and variables > Actions > New repository secret):",
    "",
    f"GOOGLE_CLIENT_ID      = {client['client_id']}",
    f"GOOGLE_CLIENT_SECRET  = {client['client_secret']}",
    f"GOOGLE_REFRESH_TOKEN  = {creds.refresh_token}",
    "DASHBOARD_PASSWORD    = (make up a password; you'll type it to open the dashboard)",
    "",
    "Keep these private. Delete github_secrets.txt after copying them.",
]
# Saved next to the client secret file, so it stays out of the repo folder you upload.
out_file = matches[0].parent / "github_secrets.txt"
out_file.write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
print(f"\nAlso saved to {out_file}")
