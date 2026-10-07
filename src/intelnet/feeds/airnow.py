"""AirNow: the EPA's hourly air-monitor readings (PM2.5, ozone) for the state.

AirNow publishes every monitor's hourly value in a public file per hour (HourlyData_<UTC
hour>.dat, pipe-delimited, no key) and the monitors' places in another (the state's kept in kv
for a day). A monitor's AQS id starts with its county's FIPS code; the pack's
`airnow_parameters` (PM2.5, ozone) become `station` readings at trust 0.95, timed to the end of
their hour. Polled on the station cadence, from the newest file there is (one appears within
about an hour). Readings past the air pack's thresholds drive events like any official reading:
one monitor's hour "unhealthy for sensitive groups" stays under the push score, an unhealthy one
(smoke, an ozone action day) reaches it. Before this the air topic had no measurements at all.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import requests

from intelnet import db, geo
from intelnet.config import settings
from intelnet.feeds.base import ReferenceFeed
from intelnet.models import KIND_STATION, Signal, parse_iso, utcnow
from intelnet.topics import get_topic

logger = logging.getLogger(__name__)

BASE = "https://files.airnowtech.org/airnow"
KV_LAST_POLL = "airnow_last_poll"
KV_SITES = "airnow_sites"


def _state_fips() -> str:
    return next(iter(geo.counties().values())).fips[:2]


def _get(url: str) -> requests.Response:
    return requests.get(url, timeout=40, headers={"User-Agent": settings.nws_user_agent})


def parse_sites(text: str, state_fips: str) -> dict[str, list[Any]]:
    """Monitoring_Site_Locations_V2.dat → {AQS id: [name, lat, lon]} for the state."""
    out: dict[str, list[Any]] = {}
    for line in text.splitlines()[1:]:
        f = line.split("|")
        if len(f) < 13 or not f[1].startswith(state_fips) or f[1] in out:
            continue
        try:
            out[f[1]] = [f[6].strip(), float(f[11]), float(f[12])]
        except ValueError:
            continue
    return out


def fetch_sites() -> str:
    r = _get(f"{BASE}/today/Monitoring_Site_Locations_V2.dat")
    r.raise_for_status()
    return r.text


def sites() -> dict[str, list[Any]]:
    """The state's monitors, refreshed once a day."""
    today = utcnow().strftime("%Y-%m-%d")
    try:
        cached = json.loads(db.kv_get(KV_SITES) or "{}")
    except ValueError:
        cached = {}
    if cached.get("date") == today and cached.get("sites"):
        return cached["sites"]
    found = parse_sites(fetch_sites(), _state_fips())
    if found:
        db.kv_set(KV_SITES, json.dumps({"date": today, "sites": found}))
    return found or cached.get("sites") or {}


def fetch() -> tuple[datetime, str]:
    """The newest hourly file of the last three hours, with its hour (UTC)."""
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    for back in (1, 2, 3):
        hour = now - timedelta(hours=back)
        r = _get(f"{BASE}/{hour:%Y}/{hour:%Y%m%d}/HourlyData_{hour:%Y%m%d%H}.dat")
        if r.status_code == 404:
            continue
        r.raise_for_status()
        return hour, r.text
    raise LookupError("no AirNow hourly file in the last three hours")


def parse_hourly(text: str, hour: datetime, known: dict[str, list[Any]], topic_name: str = "air") -> list[Signal]:
    """An hourly file's rows at the state's monitors → readings (pure)."""
    topic = get_topic(topic_name)
    mapping = topic.mapping("airnow_parameters")
    observed = hour + timedelta(hours=1)                    # an hour's average is complete at its end
    now = utcnow()
    out: list[Signal] = []
    for line in text.splitlines():
        f = line.split("|")
        if len(f) < 8 or f[2] not in known or f[5] not in mapping:
            continue
        aqsid, param, raw = f[2], f[5], f[7]
        spec = mapping[param]
        metric = topic.metrics.get(spec["metric"])
        try:
            value = metric.convert(float(raw), spec.get("unit")) if metric else None
        except ValueError:
            continue
        if metric is None or value is None or value < 0 or not metric.in_range(value):
            continue
        name, lat, lon = known[aqsid]
        c = geo.county(aqsid[:5]) or geo.county_at(lat, lon)
        # sites go by agency codes (CHI_COM, SPFD_IB, BRAIDWD): the county says where
        label = f"{c.name} County air monitor" if c else f"{str(name).title()} air monitor"
        loc = geo.Location(lat=lat, lon=lon, county_fips=c.fips if c else None, label=label, precision="point")
        out.append(Signal(
            source="airnow", source_id=f"{aqsid}|{param}|{hour:%Y%m%d%H}", sensor_id=f"airnow:{aqsid}",
            sensor_kind=KIND_STATION, topic=topic.name, metric=metric.key, value=value, unit=metric.unit,
            text=None, observed_at=observed, received_at=now, location=loc, confidence=1.0, quality="reference",
            evidence={"kind": "monitor", "network": "AirNow", "site": aqsid, "name": name, "parameter": param,
                      "raw_value": raw, "raw_unit": f[6], "agency": f[8] if len(f) > 8 else None},
        ))
    return out


class AirNowFeed(ReferenceFeed):
    """The EPA's hourly PM2.5 and ozone at the state's monitors."""

    name = "airnow"
    sensor_kind = KIND_STATION
    trust = 0.95
    topic = "air"
    doc = "EPA AirNow: the state's hourly PM2.5 and ozone monitor readings"

    def should_run(self) -> bool:
        last = parse_iso(db.kv_get(KV_LAST_POLL))
        return not (last and utcnow() - last < timedelta(minutes=settings.station_poll_minutes))

    def fetch_signals(self) -> list[Signal]:
        hour, text = fetch()
        db.kv_set(KV_LAST_POLL, utcnow().isoformat())
        signals = parse_hourly(text, hour, sites(), self.topic)
        seen: set[str] = set()
        for s in signals:
            if s.sensor_id not in seen:
                seen.add(s.sensor_id)
                db.ensure_reference_sensor(s.sensor_id, self.sensor_kind,
                                           f"{s.location.label} ({s.evidence['name']}, {s.evidence['site']})",
                                           s.location, self.trust)
        return signals
