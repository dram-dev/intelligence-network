"""A warning, drawn: the picture on an alert card.

The card has to hold its own next to a radar app, so the picture is drawn the way one
is: one radar's lowest sweep at full resolution (the pack's `card_radar`: for weather,
NEXRAD Level III super-resolution reflectivity, 0.5° × 250 m, read by nexrad.py), on a
smooth colour scale over a dark map; the warning outlined on top in its own colour (the
pack's `alert_colours`); the storm's projected track with ten-minute time marks, from
the NWS storm motion; what people and spotters reported nearby in the last two hours
(people at their ZIP's centre, as everywhere public); and the reader's own place, with
when the storm reaches it. An alert without a polygon is drawn as its counties, named.

A picture is named by what it shows (the alert version, the place, the radar sweep, the
reports), rendered once with Pillow and numpy and kept for a day and a half under
data/cardmaps/. Telegram gets the file once: the file id it hands back
(kv `tgfile:<name>`) serves every later card showing the same picture.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from intelnet import db, geo, nexrad
from intelnet.config import CONFIG_DIR, PROJECT_ROOT, settings
from intelnet.models import Signal, local_time, utcnow
from intelnet.topics import find_metric, get_topic

logger = logging.getLogger(__name__)

W, H = 1080, 720                    # 3:2, what a phone shows at full width
SS = 2                              # vector layers are drawn twice as large, then scaled down: smooth edges
EARTH = 6378137.0
ASSETS = PROJECT_ROOT / "site" / "assets"
FONT = CONFIG_DIR / "fonts" / "IBMPlexSans.ttf"
KEEP = timedelta(hours=36)          # pictures (and radar files) on disk
NEAR_KM = 45                        # a place this close to the warning is inside the frame
TRACK_STEP, TRACK_MAX = 10, 60      # the storm's track: a mark every 10 minutes, up to an hour
REPORT_HOURS = 2                    # reports drawn: the last two hours
PHOTO = re.compile(r"tg://photo\?id=([A-Za-z0-9_-]{1,64})")
VIDEO = re.compile(r"tg://video\?id=([A-Za-z0-9_-]{1,64})")
LOOP_MINUTES = 45                   # a card's loop: the last three quarters of an hour of radar

# A dark map, so the radar reads the way it does in a radar app.
BEYOND, LAND = (16, 20, 23), (27, 33, 37)
WATER, RIVER = (21, 43, 55), (40, 82, 104)
ROAD_I, ROAD_US = (78, 70, 54), (54, 57, 53)
COUNTY = (60, 72, 68)
TEXT, TEXT_DIM, HALO = (232, 237, 234), (168, 180, 174), (9, 12, 14)
PEOPLE, SPOTTER, YOU = (96, 205, 255), (255, 255, 255), (10, 132, 255)
SEVERITY = {"Extreme": (255, 59, 48), "Severe": (255, 176, 32), "Moderate": (240, 170, 80),
            "Minor": (120, 190, 210), "Unknown": (170, 180, 175)}


def _hex(value: str) -> tuple[int, int, int, int]:
    v = value.lstrip("#")
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16), int(v[6:8], 16) if len(v) >= 8 else 255)


# ── geometry ─────────────────────────────────────────────────────────────

def _merc(lat: float, lon: float) -> tuple[float, float]:
    lat = max(min(lat, 85.0), -85.0)
    return math.radians(lon) * EARTH, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * EARTH


def _unmerc_lat(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y / EARTH)) - math.pi / 2)


@dataclass(frozen=True)
class Frame:
    """The picture's extent in Web Mercator metres; y1 is north."""
    x0: float
    y0: float
    x1: float
    y1: float

    def px(self, lat: float, lon: float, scale: int = 1) -> tuple[float, float]:
        x, y = _merc(lat, lon)
        return ((x - self.x0) / (self.x1 - self.x0) * W * scale,
                (self.y1 - y) / (self.y1 - self.y0) * H * scale)

    def contains(self, lat: float, lon: float, margin: float = 0.0) -> bool:
        x, y = self.px(lat, lon)
        return -margin * W <= x <= (1 + margin) * W and -margin * H <= y <= (1 + margin) * H

    @property
    def km_per_px(self) -> float:
        """Ground kilometres per pixel at the picture's middle latitude."""
        lat = _unmerc_lat((self.y0 + self.y1) / 2)
        return (self.x1 - self.x0) / W * math.cos(math.radians(lat)) / 1000

    def bbox(self) -> tuple[float, float, float, float]:
        """(south, west, north, east) in degrees."""
        return (_unmerc_lat(self.y0), math.degrees(self.x0 / EARTH),
                _unmerc_lat(self.y1), math.degrees(self.x1 / EARTH))

    def grid(self) -> tuple[np.ndarray, np.ndarray]:
        """Latitude and longitude of every pixel's centre."""
        xs = self.x0 + (np.arange(W) + 0.5) / W * (self.x1 - self.x0)
        ys = self.y1 - (np.arange(H) + 0.5) / H * (self.y1 - self.y0)
        lons = np.degrees(xs / EARTH)
        lats = np.degrees(2 * np.arctan(np.exp(ys / EARTH)) - np.pi / 2)
        return np.broadcast_to(lats[:, None], (H, W)), np.broadcast_to(lons[None, :], (H, W))


