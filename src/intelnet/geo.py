"""Geography — Illinois counties, ZIP / ZIP+4, geohash, and area keys.

The network's ground is one state resolved to fine grain: county FIPS, ZIP5
(ZCTA centroid) and ZIP+4. County + ZCTA tables are vendored from the Census
Bureau gazetteer + ZCTA→county relationship file (config/geo/), so location
resolution needs no network. Two things are worth knowing:

* **ZIP+4 has no free public geocode.** The +4 is kept verbatim as the finest
  grouping / subscription key; its coordinates are the ZIP5 (ZCTA) centroid
  unless the sensor shares a Telegram location.
* **Point → county** prefers api.weather.gov's `/points` lookup (exact,
  cached in the kv table); with the lookup disabled or offline it falls back
  to the nearest county centroid, which is only approximate near borders.

Area keys form the subscription hierarchy: `il` ⊃ `il.cook` ⊃
`il.zip.60601` ⊃ `il.zip.60601-1234`. A subscription to a key matches any
signal whose area keys include it.
"""
from __future__ import annotations

import csv
import difflib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache

from intelnet.config import CONFIG_DIR, settings

logger = logging.getLogger(__name__)

_GEOHASH32 = "0123456789bcdefghjkmnpqrstuvwxyz"


# ── primitives ─────────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geohash(lat: float, lon: float, precision: int = 7) -> str:
    """Standard base-32 geohash (precision 7 ≈ 150 m cell)."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    bits = 0
    bit_count = 0
    ch = 0
    even = True
    out = []
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon > mid:
                ch = (ch << 1) | 1
                lon_lo = mid
            else:
                ch <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat > mid:
                ch = (ch << 1) | 1
                lat_lo = mid
            else:
                ch <<= 1
                lat_hi = mid
        even = not even
        bit_count += 1
        if bit_count == 5:
            out.append(_GEOHASH32[ch])
            bits += 5
            bit_count = 0
            ch = 0
    return "".join(out)


def county_slug(name: str) -> str:
    """'St. Clair' → 'stclair', 'Jo Daviess' → 'jodaviess'."""
    return re.sub(r"[^a-z0-9]", "", name.lower().replace(" county", ""))


# ── vendored tables ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class County:
    fips: str
    name: str
    lat: float
    lon: float

    @property
    def slug(self) -> str:
        return county_slug(self.name)

    @property
    def label(self) -> str:
        return f"{self.name} County"


@dataclass(frozen=True)
class Zcta:
    zip5: str
    lat: float
    lon: float
    county_fips: str


@lru_cache(maxsize=1)
def counties(state: str | None = None) -> dict[str, County]:
    """FIPS → County for the configured state (vendored CSV)."""
    st = (state or settings.geo_state).lower()
    path = CONFIG_DIR / "geo" / f"{st}_counties.csv"
    out: dict[str, County] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["fips"]] = County(
                fips=row["fips"], name=row["name"], lat=float(row["lat"]), lon=float(row["lon"])
            )
    return out


@lru_cache(maxsize=1)
def zctas(state: str | None = None) -> dict[str, Zcta]:
    st = (state or settings.geo_state).lower()
    path = CONFIG_DIR / "geo" / f"{st}_zcta.csv"
    out: dict[str, Zcta] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["zip5"]] = Zcta(
                zip5=row["zip5"], lat=float(row["lat"]), lon=float(row["lon"]),
                county_fips=row["county_fips"],
            )
    return out


@lru_cache(maxsize=1)
def _county_index() -> dict[str, County]:
    """slug → County, plus a few common spellings."""
    idx: dict[str, County] = {}
    for c in counties().values():
        idx[c.slug] = c
    return idx


def county(fips: str | None) -> County | None:
    return counties().get(fips or "")


def county_by_name(name: str) -> County | None:
    """Case/punctuation-insensitive county lookup with a fuzzy fallback."""
    if not name:
        return None
    key = county_slug(name)
    idx = _county_index()
    if key in idx:
        return idx[key]
    # 'dupage' vs 'du page', 'dekalb', 'st clair' … then close typos.
    close = difflib.get_close_matches(key, list(idx), n=1, cutoff=0.85)
    return idx[close[0]] if close else None


def zcta(zip5: str) -> Zcta | None:
    return zctas().get(zip5)


def nearest_county(lat: float, lon: float) -> County | None:
    """Approximate: nearest county centroid (exact enough away from borders)."""
    best: County | None = None
    best_d = float("inf")
    for c in counties().values():
        d = haversine_km(lat, lon, c.lat, c.lon)
        if d < best_d:
            best, best_d = c, d
    return best


def nearest_zcta(lat: float, lon: float, max_km: float = 15.0) -> Zcta | None:
    best: Zcta | None = None
    best_d = max_km
    for z in zctas().values():
        d = haversine_km(lat, lon, z.lat, z.lon)
        if d < best_d:
            best, best_d = z, d
    return best


# ── Location ───────────────────────────────────────────────────────────────

_ZIP9 = re.compile(r"^(\d{5})[- ]?(\d{4})$")
_ZIP5 = re.compile(r"^\d{5}$")
_LATLON = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


@dataclass
class Location:
    lat: float | None = None
    lon: float | None = None
    county_fips: str | None = None
    zip5: str | None = None
    zip9: str | None = None
    label: str = ""
    precision: str = "unknown"      # point | zip9 | zip5 | county | state | unknown
    state: str = field(default_factory=lambda: settings.geo_state)

    @property
    def has_point(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def geohash(self) -> str | None:
        return geohash(self.lat, self.lon) if self.has_point else None

    def area_keys(self) -> list[str]:
        """Subscription hierarchy keys this location belongs to (coarse→fine)."""
        st = self.state.lower()
        keys = [st]
        c = county(self.county_fips)
        if c:
            keys.append(f"{st}.{c.slug}")
        if self.zip5:
            keys.append(f"{st}.zip.{self.zip5}")
        if self.zip9:
            keys.append(f"{st}.zip.{self.zip9}")
        return keys

    def describe(self) -> str:
        parts = []
        c = county(self.county_fips)
        if self.zip9:
            parts.append(self.zip9)
        elif self.zip5:
            parts.append(self.zip5)
        if c:
            parts.append(c.label)
        if not parts and self.has_point:
            parts.append(f"{self.lat:.3f},{self.lon:.3f}")
        if not parts:
            parts.append(self.label or self.state)
        return ", ".join(parts)

    def describe_public(self) -> str:
        """ZIP5 + county — never the +4 or coordinates. For anything other people see."""
        parts = []
        if self.zip5:
            parts.append(self.zip5)
        c = county(self.county_fips)
        if c:
            parts.append(c.label)
        return ", ".join(parts) or self.state

    def to_dict(self) -> dict:
        return {
            "lat": self.lat, "lon": self.lon, "county_fips": self.county_fips,
            "zip5": self.zip5, "zip9": self.zip9, "label": self.label,
            "precision": self.precision, "state": self.state,
        }


def location_from_zip(text: str) -> Location | None:
    """'62704' or '62704-1234' / '627041234' → Location (ZCTA centroid)."""
    t = text.strip()
    m9 = _ZIP9.match(t)
    zip5 = zip9 = None
    if m9:
        zip5, zip9 = m9.group(1), f"{m9.group(1)}-{m9.group(2)}"
    elif _ZIP5.match(t):
        zip5 = t
    if not zip5:
        return None
    z = zcta(zip5)
    if z is None:
        return None
    return Location(
        lat=z.lat, lon=z.lon, county_fips=z.county_fips, zip5=zip5, zip9=zip9,
        label=zip9 or zip5, precision="zip9" if zip9 else "zip5",
    )


def location_from_county(c: County) -> Location:
    return Location(lat=c.lat, lon=c.lon, county_fips=c.fips, label=c.label, precision="county")


def location_from_point(lat: float, lon: float, *, online: bool | None = None) -> Location:
    """Lat/lon → Location with county (NWS lookup if allowed, else nearest)."""
    fips = None
    use_online = settings.geo_online_lookup if online is None else online
    if use_online:
        fips = _nws_point_county(lat, lon)
    if fips is None:
        c = nearest_county(lat, lon)
        fips = c.fips if c else None
    z = nearest_zcta(lat, lon)
    return Location(
        lat=lat, lon=lon, county_fips=fips, zip5=z.zip5 if z else None,
        label=f"{lat:.4f},{lon:.4f}", precision="point",
    )


def parse_location(text: str, *, online: bool | None = None) -> Location | None:
    """Free-form location token → Location, or None.

    Accepts a ZIP / ZIP+4, a county name ('cook', 'St. Clair County',
    'il.cook'), 'lat,lon', or — when online lookup is allowed — a place name
    geocoded within the state ('Normal', 'Peoria IL').
    """
    t = (text or "").strip().strip("@").strip()
    if not t:
        return None
    st = settings.geo_state.lower()
    if t.lower().startswith(f"{st}.zip."):
        t = t[len(st) + 5:]
    elif t.lower().startswith(f"{st}."):
        t = t[len(st) + 1:]
    if t.lower() == st:
        return Location(label=settings.geo_state, precision="state")
    loc = location_from_zip(t)
    if loc:
        return loc
    m = _LATLON.match(t)
    if m:
        return location_from_point(float(m.group(1)), float(m.group(2)), online=online)
    c = county_by_name(re.sub(r"(?i)\s+county$", "", t))
    if c:
        return location_from_county(c)
    use_online = settings.geo_online_lookup if online is None else online
    if use_online:
        return geocode_place(t)
    return None


# ── online lookups (cached in kv) ──────────────────────────────────────────

def _kv_cache_get(key: str) -> str | None:
    try:
        from intelnet import db
        return db.kv_get(key)
    except Exception:  # noqa: BLE001 — cache is optional
        return None


def _kv_cache_set(key: str, value: str) -> None:
    try:
        from intelnet import db
        db.kv_set(key, value)
    except Exception:  # noqa: BLE001
        pass


def _nws_point_county(lat: float, lon: float) -> str | None:
    """County FIPS for a point via api.weather.gov (cached by geohash-6)."""
    key = f"nwspt:{geohash(lat, lon, 6)}"
    cached = _kv_cache_get(key)
    if cached:
        return cached if cached != "-" else None
    try:
        import requests

        r = requests.get(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
            headers={"User-Agent": settings.nws_user_agent, "Accept": "application/geo+json"},
            timeout=10,
        )
        r.raise_for_status()
        county_url = (r.json().get("properties") or {}).get("county") or ""
        ugc = county_url.rsplit("/", 1)[-1]           # e.g. ILC031
        fips = None
        if len(ugc) == 6 and ugc[2] == "C":
            fips = _state_fips(ugc[:2]) + ugc[3:]
        _kv_cache_set(key, fips or "-")
        return fips
    except Exception as exc:  # noqa: BLE001
        logger.debug("geo: NWS point lookup failed (%s); using nearest centroid", exc)
        return None


def _state_fips(usps: str) -> str:
    """The configured state's FIPS prefix; other states resolve to '' (unknown)."""
    if usps.upper() == settings.geo_state:
        any_county = next(iter(counties().values()), None)
        return any_county.fips[:2] if any_county else ""
    return ""


