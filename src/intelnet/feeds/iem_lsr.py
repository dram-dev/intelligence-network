"""NWS Local Storm Reports via the Iowa Environmental Mesonet — official
eyewitness reports (spotters, public, emergency managers) with a point, a
magnitude and a type. These are the closest thing to ground truth the
network can compare a human contribution against, so they arrive as
`official` sensors (one per issuing WFO) at trust 0.95.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import requests

from intelnet import db, geo
from intelnet.config import settings
from intelnet.feeds.base import ReferenceFeed
from intelnet.models import KIND_OFFICIAL, Signal, parse_iso, utcnow
from intelnet.topics import get_topic

logger = logging.getLogger(__name__)


def parse_lsr(payload: dict[str, Any], topic_name: str = "weather") -> list[Signal]:
    """IEM lsr.geojson FeatureCollection → signals (pure)."""
    topic = get_topic(topic_name)
    out: list[Signal] = []
    now = utcnow()
    for feat in payload.get("features") or []:
        p = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or [p.get("lon"), p.get("lat")]
        if not coords or coords[0] is None or coords[1] is None:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        typetext = str(p.get("typetext") or "").upper()
        mapping = topic.lsr_types.get(typetext)
        if mapping is None:
            continue
        metric = topic.metrics.get(mapping["metric"])
        if metric is None:
            continue
        mag = p.get("magf")
        try:
            value = 1.0 if metric.is_flag else metric.convert(float(mag), mapping.get("unit"))
        except (TypeError, ValueError):
            if metric.is_flag:
                value = 1.0
            else:
                continue
        if not metric.in_range(value):
            continue
        valid = parse_iso(p.get("valid")) or now
        c = geo.county_by_name(str(p.get("county") or "")) or geo.nearest_county(lat, lon)
        loc = geo.Location(lat=lat, lon=lon, county_fips=c.fips if c else None,
                           label=str(p.get("city") or ""), precision="point")
        z = geo.nearest_zcta(lat, lon)
        if z:
            loc.zip5 = z.zip5
        wfo = str(p.get("wfo") or "nws").upper()
        text = " ".join(x for x in [
            typetext.title(), f"{mag}{(p.get('unit') or '').lower()}" if mag not in (None, "") and not metric.is_flag else "",
            f"— {p.get('city')}" if p.get("city") else "", f"({p.get('remark')})" if p.get("remark") else "",
        ] if x).strip()
        out.append(Signal(
            source="iem_lsr",
            source_id=f"{p.get('product_id') or ''}|{p.get('valid')}|{lat:.2f}|{lon:.2f}|{typetext}",
            sensor_id=f"lsr:{wfo.lower()}", sensor_kind=KIND_OFFICIAL, topic=topic.name,
            metric=metric.key, value=value, unit=metric.unit, text=text, observed_at=valid,
            received_at=now, location=loc, confidence=1.0, quality="reference",
            evidence={"kind": "lsr", "lsr_type": typetext, "city": p.get("city"),
                      "county": p.get("county"), "reporter": p.get("source"),
                      "remark": p.get("remark"), "wfo": wfo, "magnitude": mag,
                      "unit": p.get("unit")},
        ))
    return out


def fetch(hours: float | None = None, state: str | None = None) -> dict[str, Any]:
    end = utcnow()
    start = end - timedelta(hours=hours or settings.lsr_lookback_hours)
    r = requests.get(
        "https://mesonet.agron.iastate.edu/geojson/lsr.geojson",
        params={"sts": start.strftime("%Y-%m-%dT%H:%MZ"), "ets": end.strftime("%Y-%m-%dT%H:%MZ"),
                "states": state or settings.geo_state},
        timeout=40,
    )
    r.raise_for_status()
    return r.json()


class IEMLSRFeed(ReferenceFeed):
    """NWS Local Storm Reports (IEM mirror) — hail, wind, flooding, tornado…"""

    name = "iem_lsr"
    sensor_kind = KIND_OFFICIAL
    trust = 0.95
    doc = "NWS Local Storm Reports via IEM (spotter/public/EM reports with magnitude)"

    def fetch_signals(self) -> list[Signal]:
        signals = parse_lsr(fetch(), self.topic)
        for sid in {s.sensor_id for s in signals}:
            db.ensure_reference_sensor(sid, self.sensor_kind, f"NWS {sid.split(':')[-1].upper()} storm reports",
                                       geo.Location(precision="state"), self.trust)
        return signals
