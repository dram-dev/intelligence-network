"""USGS earthquakes near Illinois — the instrument half of a felt report.

The FDSN event service is keyless. The box reaches well past the state line on
purpose: the Wabash Valley and New Madrid zones sit off the southern corner,
and a quake there is felt across Illinois long before anything is felt at its
epicentre. Each quake becomes one signal per known property — magnitude, and
the community intensity from "Did You Feel It?" once enough people have
answered — placed on the county nearest the epicentre and sharing a
`group_key` so the digest counts the quake once.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from intelnet import db, geo
from intelnet.config import settings
from intelnet.feeds.base import ReferenceFeed
from intelnet.models import KIND_AUTHORITY, Signal, utcnow
from intelnet.topics import get_topic

logger = logging.getLogger(__name__)

API = "https://earthquake.usgs.gov/fdsnws/event/1/query"
KV_LAST_POLL = "usgs_quake_last_poll"
SENSOR = "quake:usgs"
POLL_MINUTES = 15
LOOKBACK_HOURS = 48
MIN_MAGNITUDE = 2.0
#: Illinois plus the seismic zones whose quakes are felt here.
BOX = {"minlatitude": 35.5, "maxlatitude": 43.5, "minlongitude": -93.0, "maxlongitude": -85.5}
#: Past this, a quake in the box is nobody's county event here.
MAX_COUNTY_KM = 150


def fetch(hours: int = LOOKBACK_HOURS) -> dict[str, Any]:
    since = (utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    r = requests.get(API, params={"format": "geojson", "starttime": since,
                                  "minmagnitude": MIN_MAGNITUDE, "orderby": "time", **BOX},
                     headers={"User-Agent": settings.nws_user_agent}, timeout=30)
    if r.status_code in (204, 404):      # FDSN says "no events matched" this way
        return {"type": "FeatureCollection", "features": []}
    r.raise_for_status()
    return r.json()


def parse_quakes(payload: dict[str, Any], topic_name: str = "quake") -> list[Signal]:
    """GeoJSON FeatureCollection → signals (pure; no I/O)."""
    topic = get_topic(topic_name)
    mapping = topic.mapping("usgs_quake_fields")
    out: list[Signal] = []
    for feat in payload.get("features") or []:
        props = feat.get("properties") or {}
        coords = ((feat.get("geometry") or {}).get("coordinates")) or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        depth = float(coords[2]) if len(coords) > 2 and coords[2] is not None else None
        quake_id = str(feat.get("id") or props.get("code") or "")
        when = props.get("time")
        if not quake_id or when is None:
            continue
        observed = datetime.fromtimestamp(float(when) / 1000, tz=timezone.utc)
        county = geo.nearest_county(lat, lon)
        if county is None:
            continue
        km = geo.haversine_km(lat, lon, county.lat, county.lon)
        if km > MAX_COUNTY_KM:
            continue
        location = geo.Location(lat=lat, lon=lon, county_fips=county.fips,
                                label=str(props.get("place") or county.label), precision="point")
        evidence = {
            "kind": "quake", "place": props.get("place"), "depth_km": depth,
            "felt_reports": props.get("felt"), "alert": props.get("alert"),
            "tsunami": props.get("tsunami"), "status": props.get("status"),
            "url": props.get("url") or f"https://earthquake.usgs.gov/earthquakes/eventpage/{quake_id}",
            "km_to_county": round(km, 1),
        }
        for field_name, spec in mapping.items():
            value = props.get(field_name)
            if value is None:
                continue
            metric = topic.metrics.get(str(spec.get("metric")))
            if metric is None:
                continue
            try:
                canon = metric.convert(float(value), str(spec.get("unit") or metric.default_unit))
            except (TypeError, ValueError):
                continue
            if not metric.in_range(canon):
                continue
            out.append(Signal(
                source="usgs_quake", source_id=f"{quake_id}|{metric.key}", sensor_id=SENSOR,
                sensor_kind=KIND_AUTHORITY, topic=topic.name, metric=metric.key, value=canon,
                unit=metric.unit, text=str(props.get("title") or props.get("place") or ""),
                observed_at=observed, location=location, group_key=quake_id, evidence=evidence,
                confidence=1.0, quality="reference",
            ))
    return out


class USGSQuakeFeed(ReferenceFeed):
    """Earthquakes in and around Illinois, from the USGS event service."""

    name = "usgs_quake"
    sensor_kind = KIND_AUTHORITY
    trust = 1.0
    topic = "quake"
    doc = "USGS FDSN earthquakes (M2.0+) in and around Illinois, on the nearest county"

    def should_run(self) -> bool:
        last = db.kv_get(KV_LAST_POLL)
        from intelnet.models import parse_iso

        stamp = parse_iso(last)
        return not (stamp and utcnow() - stamp < timedelta(minutes=POLL_MINUTES))

    def fetch_signals(self) -> list[Signal]:
        signals = parse_quakes(fetch(), topic_name=self.topic)
        db.kv_set(KV_LAST_POLL, utcnow().isoformat())
        if signals:
            db.ensure_reference_sensor(SENSOR, self.sensor_kind, "USGS",
                                       geo.Location(precision="state"), self.trust)
        return signals
