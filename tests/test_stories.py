"""Storms as stories: members cluster by place and time, stories merge, idle ones close."""
from __future__ import annotations

import json
from datetime import timedelta

from intelnet import db, stories
from intelnet.models import iso, utcnow


def test_nearby_members_share_a_story_and_far_ones_start_their_own(fresh_db):
    now = utcnow()
    a = stories.attach("weather", "event", 1, 39.78, -89.65, now, severity=0.8, title="Hail 1.75 in — Sangamon")
    b = stories.attach("weather", "alert", "T1", 39.95, -89.60, now + timedelta(minutes=20), severity=1.0,
                       title="Severe Thunderstorm Warning")
    c = stories.attach("weather", "event", 2, 41.88, -87.63, now, severity=0.5, title="Rain — Cook")   # 280 km
    assert a.opened and not b.opened and b.story_id == a.story_id and c.opened and c.story_id != a.story_id
    s = db.story(a.story_id)
    assert s["n_members"] == 2 and s["severity"] == 1.0 and s["title"] == "Severe Thunderstorm Warning"
    assert set(json.loads(s["counties_json"])) >= {"17167"}
    again = stories.attach("weather", "event", 1, 39.78, -89.65, now, severity=0.9, title="Hail 2 in — Sangamon")
    assert again.story_id == a.story_id and db.story(a.story_id)["n_members"] == 2     # updated, not added


def test_a_soil_member_never_joins_a_weather_story_and_stale_stories_are_left(fresh_db):
    now = utcnow()
    w = stories.attach("weather", "event", 1, 39.78, -89.65, now - timedelta(hours=5))
    s = stories.attach("soil", "event", 2, 39.78, -89.65, now)
    later = stories.attach("weather", "event", 3, 39.78, -89.65, now)                  # 5 h on: a new storm
    assert len({w.story_id, s.story_id, later.story_id}) == 3


def test_two_stories_that_grow_together_merge_into_the_older(fresh_db):
    now = utcnow()
    west = stories.attach("weather", "event", 1, 39.80, -90.40, now)                   # ~65 km apart
    east = stories.attach("weather", "event", 2, 39.80, -89.65, now)
    assert west.story_id != east.story_id
    mid = stories.attach("weather", "alert", "T9", 39.80, -90.02, now)                  # between: joins one…
    assert mid.merged == [east.story_id] and mid.story_id == west.story_id              # …and they meet
    assert db.story(east.story_id)["merged_into"] == west.story_id
    assert stories.current(east.story_id) == west.story_id
    assert db.story(west.story_id)["n_members"] == 3


def test_idle_stories_close(fresh_db):
    old = stories.attach("weather", "event", 1, 39.78, -89.65, utcnow() - timedelta(hours=7))
    db.update_story(old.story_id, updated_at=iso(utcnow() - timedelta(hours=7)))
    assert stories.close_idle() == 1 and db.story(old.story_id)["status"] == "closed"


def test_events_and_storm_based_warnings_gather_into_one_story(fresh_db, monkeypatch, make_sensor):
    from test_alert_threads import feature, run_feed

    from intelnet import contrib, watch

    box = [[-89.90, 39.60], [-89.40, 39.60], [-89.40, 39.95], [-89.90, 39.95], [-89.90, 39.60]]
    ann = make_sensor("tg:1", zip_code="62704")
    a = contrib.contribute(ann, "hail golf ball", source_id_base="m1", online=False,
                           use_llm=False).assessments[0]
    assert a.story is not None and a.story.opened
    run_feed(monkeypatch, feature("W1", polygon=box))                               # a polygon warning
    run_feed(monkeypatch, feature("W1", polygon=box), feature("F1", event="Flood Warning",
                                                            severity="Moderate", minutes=600))
    kinds = sorted((m["kind"], m["ref"]) for m in db.story_members(a.story.story_id))
    assert kinds == [("alert", "W1"), ("event", str(a.event["id"]))]              # no county-wide F1
    assert db.story(a.story.story_id)["title"].startswith("Hail size 1.75 in")      # the most severe member
    assert watch.run_once(only=["iem_lsr"])["stories_closed"] == 0


def test_one_card_per_storm_edited_as_it_grows(fresh_db, sent, monkeypatch, make_sensor):
    from test_alert_threads import feature, run_feed

    from intelnet import contrib
    from intelnet.feeds import nws_alerts

    box = [[-89.90, 39.60], [-89.40, 39.60], [-89.40, 39.95], [-89.90, 39.95], [-89.90, 39.60]]
    db.add_subscription("50", "weather.events", "il.sangamon")
    ann, bob = make_sensor("tg:1", zip_code="62704"), make_sensor("tg:2", name="Bob", zip_code="62711")
    def say(s, text, base):
        return contrib.contribute(s, text, source_id_base=base, online=False, use_llm=False)

    say(ann, "hail golf ball", "m1")
    say(bob, "hail 1.75in", "m2")                                     # verified: the storm's card goes out
    cards = [(c, t) for c, t in sent if c == "50"]
    assert len(cards) == 1 and "🟥" in cards[0][1] and "Sangamon County" in cards[0][1] and "verified" in cards[0][1]
    story = db.story_of("event", str(db.recent_signals(1, kinds=("human",), metric="hail_mm")[0].event_id))
    assert db.card(f"story:{story}", "50") is not None
    say(ann, "gust 70 mph", "m3")
    say(bob, "gust 65 mph", "m4")                                     # a second verified event, same storm
    assert len([c for c, _ in sent if c == "50"]) == 1                # no second message…
    assert "Wind gust" in sent.edits[-1][2] and "Hail size" in sent.edits[-1][2]     # …the card grew
    run_feed(monkeypatch, feature("W1", polygon=box))
    assert "⚠️ Severe Thunderstorm Warning" in sent.edits[-1][2]
    say(ann, "hail baseball", "m5")                                   # the hail event escalates
    assert sent.replies[-1][1] == db.card(f"story:{story}", "50")["message_id"] and not sent.replies[-1][2]
    assert "Escalating" in sent[-1][1]
    run_feed(monkeypatch)
    monkeypatch.setattr(nws_alerts, "ABSENT_CONFIRM", timedelta(0))
    run_feed(monkeypatch)
    assert "<s>Severe Thunderstorm Warning</s> ended" in sent.edits[-1][2]


