"""What's ahead: the day's forecast for a place, and the outlooks that cover it.

A pack's `ahead` section names the sources. For weather: the National Weather Service
forecast for a point (`points_url` gives the place's forecast URL, kept in kv by
geohash-6), and outlooks published as polygons with a level (the Storm Prediction
Center's severe-storm outlook, the Weather Prediction Center's excessive-rainfall
outlook). The brief gives each county its day and any risk over it; the digest a table
of the pack's `places` and the counties at risk; /forecast answers for one place.

An outlook comes as Day 1, Day 2…, each issuance valid for a window; the one whose window
covers the forecast day's noon is used (at 01:10 the Day 1 issuance can still be last
night's, so the day comes from Day 2). Every fetch is best-effort: a source that fails
leaves its lines out, never the brief or the digest. `AHEAD_ENABLED=false` (and the test
suite) turns every fetch off.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from intelnet import geo
from intelnet.config import settings
from intelnet.models import parse_iso, utcnow
from intelnet.topics import topics

logger = logging.getLogger(__name__)

TIMEOUT = 12
TTL = 30 * 60                       # one process asks a source at most every half hour
_memo: dict[str, tuple[float, Any]] = {}


@dataclass(frozen=True)
class Period:
    """One forecast period: 'Today', 'Tonight', 'Wednesday'…"""
    name: str
    daytime: bool
    start: datetime
    end: datetime
    temp: int | None
    unit: str
    short: str                      # "Chance showers and thunderstorms"
    pop: int | None                 # chance of precipitation, %
    wind: str                       # "SW 15–25 mph"

    def text(self) -> str:
        """'Sunny, high 86°F' · 'Showers likely (60%), low 54°F' · '…, wind SW 15–25 mph'."""
        bits = [self.short + (f" ({self.pop}%)" if self.pop is not None and self.pop >= 20 else "")]
        if self.temp is not None:
            bits.append(f"{'high' if self.daytime else 'low'} {self.temp}°{self.unit}")
        if _windy(self.wind):
            bits.append(f"wind {self.wind}")
        return ", ".join(bits)


@dataclass(frozen=True)
class Risk:
    """An outlook's level over a place: 'Severe storms: marginal risk today (level 1 of 5…)'."""
    name: str                       # "Severe storms"
    word: str                       # "Marginal"
    level: int
    of: int
    source: str                     # "Storm Prediction Center"
    emoji: str
    day: str                        # "today", "tomorrow", "Thursday"

    def text(self) -> str:
        return f"{self.word.lower()} risk {self.day} (level {self.level} of {self.of}, {self.source})"


@dataclass
class Day:
    periods: list[Period] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.periods or self.risks)


def spec() -> dict[str, Any]:
    """The `ahead` section of the first pack that has one (weather's)."""
    return next((t.ahead for t in topics().values() if t.ahead), {})


def enabled() -> bool:
    return settings.ahead_enabled and bool(spec())


def _get(url: str) -> Any:
    """JSON from a source, tried twice, kept for TTL in this process."""
    hit = _memo.get(url)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    import requests

    last: Exception | None = None
    for _ in range(2):
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={
                "User-Agent": settings.nws_user_agent, "Accept": "application/geo+json, application/json"})
            r.raise_for_status()
            data = r.json()
            _memo[url] = (time.monotonic(), data)
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
    assert last is not None
    raise last


# ── the forecast for a point ──────────────────────────────────────────────

def _windy(wind: str) -> bool:
    """Wind is worth a mention from 15 mph."""
    speeds = [int(x) for x in re.findall(r"\d+", wind or "")]
    return bool(speeds) and max(speeds) >= 15


def _sentence(text: str) -> str:
    """'Chance Showers And Thunderstorms' → 'Chance showers and thunderstorms'."""
    text = " ".join(str(text or "").split())
    return text[:1].upper() + text[1:].lower()


def _period(x: dict[str, Any]) -> Period:
    pop = (x.get("probabilityOfPrecipitation") or {}).get("value")
    speed = str(x.get("windSpeed") or "").replace(" to ", "–")
    return Period(
        name=str(x.get("name") or ""), daytime=bool(x.get("isDaytime")),
        start=parse_iso(x.get("startTime")) or utcnow(), end=parse_iso(x.get("endTime")) or utcnow(),
        temp=int(x["temperature"]) if isinstance(x.get("temperature"), (int, float)) else None,
        unit=str(x.get("temperatureUnit") or "F"), short=_sentence(x.get("shortForecast") or ""),
        pop=int(pop) if isinstance(pop, (int, float)) else None,
        wind=" ".join(w for w in (str(x.get("windDirection") or ""), speed) if w))


