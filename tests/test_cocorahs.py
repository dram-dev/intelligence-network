"""CoCoRaHS: the volunteer observers' morning reports become 24-hour totals that the map,
the digest and the brief read, and that open no events."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from intelnet import db, notable
from intelnet.feeds import cocorahs
from intelnet.models import Signal, utcnow

CT = ZoneInfo("America/Chicago")
TODAY = datetime.now(CT).date()


def _csv(rows: list[tuple[str, ...]]) -> str:
    head = ("ObservationDate,ObservationTime,EntryDateTime,StationNumber,StationName,Latitude,Longitude,"
            "TotalPrecipAmt,NewSnowDepth,NewSnowSWE,TotalSnowDepth,TotalSnowSWE,DateTimeStamp")
    return "\n".join([head] + [", ".join(r) for r in rows]) + "\n"


DAY = TODAY.isoformat()
CSV = _csv([
    (DAY, "07:00 AM", f"{DAY} 07:05 AM", "IL-CP-149", "Rantoul 1.4 NNE", "40.3218632", "-88.1440698",
     "2.85", "0.0", "NA", "NA", "NA", f"{DAY} 12:05 PM"),
    (DAY, "06:30 AM", f"{DAY} 06:31 AM", "IL-SG-12", "Springfield 2.1 W", "39.7900", "-89.6900",
     "0.45", "1.5", "NA", "NA", "NA", f"{DAY} 11:31 AM"),
    (DAY, "07:00 AM", f"{DAY} 07:02 AM", "IL-LK-144", "IL-LK-144", "42.3000", "-87.9000",
     "T", "0.0", "NA", "NA", "NA", f"{DAY} 12:02 PM"),                     # a trace: not kept
    (DAY, "07:00 AM", f"{DAY} 07:02 AM", "IL-CK-1", "Chicago 1.0 N", "41.89", "-87.63",
     "0.00", "NA", "NA", "NA", "NA", f"{DAY} 12:02 PM"),                    # dry: not kept
    (DAY, "07:00 AM", f"{DAY} 07:02 AM", "IL-XX-9", "Nowhere", "NA", "NA",
     "1.00", "0.0", "NA", "NA", "NA", f"{DAY} 12:02 PM"),                   # no place: skipped
])


def test_a_report_becomes_24_hour_totals(fresh_db):
    sigs = cocorahs.parse(CSV)
    assert [(s.sensor_id, s.metric) for s in sigs] == [
        ("cocorahs:il-cp-149", "rain_mm"), ("cocorahs:il-sg-12", "rain_mm"), ("cocorahs:il-sg-12", "snow_cm")]
    rantoul = sigs[0]
    assert round(rantoul.value, 2) == 72.39 and rantoul.evidence["period"] == "24h"
    assert rantoul.evidence["kind"] == "observer" and rantoul.sensor_kind == "station"
    assert rantoul.location.county_fips == "17019"                           # Champaign, by the outline
    assert rantoul.observed_at == datetime.combine(TODAY, datetime.min.time().replace(hour=7), CT).astimezone(
        timezone.utc)
    assert sigs[2].value == 1.5 * 2.54 and sigs[0].source_id == f"IL-CP-149|{DAY}|rain_mm"


def test_the_feed_stores_and_pushes_nothing(make_sensor, monkeypatch, sent):
    monkeypatch.setattr(cocorahs, "fetch", lambda day, state=None: CSV if day == TODAY else _csv([]))
    monkeypatch.setattr(cocorahs.CoCoRaHSFeed, "should_run", lambda self: True)
    res = cocorahs.CoCoRaHSFeed().run()
    assert (res.status, res.new) == ("ok", 3) and res.events_pushed == 0
    assert db.events_since(24) == [] and sent == []                         # 2.85 in is past the threshold
    assert cocorahs.CoCoRaHSFeed().run().new == 0                          # read again: ignored
    assert db.reference_network_sizes().get("cocorahs") == 2


def test_polled_hourly_through_the_reporting_day(fresh_db, monkeypatch):
    def at(hour: int):
        moment = datetime.combine(TODAY, datetime.min.time().replace(hour=hour), CT).astimezone(timezone.utc)
        monkeypatch.setattr(cocorahs, "utcnow", lambda: moment)
        return moment

    at(5)
    assert not cocorahs.CoCoRaHSFeed().should_run()                         # before 7 AM: nothing yet
    t = at(9)
    assert cocorahs.CoCoRaHSFeed().should_run()
    db.kv_set(cocorahs.KV_LAST_POLL, (t - timedelta(minutes=20)).isoformat())
    assert not cocorahs.CoCoRaHSFeed().should_run()                         # polled 20 minutes ago
    at(21)
    assert not cocorahs.CoCoRaHSFeed().should_run()                         # after 8 PM


def test_the_map_reads_a_days_total_not_an_hourly_rate(fresh_db):
    now = utcnow()
    observer, springfield, _ = cocorahs.parse(CSV)
    observer.observed_at = springfield.observed_at = now - timedelta(hours=3)
    third = replace(springfield, source_id="IL-SG-99|x|rain_mm", sensor_id="cocorahs:il-sg-99", value=5.0,
                    location=replace(springfield.location, lat=39.70, lon=-89.40))   # "highest" needs 3 sites
    station = Signal(source="iem_asos", source_id="SPI|x|rain_mm", sensor_id="station:spi", sensor_kind="station",
                     topic="weather", metric="rain_mm", value=7.6, unit="mm", text=None,
                     observed_at=now - timedelta(hours=1), received_at=now, location=observer.location,
                     confidence=1.0, quality="reference",
                     evidence={"kind": "station", "station": "SPI", "period": "1h"})
    db.insert_signals([observer, springfield, third, station])
    for s in (observer, springfield, third, station):
        db.ensure_reference_sensor(s.sensor_id, "station", s.sensor_id, s.location, 0.9)
    day = notable.build(now)
    [rain] = [p for p in day["places"] if p["label"].startswith("Rain ")]
    assert rain["label"] == "Rain 2.85 in" and rain["name"] == "1.4 mi NNE of Rantoul"
    assert rain["why"][0].startswith("Highest rainfall total in Illinois")     # ahead of the threshold
    assert rain["source"] == "CoCoRaHS observer"
