"""NRCS SCAN soil stations (AWDB REST) — soil moisture + temperature by depth.

Illinois has exactly one SCAN station (Mason #1, 2004:IL:SCAN); the feed is
written for any number. The shallowest depth of each element becomes the
reading (SMS → soil_moisture_pct, STO → soil_temp_c), and the deeper depths
ride along in evidence. Daily values; polled once a day.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from intelnet import db, geo
from intelnet.config import settings
from intelnet.feeds.base import ReferenceFeed
from intelnet.models import KIND_STATION, Signal, parse_iso, utcnow
from intelnet.topics import get_topic

logger = logging.getLogger(__name__)

KV_LAST_POLL = "scan_last_poll"
KV_STATIONS = "scan_stations"
BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"


def parse_awdb(payload: list[dict[str, Any]], stations: dict[str, dict[str, Any]],
               topic_name: str = "soil") -> list[Signal]:
    """AWDB /data response → one signal per (station, element) at the shallowest depth."""
    topic = get_topic(topic_name)
    mapping = topic.mapping("awdb_elements")
    out: list[Signal] = []
    now = utcnow()
    for st in payload or []:
        triplet = str(st.get("stationTriplet") or "")
        meta = stations.get(triplet) or {}
        lat, lon = meta.get("latitude"), meta.get("longitude")
        if lat is None or lon is None:
            continue
        best: dict[str, tuple[int, dict[str, Any], dict[str, Any]]] = {}
        profile: dict[str, dict[str, float]] = {}
        for series in st.get("data") or []:
            el = series.get("stationElement") or {}
            code = str(el.get("elementCode") or "")
            if code not in mapping:
                continue
            depth = int(el.get("heightDepth") or 0)
            vals = [v for v in (series.get("values") or []) if v.get("value") is not None]
            if not vals:
                continue
            latest = vals[-1]
            profile.setdefault(code, {})[str(depth)] = float(latest["value"])
            cur = best.get(code)
            if cur is None or depth > cur[0]:          # -2 in is shallower than -8 in
                best[code] = (depth, el, latest)
        c = geo.county_by_name(str(meta.get("countyName") or "")) or geo.nearest_county(float(lat), float(lon))
        loc = geo.Location(lat=float(lat), lon=float(lon), county_fips=c.fips if c else None,
                           label=str(meta.get("name") or triplet), precision="point")
        for code, (depth, el, latest) in best.items():
            metric = topic.metrics.get(mapping[code]["metric"])
            if metric is None:
                continue
            try:
                value = metric.convert(float(latest["value"]), mapping[code].get("unit"))
            except (TypeError, ValueError):
                continue
            if not metric.in_range(value):
                continue
            observed = _day_end(str(latest.get("date")))
            out.append(Signal(
                source="nrcs_scan", source_id=f"{triplet}|{code}|{latest.get('date')}",
                sensor_id=f"scan:{triplet.split(':')[0].lower()}", sensor_kind=KIND_STATION,
                topic=topic.name, metric=metric.key, value=value, unit=metric.unit, text=None,
                observed_at=observed, received_at=now, location=loc, confidence=1.0,
                quality="reference",
                evidence={"kind": "scan", "station": triplet, "name": meta.get("name"),
                          "element": code, "depth_in": depth, "profile": profile.get(code),
                          "raw_value": latest.get("value"), "raw_unit": el.get("storedUnitCode")},
            ))
    return out


def _day_end(date_s: str) -> datetime:
    try:
        d = datetime.strptime(date_s[:10], "%Y-%m-%d")
    except ValueError:
        return utcnow()
    return d.replace(hour=12, tzinfo=timezone.utc)   # daily value → noon UTC that day


def fetch_stations(state: str | None = None) -> dict[str, dict[str, Any]]:
    """SCAN stations in the state (the API's state filter is unreliable; filter here)."""
    st = (state or settings.geo_state).upper()
    r = requests.get(f"{BASE}/stations", params={"networkCds": "SCAN", "activeOnly": "true"}, timeout=60)
    r.raise_for_status()
    return {s["stationTriplet"]: s for s in r.json() if str(s.get("stationTriplet", "")).endswith(f":{st}:SCAN")}


def fetch_data(triplets: list[str]) -> list[dict[str, Any]]:
    end = utcnow().date()
    begin = end - timedelta(days=3)
    r = requests.get(
        f"{BASE}/data",
        params={"stationTriplets": ",".join(triplets), "elements": "SMS:*,STO:*", "duration": "DAILY",
                "beginDate": begin.isoformat(), "endDate": end.isoformat(), "periodRef": "END",
                "centralTendencyType": "NONE", "returnFlags": "false",
                "returnOriginalValues": "false", "returnSuspectData": "false"},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()


class NRCSScanFeed(ReferenceFeed):
    """NRCS SCAN soil moisture + temperature (daily cadence)."""

    name = "nrcs_scan"
    sensor_kind = KIND_STATION
    trust = 0.9
    topic = "soil"
    doc = "NRCS SCAN soil moisture / temperature by depth (AWDB); daily"

    def should_run(self) -> bool:
        last = parse_iso(db.kv_get(KV_LAST_POLL))
        return not (last and utcnow() - last < timedelta(hours=20))

    def fetch_signals(self) -> list[Signal]:
        import json

        cached = db.kv_get(KV_STATIONS)
        stations = json.loads(cached) if cached else None
        if not stations:
            stations = fetch_stations()
            db.kv_set(KV_STATIONS, json.dumps(stations))
        db.kv_set(KV_LAST_POLL, utcnow().isoformat())
        if not stations:
            return []
        signals = parse_awdb(fetch_data(list(stations)), stations, self.topic)
        for s in {x.sensor_id: x for x in signals}.values():
            db.ensure_reference_sensor(s.sensor_id, self.sensor_kind,
                                       f"SCAN {s.location.label}", s.location, self.trust)
        return signals
