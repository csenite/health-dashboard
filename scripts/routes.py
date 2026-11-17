"""
Turns a workout file into a small route record for the Runs tab.

Reads .tcx (Samsung Health via Health Sync, and Google Fit Takeout) and .gpx files.
Phone GPS drops out a lot, so the route keeps gaps as breaks instead of drawing straight lines
across them, and GPS distance leaves out impossible jumps. Distance for pace and 1 km laps comes
from the watch when the file has it (lap total or per-point distance); across GPS gaps it is
filled in from cadence. Everything stays inside the encrypted data file.
"""
import datetime as dt
import io
import math
import re
import zipfile
from zoneinfo import ZoneInfo

GPX_PT = re.compile(
    r'<trkpt lat="([-\d.]+)" lon="([-\d.]+)">(?:<ele>([-\d.]+)</ele>)?<time>([^<]+)</time>'
    r'(?:<extensions>.*?<gpxtpx:hr>(\d+)</gpxtpx:hr>.*?</extensions>)?', re.S)
GPX_TYPE = re.compile(r"<trk>\s*<name>([^<]*)</name>")
TCX_TP = re.compile(r"<Trackpoint>(.*?)</Trackpoint>", re.S)
GAP_S = 20          # no fix for this long: break the line
JUMP_KMH = 25       # faster than this between two fixes on foot: GPS error, not movement
EVERY_S = 4         # keep about one point every 4 seconds


def _tag(block, name):
    m = re.search(rf"<{name}>\s*([^<]+?)\s*</{name}>", block)
    return m[1] if m else None


def _metres(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    x = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(x))


def _time(s):
    return dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))


def parse_gpx(text: str, tz: ZoneInfo) -> dict | None:
    pts = []
    for la, lo, ele, t, hr in GPX_PT.findall(text):
        pts.append({"lat": float(la), "lon": float(lo), "ele": float(ele) if ele else None,
                    "t": _time(t), "hr": int(hr) if hr and int(hr) > 0 else None})
    kind = (GPX_TYPE.search(text)[1] if GPX_TYPE.search(text) else "") or "WORKOUT"
    return build(pts, kind, tz, source="gpx")


def parse_tcx(text: str, tz: ZoneInfo) -> dict | None:
    sport = re.search(r'<Activity Sport="([^"]+)"', text)
    kind = (sport[1] if sport else "WORKOUT")
    # lap totals sit before the track (Samsung) or after it (Google Fit), so read them with the tracks removed
    outside = re.sub(r"<Track>.*?</Track>", "", text, flags=re.S)
    laps = re.findall(r"<Lap\b.*?</Lap>", outside, re.S)
    laps_m = [float(_tag(l, "DistanceMeters")) for l in laps if _tag(l, "DistanceMeters")]
    laps_s = [float(_tag(l, "TotalTimeSeconds")) for l in laps if _tag(l, "TotalTimeSeconds")]
    samsung = "Samsung" in text[:3000]
    pts = []
    for b in TCX_TP.findall(text):
        t = _tag(b, "Time")
        if not t:
            continue
        la, lo = _tag(b, "LatitudeDegrees"), _tag(b, "LongitudeDegrees")
        hr = re.search(r"<HeartRateBpm>\s*<Value>(\d+)</Value>", b)
        cad = _tag(b, "Cadence") or _tag(b, "ns3:RunCadence") or _tag(b, "RunCadence")
        d = re.search(r"<DistanceMeters>([^<]+)</DistanceMeters>", b)
        pts.append({"lat": float(la) if la else None, "lon": float(lo) if lo else None,
                    "ele": float(_tag(b, "AltitudeMeters")) if _tag(b, "AltitudeMeters") else None,
                    "t": _time(t), "hr": int(hr[1]) if hr and int(hr[1]) > 0 else None,
                    # running cadence in TCX counts one foot, so steps per minute is double
                    "cad": float(cad) * 2 if cad and float(cad) > 0 else None,
                    "dist": float(d[1]) if d else None})
    return build(pts, kind, tz, source="tcx-samsung" if samsung else "tcx",
                 total_m=sum(laps_m) or None, active_s=sum(laps_s) or None)


