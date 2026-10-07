"""CoCoRaHS: the volunteer observers' morning rain and snow (the Community Collaborative Rain,
Hail and Snow Network).

Hundreds of Illinois volunteers read a standard 4-inch gauge each morning, about 7 AM, and
report the 24 hours to then; CoCoRaHS publishes every report in a daily CSV. Each measurable
amount (the pack's `cocorahs_fields`: rain, new snow) becomes a `station` reading over a
24-hour period, so it is compared only with other 24-hour amounts and reads as a total, not a
rate: the map's heaviest rain, "What stood out", a county's rain in the brief. A report tells
of a day that's over, so it opens no event and pushes nothing; it does confirm the rain people
reported near it that day (`after_store`: network.settle_by_daily_total). Polled hourly
from 7 AM to 8 PM, today's and yesterday's reports (late ones come in through the day); a
report read again is ignored (source_id = station|date|metric). Trace and zero amounts aren't
kept.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from intelnet import db, geo
from intelnet.config import settings
from intelnet.feeds.base import FeedResult, ReferenceFeed
from intelnet.models import KIND_STATION, Signal, parse_iso, utcnow
from intelnet.topics import get_topic

logger = logging.getLogger(__name__)

URL = "https://data.cocorahs.org/export/exportreports.aspx"
KV_LAST_POLL = "cocorahs_last_poll"
POLL_MINUTES = 60
HOURS = (7, 20)                     # the local hours reports come in


def parse(text: str, topic_name: str = "weather") -> list[Signal]:
    """The export's rows → readings: one per station and measurable amount."""
    topic = get_topic(topic_name)
    fields = topic.mapping("cocorahs_fields")
    zone = ZoneInfo(settings.local_tz)
    now = utcnow()
    out: list[Signal] = []
    for row in csv.DictReader(io.StringIO(text), skipinitialspace=True):
        station = str(row.get("StationNumber") or "").strip().upper()
        day = str(row.get("ObservationDate") or "").strip()
        try:
            lat, lon = float(row["Latitude"]), float(row["Longitude"])
            observed = datetime.strptime(f"{day} {str(row['ObservationTime']).strip()}",
                                         "%Y-%m-%d %I:%M %p").replace(tzinfo=zone)
        except (KeyError, TypeError, ValueError):
            continue
        if not station:
            continue
        name = str(row.get("StationName") or station).strip()
        loc: geo.Location | None = None
        for col, mapping in fields.items():
            raw = str(row.get(col) or "").strip()
            try:
                amount = float(raw)
            except ValueError:
                continue                                    # "T" (trace), "NA"
            metric = topic.metrics.get(mapping["metric"])
            if amount <= 0 or metric is None:
                continue
            value = metric.convert(amount, mapping.get("unit"))
            if not metric.in_range(value):
                continue
            if loc is None:
                c = geo.county_at(lat, lon)
                loc = geo.Location(lat=lat, lon=lon, county_fips=c.fips if c else None, label=name,
                                   precision="point")
            out.append(Signal(
                source="cocorahs", source_id=f"{station}|{day}|{metric.key}",
                sensor_id=f"cocorahs:{station.lower()}", sensor_kind=KIND_STATION, topic=topic.name,
                metric=metric.key, value=value, unit=metric.unit, text=None,
                observed_at=observed.astimezone(timezone.utc), received_at=now, location=loc,
                confidence=1.0, quality="reference",
                evidence={"kind": "observer", "network": "CoCoRaHS", "station": station, "name": name,
                          "raw_field": col, "raw_value": raw,
                          **({"period": mapping["period"]} if mapping.get("period") else {})},
            ))
    return out


def fetch(day: date, state: str | None = None) -> str:
    r = requests.get(URL, timeout=40, headers={"User-Agent": settings.nws_user_agent}, params={
        "ReportType": "Daily", "dtf": "1", "Format": "CSV", "State": state or settings.geo_state,
        "ReportDateType": "reportdate", "Date": day.strftime("%m/%d/%Y"), "TimesInGMT": "False"})
    r.raise_for_status()
    return r.text


class CoCoRaHSFeed(ReferenceFeed):
    """The volunteer observers' 24-hour rain and new snow, each morning."""

    name = "cocorahs"
    sensor_kind = KIND_STATION
    trust = 0.85
    doc = "CoCoRaHS volunteer observers: each morning's 24-hour rain and new snow"

    def should_run(self) -> bool:
        local = utcnow().astimezone(ZoneInfo(settings.local_tz))
        if not HOURS[0] <= local.hour < HOURS[1]:
            return False
        last = parse_iso(db.kv_get(KV_LAST_POLL))
        return not (last and utcnow() - last < timedelta(minutes=POLL_MINUTES))

    def fetch_signals(self) -> list[Signal]:
        today = utcnow().astimezone(ZoneInfo(settings.local_tz)).date()
        signals: list[Signal] = []
        for day in (today - timedelta(days=1), today):
            signals += parse(fetch(day), self.topic)
        db.kv_set(KV_LAST_POLL, utcnow().isoformat())
        seen: set[str] = set()
        for s in signals:
            if s.sensor_id not in seen:
                seen.add(s.sensor_id)
                db.ensure_reference_sensor(s.sensor_id, self.sensor_kind,
                                           f"{s.evidence['name']} (CoCoRaHS {s.evidence['station']})",
                                           s.location, self.trust)
        return signals

    def after_store(self, new: list[Signal], res: FeedResult) -> None:
        """A morning report tells of a day that's over: no events, no pushes (the map, the digest
        and the brief read the readings where they're stored). It does confirm the rain people
        reported near it that day, and tells them (feedback.confirmed)."""
        from intelnet import feedback, network

        for sig in new:
            feedback.confirmed(network.settle_by_daily_total(sig))
