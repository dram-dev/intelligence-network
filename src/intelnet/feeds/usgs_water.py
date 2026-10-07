"""USGS stream gauges: the fixed water network, from the USGS Water Data API.

The latest value of every Illinois stream site's gauge height (00065, ft), discharge (00060,
cfs) and water temperature (00010, °C), from the `latest-continuous` collection in one request
(about 600 values at 330 USGS sites); the sites' names and counties from `monitoring-locations`,
kept in kv for a day. Each site is a `station` sensor `gauge:<site number>` at trust 0.95; each
poll yields one signal per (site, parameter) at the reading's own time, so repeat polls dedup.
Polled on the station cadence (hourly). The legacy NWIS service (waterservices.usgs.gov) this
replaced answered 503 to 60% of polls by 6 Oct 2026 and every one that evening.
A value more than STALE_AFTER older than the newest in the same response is dropped rather
than stored as news (an active site can carry a parameter that stopped long ago).
"""
from __future__ import annotations

import json
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

API = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
KV_LAST_POLL = "usgs_last_poll"
KV_SITES = "usgs_sites"
PARAMETERS = ("00065", "00060", "00010")
STALE_AFTER = timedelta(days=2)


def _state_fips() -> str:
    return next(iter(geo.counties().values())).fips[:2]


def parse_sites(payload: dict[str, Any]) -> dict[str, list[str]]:
    """monitoring-locations → {site number: [name, county code]}, USGS sites only."""
    out: dict[str, list[str]] = {}
    for f in payload.get("features") or []:
        agency, _, number = str(f.get("id") or "").partition("-")
        if agency == "USGS" and number:
            p = f.get("properties") or {}
            out[number] = [str(p.get("monitoring_location_name") or number), str(p.get("county_code") or "")]
    return out


def fetch_sites() -> dict[str, Any]:
    r = requests.get(f"{API}/monitoring-locations/items", timeout=60, headers={"User-Agent": settings.nws_user_agent},
                     params={"f": "json", "limit": 10000, "state_code": _state_fips(), "site_type_code": "ST",
                             "properties": "monitoring_location_name,county_code"})
    r.raise_for_status()
    return r.json()


def sites() -> dict[str, list[str]]:
    """The state's stream sites, refreshed once a day."""
    today = utcnow().strftime("%Y-%m-%d")
    try:
        cached = json.loads(db.kv_get(KV_SITES) or "{}")
    except ValueError:
        cached = {}
    if cached.get("date") == today and cached.get("sites"):
        return cached["sites"]
    found = parse_sites(fetch_sites())
    if found:
        db.kv_set(KV_SITES, json.dumps({"date": today, "sites": found}))
    return found or cached.get("sites") or {}


def _bbox() -> str:
    rings = [pt for rs in geo.county_outlines().values() for r in rs for pt in r]
    if not rings:
        return "-91.6,36.9,-87.0,42.6"
    xs, ys = [x for x, _ in rings], [y for _, y in rings]
    return f"{min(xs):.3f},{min(ys):.3f},{max(xs):.3f},{max(ys):.3f}"


def fetch(state: str | None = None) -> dict[str, Any]:
    """The latest value of each gauge series around the state."""
    r = requests.get(f"{API}/latest-continuous/items", timeout=60, headers={"User-Agent": settings.nws_user_agent},
                     params={"f": "json", "limit": 10000, "parameter_code": ",".join(PARAMETERS), "bbox": _bbox(),
                             "properties": "monitoring_location_id,parameter_code,time,value,unit_of_measure,qualifier"})
    r.raise_for_status()
    return r.json()


def parse_latest(payload: dict[str, Any], known: dict[str, list[str]], topic_name: str = "water") -> list[Signal]:
    """latest-continuous features at the state's USGS stream sites → signals (pure)."""
    topic = get_topic(topic_name)
    mapping = topic.mapping("usgs_parameters")
    state = _state_fips()
    now = utcnow()
    out: list[Signal] = []
    for f in payload.get("features") or []:
        p = f.get("properties") or {}
        agency, _, site = str(p.get("monitoring_location_id") or "").partition("-")
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if agency != "USGS" or site not in known or len(coords) < 2:
            continue
        pcode = str(p.get("parameter_code") or "")
        spec = mapping.get(pcode)
        metric = topic.metrics.get(spec["metric"]) if spec else None
        if metric is None:
            continue
        try:
            raw = float(p.get("value"))
            value = metric.convert(raw, spec.get("unit"))
        except (TypeError, ValueError):
            continue
        observed = parse_iso(p.get("time"))
        if raw <= -999 or observed is None or not metric.in_range(value):
            continue
        lon, lat = float(coords[0]), float(coords[1])
        name, county = known[site]
        c = geo.county(state + county.zfill(3)) if county else None
        c = c or geo.county_at(lat, lon)
        loc = geo.Location(lat=lat, lon=lon, county_fips=c.fips if c else None, label=name.title(),
                           precision="point")
        z = geo.nearest_zcta(lat, lon)
        if z:
            loc.zip5 = z.zip5
        out.append(Signal(
            source="usgs_water", source_id=f"{site}|{pcode}|{observed.isoformat()}",
            sensor_id=f"gauge:{site}", sensor_kind=KIND_STATION, topic=topic.name,
            metric=metric.key, value=value, unit=metric.unit, text=None, observed_at=observed,
            received_at=now, location=loc, confidence=1.0, quality="reference",
            evidence={"kind": "gauge", "site": site, "name": name, "site_type": "ST", "parameter": pcode,
                      "raw_value": raw, "raw_unit": p.get("unit_of_measure"), "qualifiers": p.get("qualifier"),
                      "url": f"https://waterdata.usgs.gov/monitoring-location/{site}/"},
        ))
    if out:
        newest = max(s.observed_at for s in out)
        out = [s for s in out if newest - s.observed_at <= STALE_AFTER]
    return out


class USGSWaterFeed(ReferenceFeed):
    """USGS stream gauges: stage, discharge, water temperature (hourly cadence)."""

    name = "usgs_water"
    sensor_kind = KIND_STATION
    trust = 0.95
    topic = "water"
    doc = "USGS Water Data API: the latest stage / discharge / water temp at the state's gauges"

    def should_run(self) -> bool:
        last = parse_iso(db.kv_get(KV_LAST_POLL))
        return not (last and utcnow() - last < timedelta(minutes=settings.station_poll_minutes))

    def fetch_signals(self) -> list[Signal]:
        payload = fetch()
        db.kv_set(KV_LAST_POLL, utcnow().isoformat())
        signals = parse_latest(payload, sites(), self.topic)
        seen: set[str] = set()
        for s in signals:
            if s.sensor_id in seen:
                continue
            seen.add(s.sensor_id)
            db.ensure_reference_sensor(s.sensor_id, self.sensor_kind,
                                       f"{s.location.label} ({s.evidence.get('site')})", s.location, self.trust)
        return signals
