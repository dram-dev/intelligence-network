"""High water: river gauges at action stage or above, with their trend and forecast crest.

The water pack's `rivers` section names the source: the National Weather Service's river
gauges, listed with each one's observed and forecast flood category; for the ones running
high, the stages at which the river floods and the stage forecast where one is issued
(`stageflow`: the observed series gives the trend, the forecast series the crest). The
digest lists every gauge at action stage or above, or forecast to reach it; the brief the
ones within `near_km` of the reader's county; the narrative the ones in flood.

Best-effort like ahead.py, and behind the same switch (AHEAD_ENABLED): a source that
fails leaves its lines out.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from intelnet import fetch, geo
from intelnet.config import settings
from intelnet.models import local_time, parse_iso, utcnow
from intelnet.topics import topics

logger = logging.getLogger(__name__)

TREND_HOURS = 6
TREND_STEP = 0.1                    # less than this over TREND_HOURS reads as steady
FORECAST_DAYS = 5                   # the Mississippi's forecasts run for weeks; the next days matter


def spec() -> dict[str, Any]:
    return next((t.rivers for t in topics().values() if t.rivers), {})


def enabled() -> bool:
    return settings.ahead_enabled and bool(spec())


def _get(url: str) -> Any:
    return fetch.get_json(url)


def stage_text(v: float, unit: str = "ft") -> str:
    """7 ft · 6.5 ft · 7.03 ft · 24.4 ft · 427.2 ft"""
    if float(v).is_integer():
        text = str(int(v))
    elif abs(v) >= 10:
        text = f"{v:.1f}"
    else:
        text = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}".strip()


def when(t: datetime) -> str:
    """'Wed 1 PM'; more than five days off, the date instead: 'Tue 13 Oct'."""
    if t - utcnow() > timedelta(days=5):
        return local_time(t, "%a %-d %b")
    return local_time(t, "%a %-I %p" if t.minute == 0 else "%a %-I:%M %p")


@dataclass
class Gauge:
    lid: str
    name: str                                       # "Des Plaines River near Russell"
    lat: float
    lon: float
    stage: float | None
    unit: str
    observed: tuple[int, str]                       # (2, "minor flooding"); (0, "") below action
    forecast: tuple[int, str]
    at: datetime | None = None
    county: str = ""                                # the NWS's county name, "Lake"
    floods_at: float | None = None                  # the minor-flooding stage
    trend: str = ""                                 # rising · falling · steady
    crest: tuple[float, datetime] | None = None     # the forecast's high point ahead
    falls_to: tuple[float, datetime] | None = None  # where the forecast ends, when it falls

    @property
    def level(self) -> int:
        return max(self.observed[0], self.forecast[0])

    def now_text(self) -> str:
        """'7.03 ft, steady, minor flooding' · '24.4 ft, rising'"""
        if self.stage is None:
            return self.observed[1]
        return ", ".join(x for x in (stage_text(self.stage, self.unit), self.trend, self.observed[1]) if x)

    def forecast_text(self) -> str:
        """'crest 25.3 ft Wed 1 PM, action stage' · 'falling to 6.9 ft by Thu 7 AM' · ''"""
        if self.crest:
            v, t = self.crest
            words = self.forecast[1] if self.forecast[0] > self.observed[0] else ""
            return f"crest {stage_text(v, self.unit)} {when(t)}" + (f", {words}" if words else "")
        if self.falls_to:
            v, t = self.falls_to
            return f"falling to {stage_text(v, self.unit)} by {when(t)}"
        return ""

    def text(self) -> str:
        """One line: now, the flood stage, the forecast."""
        out = self.now_text()
        if self.floods_at is not None:
            out += f" (flood stage {stage_text(self.floods_at, self.unit)})"
        fc = self.forecast_text()
        return out + (f"; forecast {fc}" if fc else "")


def _level(levels: dict[str, Any], category: Any) -> tuple[int, str]:
    hit = levels.get(str(category or ""))
    return (int(hit[0]), str(hit[1])) if hit else (0, "")


def _ours(sp: dict[str, Any], g: dict[str, Any]) -> bool:
    """A gauge in the state, or across the line within `border_km` of it (the far bank of a
    border river; the Wabash at Covington, Indiana, is the Wabash but not the border)."""
    name = str(g.get("name") or "")
    if any(w.lower() in name.lower() for w in sp.get("skip") or []):
        return False
    if str((g.get("state") or {}).get("abbreviation") or "") in (sp.get("states") or [settings.geo_state]):
        return True
    return geo.in_or_near_state(float(g.get("latitude") or 0), float(g.get("longitude") or 0),
                                float(sp.get("border_km") or 5))


def _value(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and x > -900 else None


def _series(block: dict[str, Any] | None) -> list[tuple[datetime, float]]:
    out = []
    for p in (block or {}).get("data") or []:
        t, v = parse_iso(p.get("validTime")), _value(p.get("primary"))
        if t is not None and v is not None:
            out.append((t, v))
    return sorted(out)


def _details(gauge: Gauge, sp: dict[str, Any], now: datetime) -> None:
    """The flood stage and county (gauge detail), the trend and the forecast (stageflow)."""
    detail = _get(str(sp["gauge_url"]).format(lid=gauge.lid)) or {}
    gauge.county = str(detail.get("county") or "")
    minor = ((detail.get("flood") or {}).get("categories") or {}).get("minor") or {}
    gauge.floods_at = _value(minor.get("stage"))
    flows = _get(str(sp["stageflow_url"]).format(lid=gauge.lid)) or {}
    observed = _series(flows.get("observed"))
    if observed:
        t_last, v_last = observed[-1]
        earlier = [v for t, v in observed if t <= t_last - timedelta(hours=TREND_HOURS)]
        if earlier:
            change = v_last - earlier[-1]
            gauge.trend = "rising" if change >= TREND_STEP else "falling" if change <= -TREND_STEP else "steady"
    ahead_ = [(t, v) for t, v in _series(flows.get("forecast")) if now < t <= now + timedelta(days=FORECAST_DAYS)]
    if ahead_ and gauge.stage is not None:
        top = max(ahead_, key=lambda p: (p[1], -p[0].timestamp()))       # the first time it peaks
        if top[1] > gauge.stage + 0.05:
            gauge.crest = (top[1], top[0])
        elif ahead_[-1][1] < gauge.stage - TREND_STEP:
            gauge.falls_to = (ahead_[-1][1], ahead_[-1][0])


def high_water(now: datetime | None = None) -> list[Gauge]:
    """Gauges at action stage or above, or forecast to reach it: the highest level first."""
    if not enabled():
        return []
    now = now or utcnow()
    sp = spec()
    try:
        listing = _get(str(sp["gauges_url"])) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("rivers: gauges unavailable (%s)", exc)
        return []
    levels = sp.get("levels") or {}
    out = []
    for g in listing.get("gauges") or []:
        if not _ours(sp, g):
            continue
        status = g.get("status") or {}
        obs, fc = status.get("observed") or {}, status.get("forecast") or {}
        o, f = _level(levels, obs.get("floodCategory")), _level(levels, fc.get("floodCategory"))
        if not (o[0] or f[0]):
            continue
        gauge = Gauge(lid=str(g.get("lid") or ""), name=str(g.get("name") or ""),
                      lat=float(g.get("latitude") or 0), lon=float(g.get("longitude") or 0),
                      stage=_value(obs.get("primary")), unit=str(obs.get("primaryUnit") or "ft"),
                      observed=o, forecast=f, at=parse_iso(obs.get("validTime")))
        try:
            _details(gauge, sp, now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rivers: %s details unavailable (%s)", gauge.lid, exc)
        out.append(gauge)
    return sorted(out, key=lambda x: (-x.level, x.name))


def near(gauges: list[Gauge], county: geo.County) -> list[Gauge]:
    """The gauges within `near_km` of a county's centre, nearest first."""
    km = float(spec().get("near_km") or 40)
    hits = [(geo.haversine_km(county.lat, county.lon, g.lat, g.lon), g) for g in gauges]
    return [g for d, g in sorted(hits, key=lambda x: x[0]) if d <= km]