def _storm(monkeypatch, make_sensor):
    from test_alert_threads import feature, run_feed

    from intelnet import contrib

    box = [[-89.90, 39.60], [-89.40, 39.60], [-89.40, 39.95], [-89.90, 39.95], [-89.90, 39.60]]
    ann, bob = make_sensor("tg:1", zip_code="62704"), make_sensor("tg:2", name="Bob", zip_code="62711")
    for s, text, base in ((ann, "hail golf ball", "m1"), (bob, "hail 1.75in", "m2"),
                          (ann, "gust 70 mph", "m3"), (bob, "gust 65 mph", "m4")):
        contrib.contribute(s, text, source_id_base=base, online=False, use_llm=False)
    run_feed(monkeypatch, feature("W1", polygon=box))
    return db.story_of("event", "1")


def test_the_brief_without_an_llm_is_built_from_the_facts_and_cites_them(fresh_db, monkeypatch, make_sensor):
    from intelnet import story_brief, subscriptions

    sid = _storm(monkeypatch, make_sensor)
    fs = story_brief.facts(sid)
    assert [f.kind for f in fs] == ["alert", "event", "event"]
    assert story_brief.current(sid) == ("Hail size up to 1.75 in [2] and wind gust up to 70 mph [3], "
                                        "from 2 people. 1 NWS warning in effect [1].")
    card = subscriptions.format_story(sid)
    assert "⚠️ Severe Thunderstorm Warning [1]" in card and "verified [3]" in card


def test_an_llm_brief_is_kept_only_when_grounded(fresh_db, sent, monkeypatch, make_sensor):
    from intelnet import llm, story_brief
    from intelnet.config import settings

    db.add_subscription("50", "weather.events", "il.sangamon")
    sid = _storm(monkeypatch, make_sensor)
    monkeypatch.setattr(settings, "llm_enabled", True)
    good = "Golf-ball hail, 1.75 in, fell in Sangamon County [2] as gusts reached 70 mph [3] under a warning [1]."
    monkeypatch.setattr(llm, "call", lambda *a, **k: good)
    assert story_brief.cards_changed(story_brief.write_briefs()) == 0          # edits, not new messages
    assert story_brief.current(sid) == good and good.replace("'", "&#x27;") in sent.edits[-1][2]
    assert story_brief.write_briefs() == []                                     # once per version
    for bad in ("Hail up to 3 in fell [2].",                                    # a number not in the facts
                "Hail fell across the county.",                                 # no citation
                "Hail fell [9]."):                                              # a fact that doesn't exist
        assert not story_brief.valid(bad, story_brief.facts(sid))
    db.update_story(sid, brief_at=None)
    monkeypatch.setattr(llm, "call", lambda *a, **k: "Hail up to 3 in fell [2].")
    story_brief.write_briefs()
    assert story_brief.current(sid).startswith("Hail size up to 1.75 in [2]")   # the template stands


def test_storms_reach_the_digest_the_morning_brief_and_the_site(fresh_db, monkeypatch, make_sensor):
    from intelnet import brief, digest, export, story_brief

    _storm(monkeypatch, make_sensor)
    [st] = story_brief.summaries(24)
    assert st["counties"] == ["Sangamon"] and len(st["facts"]) == 3 and st["brief"].endswith("[1].")
    model = digest.build(24)
    assert model.storms[0]["id"] == st["id"]
    assert "Storms:" in digest.render_text(model) and "[2] Hail size peak 1.75 in" in digest.render_text(model)
    assert "What the brief cites" not in digest.render_html(model) and st["title"] in digest.render_html(model)
    assert "⛈ <b>" in brief.compose("17167", {})
    assert export.snapshot(days=1)["storms"][0]["brief"] == st["brief"]


def test_a_clock_time_never_vouches_for_a_number():
    from intelnet.story_brief import Fact, valid

    facts = [Fact("alert:W1", "alert", "NWS Severe Thunderstorm Warning, issued 3:01 PM, in effect", {}),
             Fact("event:1", "event", "Hail size peak 1.75 in in Sangamon County, from 2 people, verified", {})]
    assert not valid("Hail up to 3 in fell [2].", facts)                 # "3" only appears inside 3:01
    assert valid("A warning came at 3:01 PM [1] and hail reached 1.75 in [2].", facts)
    assert not valid("A warning came at 3:15 PM [1].", facts)             # a time the facts don't have
