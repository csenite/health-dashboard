"""
Pulls daily totals from the Google Health API and saves them for the dashboard.

Runs in GitHub Actions (see .github/workflows/update-data.yml). Needs these env vars
(set them as repository secrets):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN   (Google Health API)
  GOOGLE_SERVICE_ACCOUNT_JSON, HEALTH_SYNC_FOLDER_ID              (Health Sync files on Drive)
  DASHBOARD_PASSWORD   (optional but recommended: encrypts the data so only you can read it)
Either source can be missing or failing; the other still updates.
  TZ_NAME              (optional, default Asia/Kuala_Lumpur)

Output: data/health.enc.json (encrypted) or data/health.json (plain, if no password).
"""
import base64
import copy
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ENC_FILE = DATA_DIR / "health.enc.json"
PLAIN_FILE = DATA_DIR / "health.json"

BASE = "https://health.googleapis.com/v4"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TZ = ZoneInfo(os.environ.get("TZ_NAME") or "Asia/Kuala_Lumpur")

# data type -> longest range (days) the API allows per daily roll-up request
TYPES = {
    "steps": 90,
    "distance": 90,
    "heart-rate": 14,
    "weight": 90,
}
SAMSUNG_APP = "com.sec.android.app.shealth"
BACKFILL_DAYS = 365   # first run: how far back to fetch
REFRESH_DAYS = 21     # later runs: re-fetch this many recent days (catches late syncs)
KDF_ITER = 250_000