def frame_for(points: list[tuple[float, float]], *, pad: float = 0.2, min_km: float = 50.0) -> Frame:
    """The smallest 3:2 frame around these (lat, lon) points, with a margin, at least
    `min_km` across (a small warning still shows the towns around it)."""
    xs, ys = zip(*(_merc(la, lo) for la, lo in points), strict=True)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    k = 1 / math.cos(math.radians(_unmerc_lat(cy)))                # Mercator metres per ground metre
    w = max((max(xs) - min(xs)) * (1 + 2 * pad), min_km * 1000 * k)
    h = max((max(ys) - min(ys)) * (1 + 2 * pad), min_km * 1000 * k * H / W)
    w, h = (h * W / H, h) if w / h < W / H else (w, w * H / W)
    return Frame(round(cx - w / 2), round(cy - h / 2), round(cx + w / 2), round(cy + h / 2))


@lru_cache(maxsize=1)
def _counties() -> dict[str, list[list[list[float]]]]:
    """County FIPS → outer rings ([lon, lat] pairs), from the site's county map."""
    try:
        data = json.loads((ASSETS / "il-counties.geojson").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, list[list[list[float]]]] = {}
    for f in data.get("features", []):
        g, fips = f.get("geometry") or {}, str((f.get("properties") or {}).get("fips") or "")
        polys = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates") or []
        out[fips] = [poly[0] for poly in polys if poly]
    return out