def forecast_url(lat: float, lon: float) -> str | None:
    """The place's forecast URL (its forecast office's grid square), kept in kv."""
    from intelnet import db

    key = f"nwsfc:{geo.geohash(lat, lon, 6)}"
    try:
        cached = db.kv_get(key)
    except Exception:  # noqa: BLE001 — the cache is optional
        cached = None
    if cached:
        return cached
    url = ((_get(spec()["points_url"].format(lat=lat, lon=lon)) or {}).get("properties") or {}).get("forecast")
    if url:
        try:
            db.kv_set(key, url)
        except Exception:  # noqa: BLE001
            pass
    return url


def periods(lat: float, lon: float, *, n: int = 2, from_daytime: bool = True,
            now: datetime | None = None) -> list[Period]:
    """The next `n` forecast periods: from the next daytime one (at 01:10 that's 'Tuesday' and
    'Tuesday Night', at 08:00 'Today' and 'Tonight'), or from now for /forecast. The API's
    cache sometimes serves a forecast hours old ('This Afternoon' at 6:30 PM): periods that
    have ended are dropped."""
    url = forecast_url(lat, lon)
    if not url:
        return []
    now = now or utcnow()
    ps = [p for p in (_period(x) for x in ((_get(url) or {}).get("properties") or {}).get("periods") or [])
          if p.end > now]
    if from_daytime:
        first = next((i for i, p in enumerate(ps) if p.daytime), None)
        ps = ps[first:] if first is not None else []
    return ps[:n]


def icon(p: Period) -> str:
    """A symbol for the period's weather, from the pack's `icons` (first match wins)."""
    words = p.short.lower()
    hit = next((str(sym) for key, sym in spec().get("icons") or [] if str(key).lower() in words), "")
    if hit == "🌙" and p.daytime:
        return "☀️"
    return hit or ("☀️" if p.daytime else "🌙")


# ── outlooks ─────────────────────────────────────────────────────────────

def _features(o: dict[str, Any], day: int) -> list[dict[str, Any]]:
    url = str(o["url"]).format(day=day, layer=(o.get("layers") or {}).get(day, day - 1))
    return list((_get(url) or {}).get("features") or [])


