"""Gridded truth: radar grids that check people's readings where no station is near.

Stations and storm reports are points; most people aren't near one. NOAA's MRMS
mosaics cover every square kilometer: radar rainfall (QPE) and the largest hail
radar thinks fell (MESH). A pack's `reference_grids` section names, per metric, a
keyless grid and how to read it:

    reader    arcgis_image  an ArcGIS ImageServer point query (NOAA mapservices)
              mrms_grib2    an NCEP MRMS GRIB2 file (PNG-packed; Pillow decodes it)
    windows   window → product, e.g. {1h: MESH_Max_60min, 24h: MESH_Max_1440min}
    unit      the grid's unit (converted with the metric's own units)
    tolerance {rel, abs} in canonical units; radar is rough, so wider than a neighbor's
    min_signal the least the grid must show to confirm anything
    neighborhood_km  take the grid's maximum within this radius (hail swaths are narrow)
    max_age   readings older than this aren't checked (only the latest grid is served)

Each watch pass (`check_pending`) takes people's unsettled readings of those metrics
and, once a grid covers the reading's time, gives a verdict:

    agree     the grid shows it and matches within tolerance → corroborated
    disagree  a mild miss (or radar shows nothing where a person saw something at event
              level: radar can miss) → a half-weight mark on the record
    far       the grid shows something `far_ratio` (3) times off, well past the absolute
              tolerance → flagged
    quiet     radar shows nothing and the reading is small → nothing to say

The verdict lands in the reading's evidence (`grid`), so each reading is judged once.
Nothing here knows about weather: metrics, products and tolerances are pack data.
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import math
import re
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from PIL import Image

from intelnet import db, geo, network
from intelnet.config import settings
from intelnet.models import Signal, iso, utcnow
from intelnet.topics import Metric, find_metric, get_topic, topics

logger = logging.getLogger(__name__)

CACHE_SECONDS = 120
REQUEST_TIMEOUT = 60.0            # quick_look lowers it: the bot's reply can't wait on radar
SLACK = timedelta(minutes=15)     # a peak metric's window must start this long before the reading
MARGIN_DEG = 0.4                  # around the state when cropping a national grid
NO_COVERAGE = -2.5                # MRMS writes −3 where no radar sees
_cache: dict[str, tuple[float, Any]] = {}


class GridError(RuntimeError):
    pass


@dataclass
class Grid:
    metric: str
    topic: str
    label: str
    reader: str
    url: str
    windows: dict[str, str]
    unit: str
    tolerance: dict[str, float]
    min_signal: float = 0.0
    neighborhood_km: float = 0.0
    default_window: str | None = None
    max_age: str = "24h"
    far_ratio: float = 3.0


@dataclass
class Sample:
    value: float | None             # canonical units; None = no data there
    valid: datetime | None          # the end of the grid's window
    lat: float
    lon: float
    window: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def minutes(window: str) -> int:
    """'30m' → 30, '6h' → 360, '2d' → 2880."""
    m = re.fullmatch(r"(\d+)\s*([mhd])", window.strip().lower())
    if not m:
        raise ValueError(f"bad window {window!r}")
    return int(m.group(1)) * {"m": 1, "h": 60, "d": 1440}[m.group(2)]


def grids() -> dict[str, Grid]:
    """Every pack's reference grids, by metric key."""
    out = {}
    for t in topics().values():
        for key, g in t.reference_grids.items():
            out[key] = Grid(metric=key, topic=t.name, label=str(g.get("label") or "radar grid"),
                            reader=str(g["reader"]), url=str(g["url"]),
                            windows={str(k): str(v) for k, v in (g.get("windows") or {}).items()},
                            unit=str(g.get("unit") or ""), tolerance=dict(g.get("tolerance") or {}),
                            min_signal=float(g.get("min_signal") or 0),
                            neighborhood_km=float(g.get("neighborhood_km") or 0),
                            default_window=g.get("default_window"), max_age=str(g.get("max_age") or "24h"),
                            far_ratio=float(g.get("far_ratio") or 3.0))
    return out


# ── readers ───────────────────────────────────────────────────────────────

def _cached(key: str, load):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = load()
    _cache[key] = (time.monotonic(), value)
    return value


def _mrms_value(v: float | None) -> float | None:
    """MRMS flags: −3 (and below) is outside radar coverage; −1 means covered, nothing there."""
    if v is None or v <= NO_COVERAGE:
        return None
    return max(0.0, v)


def _signed(raw: int, bits: int) -> int:
    """GRIB2 stores signed integers as sign and magnitude, not two's complement."""
    top = 1 << (bits - 1)
    return -(raw & (top - 1)) if raw & top else raw


