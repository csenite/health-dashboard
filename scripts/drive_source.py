"""
Reads the CSV files Health Sync uploads to Google Drive, using a Google service account
(never expires, only sees the folder you share with it).

Env:
  GOOGLE_SERVICE_ACCOUNT_JSON  the service account key (whole JSON)
  HEALTH_SYNC_FOLDER_ID        id of the "Health Sync" folder in Drive
"""
import csv
import io
import json
import os
import re

import requests

DRIVE = "https://www.googleapis.com/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    creds.refresh(Request())
    return creds.token


def _list(token, parent, folders=False):
    q = f"'{parent}' in parents and trashed = false"
    q += " and mimeType " + ("=" if folders else "!=") + " 'application/vnd.google-apps.folder'"
    out, page = [], None
    while True:
        params = {"q": q, "fields": "nextPageToken, files(id, name, modifiedTime, size)",
                  "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if page:
            params["pageToken"] = page
        r = requests.get(DRIVE, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        out += data.get("files", [])
        page = data.get("nextPageToken")
        if not page:
            return out


def _csv(token, file_id):
    r = requests.get(f"{DRIVE}/{file_id}", headers={"Authorization": f"Bearer {token}"},
                     params={"alt": "media"}, timeout=120)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    return [{head[i]: (c.strip() if i < len(row) else "") for i, c in enumerate(row[:len(head)])}
            for row in rows[1:] if any(x.strip() for x in row)]


def _num(v):
    try:
        x = float(str(v).replace('"', "").strip())
        return x
    except (TypeError, ValueError):
        return None


def _day(stamp):
    """'2026.11.09 07:52:02' -> '2026-11-09'"""
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", stamp or "")
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def _files(token, folders, name, since=None):
    fid = folders.get(name)
    if not fid:
        return []
    files = [f for f in _list(token, fid) if f["name"].lower().endswith(".csv")]
    if since:
        files = [f for f in files if f["modifiedTime"] >= since]
    return sorted(files, key=lambda f: f["modifiedTime"])


def collect(days: dict, workouts: list, since: str | None = None) -> dict:
    """Adds Health Sync data into `days` / `workouts` in place. Returns per-type counts.
    `since`: only read files modified after this ISO time (None = read everything)."""
    token = _token()
    root = os.environ["HEALTH_SYNC_FOLDER_ID"].strip()
    folders = {f["name"]: f["id"] for f in _list(token, root, folders=True)}
    counts = {}

    # Steps: Samsung Health files only (one source, so no double counting).
    per_minute = {}
    for f in _files(token, folders, "Health Sync Steps", since):
        if "samsung" not in f["name"].lower():
            continue
        for r in _csv(token, f["id"]):
            stamp, n = r.get("Date"), _num(r.get("Steps"))
            if stamp and n is not None:
                per_minute[stamp] = n          # same minute in daily/weekly/monthly files -> counted once
    totals = {}
    for stamp, n in per_minute.items():
        d = _day(stamp)
        if d:
            totals[d] = totals.get(d, 0) + n
    for d, n in totals.items():
        days.setdefault(d, {})["hs-steps"] = {"count": int(n)}
    counts["steps"] = len(totals)

    # Resting heart rate (one value per day)
    for f in _files(token, folders, "Health Sync Heart rate", since):
        if not f["name"].upper().startswith("RHR"):
            continue
        for r in _csv(token, f["id"]):
            d, v = _day(r.get("Date")), _num(r.get("Resting heart rate"))
            if d and v:
                days.setdefault(d, {})["hs-rhr"] = {"bpm": v}
                counts["rhr"] = counts.get("rhr", 0) + 1

    # Weight + body composition: first morning weigh-in of each day (from 4 am), which is the
    # steadiest reading to compare. Days with only a late-night reading keep that one.
    last = {}
    for f in _files(token, folders, "Health Sync Weight", since):
        for r in _csv(token, f["id"]):
            stamp, w = r.get("Date"), _num(r.get("Weight"))
            if stamp and w:
                last[stamp] = r
    by_day = {}
    # Preferred reading goes last so it wins. 0.0 means "not measured", so those keep another reading's value.
    for stamp in sorted(sorted(last, reverse=True), key=lambda s: s[11:13] >= "04"):
        d, r = _day(stamp), last[stamp]
        if not d:
            continue
        rec = by_day.setdefault(d, {})
        rec["kg"] = _num(r.get("Weight"))
        measured = bool(_num(r.get("Body fat percentage")))
        for key, col in (("bodyFatPct", "Body fat percentage"), ("waterKg", "Total body water"),
                         ("boneKg", "Bone mass"), ("bmr", "Base metabolic rate")):
            v = _num(r.get(col))
            # Readings without body fat carry a much lower BMR formula, so skip theirs.
            if v and (measured or key != "bmr"):
                rec[key] = v
    for d, rec in by_day.items():
        days.setdefault(d, {})["hs-weight"] = rec
    counts["weight"] = len(by_day)

    # VO2 max
    for f in _files(token, folders, "Health Sync VO2 max", since):
        for r in _csv(token, f["id"]):
            d, v = _day(r.get("Date")), _num(r.get("VO2 max"))
            if d and v:
                days.setdefault(d, {})["hs-vo2max"] = {"value": v}
                counts["vo2max"] = counts.get("vo2max", 0) + 1

    # Workouts: one summary CSV per activity
    known = {(w["type"], w["start"]) for w in workouts}
    for f in _files(token, folders, "Health Sync Activities", since):
        for r in _csv(token, f["id"]):
            start, kind = r.get("Date"), (r.get("Activity type") or "").upper()
            if not start or not kind or (kind, start) in known:
                continue
            known.add((kind, start))
            workouts.append({
                "type": kind, "start": start, "date": _day(start),
                "source": (r.get("Source app") or "").replace("\\_", "_"),
                "seconds": _num(r.get("Elapsed time")), "activeSeconds": _num(r.get("Active time")),
                # Some files label this column "miles", but speed x time shows the value is still km.
                "km": _num(r.get("Distance (km)") or r.get("Distance (miles)")),
                "kcal": _num(r.get("Calories (kcal)")),
                "steps": _num(r.get("Steps")), "avgHr": _num(r.get("Average heart rate")),
                "maxHr": _num(r.get("Max heart rate")),
            })
            counts["workouts"] = counts.get("workouts", 0) + 1
    workouts.sort(key=lambda w: w["start"], reverse=True)
    return counts
