"""Follow-ups: a question after an alert, and notes telling people what their reports did."""
from __future__ import annotations

import json
from datetime import timedelta

from test_alert_threads import feature, run_feed

from intelnet import bot, contrib, db, feedback, geo, metrics
from intelnet.feeds import FEEDS, nws_alerts
from intelnet.models import KIND_OFFICIAL, Signal, parse_iso, utcnow

# a small warning polygon over Springfield: ZIP 62704's center is inside, 62707's is not
SMALL = [[-89.70, 39.74], [-89.60, 39.74], [-89.60, 39.80], [-89.70, 39.80], [-89.70, 39.74]]


def _tap(uid: int, message_id: int, data: str, cid: str) -> dict:
    return {"update_id": 700 + len(cid), "callback_query": {
        "id": cid, "from": {"id": uid, "first_name": "Q"}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": uid, "type": "private"}}}}


def _end(monkeypatch) -> None:
    """The warning leaves the feed: missing once, then ended."""
    run_feed(monkeypatch)
    monkeypatch.setattr(nws_alerts, "ABSENT_CONFIRM", timedelta(0))
    run_feed(monkeypatch)


def _contrib(sensor, text, base):
    return contrib.contribute(sensor, text, source_id_base=base, online=False, use_llm=False)


def test_an_ended_warning_asks_the_people_it_covered_what_they_saw(fresh_db, sent, monkeypatch, make_sensor):
    make_sensor("tg:21", zip_code="62704")                       # inside the polygon
    make_sensor("tg:22", name="Out", zip_code="62707")           # same county, outside it
    for chat in ("21", "22", "23"):                              # 23 has no place at all
        db.add_subscription(chat, "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("S1", polygon=SMALL, sent_ago=10))
    del sent[:], sent.replies[:], sent.markups[:]
    _end(monkeypatch)
    by_chat = {c: (text, sent.replies[i], sent.markups[i]) for i, (c, text) in enumerate(sent)}
    text, (_, reply_to, silent), markup = by_chat["21"]
    assert "has ended" in text and "What did the storm bring to your place?" in text
    assert reply_to == db.card("alert:S1", "21")["message_id"] and silent
    assert [b["text"] for row in markup["inline_keyboard"] for b in row] == \
        ["🧊 Hail", "💨 Wind", "🌧 Rain", "🌳 Damage", "✅ Nothing here"]
    for chat in ("22", "23"):                                    # just the all-clear
        assert "has ended" in by_chat[chat][0] and "❓" not in by_chat[chat][0] and by_chat[chat][2] is None
    q = db.question(markup["inline_keyboard"][0][0]["callback_data"].split(":")[1])
    assert q["thread"] == "alert:S1" and json.loads(q["reports"])[0] == "hail"
    # answers count as observed mid-warning (no storm motion to time it by)
    mid_alert = utcnow() - timedelta(minutes=5)
    assert abs((parse_iso(q["observed_at"]) - mid_alert).total_seconds()) < 90


def test_answering_takes_two_taps_is_timed_to_the_storm_and_can_be_undone(fresh_db, sent, monkeypatch,
                                                                          make_sensor):
    make_sensor("tg:21", zip_code="62704")
    db.add_subscription("21", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("S1", polygon=SMALL, sent_ago=10))
    _end(monkeypatch)
    qid = sent.markups[-1]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    observed = parse_iso(db.question(qid)["observed_at"])

    bot.handle_updates([_tap(21, 555, f"a:{qid}:0", "c1")])            # Hail → its picker, in place
    picker = sent.edit_markups[-1]["inline_keyboard"]
    assert "How big?" in sent.edits[-1][2] and picker[-1][0]["callback_data"] == f"a:{qid}"
    golf = next(b for row in picker for b in row if b["text"].startswith("Golf"))
    bot.handle_updates([_tap(21, 555, golf["callback_data"], "c2")])
    hail = db.recent_signals(1, kinds=("human",), metric="hail_mm")
    assert [round(s.value) for s in hail] == [44] and hail[0].source_id == "21:555:a0:0"
    assert abs((hail[0].observed_at - observed).total_seconds()) < 2      # when the storm was there
    assert db.question(qid)["answered_at"]
    assert [b["callback_data"] for b in sent.edit_markups[-1]["inline_keyboard"][0]] == \
        [f"u:555:{qid}", f"a:{qid}"]

    bot.handle_updates([_tap(21, 555, f"a:{qid}", "c3")])              # ➕ Add another
    bot.handle_updates([_tap(21, 555, f"a:{qid}:4", "c4")])            # ✅ Nothing here: one tap
    assert db.message_signals("telegram", "tg:21", "21:555")[-1].metric == "nothing_here"

    bot.handle_updates([_tap(21, 555, f"u:555:{qid}", "c5")])          # Undo takes both back
    assert db.message_signals("telegram", "tg:21", "21:555") == []
    assert "Removed" in sent.edits[-1][2] and "What did the storm bring" in sent.edits[-1][2]
    assert sent.edit_markups[-1]["inline_keyboard"][0][0]["callback_data"] == f"a:{qid}:0"

    bot.handle_updates([_tap(99, 555, f"a:{qid}:0", "c6")])            # someone else's question
    assert sent.answers[-1] == ("c6", "That question has expired.")


def test_one_question_every_few_hours_and_none_after_followups_off(fresh_db, sent, monkeypatch, make_sensor):
    make_sensor("tg:21", zip_code="62704")
    make_sensor("tg:24", name="Off", zip_code="62703")
    for chat in ("21", "24"):
        db.add_subscription(chat, "weather.warnings", "il.sangamon")
    assert "Follow-ups off" in bot.handle_message(
        {"message_id": 1, "date": 0, "chat": {"id": 24, "type": "private"}, "from": {"id": 24},
         "text": "/followups off"})
    run_feed(monkeypatch, feature("S1", polygon=SMALL, sent_ago=10))
    _end(monkeypatch)
    run_feed(monkeypatch, feature("S2", polygon=SMALL, sent_ago=10))
    monkeypatch.setattr(nws_alerts, "ABSENT_CONFIRM", timedelta(minutes=2))
    _end(monkeypatch)
    asked = [(c, "❓" in t) for c, t in sent if "has ended" in t]
    assert asked == [("21", True), ("24", False), ("21", False), ("24", False)]


def test_a_slow_hazard_asks_while_it_is_in_effect(fresh_db, sent, monkeypatch, make_sensor):
    make_sensor("tg:21", zip_code="62704")
    db.add_subscription("21", "weather.alerts", "il.sangamon")
    db.add_subscription("21", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("F1", event="Flood Warning", severity="Moderate", minutes=600))
    assert len(sent) == 2 and sent.replies[1][1] == db.card("alert:F1", "21")["message_id"]
    assert "Is there water where it shouldn't be" in sent[1][1]
    assert [b["text"] for b in sent.markups[1]["inline_keyboard"][0]] == ["🌊 Flooding", "✅ Nothing here"]
    q = db.question(sent.markups[1]["inline_keyboard"][0][0]["callback_data"].split(":")[1])
    assert q["observed_at"] is None                                    # answers count from when they're sent


def test_a_neighbor_agreeing_later_tells_you_and_so_does_the_event_you_made(fresh_db, sent, make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    bob = make_sensor("tg:2", name="Bob", zip_code="62711")
    _contrib(ann, "hail golf ball", "m1")
    assert not sent
    _contrib(bob, "hail 1.75in", "m2")
    notes = [t for c, t in sent if c == "1"]
    assert len(notes) == 2 and all(s for _c, _r, s in sent.replies)     # both quiet
    assert "Your report checked out" in notes[0] and "another person's report" in notes[0]
    assert "mi away" in notes[0]
    assert "helped verify an event" in notes[1] and "Hail size" in notes[1]
    assert not [c for c, _ in sent if c == "2"]              # Bob's own reply already told him


def test_an_official_storm_report_confirms_a_reading_through_the_feed(fresh_db, sent, monkeypatch, make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    _contrib(ann, "hail golf ball", "m1")
    loc = geo.location_from_zip("62703")
    db.ensure_reference_sensor("lsr:ILX", KIND_OFFICIAL, "NWS Lincoln", loc, 0.95)
    lsr = Signal(source="iem_lsr", source_id="ILX|1", sensor_id="lsr:ILX", sensor_kind=KIND_OFFICIAL,
                 topic="weather", metric="hail_mm", value=44.0, unit="mm", location=loc, quality="reference")
    feed = FEEDS["iem_lsr"]()
    monkeypatch.setattr(feed, "fetch_signals", lambda: [lsr])
    feed.run()
    notes = [t for c, t in sent if c == "1"]
    assert any("checked out" in t and "an NWS storm report" in t for t in notes)


def test_reporting_before_the_warning_is_confirmed_by_it(fresh_db, sent, monkeypatch, make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    far = make_sensor("tg:3", name="Far", zip_code="62707")          # outside the polygon
    _contrib(ann, "hail golf ball", "m1")
    _contrib(far, "trees down", "m3")                               # the warning supports it too
    del sent[:]
    run_feed(monkeypatch, feature("W1", polygon=SMALL))
    [hail] = db.recent_signals(1, kinds=("human",), metric="hail_mm")
    [trees] = db.recent_signals(1, kinds=("human",), metric="wind_damage")
    assert (hail.quality, hail.reference_agreement) == ("corroborated", "agree")
    assert trees.quality == "raw"                                   # outside the polygon
    notes = [t for c, t in sent if c == "1"]
    assert len(notes) == 1 and "ahead of the warning" in notes[0]
    assert "Severe Thunderstorm Warning" in notes[0] and "Sangamon County" in notes[0]


def test_notes_have_a_daily_limit_and_an_off_switch(fresh_db, sent, monkeypatch, make_sensor):
    monkeypatch.setattr(feedback, "DAILY_MAX", 1)
    ann = make_sensor("tg:1", zip_code="62704")
    bob = make_sensor("tg:2", name="Bob", zip_code="62711")
    _contrib(ann, "hail 1in", "m1")
    _contrib(bob, "hail 1in", "m2")
    assert len([c for c, _ in sent if c == "1"]) == 1
    feedback.set_enabled("2", False)
    _contrib(bob, "gust 60 mph", "m3")
    _contrib(ann, "gust 60 mph", "m4")
    assert not [c for c, _ in sent if c == "2"]
    feedback.set_enabled("2", True)
    assert feedback.enabled_for("2")


def test_weekly_measures(fresh_db, sent, monkeypatch, make_sensor):
    ann = make_sensor("tg:21", zip_code="62704")
    db.add_subscription("21", "weather.warnings", "il.sangamon")
    _contrib(ann, "hail quarter", "m1")
    run_feed(monkeypatch, feature("S1", polygon=SMALL, sent_ago=2))
    _end(monkeypatch)
    qid = sent.markups[-1]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    bot.handle_updates([_tap(21, 555, f"a:{qid}:4", "c1")])
    m = metrics.weekly()
    assert (m["joined"], m["activated"]) == (1, 1)
    assert (m["asked"], m["answered"]) == (1, 1)
    assert m["counties_covered"] == 1 and m["counties_total"] == 102
    assert m["alert_cards"] == 1 and 100 <= m["alert_seconds_p50"] <= 140     # sent 2 min before we saw it
    assert m["subscribers"] == 1 and m["messages"]["alerts"] >= 1 and m["messages"]["questions"] == 1
    text = "\n".join(metrics.lines(m))
    assert "1 of 1 (100%) who joined" in text and "1 of 102 counties" in text
    assert metrics.send_weekly(force=True) == 1 and sent[-1][0] == "999" and "Ask rate" in sent[-1][1]
    assert metrics.send_weekly(force=True) == 0                                # once a week
