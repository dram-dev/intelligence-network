"""NEXRAD Level III radials: what one radar saw on its lowest sweep, at full resolution.

The card's picture draws a single radar's super-resolution base product (for weather,
N0B: reflectivity in 0.5° × 250 m bins, a new sweep every few minutes) the way a radar
app does, instead of a mosaic already coloured and blurred to a kilometre by someone
else. Files come from Unidata's public archive on AWS (`unidata-nexrad-level3`, keys like
`ILX_N0B_2026_09_30_23_09_24`); which radars, product and colours a topic uses is the
pack's `card_radar` section.

The format is the WSR-88D RPG-to-class-1 ICD (2620001): an optional WMO header, the
message header, the product description block (radar site, time, the value thresholds,
and whether the rest is bzip2-compressed), then a symbology block whose digital radial
packet (code 16) holds one byte per bin.
"""
from __future__ import annotations

import bz2
import logging
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

logger = logging.getLogger(__name__)

BUCKET = "https://unidata-nexrad-level3.s3.amazonaws.com"
TIMEOUT = 8.0
_KEY = re.compile(r"<Key>([^<]+)</Key>")


@dataclass
class Sweep:
    """One sweep: `data[radial, bin]` in the product's units (NaN = nothing there),
    radials' start azimuths (degrees clockwise from north) and bin edges (km)."""
    site_lat: float
    site_lon: float
    time: datetime
    azimuths: np.ndarray        # (n_radials,) start angle of each radial
    width: float                # degrees per radial
    first_km: float             # range to the first bin's near edge
    gate_km: float              # bin length
    data: np.ndarray            # (n_radials, n_bins) float32


# Products whose thresholds are "minimum, step, levels" (tenths): reflectivity. Every newer
# digital product (dual-pol: correlation coefficient 161, ZDR 159, …) carries a float scale
# and offset instead.
_REFLECTIVITY = {94, 153, 180, 186}


def _float32(hi: int, lo: int) -> float:
    return struct.unpack(">f", struct.pack(">HH", hi & 0xFFFF, lo & 0xFFFF))[0]


def decode(raw: bytes, gate_km: float = 0.25) -> Sweep:
    """A Level III digital radial product → a Sweep. Reflectivity: levels 0 = nothing,
    1 = range folded, 2… = min + (level − 2) × step. Others: (level − offset) / scale
    between the leading and trailing flag levels (the ICD's generic digital form)."""
    start = raw.find(b"\r\r\n", raw.find(b"\r\r\n") + 3) + 3 if raw[:4] == b"SDUS" else 0
    buf = raw[start:]
    pdb = buf[18:120]                                        # after the 18-byte message header
    lat, lon = struct.unpack(">ii", pdb[2:10])
    vol_days, vol_secs = struct.unpack(">hi", pdb[22:28])
    thresholds = struct.unpack(">16h", pdb[42:74])              # halfwords 31–46
    compressed = struct.unpack(">h", pdb[82:84])[0]         # halfword 51: 1 = bzip2
    body = bz2.decompress(buf[120:]) if compressed == 1 else buf[120:]
    product = struct.unpack(">h", pdb[12:14])[0]
    # symbology block: divider, id, length, layers; layer divider + length; then the packet
    pos = 16
    packet = struct.unpack(">h", body[pos:pos + 2])[0]
    if packet != 16:
        raise ValueError(f"expected a digital radial packet (16), got {packet}")
    first_bin, n_bins, _i, _j, _scale, n_radials = struct.unpack(">hhhhhh", body[pos + 2:pos + 14])
    pos += 14
    lut = np.full(256, np.nan, dtype=np.float32)
    if product in _REFLECTIVITY:
        n = min(thresholds[2], 254)                               # levels 2…255
        lut[2:2 + n] = thresholds[0] / 10 + np.arange(n, dtype=np.float32) * thresholds[1] / 10
    else:
        scale, offset = _float32(thresholds[0], thresholds[1]), _float32(thresholds[2], thresholds[3])
        top, leading, trailing = min(thresholds[5] & 0xFFFF, 255), thresholds[6], thresholds[7]
        levels = np.arange(leading, top - trailing + 1)
        lut[levels] = (levels - offset) / scale
    azimuths = np.empty(n_radials, dtype=np.float32)
    widths = np.empty(n_radials, dtype=np.float32)
    data = np.empty((n_radials, n_bins), dtype=np.uint8)
    for r in range(n_radials):
        n_bytes, angle, delta = struct.unpack(">hhh", body[pos:pos + 6])
        pos += 6
        data[r] = np.frombuffer(body, dtype=np.uint8, count=n_bins, offset=pos)
        azimuths[r], widths[r] = angle / 10, delta / 10
        pos += n_bytes + (n_bytes % 2)
    when = datetime(1969, 12, 31, tzinfo=timezone.utc) + timedelta(days=vol_days, seconds=vol_secs)
    return Sweep(lat / 1000, lon / 1000, when, azimuths, float(np.median(widths)),
                 first_bin * gate_km, gate_km, lut[data])


