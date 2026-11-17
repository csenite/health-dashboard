"""
Turns a workout's GPX file (Samsung Health, via Health Sync) into a small route record for the Runs tab.

Phone GPS drops out a lot, so the route keeps gaps as breaks instead of drawing straight lines
across them, and the distance leaves out impossible jumps (a lost signal that comes back far away).
Everything stays inside the encrypted data file.
"""
import datetime as dt
import math
import re
from zoneinfo import ZoneInfo

PT = re.compile(
    r'<trkpt lat="([-\d.]+)" lon="([-\d.]+)">(?:<ele>([-\d.]+)</ele>)?<time>([^<]+)</time>'
    r'(?:<extensions>.*?<gpxtpx:hr>(\d+)</gpxtpx:hr>.*?</extensions>)?', re.S)
TYPE = re.compile(r"<trk>\s*<name>([^<]*)</name>")
GAP_S = 20          # no fix for this long: break the line
JUMP_KMH = 25       # faster than this between two fixes on foot: GPS error, not movement
EVERY_S = 4         # keep about one point every 4 seconds


def _metres(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    x = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(x))


def parse_gpx(text: str, tz: ZoneInfo) -> dict | None:
    pts = []
    for la, lo, ele, t, hr in PT.findall(text):
        stamp = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        bpm = int(hr) if hr and int(hr) > 0 else None
        pts.append((float(la), float(lo), float(ele) if ele else None, stamp, bpm))
    if len(pts) < 10:
        return None
    t0 = pts[0][3]
    start = t0.astimezone(tz)
    kind = (TYPE.search(text)[1] if TYPE.search(text) else "WORKOUT").strip().upper() or "WORKOUT"

    # distance and GPS coverage
    km = 0.0
    covered = 0.0
    for a, b in zip(pts, pts[1:]):
        secs = (b[3] - a[3]).total_seconds()
        if secs <= 0:
            continue
        m = _metres(a, b)
        if m / secs * 3.6 > JUMP_KMH:
            continue
        km += m / 1000
        if secs <= GAP_S:
            covered += secs
    total = (pts[-1][3] - t0).total_seconds()

    # heart rate: time in each band, weighted by the seconds each reading covers (capped at a gap)
    bands = {"easy": 0.0, "steady": 0.0, "hard": 0.0, "light": 0.0}
    hrs = [p[4] for p in pts if p[4]]
    for a, b in zip(pts, pts[1:]):
        if not a[4]:
            continue
        secs = min((b[3] - a[3]).total_seconds(), GAP_S)
        key = "hard" if a[4] >= 150 else "steady" if a[4] >= 140 else "easy" if a[4] >= 120 else "light"
        bands[key] += secs

    # elevation gain on a smoothed profile
    eles = [p[2] for p in pts if p[2] is not None]
    gain = 0.0
    if len(eles) > 5:
        k = 15
        sm = [sum(eles[max(0, i - k):i + k + 1]) / len(eles[max(0, i - k):i + k + 1]) for i in range(len(eles))]
        gain = sum(max(0.0, sm[i] - sm[i - 1]) for i in range(1, len(sm)))

    # compact track: [metres east, metres north, seconds from start, bpm]; None marks a break
    lat0, lon0 = pts[0][0], pts[0][1]
    kx = 111320 * math.cos(math.radians(lat0))
    track, last_t, prev = [], None, None
    for p in pts:
        s = (p[3] - t0).total_seconds()
        if prev is not None:
            secs = (p[3] - prev[3]).total_seconds()
            jump = secs > 0 and _metres(prev, p) / secs * 3.6 > JUMP_KMH
            if secs > GAP_S or jump:
                track.append(None)
                last_t = None
        if last_t is None or s - last_t >= EVERY_S or p is pts[-1]:
            track.append([round((p[1] - lon0) * kx), round((p[0] - lat0) * 110540), round(s), p[4]])
            last_t = s
        prev = p

    return {
        "id": start.strftime("%Y-%m-%d %H:%M"),
        "date": start.strftime("%Y-%m-%d"),
        "type": kind,
        "seconds": round(total),
        "gpsKm": round(km, 2),
        "gpsCover": round(covered / total, 2) if total else 0,
        "avgHr": round(sum(hrs) / len(hrs)) if hrs else None,
        "maxHr": max(hrs) if hrs else None,
        "hrBands": {k: round(v) for k, v in bands.items()},
        "climbM": round(gain),
        "near": [round(lat0, 3), round(lon0, 3)],   # about 100 m precision, only used to group routes by place
        "track": track,
    }