@lru_cache(maxsize=1)
def _reference() -> dict[str, Any]:
    """Rivers, lakes, roads and towns (scripts/build_map_layers.py)."""
    try:
        return json.loads((ASSETS / "il-reference.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=16)
def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        f = ImageFont.truetype(str(FONT), size)
        f.set_variation_by_name(weight)
        return f
    except (OSError, ValueError):
        return ImageFont.load_default(size=size)


# ── what a picture shows ─────────────────────────────────────────────────

@dataclass
class Mark:
    """The reader's place on the picture."""
    lat: float
    lon: float
    label: str                      # "You" · "Home" · "ZIP 62704"
    note: str = ""                  # "storm ~6:19 PM"


@dataclass
class Report:
    """A reading drawn on the picture: a person's (at their ZIP's centre) or a spotter's."""
    lat: float
    lon: float
    text: str                       # "1.75 in" · "Tornado"
    official: bool


@dataclass
class Scene:
    rings: list[list[list[float]]]                    # warned area, [lon, lat] pairs
    colour: tuple[int, int, int]
    counties: bool = False                            # the rings are whole counties (no polygon)
    motion: dict[str, Any] | None = None
    mark: Mark | None = None
    version: str = ""
    extra: list[str] = field(default_factory=list)    # the counties' FIPS, for a county alert
    topic: str = "weather"
    reports: list[Report] = field(default_factory=list)

    def name(self, slot: str) -> str:
        """What the picture shows, as a file name: same scene, same picture."""
        m = self.mark
        spec = [self.version, len(self.rings), self.counties, slot, self.extra,
                [round(m.lat, 3), round(m.lon, 3), m.label, m.note] if m else None,
                [[round(r.lat, 3), round(r.lon, 3), r.text] for r in self.reports]]
        return "wm-" + hashlib.sha1(json.dumps(spec, default=str).encode()).hexdigest()[:20]


def _colour(topic: str, event: str, severity: str) -> tuple[int, int, int]:
    own = get_topic(topic).alert_colours.get(event)
    return _hex(own)[:3] if own else SEVERITY.get(severity, SEVERITY["Unknown"])


def scene(sig: Signal, siblings: list[Signal], mark: Mark | None) -> Scene | None:
    """The warned area of an alert (its polygon, or else the counties it names), with the
    reports around it."""
    ev = sig.evidence
    colour = _colour(sig.topic, str(ev.get("event") or ""), str(ev.get("severity") or "Unknown"))
    version = str(ev.get("alert_id") or sig.key)
    rings = ev.get("polygon")
    if rings:
        sc = Scene(rings, colour, motion=ev.get("motion"), mark=mark, version=version, topic=sig.topic)
    else:
        shapes = _counties()
        fips = sorted({s.location.county_fips for s in (siblings or [sig]) if s.location.county_fips})
        county_rings = [r for f in fips for r in shapes.get(f, [])]
        if not county_rings:
            return None
        sc = Scene(county_rings, colour, counties=True, mark=mark, version=version, extra=fips, topic=sig.topic)
    sc.reports = reports_near(frame_for(_points(sc)), sig.topic)
    return sc


def _points(sc: Scene) -> list[tuple[float, float]]:
    pts = [(lat, lon) for ring in sc.rings for lon, lat in ring]
    if sc.motion:
        pts += [(p[0], p[1]) for p in sc.motion.get("points") or []]
    if sc.mark and min(geo.haversine_km(sc.mark.lat, sc.mark.lon, la, lo) for la, lo in pts) <= NEAR_KM:
        pts.append((sc.mark.lat, sc.mark.lon))
    return pts


def reports_near(f: Frame, topic: str, *, hours: float = REPORT_HOURS, limit: int = 14) -> list[Report]:
    """What people and spotters reported in the frame lately: the strongest reading of each
    kind at each place. People are placed at their ZIP's centre, never where they stood."""
    from intelnet.opendata import public_point

    best: dict[tuple[float, float, str], tuple[float, Report]] = {}
    for s in db.recent_signals(hours, topic=topic, kinds=("human", "official"), limit=2000):
        if s.quality == "rejected" or s.metric.startswith("alert."):
            continue
        m = find_metric(s.metric)
        if m is None or not m.scored:
            continue
        official = s.sensor_kind == "official"
        at = (s.location.lat, s.location.lon) if official else public_point(s.location.zip5, s.location.county_fips)
        if not at or at[0] is None or not f.contains(at[0], at[1], -0.02):
            continue
        text = m.label if m.is_flag else m.display(s.value)
        weight = m.severity(s.value if s.value is not None else 1.0) + (0.01 if official else 0)
        key = (round(at[0], 3), round(at[1], 3), s.metric)
        if key not in best or weight > best[key][0]:
            best[key] = (weight, Report(at[0], at[1], text[:16], official))
    return [r for _, r in sorted(best.values(), key=lambda wr: -wr[0])[:limit]]


# ── radar ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=8)
def _lut(topic: str) -> np.ndarray:
    """The pack's colour scale as 256 RGBA rows, one per half dBZ from −32 (the byte scale
    Level III uses), interpolated between the stops."""
    stops = [(float(v), _hex(c)) for v, c in get_topic(topic).card_radar.get("colours") or []]
    lut = np.zeros((256, 4), dtype=np.uint8)
    if not stops:
        return lut
    values = -32 + np.arange(256) * 0.5
    xs = [v for v, _ in stops]
    for ch in range(4):
        col = np.interp(values, xs, [c[ch] for _, c in stops])
        lut[:, ch] = np.clip(np.round(col), 0, 255)
    lut[values < xs[0], 3] = 0
    return lut


def _site(topic: str, f: Frame) -> tuple[str, str] | None:
    """The radar nearest the frame's middle: (id, name)."""
    sites = get_topic(topic).card_radar.get("sites") or {}
    if not sites:
        return None
    s, w, n, e = f.bbox()
    lat, lon = (s + n) / 2, (w + e) / 2
    sid = min(sites, key=lambda k: geo.haversine_km(lat, lon, float(sites[k][0]), float(sites[k][1])))
    return sid, str(sites[sid][2]) if len(sites[sid]) > 2 else sid


_KEYS: dict[str, tuple[float, list[str]]] = {}


def radar_keys(topic: str, f: Frame, minutes: float) -> list[str]:
    """The nearest radar's sweeps from the last `minutes`, oldest first (listings are
    reused for a minute)."""
    site = _site(topic, f)
    product = get_topic(topic).card_radar.get("product")
    if site is None or not product:
        return []
    cache_key = f"{site[0]}:{product}:{minutes}"
    hit = _KEYS.get(cache_key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    now = utcnow()
    found = nexrad.keys(site[0], str(product), now - timedelta(minutes=minutes), now)
    _KEYS[cache_key] = (time.time(), found)
    return found


_LAYERS: dict[tuple[str, Frame], Image.Image | None] = {}


def radar_layer(topic: str, key: str, f: Frame) -> Image.Image | None:
    """One sweep, sampled at every pixel of the frame and coloured: an RGBA layer."""
    hit = _LAYERS.get((key, f))
    if hit is not None:
        return hit
    raw = nexrad.fetch(key, _dir().parent / "radar")
    if raw is None:
        return None
    try:
        sweep = nexrad.decode(raw)
    except (ValueError, OSError, EOFError) as exc:
        logger.info("cardmap: can't read %s (%s)", key, type(exc).__name__)
        return None
    lats, lons = f.grid()
    values = nexrad.sample(sweep, lats, lons)
    values = _quality(topic, key, values, lats, lons)
    idx = np.clip(np.round((np.nan_to_num(values, nan=-40.0) + 32) * 2), 0, 255).astype(np.uint8)
    layer = Image.fromarray(_lut(topic)[idx], "RGBA")
    if len(_LAYERS) > 48:
        _LAYERS.clear()
    _LAYERS[(key, f)] = layer
    return layer


def _quality(topic: str, key: str, values: np.ndarray, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Drop what isn't weather, by the pack's `quality` product from the same scan (for
    weather: the correlation coefficient). Where it's unknown, or the echo is strong,
    the echo stays."""
    cfg = get_topic(topic).card_radar
    q = cfg.get("quality") or {}
    if not q.get("product"):
        return values
    raw = nexrad.fetch(key.replace(f"_{cfg.get('product')}_", f"_{q['product']}_"), _dir().parent / "radar")
    if raw is None:
        return values
    try:
        cc = nexrad.sample(nexrad.decode(raw), lats, lons, floor=None)
    except (ValueError, OSError, EOFError, struct.error):
        return values
    below, strong = float(q.get("below", 0.85)), float(q.get("keep_above", 40))
    doubtful = cc < below
    # a strong echo that isn't rain stays only inside a storm (a tornado's debris); out in
    # clear air it's a wind farm or ground clutter, and it would look like a storm core
    storm = _within(~doubtful & (values >= 30), float(q.get("storm_km", 3)) / _km_per_px(lats, lons))
    drop = doubtful & ~((values >= strong) & storm)
    kept = np.where(drop, np.nan, values)
    return _fill_holes(kept, drop, float(q.get("fill_km", 1.5)) / _km_per_px(lats, lons))


def _box(a: np.ndarray, r: int) -> np.ndarray:
    """The sum of each pixel's (2r+1)² neighbourhood (summed-area table)."""
    s = np.pad(a, ((r + 1, r), (r + 1, r))).cumsum(0).cumsum(1)
    n = 2 * r + 1
    return s[n:, n:] - s[:-n, n:] - s[n:, :-n] + s[:-n, :-n]


def _fill_holes(values: np.ndarray, dropped: np.ndarray, radius_px: float) -> np.ndarray:
    """Clutter under rain leaves holes when it's dropped; a radar app fills them from the
    rain around. Only where most of the neighbourhood is weather: clutter in clear air stays
    gone."""
    r = max(2, int(radius_px))
    valid = ~np.isnan(values) & (values >= 15)
    total = _box(valid.astype(np.float64), r)
    mean = _box(np.where(valid, values, 0.0), r) / np.maximum(total, 1)
    area = (2 * r + 1) ** 2
    empty = np.isnan(values) | (values < 15)             # nothing seen: spokes by the radar, bins it lost
    holes = (dropped & (total >= 0.55 * area)) | (empty & (total >= 0.8 * area))
    return np.where(holes, mean, values).astype(np.float32)


def _km_per_px(lats: np.ndarray, lons: np.ndarray) -> float:
    return float(abs(lons[0, 1] - lons[0, 0]) * 111.32 * math.cos(math.radians(float(lats[lats.shape[0] // 2, 0]))))


def _within(mask: np.ndarray, radius_px: float) -> np.ndarray:
    """Every pixel within `radius_px` of a solid area of the mask. Worked on an eighth-size
    grid: a cell counts when most of it is masked, lone cells are dropped (scattered clutter
    passes the rain test here and there; a storm is solid), and what's left is grown."""
    from PIL import ImageFilter

    step = 8
    h, w = mask.shape
    solid = mask.reshape(h // step, step, w // step, step).mean(axis=(1, 3)) >= 0.6
    small = Image.fromarray((solid * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(3))
    size = max(3, int(radius_px / step) * 2 + 1)
    grown = np.asarray(small.filter(ImageFilter.MaxFilter(size))) > 0
    return np.repeat(np.repeat(grown, step, axis=0), step, axis=1)[:h, :w]


# ── drawing ──────────────────────────────────────────────────────────────

def _path(f: Frame, ring: list[list[float]], scale: int = SS) -> list[tuple[float, float]]:
    return [f.px(lat, lon, scale) for lon, lat in ring]


def _near(f: Frame, coords: list[list[float]], margin: float = 0.1) -> bool:
    """Does this line or ring's bounding box reach the frame (plus a margin)?"""
    s, w, n, e = f.bbox()
    dy, dx = (n - s) * margin, (e - w) * margin
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons) <= e + dx and max(lons) >= w - dx and min(lats) <= n + dy and max(lats) >= s - dy


_BASES: dict[tuple[Frame, str, int], Image.Image] = {}


def _base(f: Frame, sc: Scene) -> Image.Image:
    """Land, water, roads, county lines and the warning's tint, at the output size (kept:
    every frame of a loop, and every reader of a warning in the same frame, shares it)."""
    key = (f, sc.version, len(sc.rings))
    if key not in _BASES:
        if len(_BASES) > 16:
            _BASES.clear()
        _BASES[key] = _draw_base(f, sc)
    return _BASES[key].copy()


def _draw_base(f: Frame, sc: Scene) -> Image.Image:
    img = Image.new("RGB", (W * SS, H * SS), BEYOND)
    d = ImageDraw.Draw(img, "RGBA")
    shapes = _counties()
    for rings in shapes.values():
        for ring in rings:
            if _near(f, ring, 0.5):
                d.polygon(_path(f, ring), fill=LAND)
    ref = _reference()
    for wt in ref.get("water", []):
        for ring in wt.get("rings", []):
            if _near(f, ring):
                d.polygon(_path(f, ring), fill=WATER)
    for rv in ref.get("rivers", []):
        for line in rv.get("lines", []):
            if len(line) > 1 and _near(f, line):
                d.line(_path(f, line), fill=RIVER, width=3, joint="curve")
    for rd in sorted(ref.get("roads", []), key=lambda r: r.get("type") == "I"):
        interstate = rd.get("type") == "I"
        for line in rd.get("lines", []):
            if len(line) > 1 and _near(f, line):
                d.line(_path(f, line), fill=ROAD_I if interstate else ROAD_US, width=5 if interstate else 3,
                       joint="curve")
    for rings in shapes.values():
        for ring in rings:
            if _near(f, ring, 0.5):
                d.line(_path(f, ring) + _path(f, ring[:1]), fill=COUNTY, width=3, joint="curve")
    for ring in sc.rings:                        # the warned area's tint, under the radar
        d.polygon(_path(f, ring), fill=(*sc.colour, 20 if sc.counties else 11))
    return img.resize((W, H), Image.Resampling.LANCZOS)


def _dashes(d: ImageDraw.ImageDraw, pts: list[tuple[float, float]], *, dash: float, gap: float,
            fill: tuple[int, ...], width: int) -> None:
    for (x0, y0), (x1, y1) in pairwise(pts):
        length = math.hypot(x1 - x0, y1 - y0)
        at = 0.0
        while at < length:
            a, b = at / length, min(at + dash, length) / length
            d.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)],
                   fill=fill, width=width)
            at += dash + gap


def track(sc: Scene, f: Frame) -> list[tuple[int, float, float]]:
    """The storm's projected path: (minutes from the motion's time, lat, lon) every
    TRACK_STEP minutes while it's in the frame, starting where it was."""
    m = sc.motion
    if not m or not m.get("points") or not m.get("speed_kt"):
        return []
    pts = m["points"]
    lat = sum(p[0] for p in pts) / len(pts)
    lon = sum(p[1] for p in pts) / len(pts)
    heading = math.radians((float(m["from_deg"]) + 180) % 360)
    kmh = float(m["speed_kt"]) * 1.852
    out = []
    for minutes in range(0, TRACK_MAX + 1, TRACK_STEP):
        km = kmh * minutes / 60
        la = lat + km * math.cos(heading) / 110.57
        lo = lon + km * math.sin(heading) / (111.32 * math.cos(math.radians(lat)))
        if minutes and not f.contains(la, lo, -0.03):
            break
        out.append((minutes, la, lo))
    return out


class _Labels:
    """Text placed so no two labels overlap (greedy, most important first)."""

    def __init__(self, d: ImageDraw.ImageDraw) -> None:
        self.d = d
        self.taken: list[tuple[float, float, float, float]] = []

    def free(self, box: tuple[float, float, float, float]) -> bool:
        x0, y0, x1, y1 = box
        if x0 < 8 or y0 < 8 or x1 > W - 8 or y1 > H - 8:
            return False
        return not any(x0 < b[2] and b[0] < x1 and y0 < b[3] and b[1] < y1 for b in self.taken)

    def put(self, xy: tuple[float, float], text: str, font: Any, *, fill: tuple[int, ...] = TEXT,
            halo: tuple[int, ...] = HALO, stroke: int = 4, anchor: str = "lm", force: bool = False) -> bool:
        box = self.d.textbbox(xy, text, font=font, anchor=anchor, stroke_width=stroke)
        if not force and not self.free(box):
            return False
        self.d.text(xy, text, font=font, anchor=anchor, fill=fill, stroke_width=stroke, stroke_fill=halo)
        self.taken.append(box)
        return True

    def block(self, x: float, y: float, lines: list[tuple[str, Any, tuple[int, ...]]], gap: float) -> None:
        """Lines stacked beside a point, all on one side: right if they fit, else left."""
        for side in (1, -1):
            boxes, top = [], y - sum(self.d.textbbox((0, 0), s, font=f)[3] for s, f, _ in lines) / 2 - 4
            for text, font, _ in lines:
                anchor = "lt" if side > 0 else "rt"
                box = self.d.textbbox((x + side * gap, top), text, font=font, anchor=anchor, stroke_width=4)
                boxes.append(((x + side * gap, top), anchor, box))
                top = box[3] + 2
            if all(self.free(b) for *_, b in boxes) or side < 0:
                for (xy, anchor, box), (text, font, fill) in zip(boxes, lines, strict=True):
                    self.d.text(xy, text, font=font, anchor=anchor, fill=fill, stroke_width=4, stroke_fill=HALO)
                    self.taken.append(box)
                return

    def near(self, x: float, y: float, text: str, font: Any, gap: float = 14, **kw: Any) -> bool:
        """Right of a point, else left, above, below."""
        return any(self.put(xy, text, font, anchor=anchor, **kw) for xy, anchor in (
            ((x + gap, y), "lm"), ((x - gap, y), "rm"), ((x, y - gap), "mb"), ((x, y + gap), "mt")))


def _pill(d: ImageDraw.ImageDraw, box: tuple[float, float, float, float]) -> None:
    d.rounded_rectangle(box, radius=12, fill=(8, 11, 13, 190), outline=(255, 255, 255, 28), width=1)


def _scale_bar(d: ImageDraw.ImageDraw, f: Frame, labels: _Labels) -> None:
    target = W * 0.14 * f.km_per_px / 1.609344                     # miles in ~14% of the width
    miles = next((x for x in (1, 2, 5, 10, 20, 25, 50, 100) if x >= target * 0.7), 100)
    length = miles * 1.609344 / f.km_per_px
    x1, y = W - 26, H - 30
    x0 = x1 - length
    _pill(d, (x0 - 14, y - 36, x1 + 14, y + 12))
    d.line([(x0, y), (x1, y)], fill=TEXT, width=3)
    for tx in (x0, x1):
        d.line([(tx, y - 7), (tx, y + 3)], fill=TEXT, width=3)
    d.text(((x0 + x1) / 2, y - 12), f"{miles} mi", font=_font(23, "Medium"), fill=TEXT, anchor="ms")
    labels.taken.append((x0 - 14, y - 36, x1 + 14, y + 12))


def _legend(d: ImageDraw.ImageDraw, topic: str, labels: _Labels) -> None:
    """The colour scale, named: Light · Moderate · Heavy · Hail."""
    cfg = get_topic(topic).card_radar
    names = cfg.get("legend") or []
    stops = [float(v) for v, _ in cfg.get("colours") or []]
    if not names or not stops:
        return
    lo, hi = stops[1] if len(stops) > 1 else stops[0], stops[-1]
    x0, y0, width = 26, H - 44, 290
    _pill(d, (x0 - 14, y0 - 16, x0 + width + 14, y0 + 36))
    lut = _lut(topic)
    for i in range(width):
        v = lo + (hi - lo) * i / (width - 1)
        r, g, b, _ = lut[int(np.clip(round((v + 32) * 2), 0, 255))]
        d.line([(x0 + i, y0), (x0 + i, y0 + 10)], fill=(int(r), int(g), int(b)))
    small = _font(19, "Medium")
    right = x0 - 10
    for i, (v, name) in enumerate(names):
        x = x0 + (float(v) - lo) / (hi - lo) * (width - 1)
        anchor = "ls" if i == 0 else "rs" if i == len(names) - 1 else "ms"
        box = d.textbbox((x, y0 + 30), str(name), font=small, anchor=anchor)
        if box[0] < right + 8:                                  # keep names apart
            continue
        d.text((x, y0 + 30), str(name), font=small, fill=TEXT_DIM, anchor=anchor)
        right = box[2]
    labels.taken.append((x0 - 14, y0 - 16, x0 + width + 14, y0 + 36))


def _edge_pointer(d: ImageDraw.ImageDraw, f: Frame, mark: Mark, labels: _Labels) -> None:
    """A place outside the picture: an arrow at the edge, toward it, with the distance."""
    s, w, n, e = f.bbox()
    clat, clon = (s + n) / 2, (w + e) / 2
    mx, my = f.px(mark.lat, mark.lon)
    cx, cy = W / 2, H / 2
    dx, dy = mx - cx, my - cy
    scale = min((W / 2 - 60) / abs(dx) if dx else 1e9, (H / 2 - 60) / abs(dy) if dy else 1e9)
    x, y = cx + dx * scale, cy + dy * scale
    ang = math.atan2(dy, dx)
    tip = (x + 16 * math.cos(ang), y + 16 * math.sin(ang))
    left = (x - 10 * math.cos(ang - 0.6), y - 10 * math.sin(ang - 0.6))
    right = (x - 10 * math.cos(ang + 0.6), y - 10 * math.sin(ang + 0.6))
    d.polygon([tip, left, right], fill=TEXT, outline=HALO)
    miles = round(geo.haversine_km(clat, clon, mark.lat, mark.lon) / 1.609344)
    labels.near(x, y, f"{mark.label} · {miles} mi", _font(27, "SemiBold"), gap=24)


def render(sc: Scene, *, radar: bool = True, radar_key: str | None = None, now: datetime | None = None,
            frame: Frame | None = None) -> bytes:
    """The picture, as JPEG bytes."""
    img = render_image(sc, radar=radar, radar_key=radar_key, now=now, frame=frame)
    out = BytesIO()
    img.save(out, "JPEG", quality=92, subsampling=0, optimize=True)
    return out.getvalue()


def render_image(sc: Scene, *, radar: bool = True, radar_key: str | None = None, now: datetime | None = None,
                 frame: Frame | None = None) -> Image.Image:
    f = frame or frame_for(_points(sc))
    img = _base(f, sc).convert("RGBA")
    site = _site(sc.topic, f)
    swept_at = None
    if radar and radar_key is None:
        found = radar_keys(sc.topic, f, float(get_topic(sc.topic).card_radar.get("stale_minutes", 15)))
        radar_key = found[-1] if found else None
    layer = radar_layer(sc.topic, radar_key, f) if radar and radar_key else None
    if layer is not None:
        img.alpha_composite(layer)
        swept_at = nexrad.key_time(radar_key)

    # vector layers at twice the size, then scaled down: smooth outlines, dots and dashes
    over = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    for ring in sc.rings:
        path = _path(f, ring) + _path(f, ring[:1])
        d.line(path, fill=(*HALO, 230), width=13 if not sc.counties else 9, joint="curve")
        d.line(path, fill=(*sc.colour, 255), width=7 if not sc.counties else 5, joint="curve")
    steps = track(sc, f)
    if steps:
        pts = [f.px(la, lo, SS) for _, la, lo in steps]
        if len(sc.motion.get("points") or []) > 1:            # a line of storms: its front, now
            front = [f.px(p[0], p[1], SS) for p in sc.motion["points"]]
            d.line(front, fill=(*HALO, 240), width=15, joint="curve")
            d.line(front, fill=(*TEXT, 255), width=8, joint="curve")
        _dashes(d, pts, dash=26, gap=14, fill=(*HALO, 220), width=11)
        _dashes(d, pts, dash=26, gap=14, fill=(*TEXT, 245), width=5)
        for (x, y) in pts[1:]:
            d.ellipse((x - 11, y - 11, x + 11, y + 11), fill=(*TEXT, 255), outline=(*HALO, 255), width=4)
        x, y = pts[0]
        d.ellipse((x - 20, y - 20, x + 20, y + 20), fill=(*sc.colour, 255), outline=(*TEXT, 255), width=6)
    for r in sc.reports:
        x, y = f.px(r.lat, r.lon, SS)
        ring = SPOTTER if r.official else PEOPLE
        d.ellipse((x - 17, y - 17, x + 17, y + 17), fill=(*HALO, 255), outline=(*ring, 255), width=7)
        d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(*ring, 255))
    mark_xy = None
    if sc.mark and f.contains(sc.mark.lat, sc.mark.lon, -0.02):
        x, y = f.px(sc.mark.lat, sc.mark.lon, SS)
        mark_xy = (x / SS, y / SS)
        d.ellipse((x - 44, y - 44, x + 44, y + 44), fill=(*YOU, 60))
        d.ellipse((x - 25, y - 25, x + 25, y + 25), fill=(*TEXT, 255))
        d.ellipse((x - 17, y - 17, x + 17, y + 17), fill=(*YOU, 255))
    over = over.convert("RGBa").resize((W, H), Image.Resampling.LANCZOS).convert("RGBA")
    img.alpha_composite(over)

    d = ImageDraw.Draw(img, "RGBA")
    labels = _Labels(d)
    _legend(d, sc.topic, labels)
    _scale_bar(d, f, labels)
    head = (f"{site[1]} radar · {local_time(swept_at, '%-I:%M %p')}" if swept_at and site
            else "No radar right now" if radar else "")
    if head:
        font = _font(24, "Medium")
        box = d.textbbox((26, 22), head, font=font, anchor="lt")
        _pill(d, (box[0] - 14, box[1] - 10, box[2] + 14, box[3] + 10))
        d.text((26, 22), head, font=font, fill=TEXT, anchor="lt")
        labels.taken.append((box[0] - 14, box[1] - 10, box[2] + 14, box[3] + 10))
    kinds = [(name, colour) for name, colour, shown in (
        ("people", PEOPLE, any(not r.official for r in sc.reports)),
        ("spotters", SPOTTER, any(r.official for r in sc.reports))) if shown]
    if kinds:                                   # the key to the report dots, drawn (no glyph needed)
        font = _font(22, "Medium")
        widths = [d.textbbox((0, 0), name, font=font)[2] for name, _ in kinds]
        total = sum(w_ + 30 for w_ in widths) + 12 * (len(kinds) - 1)
        x, y = W - 26 - total, 22
        _pill(d, (x - 14, y - 10, W - 12, y + 34))
        for (name, colour), w_ in zip(kinds, widths, strict=True):
            d.ellipse((x, y + 5, x + 16, y + 21), fill=HALO, outline=colour, width=4)
            d.text((x + 24, y + 13), name, font=font, fill=TEXT, anchor="lm")
            x += w_ + 42
        labels.taken.append((W - 26 - total - 14, y - 10, W - 12, y + 34))
    if mark_xy:                                     # the reader first: it's why the picture exists
        x, y = mark_xy
        labels.taken.append((x - 22, y - 22, x + 22, y + 22))
        big, small = _font(36, "SemiBold"), _font(25, "Medium")
        lines = [(sc.mark.label, big, TEXT)] + ([(sc.mark.note, small, (*sc.colour, 255))] if sc.mark.note else [])
        labels.block(x, y, lines, gap=26)
    elif sc.mark:
        _edge_pointer(d, f, sc.mark, labels)
    for r in sc.reports:
        x, y = f.px(r.lat, r.lon)
        labels.taken.append((x - 9, y - 9, x + 9, y + 9))
    for r in sc.reports:
        x, y = f.px(r.lat, r.lon)
        labels.near(x, y, r.text, _font(23, "SemiBold"), gap=13, fill=PEOPLE if not r.official else TEXT)
    if steps:
        at = parse_motion_time(sc.motion)
        x, y = f.px(steps[0][1], steps[0][2])
        mph = round(float(sc.motion["speed_kt"]) * 1.15078)
        heading = (float(sc.motion["from_deg"]) + 180) % 360
        labels.taken.append((x - 12, y - 12, x + 12, y + 12))
        labels.near(x, y, f"Storm · {geo.compass(heading)} {mph} mph", _font(25, "SemiBold"), gap=18)
        for minutes, la, lo in steps[1:]:
            px_, py_ = f.px(la, lo)
            labels.taken.append((px_ - 7, py_ - 7, px_ + 7, py_ + 7))
            if at:
                labels.near(px_, py_, local_time(at + timedelta(minutes=minutes), "%-I:%M"), _font(22, "SemiBold"),
                            gap=12)
    if sc.counties:                             # a county-wide alert: name its counties
        for fips in sc.extra:
            c = geo.county(fips)
            if c is not None and f.contains(c.lat, c.lon, -0.04):
                x, y = f.px(c.lat, c.lon)
                any(labels.put((x, y + dy), c.name.upper(), _font(24, "SemiBold"), fill=(*sc.colour, 255),
                               anchor="mm") for dy in (0, 34, -34, 64, -64))
    placed: list[tuple[float, float]] = []
    for name, lat, lon, *_ in sorted((p for p in _reference().get("places", []) if f.contains(p[1], p[2], -0.03)),
                                     key=lambda p: -float(p[3] if len(p) > 3 else 0)):
        if len(placed) >= 8:
            break
        x, y = f.px(lat, lon)
        if mark_xy and math.hypot(x - mark_xy[0], y - mark_xy[1]) < 40:
            continue
        if any(geo.haversine_km(lat, lon, la, lo) < 9 for la, lo in placed):     # South Jacksonville, by Jacksonville
            continue
        if labels.near(x, y, name, _font(26, "Medium"), gap=10, fill=TEXT_DIM):
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=TEXT_DIM, outline=HALO, width=2)
            placed.append((lat, lon))
    return img.convert("RGB")


def render_loop(sc: Scene, *, minutes: float = 45, frame: Frame | None = None, fps: float = 4.0,
                hold: float = 1.6) -> bytes | None:
    """The last `minutes` of radar as a looping silent video (H.264 MP4): the motion a
    still can't show, played inline by Telegram. The last sweep is held a moment before it
    loops. None when ffmpeg or enough sweeps are missing."""
    import shutil
    import subprocess
    import tempfile

    ffmpeg = shutil.which("ffmpeg")
    f = frame or frame_for(_points(sc))
    keys = radar_keys(sc.topic, f, minutes)
    if not ffmpeg or len(keys) < 3:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        for i, key in enumerate(keys):
            render_image(sc, radar_key=key, frame=f).save(f"{tmp}/f{i:03d}.png")
        subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-framerate", f"{fps:g}", "-i", f"{tmp}/f%03d.png",
                        "-vf", f"tpad=stop_mode=clone:stop_duration={hold:g}", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-crf", "19", "-preset", "medium", "-movflags", "+faststart",
                        "-an", f"{tmp}/loop.mp4"], check=True, timeout=90)
        return Path(f"{tmp}/loop.mp4").read_bytes()


def parse_motion_time(motion: dict[str, Any] | None) -> datetime | None:
    from intelnet.models import parse_iso

    try:
        return parse_iso(str((motion or {}).get("at") or "")) or None
    except ValueError:
        return None


# ── files, and Telegram's copies ─────────────────────────────────────────

def _dir() -> Path:
    return Path(settings.db_path).resolve().parent / "cardmaps"


def prepare(sig: Signal, siblings: list[Signal], mark: Mark | None) -> str | None:
    """Make (or find) the picture for this alert and place; returns its name, or None when
    pictures are off or the alert has no area to draw. The name goes in the card's HTML
    as tg://photo?id=<name>; delivery attaches the file (`media_for`)."""
    if not settings.card_maps:
        return None
    sc = scene(sig, siblings, mark)
    if sc is None:
        return None
    radar = settings.card_map_radar
    f = frame_for(_points(sc))
    found = radar_keys(sc.topic, f, float(get_topic(sc.topic).card_radar.get("stale_minutes", 15))) if radar else []
    key = found[-1] if found else None
    name = sc.name(key or ("no-radar" if radar else ""))
    path = _dir() / f"{name}.jpg"
    if path.exists() or db.kv_get(f"tgfile:{name}"):
        return name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render(sc, radar=radar, radar_key=key, frame=f))
        _save_scene(name, sc, f)
    except Exception:          # a card goes out without its picture rather than not at all
        logger.exception("cardmap: rendering %s failed", name)
        return None
    _prune()
    return name


def _prune() -> None:
    cutoff = time.time() - KEEP.total_seconds()
    for p in [*_dir().glob("w[ml]-*"), *(_dir().parent / "radar").glob("*_*")]:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


# ── loops ────────────────────────────────────────────────────────────────

def loop_name(picture: str) -> str:
    """The loop that goes with a card's picture (same scene): wm-… → wl-…."""
    return "wl-" + picture.removeprefix("wm-")


def _save_scene(name: str, sc: Scene, f: Frame) -> None:
    """What a picture showed, kept beside it: its loop is drawn later, when delivery gets to it."""
    spec = {"rings": sc.rings, "colour": list(sc.colour), "counties": sc.counties, "motion": sc.motion,
            "mark": sc.mark.__dict__ if sc.mark else None, "version": sc.version, "extra": sc.extra,
            "topic": sc.topic, "reports": [r.__dict__ for r in sc.reports],
            "frame": [f.x0, f.y0, f.x1, f.y1]}
    (_dir() / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _load_scene(name: str) -> tuple[Scene, Frame] | None:
    try:
        spec = json.loads((_dir() / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sc = Scene(spec["rings"], tuple(spec["colour"]), counties=spec["counties"], motion=spec["motion"],
               mark=Mark(**spec["mark"]) if spec.get("mark") else None, version=spec["version"],
               extra=spec["extra"], topic=spec["topic"], reports=[Report(**r) for r in spec["reports"]])
    return sc, Frame(*spec["frame"])


def loop_possible(picture: str) -> bool:
    """Can this picture's card be given a loop? (Loops on, radar on, the scene kept, ffmpeg.)"""
    import shutil

    return (settings.card_map_loop and settings.card_map_radar and shutil.which("ffmpeg") is not None
            and (_dir() / f"{picture}.json").exists())


def loop_bytes(name: str) -> bytes | None:
    """A loop's file: made the first time it's asked for (at delivery, after urgent messages)."""
    path = _dir() / f"{name}.mp4"
    if path.exists():
        return path.read_bytes()
    loaded = _load_scene("wm-" + name.removeprefix("wl-"))
    if loaded is None:
        return None
    sc, f = loaded
    try:
        data = render_loop(sc, minutes=LOOP_MINUTES, frame=f)
    except Exception:          # a missing loop leaves the card's picture in place
        logger.exception("cardmap: loop %s failed", name)
        return None
    if data:
        path.write_bytes(data)
    return data


def media_for(rich: str | None) -> dict[str, str | bytes]:
    """The media a rich message names: Telegram's file id when it has one, else the file
    (a loop is rendered on first use)."""
    out: dict[str, str | bytes] = {}
    for name in PHOTO.findall(rich or ""):
        known = db.kv_get(f"tgfile:{name}")
        if known:
            out[name] = known
            continue
        path = _dir() / f"{name}.jpg"
        if path.exists():
            out[name] = path.read_bytes()
    for name in VIDEO.findall(rich or ""):
        known = db.kv_get(f"tgfile:{name}")
        data = known or loop_bytes(name)
        if data:
            out[name] = data
    return out


def missing(rich: str | None, media: dict[str, Any]) -> bool:
    """Does the message name a loop that couldn't be made? (Then it isn't sent.)"""
    return any(name not in media for name in VIDEO.findall(rich or ""))


def remember(rich: str | None, result: Any) -> None:
    """After a send or edit: keep Telegram's file id for each uploaded picture and loop."""
    if not isinstance(result, dict):
        return
    blocks = _walk(((result.get("rich_message") or {}).get("blocks")) or [])
    photos = [b for b in blocks if b.get("type") == "photo" and b.get("photo")]
    for name, block in zip(PHOTO.findall(rich or ""), photos, strict=False):
        biggest = max(block["photo"], key=lambda s: s.get("width", 0) * s.get("height", 0))
        if biggest.get("file_id"):
            db.kv_set(f"tgfile:{name}", biggest["file_id"])
    loops = [b for b in blocks if b.get("type") in ("animation", "video") and isinstance(
        b.get("animation") or b.get("video"), dict)]
    for name, block in zip(VIDEO.findall(rich or ""), loops, strict=False):
        file_id = (block.get("animation") or block.get("video") or {}).get("file_id")
        if file_id:
            db.kv_set(f"tgfile:{name}", file_id)


def _walk(blocks: list[Any]) -> list[dict[str, Any]]:
    """Every block, nested ones included (a photo can sit in a details block)."""
    out: list[dict[str, Any]] = []
    for b in blocks:
        if isinstance(b, dict):
            out.append(b)
            for v in b.values():
                if isinstance(v, list):
                    out += _walk(v)
    return out