# ---------------------------------------------------------------- auth
def access_token() -> str:
    env = {k: os.environ.get(k, "").strip() for k in
           ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        sys.exit(f"Missing secrets: {', '.join(missing)}. See README > Setup.")
    r = requests.post(TOKEN_URL, data={
        "client_id": env["GOOGLE_CLIENT_ID"],
        "client_secret": env["GOOGLE_CLIENT_SECRET"],
        "refresh_token": env["GOOGLE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=30)
    if r.status_code != 200:
        detail = r.text[:300]
        if "invalid_grant" in detail:
            sys.exit("Google login expired (invalid_grant). Run tools/login.bat on your PC "
                     "and update the GOOGLE_REFRESH_TOKEN secret. See README > When the login expires.")
        sys.exit(f"Token refresh failed: HTTP {r.status_code} {detail}")
    return r.json()["access_token"]


# ---------------------------------------------------------------- API
def api_request(method: str, url: str, **kwargs):
    """Google's API sometimes returns a brief 503, so give it a few more tries."""
    for attempt in range(4):
        r = requests.request(method, url, **kwargs)
        if r.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
            return r
        time.sleep(10 * (attempt + 1))


def civil(d: dt.date) -> dict:
    return {"date": {"year": d.year, "month": d.month, "day": d.day},
            "time": {"hours": 0, "minutes": 0, "seconds": 0}}


def to_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v) if "." in v else int(v)
        except ValueError:
            return None
    return None


def flatten(obj, prefix="") -> dict:
    """Nested value object -> {"a.b": number} with numeric leaves only."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}{i}."))
    else:
        n = to_number(obj)
        if n is not None:
            out[prefix.rstrip(".")] = n
    return out


def daily_rollup(token: str, data_type: str, start: dt.date, end_excl: dt.date) -> dict:
    """Returns {"YYYY-MM-DD": {flat values}} for [start, end_excl)."""
    url = f"{BASE}/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"
    body = {"range": {"start": civil(start), "end": civil(end_excl)}, "windowSizeDays": 1}
    result, page = {}, None
    while True:
        if page:
            body["pageToken"] = page
        r = api_request("POST", url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        for p in data.get("rollupDataPoints", []):
            d = (p.get("civilStartTime") or {}).get("date") or {}
            if not d:
                continue
            day = f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
            values = {}
            for k, v in p.items():
                if k in ("civilStartTime", "civilEndTime"):
                    continue
                values.update(flatten(v))
            if values:
                result[day] = values
        page = data.get("nextPageToken")
        if not page:
            return result


# ---------------------------------------------------------------- storage
def _kdf(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=KDF_ITER).derive(password.encode())


def encrypt(payload: dict, password: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_kdf(password, salt)).encrypt(
        iv, json.dumps(payload, separators=(",", ":")).encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": KDF_ITER,
            "salt": b64(salt), "iv": b64(iv), "data": b64(ct)}


def decrypt(blob: dict, password: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = lambda k: base64.b64decode(blob[k])
    pt = AESGCM(_kdf(password, raw("salt"))).decrypt(raw("iv"), raw("data"), None)
    return json.loads(pt)


def load_existing(password: str | None) -> dict:
    try:
        if password and ENC_FILE.exists():
            return decrypt(json.loads(ENC_FILE.read_text()), password)
        if not password and PLAIN_FILE.exists():
            return json.loads(PLAIN_FILE.read_text())
    except Exception as e:  # wrong password after a change, corrupt file, ...
        print(f"Could not read existing data ({e}); starting fresh.")
    return {"days": {}}


def save(payload: dict, password: str | None):
    DATA_DIR.mkdir(exist_ok=True)
    if password:
        ENC_FILE.write_text(json.dumps(encrypt(payload, password)))
        if PLAIN_FILE.exists():
            PLAIN_FILE.unlink()   # never leave an unencrypted copy behind
    else:
        PLAIN_FILE.write_text(json.dumps(payload, separators=(",", ":")))


# ---------------------------------------------------------------- main
def samsung_steps(token: str, since: dt.date) -> dict:
    """Samsung Health's own daily steps. The merged roll-up runs about 1.7x higher than any
    single source, so it looks double counted. Points arrive newest first."""
    url = f"{BASE}/users/me/dataTypes/steps/dataPoints"
    first, totals, page = since.isoformat(), {}, None
    while True:
        params = {"pageSize": 1000}
        if page:
            params["pageToken"] = page
        r = api_request("GET", url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        data, seen = r.json(), []
        for p in data.get("dataPoints", []):
            rec = p.get("steps")
            if not rec:
                continue
            d = rec["interval"]["civilStartTime"]["date"]
            day = f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
            seen.append(day)
            app = p.get("dataSource", {}).get("application", {}).get("packageName")
            if app == SAMSUNG_APP and day >= first:
                totals[day] = totals.get(day, 0) + int(rec["count"])
        page = data.get("nextPageToken")
        if not page or (seen and max(seen) < first):
            return totals


def list_points(token: str, data_type: str) -> list:
    """Every stored data point of a type. Only for types with few points, such as sleep."""
    url = f"{BASE}/users/me/dataTypes/{data_type}/dataPoints"
    out, page = [], None
    while True:
        params = {"pageSize": 1000}
        if page:
            params["pageToken"] = page
        r = api_request("GET", url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        out += data.get("dataPoints", [])
        page = data.get("nextPageToken")
        if not page:
            return out


def local_time(iso: str, offset: str | None) -> dt.datetime:
    """'2026-11-08T23:52:02.095Z' + '28800s' -> the wearer's local clock time."""
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + dt.timedelta(seconds=int((offset or "0s").rstrip("s")))).replace(tzinfo=None)


def source_rank(point: dict) -> int:
    """Higher wins when several apps recorded the same thing. Samsung is the original source."""
    app = point.get("dataSource", {}).get("application", {}).get("packageName")
    return {SAMSUNG_APP: 2, "com.google.android.apps.fitness": 1}.get(app, 0)


def morning_readings(points: list, key: str, value) -> dict:
    """{day: value} using each day's first reading from 4 am, else its earliest."""
    best = {}
    for p in points:
        rec = p.get(key) or {}
        civ = (rec.get("sampleTime") or {}).get("civilTime") or {}
        d, t = civ.get("date"), civ.get("time") or {}
        v = value(rec)
        if not d or not v:
            continue
        day = f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
        clock = (t.get("hours", 0), t.get("minutes", 0), t.get("seconds", 0))
        rank = (clock[0] < 4, clock)
        if day not in best or rank < best[day][0]:
            best[day] = (rank, v)
    return {day: v for day, (_, v) in best.items()}


def sleep_by_day(points: list) -> dict:
    """{wake-up day: {"minutes": n, "stages": {...}}}, taking one app per day to avoid double counting."""
    per = {}
    for p in points:
        rec = p.get("sleep") or {}
        iv, summary = rec.get("interval") or {}, rec.get("summary") or {}
        if not iv.get("endTime"):
            continue
        day = local_time(iv["endTime"], iv.get("endUtcOffset")).strftime("%Y-%m-%d")
        s = per.setdefault((day, source_rank(p)), {"minutes": 0, "stages": {}})
        s["minutes"] += int(summary.get("minutesAsleep") or 0)
        for st in summary.get("stagesSummary") or []:
            name = st.get("type", "ASLEEP")
            s["stages"][name] = s["stages"].get(name, 0) + int(st.get("minutes") or 0)
    out = {}
    for (day, rank), s in sorted(per.items(), key=lambda kv: kv[0][1]):
        if s["minutes"]:
            out[day] = s       # sorted by rank, so the preferred app is written last
    return out


def merge_exercises(points: list, workouts: list) -> int:
    """Adds or updates workouts. These distances look right, unlike the Health Sync workout files."""
    chosen = {}
    for p in sorted(points, key=source_rank):
        rec = p.get("exercise") or {}
        iv, m = rec.get("interval") or {}, rec.get("metricsSummary") or {}
        kind = (rec.get("exerciseType") or "").upper()
        if not iv.get("startTime") or not kind:
            continue
        start = local_time(iv["startTime"], iv.get("startUtcOffset"))
        secs = rec.get("activeDuration") or ""
        field = lambda word: next((to_number(v) for k, v in m.items() if word in k.lower()), None)
        chosen[(kind, start.strftime("%Y.%m.%d %H:%M"))] = {
            "type": kind, "start": start.strftime("%Y.%m.%d %H:%M:%S"), "date": start.strftime("%Y-%m-%d"),
            "seconds": float(secs.rstrip("s")) if secs else None,
            "km": to_number(m.get("distanceMillimeters")) / 1e6 if m.get("distanceMillimeters") else None,
            "avgHr": to_number(m.get("averageHeartRateBeatsPerMinute")),
            "maxHr": field("maxheartrate") or field("maximumheartrate"),
            "kcal": field("calor"),
            "steps": field("step"),
        }
    index = {(w["type"], w["start"][:16]): w for w in workouts}
    for key, new in chosen.items():
        old = index.get(key)
        if old:
            old.update({k: v for k, v in new.items() if v is not None})
        else:
            workouts.append(new)
    workouts.sort(key=lambda w: w["start"], reverse=True)
    return len(chosen)


def fetch_history(token: str, days: dict, workouts: list, errors: dict):
    """Metrics the daily roll-up can't provide. These types are small, so each run reads all of them."""
    def weight_kg(rec):
        grams = next((to_number(v) for k, v in rec.items() if "gram" in k.lower()), None)
        return grams / 1000 if grams else None

    def per_day(data_type, key, field, value):
        for day, v in morning_readings(list_points(token, data_type), key, value).items():
            days.setdefault(day, {})[field] = v

    def weight():
        per_day("weight", "weight", "gh-weight", lambda r: (lambda kg: {"kg": kg} if kg else None)(weight_kg(r)))

    def body_fat():
        per_day("body-fat", "bodyFat", "gh-bodyfat",
                lambda r: {"pct": to_number(r.get("percentage"))} if to_number(r.get("percentage")) else None)

    def resting():
        for p in list_points(token, "daily-resting-heart-rate"):
            rec = p.get("dailyRestingHeartRate") or {}
            d, v = rec.get("date"), to_number(rec.get("beatsPerMinute"))
            if d and v:
                days.setdefault(f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}", {})["gh-rhr"] = {"bpm": v}

    def vo2():
        per_day("vo2-max", "vo2Max", "gh-vo2", lambda r: {"value": to_number(r.get("vo2Max"))} if to_number(r.get("vo2Max")) else None)
        for p in list_points(token, "daily-vo2-max"):
            rec = p.get("dailyVo2Max") or {}
            d, v = rec.get("date"), to_number(rec.get("vo2Max"))
            if d and v:
                days.setdefault(f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}", {})["gh-vo2"] = {"value": v}

    def sleep():
        for day, s in sleep_by_day(list_points(token, "sleep")).items():
            days.setdefault(day, {})["gh-sleep"] = s

    def exercise():
        merge_exercises(list_points(token, "exercise"), workouts)

    for name, step in (("weight", weight), ("body-fat", body_fat), ("resting-heart-rate", resting),
                       ("vo2-max", vo2), ("sleep", sleep), ("exercise", exercise)):
        try:
            step()
            print(f"Google Health history {name}: ok")
        except Exception as e:
            errors[f"history-{name}"] = str(e)[:300]
            print(f"Google Health history {name}: FAILED {e}")


def fetch_google_health(days: dict, workouts: list, errors: dict):
    token = access_token()
    fetch_history(token, days, workouts, errors)
    today = dt.datetime.now(TZ).date()
    end_excl = today + dt.timedelta(days=1)
    full = os.environ.get("FULL_REFRESH") == "true"   # the workflow's "full refresh" option
    window = lambda key: BACKFILL_DAYS if full or not any(key in v for v in days.values()) else REFRESH_DAYS
    try:
        since = today - dt.timedelta(days=window("samsung-steps") - 1)
        totals = samsung_steps(token, since)
        for day, count in totals.items():
            days.setdefault(day, {})["samsung-steps"] = {"count": count}
        print(f"Google Health samsung-steps: {len(totals)} days")
    except Exception as e:
        errors["samsung-steps"] = str(e)[:300]
        print(f"Google Health samsung-steps: FAILED {e}")
    for data_type, max_span in TYPES.items():
        start = today - dt.timedelta(days=window(data_type) - 1)
        fetched = 0
        try:
            while start < end_excl:
                chunk_end = min(start + dt.timedelta(days=max_span), end_excl)
                for day, values in daily_rollup(token, data_type, start, chunk_end).items():
                    days.setdefault(day, {})[data_type] = values
                    fetched += 1
                start = chunk_end
            print(f"Google Health {data_type}: {fetched} days")
        except Exception as e:
            errors[data_type] = str(e)[:300]
            print(f"Google Health {data_type}: FAILED {e}")


def main():
    password = os.environ.get("DASHBOARD_PASSWORD") or None
    if os.environ.get("GITHUB_ACTIONS") and not password:
        sys.exit("DASHBOARD_PASSWORD secret is missing. Stopping so your data isn't published unencrypted.")
    existing = load_existing(password)
    before = copy.deepcopy(existing)   # days and workouts below are updated in place
    days: dict = existing.get("days", {})
    workouts: list = existing.get("workouts", [])
    errors, sources_ok = {}, []

    # 1) Google Health API (steps, heart rate, distance). Login lasts 7 days in Testing mode.
    if os.environ.get("GOOGLE_REFRESH_TOKEN"):
        try:
            fetch_google_health(days, workouts, errors)
            sources_ok.append("google-health")
        except SystemExit as e:          # expired / missing login: keep going with Drive
            errors["google-health"] = str(e)
            print(f"Google Health skipped: {e}")
        except Exception as e:
            errors["google-health"] = str(e)[:300]
            print(f"Google Health failed: {e}")

    # 2) Health Sync files on Google Drive (service account, never expires)
    drive_since = existing.get("driveSince")
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") and os.environ.get("HEALTH_SYNC_FOLDER_ID"):
        import drive_source
        started = dt.datetime.now(dt.timezone.utc)
        try:
            since = None
            if drive_since:
                since = (dt.datetime.fromisoformat(drive_since) - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
            counts = drive_source.collect(days, workouts, since)
            print(f"Health Sync (Drive): {counts}")
            drive_since = started.isoformat(timespec="seconds")
            sources_ok.append("health-sync")
        except Exception as e:
            errors["health-sync"] = str(e)[:300]
            print(f"Health Sync (Drive) failed: {e}")

    if not sources_ok:
        print("No data source worked. Errors: " + json.dumps(errors))
        sys.exit(1)

    # Height, birth date and sex stay in a secret and the encrypted file, never in the public code.
    profile = json.loads(os.environ.get("PROFILE_JSON") or "null") or existing.get("profile")
    new = {"days": dict(sorted(days.items())), "workouts": workouts[:500], "tz": str(TZ),
           "errors": errors, "sources": sources_ok, "profile": profile}
    old = {k: before.get(k) for k in new}
    if new == old and (ENC_FILE.exists() or PLAIN_FILE.exists()):
        print("No changes.")
    else:
        new["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        new["driveSince"] = drive_since
        save(new, password)
        print(f"Saved {len(days)} days, {len(workouts)} workouts ({'encrypted' if password else 'NOT encrypted'}).")
    if "google-health" in errors:
        # Still saved Drive data, but fail the run so GitHub emails you to renew the login.
        sys.exit("Google Health login needs renewing (see README). Health Sync data was still updated.")


if __name__ == "__main__":
    main()
