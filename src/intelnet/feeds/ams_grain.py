"""USDA AMS Market News — the published side of what an elevator is paying.

Futures are public and basis is not: it moves by elevator, by day, and the
only people who see it are the ones at the scale. AMS surveys it and publishes
report 3192, "Illinois Grain Bids", each afternoon — corn, soybean and wheat
basis and cash price for twelve trading districts, from Chicago to Little
Egypt. That is the number a member's "corn basis -0.35" gets checked against.

Basis arrives in cents against a futures month; the pack keeps dollars a
bushel, so a -35Z quote is stored as -0.35 with the month in evidence. A
district is quoted as a whole, so each reading sits on the county that stands
for it (`ams_region_codes` in the pack) and the metric's radius reaches the
rest of the district.

The API needs a free key from mymarketnews.ams.usda.gov, sent as the HTTP Basic
username. Without one the feed stays quiet and the pack simply has no reference.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from intelnet import db, geo
from intelnet.config import settings
from intelnet.feeds.base import ReferenceFeed
from intelnet.models import KIND_OFFICIAL, Signal, utcnow
from intelnet.topics import get_topic

logger = logging.getLogger(__name__)

API = "https://marsapi.ams.usda.gov/services/v1.2/reports"
DEFAULT_SLUGS = ("3192",)          # Illinois Grain Bids
SECTION = "Report Detail"
KV_LAST_POLL = "ams_grain_last_poll"
SENSOR = "ams:il_grain"
POLL_HOURS = 6                     # published once a weekday afternoon


def report_slugs() -> tuple[str, ...]:
    configured = [s.strip() for s in settings.ams_report_slugs.split(",") if s.strip()]
    return tuple(configured) or DEFAULT_SLUGS


def fetch(slug: str = DEFAULT_SLUGS[0], date: str | None = None) -> dict[str, Any]:
    """One report section. `date` is AMS-style MM/DD/YYYY; None = everything held."""
    params = {"q": f"report_date={date}"} if date else None
    r = requests.get(f"{API}/{slug}/{SECTION}", params=params,
                     auth=(settings.usda_mars_key, ""), timeout=45)
    if r.status_code in (204, 404):
        return {"results": []}
    r.raise_for_status()
    return r.json()


def fetch_latest(slug: str = DEFAULT_SLUGS[0], back_days: int = 6) -> dict[str, Any]:
    """The most recent published report — asking without a date returns years of it.

    Grain bids are a weekday report, so walk back over weekends and holidays
    until a date answers with rows.
    """
    today = datetime.now(ZoneInfo(settings.local_tz)).date()
    for delta in range(back_days + 1):
        payload = fetch(slug, (today - timedelta(days=delta)).strftime("%m/%d/%Y"))
        if payload.get("results"):
            return payload
    return {"results": []}


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _observed(row: dict[str, Any]) -> datetime:
    """When the quote was published, in UTC (AMS stamps it in central time)."""
    stamp = str(row.get("published_date") or "").strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            naive = datetime.strptime(stamp, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=ZoneInfo(settings.local_tz)).astimezone(timezone.utc)
    try:
        day = datetime.strptime(str(row.get("report_date") or ""), "%m/%d/%Y")
    except ValueError:
        return utcnow()
    return day.replace(hour=18, tzinfo=timezone.utc)      # early afternoon, central


def parse_bids(payload: dict[str, Any], topic_name: str = "markets",
               *, spot_only: bool = True) -> list[Signal]:
    """AMS "Report Detail" rows → signals (pure; no I/O).

    Each district is quoted twice: a spot bid and a forward delivery window.
    "Corn basis today" means the spot one, so the forward rows are left out
    unless `spot_only` is off.
    """
    topic = get_topic(topic_name)
    commodities = topic.mapping("ams_fields")
    regions = topic.mapping("ams_region_codes")
    out: list[Signal] = []
    for row in payload.get("results") or []:
        if spot_only and str(row.get("current")).strip().lower() != "yes":
            continue
        spec = commodities.get(str(row.get("commodity")))
        region = regions.get(str(row.get("trade_loc")))
        if not spec or not region:
            if row.get("commodity") and not region:
                logger.debug("ams_grain: no county for district %r", row.get("trade_loc"))
            continue
        county = geo.county_by_name(str(region.get("county")))
        if county is None:
            continue
        location = geo.location_from_county(county)
        observed = _observed(row)
        where = f"{row.get('trade_loc')} · {row.get('delivery_point')}"
        group = (f"{row.get('report_date')}|{row.get('trade_loc')}|"
                 f"{row.get('delivery_point')}|{row.get('commodity')}")
        evidence = {
            "kind": "grain_bid", "district": row.get("trade_loc"),
            "delivery_point": row.get("delivery_point"), "commodity": row.get("commodity"),
            "futures_month": row.get("basis Max Futures Month") or row.get("basis Min Futures Month"),
            "delivery_start": row.get("delivery_start"), "delivery_end": row.get("delivery_end"),
            "report": row.get("report_title"), "report_date": row.get("report_date"),
            "trans_mode": row.get("trans_mode"), "freight": row.get("freight"),
            "url": "https://mymarketnews.ams.usda.gov/viewReport/3192",
        }
        # basis first (cents a bushel against the board), then the cash price
        for metric_key, raw, unit in (
            (spec.get("basis"), row.get("basis Max", row.get("basis Min")), "cents"),
            (spec.get("price"), row.get("avg_price") or row.get("price Max"), "$/bu"),
        ):
            metric = topic.metrics.get(str(metric_key))
            value = _number(raw)
            if metric is None or value is None:
                continue
            try:
                canon = metric.convert(value, unit)
            except ValueError:
                continue
            if not metric.in_range(canon):
                continue
            out.append(Signal(
                source="ams_grain", source_id=f"{group}|{metric.key}", sensor_id=SENSOR,
                sensor_kind=KIND_OFFICIAL, topic=topic.name, metric=metric.key, value=canon,
                unit=metric.unit, text=f"{row.get('commodity')} — {where}",
                observed_at=observed, location=location, group_key=group, evidence=evidence,
                confidence=1.0, quality="reference",
            ))
    return out


class AMSGrainFeed(ReferenceFeed):
    """Illinois grain bids and basis by trading district, from USDA AMS."""

    name = "ams_grain"
    sensor_kind = KIND_OFFICIAL
    trust = 0.95
    topic = "markets"
    doc = "USDA AMS Market News grain bids (report 3192), by Illinois trading district"

    def should_run(self) -> bool:
        if not settings.usda_mars_key.strip():
            return False
        from intelnet.models import parse_iso

        last = parse_iso(db.kv_get(KV_LAST_POLL))
        return not (last and utcnow() - last < timedelta(hours=POLL_HOURS))

    def fetch_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        for slug in report_slugs():
            try:
                signals += parse_bids(fetch_latest(slug), topic_name=self.topic)
            except Exception as exc:  # noqa: BLE001 — one report can't sink the rest
                logger.warning("ams_grain: report %s failed: %s", slug, exc)
        db.kv_set(KV_LAST_POLL, utcnow().isoformat())
        if signals:
            db.ensure_reference_sensor(SENSOR, self.sensor_kind, "USDA AMS Market News",
                                       geo.Location(precision="state"), self.trust)
        return signals