@dataclass
class GribField:
    """One GRIB2 field on a regular lat/lon grid, cropped to the state."""
    valid: datetime
    image: Image.Image          # 16-bit PNG values, cropped
    lat0: float                 # latitude of the crop's first row
    lon0: float                 # longitude of the crop's first column (−180..180)
    di: float
    dj: float
    ref: float                  # value = (ref + X·2^E) / 10^D
    e: int
    d: int

    def value_at(self, row: int, col: int) -> float | None:
        if not (0 <= row < self.image.height and 0 <= col < self.image.width):
            return None
        return _mrms_value((self.ref + self.image.getpixel((col, row)) * 2.0 ** self.e) / 10.0 ** self.d)

    def cell(self, lat: float, lon: float) -> tuple[int, int]:
        return round((self.lat0 - lat) / self.dj), round((lon - self.lon0) / self.di)

    def max_near(self, lat: float, lon: float, radius_km: float) -> tuple[float | None, float, float]:
        """The largest value within radius_km of a point, and where it is."""
        r0, c0 = self.cell(lat, lon)
        dr = max(0, math.ceil(radius_km / (111.32 * self.dj)))
        dc = max(0, math.ceil(radius_km / (111.32 * max(0.1, math.cos(math.radians(lat))) * self.di)))
        best: tuple[float | None, float, float] = (None, lat, lon)
        for r in range(r0 - dr, r0 + dr + 1):
            for c in range(c0 - dc, c0 + dc + 1):
                la, lo = self.lat0 - r * self.dj, self.lon0 + c * self.di
                if radius_km and geo.haversine_km(lat, lon, la, lo) > radius_km:
                    continue
                v = self.value_at(r, c)
                if v is not None and (best[0] is None or v > best[0]):
                    best = (v, la, lo)
        return best if best[0] else (best[0], lat, lon)      # nothing anywhere: "at" is the point


def parse_grib2(data: bytes, bbox: tuple[float, float, float, float] | None = None) -> GribField:
    """Decode an MRMS GRIB2 message: a regular lat/lon grid (template 3.0) whose values
    are PNG-packed (template 5.41). `bbox` = (south, west, north, east) crops it."""
    if data[:4] != b"GRIB" or data[7] != 2:
        raise GridError("not a GRIB2 message")
    secs: dict[int, bytes] = {}
    i = 16
    while i < len(data) - 4 and data[i:i + 4] != b"7777":
        length, num = struct.unpack(">IB", data[i:i + 5])
        secs.setdefault(num, data[i:i + length])
        i += length
    s1, s3, s5, s7 = secs[1], secs[3], secs[5], secs[7]
    year, month, day, hour, minute, second = struct.unpack(">HBBBBB", s1[12:19])
    valid = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    if struct.unpack(">H", s3[12:14])[0] != 0:
        raise GridError("only regular lat/lon grids (template 3.0)")
    if struct.unpack(">H", s5[9:11])[0] != 41:
        raise GridError("only PNG-packed data (template 5.41)")
    ni, nj = struct.unpack(">II", s3[30:38])
    la1 = _signed(struct.unpack(">I", s3[46:50])[0], 32) / 1e6
    lo1 = struct.unpack(">I", s3[50:54])[0] / 1e6
    di, dj = (v / 1e6 for v in struct.unpack(">II", s3[63:71]))
    if s3[71] & 0x40 or s3[71] & 0x80:
        raise GridError("only north-to-south, west-to-east scanning")
    ref = struct.unpack(">f", s5[11:15])[0]
    e, d = _signed(struct.unpack(">H", s5[15:17])[0], 16), _signed(struct.unpack(">H", s5[17:19])[0], 16)
    image = Image.open(io.BytesIO(s7[5:]))
    image.load()
    if image.size != (ni, nj):
        raise GridError(f"PNG is {image.size}, grid says {(ni, nj)}")
    lon0 = lo1 - 360 if lo1 > 180 else lo1
    if bbox:
        south, west, north, east = bbox
        r0, r1 = max(0, math.floor((la1 - north) / dj)), min(nj, math.ceil((la1 - south) / dj) + 1)
        c0, c1 = max(0, math.floor((west - lon0) / di)), min(ni, math.ceil((east - lon0) / di) + 1)
        image = image.crop((c0, r0, c1, r1))
        la1, lon0 = la1 - r0 * dj, lon0 + c0 * di
    return GribField(valid, image, la1, lon0, di, dj, ref, e, d)


def state_bbox() -> tuple[float, float, float, float]:
    """(south, west, north, east) around the state's ZIP and county centers, with margin."""
    pts = [(z.lat, z.lon) for z in geo.zctas().values()] + [(c.lat, c.lon) for c in geo.counties().values()]
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    return (min(lats) - MARGIN_DEG, min(lons) - MARGIN_DEG, max(lats) + MARGIN_DEG, max(lons) + MARGIN_DEG)