def _window(o: dict[str, Any], f: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start, end = (o.get("valid") or ["", ""])[:2]
    props = f.get("properties") or {}
    try:
        a, b = parse_iso(str(props.get(start) or "")), parse_iso(str(props.get(end) or ""))
    except ValueError:
        return None
    return (a, b) if a and b else None


def _level(o: dict[str, Any], f: dict[str, Any]) -> tuple[int, str] | None:
    value = str((f.get("properties") or {}).get(o.get("field", "")) or "").split()
    hit = (o.get("levels") or {}).get(value[0]) if value else None
    return (int(hit[0]), str(hit[1])) if hit else None


def covering(o: dict[str, Any], at: datetime) -> list[dict[str, Any]]:
    """The outlook's features from the issuance valid at `at`: Day 1, else Day 2, else Day 3."""
    for day in sorted(int(d) for d in (o.get("layers") or {1: 0, 2: 1, 3: 2})):
        try:
            feats = _features(o, day)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ahead: %s day %s unavailable (%s)", o.get("name"), day, exc)
            continue
        hits = [f for f in feats if (w := _window(o, f)) and w[0] <= at < w[1]]
        if hits:
            return hits
    return []


def contains(geometry: dict[str, Any] | None, lat: float, lon: float) -> bool:
    """Point in a GeoJSON Polygon / MultiPolygon, holes respected."""
    if not geometry:
        return False
    polys = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        polys = [polys]
    elif geometry.get("type") != "MultiPolygon":
        return False
    return any(rings and geo.point_in_polygon(lat, lon, [rings[0]])
               and not any(geo.point_in_polygon(lat, lon, [hole]) for hole in rings[1:])
               for rings in polys)


def _risk(o: dict[str, Any], level: tuple[int, str], day: str) -> Risk:
    return Risk(name=str(o.get("name") or "Outlook"), word=level[1], level=level[0], of=int(o.get("of") or 0),
                source=str(o.get("source") or ""), emoji=str(o.get("emoji") or "⚠️"), day=day)


def risks_at(lat: float, lon: float, at: datetime, day: str) -> list[Risk]:
    """Each outlook's highest level over the point at `at`."""
    out = []
    for o in spec().get("outlooks") or []:
        levels = [lv for f in covering(o, at) if (lv := _level(o, f)) and contains(f.get("geometry"), lat, lon)]
        if levels:
            out.append(_risk(o, max(levels), day))
    return out


def county_risks(at: datetime, day: str) -> list[tuple[Risk, list[str]]]:
    """Statewide: each outlook level over Illinois, with the counties (by centroid) under it."""
    out: list[tuple[Risk, list[str]]] = []
    for o in spec().get("outlooks") or []:
        feats = [(lv, f) for f in covering(o, at) if (lv := _level(o, f))]
        if not feats:
            continue
        by_level: dict[tuple[int, str], list[str]] = {}
        for c in geo.counties().values():
            best = max((lv for lv, f in feats if contains(f.get("geometry"), c.lat, c.lon)), default=None)
            if best:
                by_level.setdefault(best, []).append(c.name)
        out += [(_risk(o, lv, day), sorted(names)) for lv, names in sorted(by_level.items(), reverse=True)]
    return out


# ── the day, put together ────────────────────────────────────────────────

def _zone() -> ZoneInfo:
    return ZoneInfo(settings.local_tz)


def target(now: datetime, ps: list[Period]) -> datetime:
    """The instant an outlook must cover: noon of the forecast's first day (after 3 PM with no
    forecast to go by: tomorrow's noon)."""
    first = next((p for p in ps if p.daytime), None)
    if first is not None:
        return first.start + timedelta(hours=6)
    local = now.astimezone(_zone())
    noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
    return noon + timedelta(days=1) if local.hour >= 15 else noon


def day_word(now: datetime, at: datetime) -> str:
    a, b = now.astimezone(_zone()).date(), at.astimezone(_zone()).date()
    if a == b:
        return "today"
    if (b - a).days == 1:
        return "tomorrow"
    return at.astimezone(_zone()).strftime("%A")


def for_point(lat: float, lon: float, *, now: datetime | None = None, n: int = 2,
              from_daytime: bool = True) -> Day:
    """The forecast periods and outlook risks for a place; empty when off or unreachable."""
    if not enabled():
        return Day()
    now = now or utcnow()
    try:
        ps = periods(lat, lon, n=n, from_daytime=from_daytime, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ahead: forecast for %.3f,%.3f unavailable (%s)", lat, lon, exc)
        ps = []
    at = target(now, ps)
    try:
        rs = risks_at(lat, lon, at, day_word(now, at))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ahead: outlooks unavailable (%s)", exc)
        rs = []
    return Day(ps, rs)


@dataclass
class Statewide:
    """The digest's view: the pack's places with their two periods, and the counties at risk."""
    places: list[tuple[str, list[Period]]] = field(default_factory=list)
    risks: list[tuple[Risk, list[str]]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.places or self.risks)


def statewide(now: datetime | None = None) -> Statewide:
    if not enabled():
        return Statewide()
    now = now or utcnow()
    out = Statewide()
    for name, lat, lon in spec().get("places") or []:
        try:
            ps = periods(float(lat), float(lon), now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ahead: forecast for %s unavailable (%s)", name, exc)
            continue
        if ps:
            out.places.append((str(name), ps))
    first = next((ps for _, ps in out.places), [])
    at = target(now, first)
    try:
        out.risks = county_risks(at, day_word(now, at))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ahead: outlooks unavailable (%s)", exc)
    return out


def names(counties: list[str], limit: int = 6) -> str:
    """'Henry County' · 'Henry, Lee and Stark counties' · 'Bureau, …, Whiteside and 8 more counties'."""
    if len(counties) == 1:
        return f"{counties[0]} County"
    if len(counties) <= limit:
        return f"{', '.join(counties[:-1])} and {counties[-1]} counties"
    return f"{', '.join(counties[:limit])} and {len(counties) - limit} more counties"
