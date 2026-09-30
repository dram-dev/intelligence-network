"""NWS alert threads: updates edit the card, rises re-notify, ends close it, and the alert lane."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from intelnet import db, geo, watch
from intelnet.feeds import FEEDS, nws_alerts
from intelnet.models import iso, utcnow

SANGAMON, MENARD, LOGAN = "017167", "017129", "017107"


def feature(alert_id: str, *, event: str = "Severe Thunderstorm Warning", severity: str = "Severe",
            same: tuple[str, ...] = (SANGAMON,), refs: tuple[str, ...] = (), hail: str | None = None,
            wind: str | None = None, sent_ago: int = 0, minutes: int = 45,
            message_type: str = "Alert", polygon: list | None = None, motion: str | None = None) -> dict:
    """One CAP message as api.weather.gov serves it."""
    now = datetime.now(timezone.utc)
    ends = (now + timedelta(minutes=minutes)).isoformat()
    params = {k: [v] for k, v in (("maxHailSize", hail), ("maxWindGust", wind),
                                  ("eventMotionDescription", motion)) if v}
    return {"id": f"https://api.weather.gov/alerts/{alert_id}",
            "geometry": {"type": "Polygon", "coordinates": [polygon]} if polygon else None,
            "properties": {
        "id": alert_id, "@id": f"https://api.weather.gov/alerts/{alert_id}", "event": event,
        "severity": severity, "messageType": message_type,
        "sent": (now - timedelta(minutes=sent_ago)).isoformat(), "expires": ends, "ends": ends,
        "senderName": "NWS Lincoln IL", "headline": f"{event} issued by NWS Lincoln IL",
        "geocode": {"SAME": list(same)}, "parameters": params,
        "references": [{"identifier": r} for r in refs],
        "instruction": "Move to an interior room on the lowest floor.",
    }}


def run_feed(monkeypatch, *features: dict, etag: str | None = None):
    monkeypatch.setattr(nws_alerts, "fetch", lambda *a, **k: ({"features": list(features)}, etag))
    return FEEDS["nws_alerts"]().run()


def test_an_update_edits_the_card_instead_of_pushing_again(fresh_db, sent, monkeypatch):
    db.add_subscription("1", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("A1", hail="1.00", sent_ago=10))
    run_feed(monkeypatch, feature("A2", refs=("A1",), hail="1.00", sent_ago=2, message_type="Update"))
    assert [c for c, _ in sent] == ["1"]                                  # one push, ever
    card = db.card("alert:A1", "1")
    assert [(c, m) for c, m, _ in sent.edits] == [("1", card["message_id"])]
    assert "updated" in sent.edits[0][2]
    groups = nws_alerts.active_alert_groups()
    assert len(groups) == 1 and groups[0]["signal"].evidence["alert_id"] == "A2"


def test_rising_impact_notifies_again_as_a_reply_to_the_card(fresh_db, sent, monkeypatch):
    db.add_subscription("1", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("B1", hail="1.00", sent_ago=10))
    run_feed(monkeypatch, feature("B2", refs=("B1",), hail="1.75", sent_ago=2, message_type="Update"))
    assert [c for c, _ in sent] == ["1", "1"]
    card = db.card("alert:B1", "1")
    assert sent.replies[1] == ("1", card["message_id"], False)           # quotes the card, with sound
    assert "hail up to 1.75 in (was 1 in)" in sent[1][1]
    assert len(sent.edits) == 1 and "Hail up to 1.75 in" in sent.edits[0][2]


def test_an_alert_gone_from_the_feed_ends_early_with_a_quiet_all_clear(fresh_db, sent, monkeypatch):
    db.add_subscription("1", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("C1", sent_ago=10))
    run_feed(monkeypatch)                                  # missing once: not ended yet
    t = db.alert_thread("C1")
    assert t["status"] == "active" and t["missing_since"]
    monkeypatch.setattr(nws_alerts, "ABSENT_CONFIRM", timedelta(0))
    run_feed(monkeypatch)
    t = db.alert_thread("C1")
    assert (t["status"], t["ended_reason"]) == ("ended", "cancelled")
    assert "Ended early" in sent.edits[-1][2] and "<s>Severe Thunderstorm Warning</s>" in sent.edits[-1][2]
    assert sent.replies[-1][2] is True and "has ended" in sent[-1][1]   # silent all-clear
    assert nws_alerts.active_alert_groups() == []


def test_an_empty_feed_is_not_an_all_clear_while_several_alerts_are_in_force(fresh_db, monkeypatch):
    run_feed(monkeypatch, feature("D1", same=(SANGAMON,)), feature("D2", same=(MENARD,)),
             feature("D3", same=(LOGAN,)))
    monkeypatch.setattr(nws_alerts, "ABSENT_CONFIRM", timedelta(0))
    run_feed(monkeypatch)
    threads = db.active_alert_threads()
    assert len(threads) == 3 and not any(t["missing_since"] for t in threads)


def test_expiry_ends_a_thread_even_when_the_feed_is_unchanged(fresh_db, monkeypatch):
    run_feed(monkeypatch, feature("E1"), etag='W/"v1"')
    assert db.kv_get(nws_alerts.ETAG_KEY) == 'W/"v1"'
    db.save_alert_thread("E1", expires_at=iso(utcnow() - timedelta(minutes=1)))
    monkeypatch.setattr(nws_alerts, "fetch", lambda *a, **k: (None, 'W/"v1"'))      # 304
    res = FEEDS["nws_alerts"]().run()
    assert res.fetched == 0 and db.alert_thread("E1")["ended_reason"] == "expired"


def test_a_chat_the_update_no_longer_covers_gets_its_card_closed(fresh_db, sent, monkeypatch):
    db.add_subscription("1", "weather.warnings", "il.menard")
    db.add_subscription("2", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("F1", same=(SANGAMON, MENARD), sent_ago=10))
    run_feed(monkeypatch, feature("F2", refs=("F1",), same=(SANGAMON,), sent_ago=2, message_type="Update"))
    closed = [t for c, _, t in sent.edits if c == "1"]
    assert closed and "No longer includes your area" in closed[-1] and "Sangamon County" in closed[-1]
    assert db.card("alert:F1", "1")["state"] == "ended" and db.card("alert:F1", "2")["state"] == "active"
    assert sorted(c for c, _ in sent) == ["1", "2"]                      # nobody pushed twice


def test_impact_tags_come_from_the_pack_and_rises_are_worded():
    impact = nws_alerts.parse_impact({"maxHailSize": ["Up to .75"], "maxWindGust": ["70 MPH"],
                                      "tornadoDetection": ["radar indicated"],
                                      "thunderstormDamageThreat": ["CONSIDERABLE"]})
    assert impact == {"maxHailSize": 0.75, "maxWindGust": 70.0, "tornadoDetection": "RADAR INDICATED",
                      "thunderstormDamageThreat": "CONSIDERABLE"}
    assert nws_alerts.parse_impact({"maxHailSize": ["0.00"]}) == {}
    prev = {"event": "Severe Thunderstorm Warning", "severity": "Severe",
            "impact_json": json.dumps({"maxHailSize": 1.0, "tornadoDetection": "RADAR INDICATED"})}
    ev = {"event": "Severe Thunderstorm Warning", "severity": "Severe",
          "impact": {"maxHailSize": 1.75, "tornadoDetection": "OBSERVED"}}
    assert nws_alerts.rises(prev, ev) == ["hail up to 1.75 in (was 1 in)",
                                          "tornado: observed (was radar indicated)"]
    assert nws_alerts.rises(prev, {**ev, "impact": {"maxHailSize": 0.75}}) == []   # smaller is not news


def test_fetch_asks_with_the_last_etag_and_a_304_is_nothing_new(monkeypatch):
    seen: list[str | None] = []

    class _Resp:
        def __init__(self, status: int, body: dict | None = None, etag: str | None = None) -> None:
            self.status_code, self._body = status, body
            self.headers = {"ETag": etag} if etag else {}

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict | None:
            return self._body

    answers = iter([_Resp(200, {"features": []}, 'W/"1"'), _Resp(304)])

    def _get(url, params=None, headers=None, timeout=None):
        seen.append((headers or {}).get("If-None-Match"))
        return next(answers)

    monkeypatch.setattr(nws_alerts.requests, "get", _get)
    assert nws_alerts.fetch() == ({"features": []}, 'W/"1"')
    assert nws_alerts.fetch(etag='W/"1"') == (None, 'W/"1"')
    assert seen == [None, 'W/"1"']


def test_the_watch_leaves_alerts_to_a_live_alert_loop(fresh_db, monkeypatch):
    for name, cls in FEEDS.items():
        if name != "nws_alerts":
            monkeypatch.setattr(cls, "fetch_signals", lambda self: [])
    polls: list[int] = []
    monkeypatch.setattr(nws_alerts, "fetch", lambda *a, **k: polls.append(1) or ({"features": []}, None))
    watch.alert_pass()
    assert polls == [1] and watch.alert_loop_alive()
    assert watch.run_once()["feeds"]["nws_alerts"]["skipped"] and polls == [1]
    db.kv_set(watch.ALERT_LOOP_HEARTBEAT, iso(utcnow() - timedelta(minutes=10)) or "")
    assert not watch.run_once()["feeds"]["nws_alerts"]["skipped"] and polls == [1, 1]


def test_point_in_polygon_and_storm_arrival():
    square = [[[-90.0, 39.5], [-89.0, 39.5], [-89.0, 40.0], [-90.0, 40.0], [-90.0, 39.5]]]
    assert geo.point_in_polygon(39.75, -89.5, square) and not geo.point_in_polygon(40.2, -89.5, square)
    at = datetime(2026, 9, 29, 21, 30, tzinfo=timezone.utc)
    lat, lon = 39.80, -89.64
    west = [[lat, lon - 20 / (111.32 * math.cos(math.radians(lat)))]]          # 20 km west
    eta = geo.storm_arrival(at, 270, 43, west, lat, lon)                       # east at 43 kt
    assert eta is not None and abs((eta - at).total_seconds() / 60 - 15.1) < 1
    assert geo.storm_arrival(at, 90, 43, west, lat, lon) is None               # heading away
    line = [[lat + 0.3, lon - 0.35], [lat - 0.3, lon - 0.35]]                  # a line 30 km west
    assert geo.storm_arrival(at, 270, 30, line, lat, lon) is not None          # sweeps across
    assert geo.storm_arrival(at, 270, 43, [[lat + 0.5, lon - 0.2]], lat, lon) is None   # 55 km north


BOX = [[-89.90, 39.60], [-89.40, 39.60], [-89.40, 39.95], [-89.90, 39.95], [-89.90, 39.60]]


def test_the_card_says_whether_you_are_inside_and_when_the_storm_arrives(fresh_db, sent, monkeypatch,
                                                                         make_sensor):
    make_sensor("tg:1", zip_code="62704", chat_id="1")
    db.set_sensor_location("tg:1", geo.location_from_point(39.78, -89.65, online=False))   # a real point
    make_sensor("tg:2", zip_code="62707", chat_id="2")                  # home known only as a ZIP
    db.set_live_location("4", geo.location_from_point(39.70, -89.60, online=False),
                         utcnow() + timedelta(hours=1))                 # out and about
    for chat in ("1", "2", "3", "4"):
        db.add_subscription(chat, "weather.warnings", "il.sangamon")
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-00:00")
    run_feed(monkeypatch, feature("G1", polygon=BOX, motion=f"{at}...storm...270DEG...40KT...39.78,-89.95"))
    cards = dict(sent)
    assert "Your home is inside the warned area" in cards["1"]
    assert "The storm reaches you about <tg-time" in cards["1"] and 'format="r"' in cards["1"]
    assert "The center of ZIP 62707 is" in cards["2"]
    assert "Share your location" in cards["3"]
    assert "You are inside the warned area" in cards["4"]
    assert sent[-1][0] == "3"                  # people inside the polygon go out first


def test_an_update_that_newly_covers_you_rings_again(fresh_db, sent, monkeypatch, make_sensor):
    make_sensor("tg:1", zip_code="62704", chat_id="1")
    db.set_sensor_location("tg:1", geo.location_from_point(39.78, -89.65, online=False))
    db.add_subscription("1", "weather.warnings", "il.sangamon")
    short = [[-90.30, 39.60], [-89.80, 39.60], [-89.80, 39.95], [-90.30, 39.95], [-90.30, 39.60]]
    wider = [[-90.30, 39.60], [-89.40, 39.60], [-89.40, 39.95], [-90.30, 39.95], [-90.30, 39.60]]
    run_feed(monkeypatch, feature("H1", polygon=short, sent_ago=10))
    assert "outside the warned area" in sent[0][1]
    run_feed(monkeypatch, feature("H2", refs=("H1",), polygon=wider, sent_ago=2, message_type="Update"))
    assert len(sent) == 2 and "now covers your location" in sent[1][1] and sent.replies[1][2] is False
    assert "inside the warned area" in sent.edits[-1][2]


def test_rows_from_before_threading_retire_when_their_alert_leaves_the_feed(fresh_db, monkeypatch):
    old = nws_alerts.parse_alerts({"features": [feature("L1", sent_ago=60), feature("L2", sent_ago=30)]})
    db.insert_signals(old)                                   # stored as they were before threading
    assert len(nws_alerts.active_alert_groups()) == 2
    run_feed(monkeypatch, feature("L2", sent_ago=30))        # L1 was superseded; only L2 is live
    groups = nws_alerts.active_alert_groups()
    assert [g["signal"].evidence["alert_id"] for g in groups] == ["L2"]
