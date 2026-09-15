"""ASOS/AWOS station observations (IEM 'currents') — the fixed mesonet.

Every airport station in the state becomes a `station` sensor at trust 0.9.
Each poll yields one signal per (station, metric) at the station's own
observation time, so `INSERT OR IGNORE` dedups repeat polls of an unchanged
ob. Polled at most every STATION_POLL_MINUTES — the fine cadence is for
alerts and storm reports, not for 56 stations × 8 metrics.
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

KV_LAST_POLL = "asos_last_poll"


def parse_currents(payload: dict[str, Any], topic_name: str = "weather") -> list[Signal]:
    topic = get_topic(topic_name)
    out: list[Signal] = []
    now = utcnow()
    for row in payload.get("data") or []:
        station = str(row.get("station") or "").upper()
        if not station or row.get("lat") is None or row.get("lon") is None:
            continue
        valid = parse_iso(row.get("utc_valid"))
        if valid is None:
            continue
        lat, lon = float(row["lat"]), float(row["lon"])
        c = geo.county_by_name(str(row.get("county") or "")) or geo.nearest_county(lat, lon)
        loc = geo.Location(lat=lat, lon=lon, county_fips=c.fips if c else None,
                           label=str(row.get("name") or station).title(), precision="point")
        for col, mapping in topic.station_fields.items():
            raw = row.get(col)
            if raw is None:
                continue
            metric = topic.metrics.get(mapping["metric"])
            if metric is None:
                continue
            try:
                value = metric.convert(float(raw), mapping.get("unit"))
            except (TypeError, ValueError):
                continue
            if not metric.in_range(value):
                continue
            out.append(Signal(
                source="iem_asos", source_id=f"{station}|{row.get('utc_valid')}|{metric.key}",
                sensor_id=f"station:{station.lower()}", sensor_kind=KIND_STATION, topic=topic.name,
                metric=metric.key, value=value, unit=metric.unit, text=None, observed_at=valid,
                received_at=now, location=loc, confidence=1.0, quality="reference",
                evidence={"kind": "station", "station": station, "name": row.get("name"),
                          "raw_field": col, "raw_value": raw, "metar": row.get("raw")},
            ))
    return out


def fetch(state: str | None = None) -> dict[str, Any]:
    r = requests.get(
        "https://mesonet.agron.iastate.edu/api/1/currents.json",
        params={"network": f"{state or settings.geo_state}_ASOS"},
        timeout=40,
    )
    r.raise_for_status()
    return r.json()


class IEMASOSFeed(ReferenceFeed):
    """ASOS/AWOS station observations for the state (temp, wind, gust, rain, pressure…)."""

    name = "iem_asos"
    sensor_kind = KIND_STATION
    trust = 0.9
    doc = "Airport ASOS/AWOS observations via IEM currents (hourly cadence)"

    def should_run(self) -> bool:
        last = parse_iso(db.kv_get(KV_LAST_POLL))
        if last and utcnow() - last < timedelta(minutes=settings.station_poll_minutes):
            return False
        return True

    def fetch_signals(self) -> list[Signal]:
        payload = fetch()
        db.kv_set(KV_LAST_POLL, utcnow().isoformat())
        signals = parse_currents(payload, self.topic)
        seen: set[str] = set()
        for s in signals:
            if s.sensor_id in seen:
                continue
            seen.add(s.sensor_id)
            db.ensure_reference_sensor(s.sensor_id, self.sensor_kind,
                                       f"{s.evidence.get('name') or s.sensor_id} ({s.evidence.get('station')})",
                                       s.location, self.trust)
        return signals
