"""A warning, drawn: the picture on an alert card.

Telegram's map block centres a street map on one point and can't draw an area, so
an alert card carries a picture instead: the warned area over the counties, rivers,
roads and towns around it, the latest radar (IEM's NEXRAD mosaic, when it answers
within a few seconds), where the storm is heading over the next half hour, and the
reader's own place. An alert without a polygon is drawn as its counties.

A picture is named by what it shows (the alert version, the place, the radar's
five-minute slot), rendered once with Pillow and kept for a day and a half under
data/cardmaps/. Telegram gets the file once: the file id it hands back
(kv `tgfile:<name>`) serves every later card showing the same picture.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from intelnet import db, geo
from intelnet.config import CONFIG_DIR, PROJECT_ROOT, settings
from intelnet.models import Signal, local_time, utcnow

logger = logging.getLogger(__name__)

W, H = 1080, 720                    # 3:2, what a phone shows at full width
SS = 2                              # lines and fills are drawn twice as large, then scaled down: smooth edges
EARTH = 6378137.0
ASSETS = PROJECT_ROOT / "site" / "assets"
FONT = CONFIG_DIR / "fonts" / "IBMPlexSans.ttf"
RADAR_URL = "https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q.cgi"
RADAR_TIMEOUT = 5.0
RADAR_ALPHA = 0.72
RADAR_CELL = 0.01                   # degrees: the mosaic's own grid
KEEP = timedelta(hours=36)          # pictures on disk; Telegram keeps its own copy
NEAR_KM = 80                        # a place farther than this from the warning isn't drawn
MOTION_MINUTES = 30                 # the storm arrow: where it is in half an hour
PHOTO = re.compile(r"tg://photo\?id=([A-Za-z0-9_-]{1,64})")

# the site's palette (site/index.fragment.html)
INK, MUTED, PAPER = (26, 31, 28), (91, 101, 95), (255, 255, 255)
LAND, OUTSIDE = (245, 247, 243), (225, 230, 224)
WATER, RIVER = (190, 219, 231), (112, 164, 196)
ROAD_I, ROAD_US = (203, 186, 146), (219, 209, 183)
COUNTY = (172, 184, 176)
TOWN = (62, 72, 67)
SEVERITY = {"Extreme": (179, 38, 30), "Severe": (196, 80, 27), "Moderate": (183, 121, 31),
            "Minor": (75, 123, 138), "Unknown": (91, 101, 95)}


# ── geometry ─────────────────────────────────────────────────────────────

def _merc(lat: float, lon: float) -> tuple[float, float]:
    lat = max(min(lat, 85.0), -85.0)
    return math.radians(lon) * EARTH, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * EARTH


def _unmerc_lat(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y / EARTH)) - math.pi / 2)


@dataclass
class Frame:
    """The picture's extent in Web Mercator metres (the radar's projection); y1 is north."""
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


def frame_for(points: list[tuple[float, float]], *, pad: float = 0.16, min_km: float = 40.0) -> Frame:
    """The smallest 3:2 frame around these (lat, lon) points, with a margin, at least
    `min_km` across (a small warning still shows the towns around it)."""
    xs, ys = zip(*(_merc(la, lo) for la, lo in points), strict=True)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    k = 1 / math.cos(math.radians(_unmerc_lat(cy)))                # Mercator metres per ground metre
    w = max((max(xs) - min(xs)) * (1 + 2 * pad), min_km * 1000 * k)
    h = max((max(ys) - min(ys)) * (1 + 2 * pad), min_km * 1000 * k * H / W)
    w, h = (h * W / H, h) if w / h < W / H else (w, w * H / W)
    return Frame(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


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


@lru_cache(maxsize=8)
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


@dataclass
class Scene:
    rings: list[list[list[float]]]                    # warned area, [lon, lat] pairs
    colour: tuple[int, int, int]
    counties: bool = False                            # the rings are whole counties (no polygon)
    motion: dict[str, Any] | None = None
    mark: Mark | None = None
    version: str = ""
    extra: list[str] = field(default_factory=list)

    def name(self, slot: str) -> str:
        """What the picture shows, as a file name: same scene, same picture."""
        m = self.mark
        spec = [self.version, len(self.rings), self.counties, slot,
                [round(m.lat, 3), round(m.lon, 3), m.label] if m else None, self.extra]
        return "wm-" + hashlib.sha1(json.dumps(spec, default=str).encode()).hexdigest()[:20]


def scene(sig: Signal, siblings: list[Signal], mark: Mark | None) -> Scene | None:
    """The warned area of an alert: its polygon, or else the counties it names."""
    ev = sig.evidence
    colour = SEVERITY.get(str(ev.get("severity") or "Unknown"), SEVERITY["Unknown"])
    version = str(ev.get("alert_id") or sig.key)
    rings = ev.get("polygon")
    if rings:
        return Scene(rings, colour, motion=ev.get("motion"), mark=mark, version=version)
    shapes = _counties()
    fips = sorted({s.location.county_fips for s in (siblings or [sig]) if s.location.county_fips})
    county_rings = [r for f in fips for r in shapes.get(f, [])]
    if not county_rings:
        return None
    return Scene(county_rings, colour, counties=True, mark=mark, version=version, extra=fips)


def _points(sc: Scene) -> list[tuple[float, float]]:
    pts = [(lat, lon) for ring in sc.rings for lon, lat in ring]
    if sc.mark and min(geo.haversine_km(sc.mark.lat, sc.mark.lon, la, lo) for la, lo in pts) <= NEAR_KM:
        pts.append((sc.mark.lat, sc.mark.lon))
    return pts


# ── radar ────────────────────────────────────────────────────────────────

_RADAR: dict[tuple[Any, ...], tuple[float, Image.Image | None]] = {}


def _radar(f: Frame) -> Image.Image | None:
    """IEM's NEXRAD base-reflectivity mosaic for the frame, or None.

    The request names half-degree cells around the frame, never a reader's place, and one
    answer serves every picture in those cells for five minutes."""
    s, w, n, e = f.bbox()
    cells = (math.floor(s * 2) / 2, math.floor(w * 2) / 2, math.ceil(n * 2) / 2, math.ceil(e * 2) / 2)
    key = (*cells, int(time.time() // 300))
    hit = _RADAR.get(key)
    if hit is not None:
        return hit[1]
    x0, y0 = _merc(cells[0], cells[1])
    x1, y1 = _merc(cells[2], cells[3])
    # one pixel per mosaic cell (0.01°): asked for more, the server copies each cell into a
    # block, while enlarging a pixel per cell here gives smooth edges
    size = (min(2048, round((cells[3] - cells[1]) / RADAR_CELL)),
            min(2048, round((cells[3] - cells[1]) / RADAR_CELL * (y1 - y0) / (x1 - x0))))
    img = None
    try:
        r = requests.get(RADAR_URL, params={
            "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": "nexrad-n0q-900913",
            "STYLES": "", "SRS": "EPSG:3857", "BBOX": f"{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}",
            "WIDTH": size[0], "HEIGHT": size[1], "FORMAT": "image/png", "TRANSPARENT": "true"},
            timeout=RADAR_TIMEOUT)
        if r.ok and r.headers.get("content-type", "").startswith("image/"):
            whole = Image.open(BytesIO(r.content)).convert("RGBa")    # premultiplied: no dark fringes
            sx, sy = whole.width / (x1 - x0), whole.height / (y1 - y0)
            box = (max(0.0, (f.x0 - x0) * sx), max(0.0, (y1 - f.y1) * sy),
                   min(float(whole.width), (f.x1 - x0) * sx), min(float(whole.height), (y1 - f.y0) * sy))
            cell = W * SS * RADAR_CELL / (e - w)                    # one mosaic cell, in our pixels
            img = (whole.resize((W * SS, H * SS), Image.Resampling.BICUBIC, box=box)
                   .filter(ImageFilter.GaussianBlur(max(2.0, cell * 0.45))).convert("RGBA"))
    except Exception as exc:  # noqa: BLE001 — a card never waits on, or fails for, the radar
        logger.info("cardmap: no radar (%s)", type(exc).__name__)
    if len(_RADAR) > 32:
        _RADAR.clear()
    _RADAR[key] = (time.time(), img)
    return img


# ── drawing ──────────────────────────────────────────────────────────────

def _path(f: Frame, ring: list[list[float]]) -> list[tuple[float, float]]:
    return [f.px(lat, lon, SS) for lon, lat in ring]


def _near(f: Frame, coords: list[list[float]], margin: float = 0.1) -> bool:
    """Does this line or ring's bounding box reach the frame (plus a margin)?"""
    s, w, n, e = f.bbox()
    dy, dx = (n - s) * margin, (e - w) * margin
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons) <= e + dx and max(lons) >= w - dx and min(lats) <= n + dy and max(lats) >= s - dy