# ── the archive ──────────────────────────────────────────────────────────

def keys(site: str, product: str, since: datetime, until: datetime | None = None) -> list[str]:
    """The archive's files for one radar and product between two times, oldest first."""
    until = until or datetime.now(timezone.utc)
    out: list[str] = []
    hour = since.replace(minute=0, second=0, microsecond=0)
    while hour <= until:
        prefix = f"{site}_{product}_{hour:%Y_%m_%d_%H}"
        try:
            r = requests.get(BUCKET, params={"list-type": "2", "prefix": prefix}, timeout=TIMEOUT)
            out += _KEY.findall(r.text) if r.ok else []
        except requests.RequestException as exc:
            logger.info("nexrad: listing %s failed (%s)", prefix, type(exc).__name__)
        hour += timedelta(hours=1)
    return [k for k in sorted(out) if since <= key_time(k) <= until]


def key_time(key: str) -> datetime:
    """ILX_N0B_2026_09_30_23_09_24 → 2026-09-30 23:09:24 UTC."""
    return datetime.strptime(key.split("_", 2)[2], "%Y_%m_%d_%H_%M_%S").replace(tzinfo=timezone.utc)


def fetch(key: str, cache: Path | None = None) -> bytes | None:
    """One file, from the local cache when it's there (archive files never change)."""
    path = cache / key if cache else None
    if path is not None and path.exists():
        return path.read_bytes()
    try:
        r = requests.get(f"{BUCKET}/{key}", timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.info("nexrad: fetching %s failed (%s)", key, type(exc).__name__)
        return None
    if not r.ok:
        return None
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
    return r.content


# ── onto a map ───────────────────────────────────────────────────────────

def distance_km(lat: float, lon: float, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Great-circle distance from one point to each (lat, lon)."""
    p1, p2 = np.radians(lat), np.radians(lats)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lons - lon) / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def sample(sweep: Sweep, lats: np.ndarray, lons: np.ndarray, *, floor: float | None = -20.0) -> np.ndarray:
    """The sweep's value at each (lat, lon), bilinear between radials and bins so edges
    are smooth, NaN beyond the sweep. Where the radar saw nothing, `floor` stands in (so
    an echo fades out at its edge); None keeps it unknown (NaN). Ground distance on a
    sphere; at the ranges a card shows, the difference from slant range is a few hundred
    metres."""
    phi1, lam1 = np.radians(sweep.site_lat), np.radians(sweep.site_lon)
    phi2, lam2 = np.radians(lats), np.radians(lons)
    dlam = lam2 - lam1
    # great-circle range and initial bearing from the radar
    a = np.sin((phi2 - phi1) / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlam / 2) ** 2
    rng = 2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    az = np.degrees(np.arctan2(np.sin(dlam) * np.cos(phi2),
                               np.cos(phi1) * np.sin(phi2) - np.sin(phi1) * np.cos(phi2) * np.cos(dlam))) % 360
    # radials sorted by azimuth, as a regular grid of `width` steps (super-res: 720 of them)
    n = round(360 / sweep.width)
    order = np.round(((sweep.azimuths + sweep.width / 2) % 360) / sweep.width - 0.5).astype(int) % n
    grid = np.full((n, sweep.data.shape[1]), np.nan, dtype=np.float32)
    grid[order] = sweep.data
    # fractional indices of each pixel's centre; the bilinear weights treat "nothing" as a
    # value well below the lowest drawn, so an echo fades out at its edge instead of stepping
    fa = az / sweep.width - 0.5
    fr = (rng - sweep.first_km) / sweep.gate_km - 0.5
    a0 = np.floor(fa).astype(int)
    r0 = np.floor(fr).astype(int)
    ta, tr = fa - a0, fr - r0
    inside = (r0 >= 0) & (r0 + 1 < grid.shape[1])
    r0c = np.clip(r0, 0, grid.shape[1] - 2)
    g = grid if floor is None else np.where(np.isnan(grid), np.float32(floor), grid)
    v00 = g[a0 % n, r0c]
    v01 = g[a0 % n, r0c + 1]
    v10 = g[(a0 + 1) % n, r0c]
    v11 = g[(a0 + 1) % n, r0c + 1]
    out = (v00 * (1 - ta) * (1 - tr) + v01 * (1 - ta) * tr + v10 * ta * (1 - tr) + v11 * ta * tr)
    out = np.where(inside, out, np.nan)
    return out.astype(np.float32)