def _mrms_grib2(grid: Grid, product: str, lat: float, lon: float) -> Sample:
    url = grid.url.format(product=product)

    def load() -> GribField:
        r = requests.get(url, headers={"User-Agent": settings.nws_user_agent}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        raw = gzip.decompress(r.content) if r.content[:2] == b"\x1f\x8b" else r.content
        return parse_grib2(raw, state_bbox())

    f = _cached(url, load)
    value, la, lo = f.max_near(lat, lon, grid.neighborhood_km)
    return Sample(value, f.valid, la, lo)


def _arcgis_image(grid: Grid, product: str, lat: float, lon: float) -> Sample:
    """An ArcGIS ImageServer point query. The mosaic holds every product; `product` is the
    catalog item's name, and the raw pixel (not a rendered class) is what comes back."""
    r = requests.get(f"{grid.url.rstrip('/')}/identify", params={
        "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPoint",
        "mosaicRule": json.dumps({"mosaicMethod": "esriMosaicAttribute", "where": f"name='{product}'"}),
        "renderingRule": json.dumps({"rasterFunction": "None"}),
        "returnGeometry": "false", "returnCatalogItems": "true", "f": "json",
    }, headers={"User-Agent": settings.nws_user_agent}, timeout=min(30.0, REQUEST_TIMEOUT))
    r.raise_for_status()
    d = r.json()
    if d.get("error"):
        raise GridError(str(d["error"].get("message") or d["error"]))
    items = [f["attributes"] for f in (d.get("catalogItems") or {}).get("features") or []
             if f.get("attributes", {}).get("name") == product]
    end = items[0].get("idp_validendtime") if items else None
    valid = datetime.fromtimestamp(end / 1000, tz=timezone.utc) if end else None
    try:
        value = float(d.get("value"))
    except (TypeError, ValueError):
        value = None                             # "NoData"
    return Sample(_mrms_value(value), valid, lat, lon)


READERS = {"mrms_grib2": _mrms_grib2, "arcgis_image": _arcgis_image}


def sample(grid: Grid, window: str, lat: float, lon: float) -> Sample:
    """The grid's value (canonical units) at a point for one window."""
    s = READERS[grid.reader](grid, grid.windows[window], lat, lon)
    metric = find_metric(grid.metric, get_topic(grid.topic))
    if s.value is not None and metric is not None and grid.unit:
        s.value = metric.convert(s.value, grid.unit)
    s.window = window
    return s


# ── judging a reading ─────────────────────────────────────────────────────

def windows_for(grid: Grid, metric: Metric, sig: Signal) -> list[str]:
    """The windows that could judge this reading, shortest first. An amount over a stated
    period uses that period; a storm total, the default window and longer; a peak
    (hail size), any window."""
    ranked = sorted(grid.windows, key=minutes)
    if not metric.accumulates:
        return ranked
    period = str(sig.evidence.get("period") or "")
    if period in grid.windows:
        return [period]
    want = minutes(grid.default_window or ranked[-1])
    return [w for w in ranked if minutes(w) >= want]


def fits(grid: Grid, metric: Metric, sig: Signal, window: str, valid: datetime) -> bool:
    """Does this window, valid to `valid`, span the reading (and, for anything but a
    stated period, the SLACK before it, when the storm was on its way)?"""
    if sig.observed_at > valid:
        return False
    period = metric.accumulates and str(sig.evidence.get("period") or "") == window
    return valid - timedelta(minutes=minutes(window)) <= sig.observed_at - (timedelta(0) if period else SLACK)


def pick_window(grid: Grid, metric: Metric, sig: Signal, valid: datetime) -> str | None:
    """The shortest window that judges this reading if every window is valid to `valid`.
    None: not yet (the grid doesn't reach the reading's time)."""
    return next((w for w in windows_for(grid, metric, sig) if fits(grid, metric, sig, w, valid)), None)


def judge_sample(grid: Grid, metric: Metric, sig: Signal) -> Sample | None:
    """The grid's value over the shortest window that spans the reading. Products refresh
    at their own pace (MRMS's 24-hour hail every 30 min, the 30-minute one every 2), so
    each window is judged by its own valid time. None: no window reaches it yet."""
    lat, lon = sig.location.lat, sig.location.lon
    for window in windows_for(grid, metric, sig):
        s = sample(grid, window, lat, lon)
        if s.valid is not None and fits(grid, metric, sig, window, s.valid):
            return s
    return None


def _close(a: float, b: float, tol: dict[str, float], factor: float = 1.0) -> bool:
    allowed = max(tol.get("abs") or 0.0, (tol.get("rel") or 0.0) * max(abs(a), abs(b)))
    return abs(a - b) <= factor * allowed


def verdict(grid: Grid, metric: Metric, reading: float, g: float | None) -> str:
    """agree · disagree (a mild miss) · far (FAR_RATIO times off, and well past the
    absolute tolerance) · quiet (nothing on radar, nothing much reported) · nodata."""
    if g is None:
        return "nodata"
    if g >= grid.min_signal:
        if _close(reading, g, grid.tolerance):
            return "agree"
        ratio = max(reading, g) / max(min(reading, g), 1e-9)
        wide = abs(reading - g) > 2 * (grid.tolerance.get("abs") or 0.0)
        return "far" if ratio >= grid.far_ratio and wide else "disagree"
    return "disagree" if metric.is_event(reading) else "quiet"


def _witness(grid: Grid, sig: Signal, s: Sample) -> Signal:
    """The grid as a reading (never stored): what a follow-up note cites."""
    return Signal(source="grid", source_id=f"{grid.metric}|{s.window}", sensor_id=f"grid:{grid.metric}",
                  sensor_kind="grid", topic=grid.topic, metric=grid.metric, value=s.value,
                  observed_at=s.valid or utcnow(), location=geo.Location(lat=s.lat, lon=s.lon),
                  evidence={"label": grid.label, "window": s.window})


@dataclass
class Checked:
    signal: Signal
    verdict: str
    witness: Signal | None = None
    assessment: Any = None


def check_pending(now: datetime | None = None, limit: int = 200) -> list[Checked]:
    """Judge every unsettled reading a grid now covers. Returns what was decided."""
    now = now or utcnow()
    table = grids()
    if not table or not settings.grid_checks_enabled:
        return []
    oldest = now - timedelta(minutes=max(minutes(g.max_age) for g in table.values()))
    out: list[Checked] = []
    for sig in db.grid_candidates(table, oldest, limit):
        grid = table[sig.metric]
        metric = find_metric(sig.metric, get_topic(sig.topic))
        if metric is None or sig.value is None or sig.id is None or not sig.location.has_point:
            continue
        if now - sig.observed_at > timedelta(minutes=minutes(grid.max_age)):
            _mark(sig, {"verdict": "expired"})
            continue
        try:
            s = judge_sample(grid, metric, sig)
        except (requests.RequestException, GridError, OSError, KeyError, ValueError) as exc:
            logger.warning("grids: %s at %s: %s", grid.metric, sig.id, type(exc).__name__)
            continue
        if s is None:
            continue                                              # the grid hasn't caught up yet
        v = verdict(grid, metric, sig.value, s.value)
        if v == "nodata":
            continue
        _mark(sig, {"verdict": v, "source": grid.label, "window": s.window, "value": s.value,
                    "valid": iso(s.valid), "at": [round(s.lat, 3), round(s.lon, 3)]})
        a = network.settle_by_grid(sig, v)
        out.append(Checked(sig, v, _witness(grid, sig, s), a))
    return out


def quick_look(sig: Signal, timeout: float = 6.0) -> str | None:
    """What radar shows at a fresh report, for the reply: 'radar estimates 1.1 in here
    (last hour)'. Informational only; the verdict comes later from check_pending, once a
    window covers the report. None when there's no grid, no signal on it, or no answer
    within `timeout` seconds."""
    global REQUEST_TIMEOUT
    if not settings.grid_checks_enabled or sig.value is None or not sig.location.has_point:
        return None
    grid = grids().get(sig.metric)
    metric = find_metric(sig.metric, get_topic(sig.topic))
    if grid is None or metric is None or not metric.scored:
        return None
    if metric.accumulates and grid.default_window in grid.windows:
        window = grid.default_window
    else:
        window = "1h" if "1h" in grid.windows else min(grid.windows, key=minutes)
    old, REQUEST_TIMEOUT = REQUEST_TIMEOUT, timeout
    try:
        s = sample(grid, window, sig.location.lat, sig.location.lon)
    except (requests.RequestException, GridError, OSError, KeyError, ValueError):
        return None
    finally:
        REQUEST_TIMEOUT = old
    if s.value is None or s.value < grid.min_signal:
        return None
    span = "last hour" if window == "1h" else f"last {window}"
    return f"radar estimates {metric.display(s.value).split(' (')[0]} here ({span})"


def _mark(sig: Signal, verdict_: dict[str, Any]) -> None:
    sig.evidence = {**sig.evidence, "grid": verdict_}
    db.set_evidence(int(sig.id or 0), sig.evidence)
