"""NWS alert threads: updates edit the card, rises re-notify, ends close it, and the alert lane."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from intelnet import db, watch
from intelnet.feeds import FEEDS, nws_alerts
from intelnet.models import iso, utcnow

SANGAMON, MENARD, LOGAN = "017167", "017129", "017107"


def feature(alert_id: str, *, event: str = "Severe Thunderstorm Warning", severity: str = "Severe",
            same: tuple[str, ...] = (SANGAMON,), refs: tuple[str, ...] = (), hail: str | None = None,
            wind: str | None = None, sent_ago: int = 0, minutes: int = 45,
            message_type: str = "Alert") -> dict:
    """One CAP message as api.weather.gov serves it."""
    now = datetime.now(timezone.utc)
    ends = (now + timedelta(minutes=minutes)).isoformat()
    params = {k: [v] for k, v in (("maxHailSize", hail), ("maxWindGust", wind)) if v}
    return {"id": f"https://api.weather.gov/alerts/{alert_id}", "properties": {
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
