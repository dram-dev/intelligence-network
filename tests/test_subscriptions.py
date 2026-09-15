"""Subscriptions: parsing, hierarchy matching, fan-out with dedup."""
from __future__ import annotations

from datetime import timedelta

from intelnet import db, geo, subscriptions
from intelnet.models import Signal, utcnow


def test_parse_subscription_and_area_defaults(fresh_db):
    home = geo.location_from_zip("62704")
    p = subscriptions.parse_subscription("warnings", home)
    assert (p.category, p.area, p.area_label) == ("weather.warnings", "il.sangamon", "Sangamon County")
    p = subscriptions.parse_subscription("weather.events 60601-2001", home)
    assert (p.category, p.area) == ("weather.events", "il.zip.60601-2001")
    p = subscriptions.parse_subscription("alerts cook", None)
    assert p.area == "il.cook"
    p = subscriptions.parse_subscription("alerts il", None)
    assert p.area == "il"
    assert isinstance(subscriptions.parse_subscription("nope", None), str)
    assert isinstance(subscriptions.parse_subscription("alerts atlantis", None), str)
    assert subscriptions.area_label("il.zip.60601") == "60601"
    assert subscriptions.area_label("il.stclair") == "St. Clair County"


def test_hierarchy_matching(fresh_db):
    db.add_subscription("1", "weather.alerts", "il")
    db.add_subscription("2", "weather.alerts", "il.cook")
    db.add_subscription("3", "weather.alerts", "il.zip.60601")
    db.add_subscription("4", "weather.alerts", "il.zip.60601-2001")
    db.add_subscription("5", "weather.alerts", "il.sangamon")
    db.add_subscription("6", "weather.warnings", "il.cook")   # other category
    keys = geo.location_from_zip("60601-2001").area_keys()
    assert sorted(db.matching_chat_ids("weather.alerts", keys)) == ["1", "2", "3", "4"]
    keys5 = geo.location_from_zip("60601").area_keys()      # ZIP5 only: not the +4 subscriber
    assert sorted(db.matching_chat_ids("weather.alerts", keys5)) == ["1", "2", "3"]


def test_fanout_alert_pushes_once_per_chat_and_routes_by_severity(fresh_db, sent):
    db.add_subscription("10", "weather.alerts", "il.cook")
    db.add_subscription("10", "weather.alerts", "il.zip.60601")      # same chat, two levels
    db.add_subscription("11", "weather.warnings", "il.cook")
    db.add_subscription("12", "weather.warnings", "il.lake")
    cook, lake = geo.county("17031"), geo.county("17097")
    mk = lambda fips, c: Signal(  # noqa: E731
        source="nws_alerts", source_id=f"a1|{fips}", sensor_id="nws:x", sensor_kind="authority",
        topic="weather", metric="alert.beach_hazards_statement", value=2, text="Beach Hazards",
        expires_at=utcnow() + timedelta(hours=2), location=geo.location_from_county(c),
        group_key="a1", evidence={"event": "Beach Hazards Statement", "severity": "Moderate",
                                  "url": "https://api.weather.gov/alerts/a1"},
    )
    sibs = [mk("17031", cook), mk("17097", lake)]
    n = subscriptions.fanout_alert(sibs[0], sibs)
    assert n == 1 and [c for c, _ in sent] == ["10"]      # Moderate → alerts only, once
    assert "Cook County, Lake County" in sent[0][1] and "Beach Hazards Statement" in sent[0][1]
    sent.clear()
    sev = [mk("17031", cook)]
    sev[0].evidence["severity"] = "Extreme"
    sev[0].source_id, sev[0].group_key = "a2|17031", "a2"
    n = subscriptions.fanout_alert(sev[0], sev)
    assert sorted(c for c, _ in sent) == ["10", "11"]      # Extreme → warnings + alerts
    # re-sending the same alert is a no-op
    sent.clear()
    assert subscriptions.fanout_alert(sev[0], sev) == 0 and not sent


def test_fanout_report_excludes_the_author(fresh_db, sent, make_sensor):
    ann = make_sensor("tg:1", zip_code="62704", chat_id="1")
    db.add_subscription("1", "weather.reports", "il.sangamon")
    db.add_subscription("2", "weather.reports", "il.sangamon")
    sig = Signal(source="telegram", source_id="x", sensor_id="tg:1", sensor_kind="human", topic="weather",
                 metric="rain_mm", value=25.4, unit="mm", location=ann.location)
    db.insert_signal(sig)
    assert subscriptions.fanout_report(sig, "Ann") == 1 and sent[0][0] == "2"
    assert "Rainfall" in sent[0][1] and "Ann" in sent[0][1]


def test_fanout_digest_is_statewide_and_deduped(fresh_db, sent):
    db.add_subscription("1", "weather.digest", "il")
    db.add_subscription("2", "weather.digest", "il")
    n = subscriptions.fanout_digest("2026-09-15", "https://docs.google.com/document/d/abc/edit",
                                    "https://drive.google.com/drive/folders/f", "3 events")
    assert n == 2 and all("docs.google.com" in t for _, t in sent)
    assert subscriptions.fanout_digest("2026-09-15", "https://x", None) == 0


def test_banned_sensor_chat_is_skipped(fresh_db, make_sensor):
    make_sensor("tg:5", zip_code="62704", chat_id="5")
    db.add_subscription("5", "weather.alerts", "il")
    db.set_sensor_status("tg:5", "banned")
    assert db.matching_chat_ids("weather.alerts", ["il"]) == []
