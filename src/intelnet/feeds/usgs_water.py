"""USGS stream gauges (NWIS Instantaneous Values) — the fixed water network.

~280 Illinois sites report gauge height (00065, ft), discharge (00060, cfs)
and water temperature (00010, °C). Each site becomes a `station` sensor
`gauge:<siteCode>` at trust 0.95 with its county from the site's countyCd;
each poll yields one signal per (site, parameter) at the reading's own
timestamp, so repeat polls dedup. Polled on the station cadence (hourly).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import requests

from intelnet import db, geo
from intelnet.config import settings
from intelnet.feeds.base import ReferenceFeed
from intelnet.models import KIND_STATION, Signal, parse_iso, utcnow
from intelnet.topics import get_topic

logger = logging.getLogger(__name__)

KV_LAST_POLL = "usgs_last_poll"
PARAMETERS = ("00065", "00060", "00010")


def parse_iv(payload: dict[str, Any], topic_name: str = "water") -> list[Signal]:
    """NWIS IV JSON → signals (pure)."""
    topic = get_topic(topic_name)
    mapping = topic.mapping("usgs_parameters")
    out: list[Signal] = []
    now = utcnow()
    for ts in ((payload.get("value") or {}).get("timeSeries")) or []:
        si = ts.get("sourceInfo") or {}
        codes = si.get("siteCode") or []
        site = str(codes[0].get("value")) if codes else ""
        geoloc = ((si.get("geoLocation") or {}).get("geogLocation")) or {}
        lat, lon = geoloc.get("latitude"), geoloc.get("longitude")
        if not site or lat is None or lon is None:
            continue
        props = {p.get("name"): p.get("value") for p in si.get("siteProperty") or []}
        pcode = str(((ts.get("variable") or {}).get("variableCode") or [{}])[0].get("value"))
        spec = mapping.get(pcode)
        if spec is None:
            continue
        metric = topic.metrics.get(spec["metric"])
        if metric is None:
            continue
        values = ((ts.get("values") or [{}])[0].get("value")) or []
        if not values:
            continue
        latest = values[-1]
        try:
            raw = float(latest.get("value"))
        except (TypeError, ValueError):
            continue
        if raw <= -999:            # NWIS sentinel for missing
            continue
        try:
            value = metric.convert(raw, spec.get("unit"))
        except ValueError:
            continue
        if not metric.in_range(value):
            continue
        observed = parse_iso(latest.get("dateTime"))
        if observed is None:
            continue
        fips = str(props.get("countyCd") or "")
        c = geo.county(fips) or geo.nearest_county(float(lat), float(lon))
        loc = geo.Location(lat=float(lat), lon=float(lon), county_fips=c.fips if c else None,
                           label=str(si.get("siteName") or site).title(), precision="point")
        z = geo.nearest_zcta(float(lat), float(lon))
        if z:
            loc.zip5 = z.zip5
        out.append(Signal(
            source="usgs_water", source_id=f"{site}|{pcode}|{latest.get('dateTime')}",
            sensor_id=f"gauge:{site}", sensor_kind=KIND_STATION, topic=topic.name,
            metric=metric.key, value=value, unit=metric.unit, text=None, observed_at=observed,
            received_at=now, location=loc, confidence=1.0, quality="reference",
            evidence={"kind": "gauge", "site": site, "name": si.get("siteName"),
                      "site_type": props.get("siteTypeCd"), "parameter": pcode,
                      "raw_value": raw, "raw_unit": ((ts.get("variable") or {}).get("unit") or {}).get("unitCode"),
                      "qualifiers": latest.get("qualifiers"),
                      "url": f"https://waterdata.usgs.gov/monitoring-location/{site}/"},
        ))
    return out


def fetch(state: str | None = None) -> dict[str, Any]:
    r = requests.get(
        "https://waterservices.usgs.gov/nwis/iv/",
        params={"format": "json", "stateCd": (state or settings.geo_state).lower(),
                "parameterCd": ",".join(PARAMETERS), "siteStatus": "active"},
        headers={"User-Agent": settings.nws_user_agent},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


class USGSWaterFeed(ReferenceFeed):
    """USGS stream gauges: stage, discharge, water temperature (hourly cadence)."""

    name = "usgs_water"
    sensor_kind = KIND_STATION
    trust = 0.95
    topic = "water"
    doc = "USGS NWIS instantaneous stage / discharge / water temp for the state's gauges"

    def should_run(self) -> bool:
        last = parse_iso(db.kv_get(KV_LAST_POLL))
        return not (last and utcnow() - last < timedelta(minutes=settings.station_poll_minutes))

    def fetch_signals(self) -> list[Signal]:
        payload = fetch()
        db.kv_set(KV_LAST_POLL, utcnow().isoformat())
        signals = parse_iv(payload, self.topic)
        seen: set[str] = set()
        for s in signals:
            if s.sensor_id in seen:
                continue
            seen.add(s.sensor_id)
            db.ensure_reference_sensor(s.sensor_id, self.sensor_kind,
                                       f"{s.location.label} ({s.evidence.get('site')})", s.location, self.trust)
        return signals