def _base(f: Frame) -> Image.Image:
    """Land, water, roads and county lines: the part of the map that never changes."""
    img = Image.new("RGB", (W * SS, H * SS), OUTSIDE)
    d = ImageDraw.Draw(img)
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
    for rd in sorted(ref.get("roads", []), key=lambda r: r.get("type") == "I"):
        interstate = rd.get("type") == "I"
        for line in rd.get("lines", []):
            if len(line) > 1 and _near(f, line):
                d.line(_path(f, line), fill=ROAD_I if interstate else ROAD_US,
                       width=(5 if interstate else 3) * SS // 2, joint="curve")
    for rv in ref.get("rivers", []):
        for line in rv.get("lines", []):
            if len(line) > 1 and _near(f, line):
                d.line(_path(f, line), fill=RIVER, width=3 * SS // 2, joint="curve")
    for rings in shapes.values():
        for ring in rings:
            if _near(f, ring, 0.5):
                d.line(_path(f, ring) + _path(f, ring[:1]), fill=COUNTY, width=2 * SS // 2 + 1, joint="curve")
    return img


def _arrow(d: ImageDraw.ImageDraw, a: tuple[float, float], b: tuple[float, float], *, width: int,
           fill: tuple[int, ...], head: float) -> None:
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    back = (b[0] - head * 0.8 * math.cos(ang), b[1] - head * 0.8 * math.sin(ang))
    d.line([a, back], fill=fill, width=width)
    left = (b[0] - head * math.cos(ang - 0.45), b[1] - head * math.sin(ang - 0.45))
    right = (b[0] - head * math.cos(ang + 0.45), b[1] - head * math.sin(ang + 0.45))
    d.polygon([b, left, right], fill=fill)


def _track(sc: Scene, f: Frame) -> tuple[tuple[float, float], tuple[float, float], str] | None:
    """Where the storm is and where it'll be in MOTION_MINUTES, in picture pixels (×SS)."""
    m = sc.motion
    if not m or not m.get("points") or not m.get("speed_kt"):
        return None
    pts = m["points"]
    lat = sum(p[0] for p in pts) / len(pts)
    lon = sum(p[1] for p in pts) / len(pts)
    heading = (float(m["from_deg"]) + 180) % 360
    km = float(m["speed_kt"]) * 1.852 * MOTION_MINUTES / 60
    lat2 = lat + km * math.cos(math.radians(heading)) / 110.57
    lon2 = lon + km * math.sin(math.radians(heading)) / (111.32 * math.cos(math.radians(lat)))
    mph = round(float(m["speed_kt"]) * 1.15078)
    return f.px(lat, lon, SS), f.px(lat2, lon2, SS), f"Moving {geo.compass(heading)}, {mph} mph"


class _Labels:
    """Text placed so no two labels overlap (greedy, most important first)."""

    def __init__(self, d: ImageDraw.ImageDraw) -> None:
        self.d = d
        self.taken: list[tuple[float, float, float, float]] = []

    def free(self, box: tuple[float, float, float, float]) -> bool:
        x0, y0, x1, y1 = box
        if x0 < 6 or y0 < 6 or x1 > W - 6 or y1 > H - 6:
            return False
        return not any(x0 < b[2] and b[0] < x1 and y0 < b[3] and b[1] < y1 for b in self.taken)

    def put(self, xy: tuple[float, float], text: str, font: Any, *, fill: tuple[int, ...], halo: tuple[int, ...],
            stroke: int = 4, anchor: str = "lm", force: bool = False) -> bool:
        box = self.d.textbbox(xy, text, font=font, anchor=anchor, stroke_width=stroke)
        if not force and not self.free(box):
            return False
        self.d.text(xy, text, font=font, anchor=anchor, fill=fill, stroke_width=stroke, stroke_fill=halo)
        self.taken.append(box)
        return True


def _scale_bar(d: ImageDraw.ImageDraw, f: Frame) -> None:
    target = W * 0.16 * f.km_per_px / 1.609344                     # miles in ~16% of the width
    miles = next((x for x in (1, 2, 5, 10, 20, 25, 50, 100) if x >= target * 0.7), 100)
    length = miles * 1.609344 / f.km_per_px
    x, y = 28, H - 34
    d.rounded_rectangle((x - 10, y - 30, x + length + 12, y + 14), radius=8, fill=(255, 255, 255, 215))
    d.line([(x, y), (x + length, y)], fill=INK, width=4)
    for tx in (x, x + length):
        d.line([(tx, y - 8), (tx, y + 4)], fill=INK, width=3)
    d.text((x + length / 2, y - 10), f"{miles} mi", font=_font(22, "Medium"), fill=INK, anchor="ms")


def render(sc: Scene, *, radar: bool = True, now: datetime | None = None) -> bytes:
    """The picture, as JPEG bytes (Telegram stores photos as JPEG anyway; full-resolution
    colour keeps the outline and the labels crisp)."""
    f = frame_for(_points(sc))
    img = _base(f)
    radar_img = _radar(f) if radar else None
    if radar_img is not None:
        radar_img.putalpha(radar_img.getchannel("A").point(lambda a: int(a * RADAR_ALPHA)))
        img = Image.alpha_composite(img.convert("RGBA"), radar_img).convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")              # RGBA onto an RGB image: fills blend
    for ring in sc.rings:
        d.polygon(_path(f, ring), fill=(*sc.colour, 46 if sc.counties else 56))
    for ring in sc.rings:
        d.line(_path(f, ring) + _path(f, ring[:1]), fill=sc.colour, width=(3 if sc.counties else 5) * SS // 2 + 1,
               joint="curve")
    track = _track(sc, f)
    if track:
        a, b, _ = track
        _arrow(d, a, b, width=12 * SS // 2, fill=INK, head=34 * SS)                  # a dark edge…
        _arrow(d, a, b, width=7 * SS // 2, fill=PAPER, head=27 * SS)                 # …around a white arrow
        r = 8 * SS
        d.ellipse((a[0] - r, a[1] - r, a[0] + r, a[1] + r), fill=PAPER, outline=INK, width=SS * 3)
    mark_xy = None
    if sc.mark and f.contains(sc.mark.lat, sc.mark.lon, -0.02):
        mx, my = f.px(sc.mark.lat, sc.mark.lon, SS)
        mark_xy = (mx / SS, my / SS)
        for r, fill in ((16 * SS, PAPER), (10 * SS, INK)):
            d.ellipse((mx - r, my - r, mx + r, my + r), fill=fill)
    img = img.resize((W, H), Image.Resampling.LANCZOS)

    d = ImageDraw.Draw(img, "RGBA")
    labels = _Labels(d)
    if mark_xy:                                     # the reader first: it's why the picture exists
        x, y = mark_xy
        font = _font(34, "SemiBold")
        labels.taken.append((x - 18, y - 18, x + 18, y + 18))
        (labels.put((x + 24, y), sc.mark.label, font, fill=INK, halo=PAPER, stroke=5)
         or labels.put((x - 24, y), sc.mark.label, font, fill=INK, halo=PAPER, stroke=5, anchor="rm", force=True))
    if track:
        a, b, text = track
        ax, ay, bx, by = a[0] / SS, a[1] / SS, b[0] / SS, b[1] / SS
        labels.taken.append((min(ax, bx) - 14, min(ay, by) - 14, max(ax, bx) + 14, max(ay, by) + 14))
        font = _font(25, "SemiBold")
        up = by < ay                                 # the label goes on the far side of the arrow's tail
        first, second = ((ax, ay + 20, "mt"), (ax, ay - 20, "mb")) if up else ((ax, ay - 20, "mb"), (ax, ay + 20, "mt"))
        (labels.put(first[:2], text, font, fill=INK, halo=PAPER, stroke=5, anchor=first[2])
         or labels.put(second[:2], text, font, fill=INK, halo=PAPER, stroke=5, anchor=second[2]))
    placed: list[tuple[float, float]] = []
    for name, lat, lon, *_ in sorted((p for p in _reference().get("places", []) if f.contains(p[1], p[2], -0.03)),
                                     key=lambda p: -float(p[3] if len(p) > 3 else 0)):
        if len(placed) >= 7:
            break
        x, y = f.px(lat, lon)
        if mark_xy and math.hypot(x - mark_xy[0], y - mark_xy[1]) < 40:
            continue
        if any(geo.haversine_km(lat, lon, la, lo) < 9 for la, lo in placed):     # South Jacksonville, by Jacksonville
            continue
        font = _font(27, "Medium")
        if labels.put((x + 11, y), name, font, fill=TOWN, halo=PAPER, stroke=4) \
                or labels.put((x - 11, y), name, font, fill=TOWN, halo=PAPER, stroke=4, anchor="rm"):
            d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=TOWN, outline=PAPER, width=2)
            placed.append((lat, lon))
    _scale_bar(d, f)
    stamp = (f"Radar {local_time(now or utcnow(), '%-I:%M %p')} · NEXRAD via IEM" if radar_img is not None
             else "Radar unavailable" if radar else "")
    if stamp:
        labels.put((W - 16, H - 20), stamp, _font(20, "Medium"), fill=MUTED, halo=PAPER, stroke=4, anchor="rs", force=True)
    out = BytesIO()
    img.save(out, "JPEG", quality=90, subsampling=0, optimize=True)
    return out.getvalue()


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
    radar = settings.card_map_radar and not sc.counties       # county alerts are slow hazards: no radar
    slot = str(int(time.time() // 300)) if radar else ""
    name = sc.name(slot)
    path = _dir() / f"{name}.jpg"
    if path.exists() or db.kv_get(f"tgfile:{name}"):
        return name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render(sc, radar=radar))
    except Exception:          # a card goes out without its picture rather than not at all
        logger.exception("cardmap: rendering %s failed", name)
        return None
    _prune()
    return name


def _prune() -> None:
    cutoff = time.time() - KEEP.total_seconds()
    for p in _dir().glob("wm-*.jpg"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def media_for(rich: str | None) -> dict[str, str | bytes]:
    """The pictures a rich message names: Telegram's file id when it has one, else the file."""
    out: dict[str, str | bytes] = {}
    for name in PHOTO.findall(rich or ""):
        known = db.kv_get(f"tgfile:{name}")
        if known:
            out[name] = known
            continue
        path = _dir() / f"{name}.jpg"
        if path.exists():
            out[name] = path.read_bytes()
    return out


def remember(rich: str | None, result: Any) -> None:
    """After a send or edit: keep Telegram's file id for each uploaded picture, in order."""
    names = PHOTO.findall(rich or "")
    if not names or not isinstance(result, dict):
        return
    blocks = ((result.get("rich_message") or {}).get("blocks")) or []
    photos = [b for b in _walk(blocks) if b.get("type") == "photo" and b.get("photo")]
    for name, block in zip(names, photos, strict=False):
        biggest = max(block["photo"], key=lambda s: s.get("width", 0) * s.get("height", 0))
        if biggest.get("file_id"):
            db.kv_set(f"tgfile:{name}", biggest["file_id"])


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

