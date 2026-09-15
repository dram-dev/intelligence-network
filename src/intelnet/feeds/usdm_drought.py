"""U.S. Drought Monitor county statistics — the weekly drought category.

The USDM data service returns, per county and weekly map date, the percent of
county area in each category (none, D0–D4). The signal is the highest
category covering at least a quarter of the county, on the agriculture
pack's 0–5 scale (0 none … 5 = D4). One `authority` sensor for the whole
product; one signal per county per map date; polled daily.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from intelnet import db, geo
from intelnet.config import settings
from intelnet.feeds.base import ReferenceFeed
from intelnet.models import KIND_AUTHORITY, Signal, parse_iso, utcnow
from intelnet.topics import get_topic

logger = logging.getLogger(__name__)

KV_LAST_POLL = "usdm_last_poll"
SENSOR_ID = "usdm:drought_monitor"
AREA_THRESHOLD_PCT = 25.0
LEVELS = ("d0", "d1", "d2", "d3", "d4")


def category_for(row: dict[str, Any], threshold: float = AREA_THRESHOLD_PCT) -> int:
    """0 none … 5 = D4: the worst category that covers ≥ threshold % of the county.

    USDM percentages are cumulative (D1 area is included in D0), so a check from
    the worst level down finds the deepest category with enough coverage.
    """
    for level in reversed(range(len(LEVELS))):
        try:
            pct = float(row.get(LEVELS[level]) or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct >= threshold:
            return level + 1
    return 0


def parse_usdm(rows: list[dict[str, Any]], topic_name: str = "agriculture") -> list[Signal]:
    topic = get_topic(topic_name)
    metric = topic.metrics.get("drought_category")
    if metric is None:
        return []
    out: list[Signal] = []
    now = utcnow()
    latest: dict[str, dict[str, Any]] = {}
    for r in rows or []:
        fips = str(r.get("fips") or "")
        if fips not in geo.counties():
            continue
        cur = latest.get(fips)
        if cur is None or str(r.get("mapDate")) > str(cur.get("mapDate")):
            latest[fips] = r
    for fips, r in latest.items():
        c = geo.county(fips)
        if c is None:
            continue
        value = float(category_for(r))
        observed = parse_iso(str(r.get("mapDate"))) or now
        out.append(Signal(
            source="usdm", source_id=f"{fips}|{str(r.get('mapDate'))[:10]}", sensor_id=SENSOR_ID,
            sensor_kind=KIND_AUTHORITY, topic=topic.name, metric=metric.key, value=value,
            unit=metric.unit, text=f"USDM {LEVELS[int(value) - 1].upper() if value else 'none'} — {c.label}",
            observed_at=observed, received_at=now, location=geo.location_from_county(c),
            confidence=1.0, quality="reference",
            evidence={"kind": "usdm", "map_date": str(r.get("mapDate"))[:10],
                      "pct": {k: r.get(k) for k in ("none", *LEVELS)},
                      "valid_end": r.get("validEnd"),
                      "url": "https://droughtmonitor.unl.edu/CurrentMap/StateDroughtMonitor.aspx?IL"},
        ))
    return out


def fetch(state_counties: list[str] | None = None, weeks: int = 6) -> list[dict[str, Any]]:
    fips = state_counties or list(geo.counties())
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(weeks=weeks)
    r = requests.get(
        "https://usdmdataservices.unl.edu/api/CountyStatistics/GetDroughtSeverityStatisticsByAreaPercent",
        params={"aoi": ",".join(fips), "startdate": f"{start.month}/{start.day}/{start.year}",
                "enddate": f"{end.month}/{end.day}/{end.year}", "statisticsType": "1"},
        headers={"Accept": "application/json", "User-Agent": settings.nws_user_agent},
        timeout=90,
    )
    r.raise_for_status()
    return r.json() or []


class USDMDroughtFeed(ReferenceFeed):
    """U.S. Drought Monitor weekly county drought category (daily poll)."""

    name = "usdm"
    sensor_kind = KIND_AUTHORITY
    trust = 1.0
    topic = "agriculture"
    doc = "U.S. Drought Monitor county D0–D4 coverage → drought category per county; weekly product"

    def should_run(self) -> bool:
        last = parse_iso(db.kv_get(KV_LAST_POLL))
        return not (last and utcnow() - last < timedelta(hours=20))

    def fetch_signals(self) -> list[Signal]:
        rows = fetch()
        db.kv_set(KV_LAST_POLL, utcnow().isoformat())
        db.ensure_reference_sensor(SENSOR_ID, self.sensor_kind, "U.S. Drought Monitor",
                                   geo.Location(precision="state"), self.trust)
        return parse_usdm(rows, self.topic)
