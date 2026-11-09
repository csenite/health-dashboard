"""
Preview the dashboard on your PC with your real data, before setting up GitHub.

Uses the token.json from the earlier Google sign-in (or run login.bat first), fetches your
data, and opens the dashboard at http://localhost:8765. The preview copy lives in a
"preview" folder NEXT TO the repo folder, so your unencrypted data never ends up in the repo.
Close the window to stop it.
"""
import functools
import http.server
import json
import os
import shutil
import sys
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "scripts"))
import fetch_health as fh  # noqa: E402

token_file = next((d / "token.json" for d in (HERE, REPO, REPO.parent) if (d / "token.json").exists()), None)
if not token_file:
    sys.exit("No token.json found. Run run_test.bat (or login.bat) first to sign in to Google.")
tok = json.loads(token_file.read_text())
os.environ.update(
    GOOGLE_CLIENT_ID=tok["client_id"],
    GOOGLE_CLIENT_SECRET=tok["client_secret"],
    GOOGLE_REFRESH_TOKEN=tok["refresh_token"],
)
os.environ.pop("DASHBOARD_PASSWORD", None)
# Health Sync (Drive) too, if the service account key is next to the client secret
sa = next((p for d in (HERE, REPO, REPO.parent) for p in d.glob("*service*account*.json")), None) or \
     next((p for d in (HERE, REPO, REPO.parent) for p in d.glob("health-dashboard-*.json")), None)
if sa:
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = sa.read_text()
    os.environ["HEALTH_SYNC_FOLDER_ID"] = "1_C3GyA_dQGNjn6a2Hpe_suT_B6G_PPBl"

preview = REPO.parent / "preview"
(preview / "data").mkdir(parents=True, exist_ok=True)
shutil.copy(REPO / "index.html", preview / "index.html")
fh.DATA_DIR = preview / "data"
fh.PLAIN_FILE = fh.DATA_DIR / "health.json"
fh.ENC_FILE = fh.DATA_DIR / "health.enc.json"

print("Fetching your data...")
try:
    fh.main()
except SystemExit as e:
    print(e)

port = 8765
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(preview))
server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
print(f"\nDashboard preview: http://localhost:{port}/   (close this window to stop)")
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