def build(pts, kind, tz, source, total_m=None, active_s=None) -> dict | None:
    pts = sorted(pts, key=lambda p: p["t"])
    geo = [p for p in pts if p["lat"] is not None]
    if len(geo) < 10:
        return None
    t0 = pts[0]["t"]
    start = geo[0]["t"].astimezone(tz)
    total = (pts[-1]["t"] - t0).total_seconds()
    kind = kind.strip().upper().replace(" ", "_") or "WORKOUT"

    # GPS distance and coverage (a session with under 500 m of usable GPS has no route worth drawing)
    gps_m, covered = 0.0, 0.0
    for a, b in zip(geo, geo[1:]):
        secs = (b["t"] - a["t"]).total_seconds()
        if secs <= 0:
            continue
        m = _metres((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        if m / secs * 3.6 <= JUMP_KMH:
            gps_m += m
            if secs <= GAP_S:
                covered += secs

    if gps_m < 500:
        return None

    # distance along the session, for laps: per-point watch distance if present, else GPS where it's
    # good and cadence (or the average speed) across gaps, scaled to the watch's total when known
    cum = [0.0]
    if sum(1 for p in pts if p.get("dist") is not None) > len(pts) * 0.8:
        # Google Fit splits a session into many short activities whose distance restarts at 0
        last = pts[0].get("dist") or 0.0
        for p in pts[1:]:
            d = p.get("dist")
            if d is None:
                cum.append(cum[-1]); continue
            cum.append(cum[-1] + (d - last if d >= last else d))
            last = d
    else:
        good_m = good_steps = 0.0
        seg = []
        for a, b in zip(pts, pts[1:]):
            secs = max(0.0, (b["t"] - a["t"]).total_seconds())
            m = None
            if a["lat"] is not None and b["lat"] is not None and 0 < secs <= GAP_S:
                mm = _metres((a["lat"], a["lon"]), (b["lat"], b["lon"]))
                if mm / secs * 3.6 <= JUMP_KMH:
                    m = mm
            cad = a.get("cad") or b.get("cad")
            steps = cad * secs / 60 if cad else None
            if m is not None and steps:
                good_m += m; good_steps += steps
            seg.append((m, steps, secs))
        stride = good_m / good_steps if good_steps > 50 else None
        speed = gps_m / covered if covered > 30 and total_m else None   # only bridge gaps by speed when a total can rescale it
        for m, steps, secs in seg:
            if m is None:
                m = steps * stride if steps and stride else (speed * secs if speed else 0.0)
            cum.append(cum[-1] + m)
    if total_m and cum[-1] > 0:   # the watch's own total wins; spread it over the session in proportion
        cum = [c * total_m / cum[-1] for c in cum]
    dist_m = total_m or cum[-1] or gps_m

    # 1 km laps
    splits, mark, i0 = [], 1000.0, 0
    for i, c in enumerate(cum):
        while c >= mark:
            # interpolate the moment the km was passed
            a, b = cum[i - 1] if i else 0.0, c
            ta, tb = (pts[i - 1]["t"] - t0).total_seconds() if i else 0.0, (pts[i]["t"] - t0).total_seconds()
            tm = ta + (tb - ta) * ((mark - a) / (b - a) if b > a else 1)
            prev_t = sum(s["s"] for s in splits)
            hr = [p["hr"] for p in pts[i0:i + 1] if p["hr"]]
            splits.append({"km": len(splits) + 1, "s": round(tm - prev_t), "hr": round(sum(hr) / len(hr)) if hr else None})
            mark += 1000
            i0 = i
    rest = cum[-1] - (mark - 1000)
    moved = max((i for i in range(1, len(cum)) if cum[i] > cum[i - 1]), default=len(cum) - 1)
    end_s = (pts[moved]["t"] - t0).total_seconds()   # when the distance last grew, not when recording stopped
    if rest >= 100 and end_s > sum(s["s"] for s in splits):
        hr = [p["hr"] for p in pts[i0:] if p["hr"]]
        splits.append({"km": round((mark - 1000 + rest) / 1000, 2), "s": round(end_s - sum(s["s"] for s in splits)),
                       "hr": round(sum(hr) / len(hr)) if hr else None, "part": round(rest)})

    # heart rate: time in each band, weighted by the seconds each reading covers (capped at a gap)
    bands = {"light": 0.0, "easy": 0.0, "steady": 0.0, "hard": 0.0}
    hrs = [p["hr"] for p in pts if p["hr"]]
    for a, b in zip(pts, pts[1:]):
        if a["hr"]:
            secs = min((b["t"] - a["t"]).total_seconds(), GAP_S)
            key = "hard" if a["hr"] >= 150 else "steady" if a["hr"] >= 140 else "easy" if a["hr"] >= 120 else "light"
            bands[key] += secs
    cads = [p["cad"] for p in pts if p.get("cad")]

    # elevation gain on a smoothed profile
    eles = [p["ele"] for p in geo if p["ele"] is not None]
    gain = 0.0
    if len(eles) > 5:
        k = 15
        sm = [sum(eles[max(0, i - k):i + k + 1]) / len(eles[max(0, i - k):i + k + 1]) for i in range(len(eles))]
        gain = sum(max(0.0, sm[i] - sm[i - 1]) for i in range(1, len(sm)))

    # compact track: [metres east, metres north, seconds from start, bpm, steps/min]; None marks a break
    lat0, lon0 = geo[0]["lat"], geo[0]["lon"]
    kx = 111320 * math.cos(math.radians(lat0))
    track, last_t, prev = [], None, None
    for p in pts:
        s = (p["t"] - t0).total_seconds()
        if p["lat"] is None:
            if p["hr"] and (last_t is None or s - last_t >= EVERY_S):   # heart rate without a fix: keep for the chart
                track.append([None, None, round(s), p["hr"], round(p["cad"]) if p.get("cad") else None])
                last_t = s
            continue
        if prev is not None:
            secs = (p["t"] - prev["t"]).total_seconds()
            jump = secs > 0 and _metres((prev["lat"], prev["lon"]), (p["lat"], p["lon"])) / secs * 3.6 > JUMP_KMH
            if secs > GAP_S or jump:
                track.append(None)
                last_t = None
        if last_t is None or s - last_t >= EVERY_S or p is geo[-1]:
            track.append([round((p["lon"] - lon0) * kx), round((p["lat"] - lat0) * 110540), round(s), p["hr"],
                          round(p["cad"]) if p.get("cad") else None])
            last_t = s
        prev = p

    moving = active_s if active_s and active_s <= total * 1.05 else total
    return {
        "id": start.strftime("%Y-%m-%d %H:%M"),
        "date": start.strftime("%Y-%m-%d"),
        "type": kind,
        "source": source,
        "seconds": round(total),
        "activeSeconds": round(moving),
        "km": round(dist_m / 1000, 2),
        "kmFrom": "watch" if total_m else "gps",
        "gpsKm": round(gps_m / 1000, 2),
        "gpsCover": round(covered / total, 2) if total else 0,
        "paceS": round(moving / (dist_m / 1000)) if dist_m > 100 else None,
        "cadence": round(sum(cads) / len(cads)) if cads else None,
        "avgHr": round(sum(hrs) / len(hrs)) if hrs else None,
        "maxHr": max(hrs) if hrs else None,
        "hrBands": {k: round(v) for k, v in bands.items()},
        "climbM": round(gain),
        "splits": splits,
        "near": [round(lat0, 3), round(lon0, 3)],   # about 100 m precision, only used to group routes by place
        "track": track,
    }


def parse_file(name: str, data: bytes, tz: ZoneInfo) -> dict | None:
    text = data.decode("utf-8", errors="replace")
    return parse_tcx(text, tz) if name.lower().endswith(".tcx") else parse_gpx(text, tz)


RUNS = re.compile(r"(?i)running|jogging|treadmill")


def parse_takeout(data: bytes, tz: ZoneInfo, pattern=RUNS) -> list:
    """Google Fit activities from a Takeout zip (Takeout/Fit/Activities/*.tcx). Only sessions whose
    name matches `pattern` (runs and walk-runs by default) and that have GPS."""
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if not n.lower().endswith(".tcx") or "/activities/" not in n.lower() or not pattern.search(n.rsplit("/", 1)[-1]):
                continue
            r = parse_tcx(z.read(n).decode("utf-8", errors="replace"), tz)
            if r:
                r["source"] = "google-fit"
                # the file name lists what the session held, e.g. "..._Walking,Running.tcx"
                kinds = n.rsplit("_", 1)[-1][:-4].lower()
                r["type"] = "RUNNING" if kinds == "running" else "WALK_RUN"
                out.append(r)
    return out


RANK = {"tcx-samsung": 3, "tcx": 2, "google-fit": 2, "gpx": 1}


def better(new: dict, old: dict | None) -> bool:
    """Same workout saved twice (Samsung .tcx and .gpx, or Health Sync's two file names): keep the richer one."""
    if not old:
        return True
    return (RANK.get(new.get("source"), 0), len(new["track"])) > (RANK.get(old.get("source"), 0), len(old["track"]))