def geocode_place(name: str) -> Location | None:
    """Place name → Location via Open-Meteo's free geocoder, state-restricted."""
    key = f"geocode:{settings.geo_state}:{name.strip().lower()}"
    cached = _kv_cache_get(key)
    if cached:
        if cached == "-":
            return None
        d = json.loads(cached)
        return location_from_point(d["lat"], d["lon"], online=False)
    try:
        import requests

        q = re.sub(r"(?i),?\s*(il|illinois)$", "", name.strip())
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": q, "count": 10, "language": "en", "format": "json",
                    "countryCode": "US"},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        state_name = {"IL": "Illinois"}.get(settings.geo_state, settings.geo_state)
        hit = next((x for x in results if x.get("admin1") == state_name), None)
        if not hit:
            _kv_cache_set(key, "-")
            return None
        _kv_cache_set(key, json.dumps({"lat": hit["latitude"], "lon": hit["longitude"]}))
        loc = location_from_point(hit["latitude"], hit["longitude"])
        loc.label = f"{hit.get('name')}, {settings.geo_state}"
        return loc
    except Exception as exc:  # noqa: BLE001
        logger.debug("geo: geocode failed for %r (%s)", name, exc)
        return None


def same_to_fips(same: str) -> str | None:
    """NWS SAME code ('017031') → county FIPS ('17031') when it's in-state."""
    s = (same or "").strip()
    if len(s) == 6 and s[0] == "0":
        fips = s[1:]
        return fips if fips in counties() else None
    return None
