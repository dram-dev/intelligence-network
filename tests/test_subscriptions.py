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
    # a county-wide product: the county's keys plus every ZIP-level subscription inside it
    cook = db.matching_chat_ids("weather.alerts", ["il", "il.cook"], zip5s=geo.zip5s_in_county("17031"))
    assert sorted(cook) == ["1", "2", "3", "4"]
    sangamon = db.matching_chat_ids("weather.alerts", ["il", "il.sangamon"],
                                    zip5s=geo.zip5s_in_county("17167"))
    assert sorted(sangamon) == ["1", "5"]
    assert sorted(db.matching_chat_ids("weather.alerts", [], zip5s=["60601"])) == ["3", "4"]


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


def test_county_alert_reaches_zip_and_zip4_subscribers_in_that_county(fresh_db, sent):
    db.add_subscription("20", "weather.alerts", "il.zip.60601")          # Chicago: Cook County
    db.add_subscription("20", "weather.alerts", "il.zip.60601-2001")     # same chat, finer key
    db.add_subscription("21", "weather.alerts", "il.zip.60601-2001")
    db.add_subscription("22", "weather.alerts", "il.zip.62704")          # Springfield: Sangamon
    db.add_subscription("23", "weather.warnings", "il.zip.60601")        # Moderate is not a warning
    alert = Signal(
        source="nws_alerts", source_id="a3|17031", sensor_id="nws:x", sensor_kind="authority",
        topic="weather", metric="alert.flood_advisory", value=2, text="Flood Advisory",
        expires_at=utcnow() + timedelta(hours=2), location=geo.location_from_county(geo.county("17031")),
        group_key="a3", evidence={"event": "Flood Advisory", "severity": "Moderate"},
    )
    assert subscriptions.fanout_alert(alert, [alert]) == 2
    assert sorted(c for c, _ in sent) == ["20", "21"]


def test_fanout_report_excludes_the_author_and_hides_identity(fresh_db, sent, make_sensor):
    from intelnet.models import public_handle

    make_sensor("tg:1", zip_code="62704", chat_id="1")
    db.add_subscription("1", "weather.reports", "il.sangamon")
    db.add_subscription("2", "weather.reports", "il.sangamon")
    sig = Signal(source="telegram", source_id="x", sensor_id="tg:1", sensor_kind="human", topic="weather",
                 metric="rain_mm", value=25.4, unit="mm", location=geo.location_from_zip("62704-1234"))
    db.insert_signal(sig)
    assert subscriptions.fanout_report(sig) == 1 and sent[0][0] == "2"
    text = sent[0][1]
    assert "Rainfall" in text and public_handle("tg:1") in text and "62704, Sangamon County" in text
    assert "Ann" not in text and "62704-1234" not in text and "tg:1" not in text


def test_banned_sensor_chat_is_skipped(fresh_db, make_sensor):
    make_sensor("tg:5", zip_code="62704", chat_id="5")
    db.add_subscription("5", "weather.alerts", "il")
    db.set_sensor_status("tg:5", "banned")
    assert db.matching_chat_ids("weather.alerts", ["il"]) == []


def test_people_see_local_times_and_impact_not_utc(fresh_db):
    from datetime import datetime, timezone

    from intelnet import digest, telegram

    when = datetime(2026, 9, 29, 22, 15, tzinfo=timezone.utc)                # 5:15 PM in Illinois
    assert telegram.tg_time(when) == (f'<tg-time unix="{int(when.timestamp())}" format="wt">'
                                      "Tue 5:15 PM CDT</tg-time>")
    alert = Signal(
        source="nws_alerts", source_id="z|17167", sensor_id="nws:x", sensor_kind="authority",
        topic="weather", metric="alert.severe_thunderstorm_warning", value=3, expires_at=when,
        location=geo.location_from_county(geo.county("17167")), group_key="z",
        evidence={"event": "Severe Thunderstorm Warning", "severity": "Severe",
                  "impact": {"maxHailSize": 1.75, "maxWindGust": 70.0,
                             "thunderstormDamageThreat": "CONSIDERABLE"}},
    )
    text = subscriptions.format_alert(alert, ["Sangamon County"])
    assert "Until <tg-time" in text and "22:15Z" not in text
    assert "Hail up to 1.75 in · Wind 70 mph · Damage threat: considerable" in text
    assert digest._when("2026-09-16T13:00:00+00:00") == "16 Sep 8:00 AM"
    assert digest._when("—") == "—"
