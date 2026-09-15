"""The network engine: corroboration both ways, trust, events, mesh, gaps."""
from __future__ import annotations

from datetime import timedelta

import pytest

from intelnet import contrib, db, geo, network
from intelnet.models import KIND_OFFICIAL, KIND_STATION, Signal, utcnow


def _contrib(sensor, text, base):
    return contrib.contribute(sensor, text, source_id_base=base, online=False, use_llm=False)


def test_second_sensor_corroborates_first_and_both_gain_trust(make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    bob = make_sensor("tg:2", name="Bob", zip_code="62711")      # ~4 km away
    c1 = _contrib(ann, "hail golf ball", "m1")
    assert c1.assessments[0].quality == "raw"
    c2 = _contrib(bob, "hail 1.75in", "m2")
    a = c2.assessments[0]
    assert a.quality == "corroborated" and a.n_corroborating == 1
    # Ann's earlier raw reading was corroborated back, and her trust moved.
    ann_sig = db.recent_signals(1, kinds=("human",), metric="hail_mm")
    assert {s.quality for s in ann_sig} == {"corroborated"}
    assert db.get_sensor("tg:1").trust > 0.5 and db.get_sensor("tg:2").trust > 0.5


def test_far_apart_or_too_old_readings_do_not_corroborate(make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")          # Springfield
    zed = make_sensor("tg:3", name="Zed", zip_code="60601")   # Chicago, 280 km
    _contrib(ann, "hail quarter", "m1")
    c = _contrib(zed, "hail quarter", "m2")
    assert c.assessments[0].quality == "raw"
    bob = make_sensor("tg:2", name="Bob", zip_code="62711")
    c = _contrib(bob, "hail quarter 5h ago", "m3")          # outside the 90-min window
    assert c.assessments[0].quality == "raw"


def test_two_contradictions_flag_a_reading(make_sensor):
    a = make_sensor("tg:1", zip_code="62704")
    b = make_sensor("tg:2", name="B", zip_code="62711")
    c = make_sensor("tg:3", name="C", zip_code="62702")
    _contrib(a, "temp 70", "m1")
    _contrib(b, "temp 71", "m2")
    res = _contrib(c, "temp 95", "m3")
    assert res.assessments[0].quality == "flagged" and res.assessments[0].n_contradicting == 2
    s = db.get_sensor("tg:3")
    assert s.n_contradicted == 1 and s.trust < 0.5


def test_reference_station_settles_agreement_forward_and_back(make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    loc = geo.location_from_zip("62704")
    db.ensure_reference_sensor("station:kspi", KIND_STATION, "Springfield", loc, 0.9)
    # a human reading first, raw
    c = _contrib(ann, "temp 70", "m1")
    assert c.assessments[0].quality == "raw"
    # then the station reports 69°F → the human reading becomes corroborated
    st = Signal(source="iem_asos", source_id="SPI|t|temp_c", sensor_id="station:kspi",
                sensor_kind=KIND_STATION, topic="weather", metric="temp_c", value=(69 - 32) * 5 / 9,
                unit="degC", location=loc, quality="reference")
    network.process(st)
    sig = db.recent_signals(1, kinds=("human",))[0]
    assert sig.quality == "corroborated" and sig.reference_agreement == "agree"
    assert db.get_sensor("tg:1").trust > 0.5
    # and a later human reading near the station is corroborated on arrival
    bob = make_sensor("tg:2", name="Bob", zip_code="62711")
    c = _contrib(bob, "temp 68", "m2")
    assert c.assessments[0].reference == "agree"


def test_active_alert_supports_a_matching_flag(make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    db.ensure_reference_sensor("nws:x", "authority", "NWS", geo.Location(), 1.0)
    alert = Signal(source="nws_alerts", source_id="a1|17167", sensor_id="nws:x", sensor_kind="authority",
                   topic="weather", metric="alert.tornado_warning", value=4, text="Tornado Warning",
                   expires_at=utcnow() + timedelta(hours=1),
                   location=geo.location_from_county(geo.county("17167")), quality="reference",
                   group_key="a1", evidence={"event": "Tornado Warning", "severity": "Extreme"})
    db.insert_signal(alert)
    c = _contrib(ann, "tornado on the ground", "m1")
    a = c.assessments[0]
    assert a.quality == "corroborated" and a.reference == "agree"
    assert a.event and a.push_event and a.event["score"] >= 1.0


def test_events_open_join_score_and_push_gate(make_sensor, monkeypatch):
    ann = make_sensor("tg:1", zip_code="62704")
    bob = make_sensor("tg:2", name="Bob", zip_code="62711")
    c1 = _contrib(ann, "hail quarter", "m1")
    a1 = c1.assessments[0]
    assert a1.event_opened and not a1.push_event          # single unverified sensor: no push
    c2 = _contrib(bob, "hail golf ball", "m2")
    a2 = c2.assessments[0]
    assert a2.event["id"] == a1.event["id"]                # joined the same county event
    assert a2.event["n_sensors"] == 2 and a2.event["peak_value"] == 44
    assert a2.push_event and a2.push_reason == "new"
    # a third report that doesn't move the score much is not re-pushed
    cy = make_sensor("tg:3", name="Cy", zip_code="62702")
    a3 = _contrib(cy, "hail golf ball", "m3").assessments[0]
    assert not a3.push_event
    assert db.count_signals(1, kinds=("human",)) == 3
    ev = network.event_summary(db.event_by_id(a1.event["id"]))
    assert ev["verified"] and "Sangamon" in ev["county_label"]


def test_official_report_alone_opens_and_pushes_an_event(fresh_db):
    loc = geo.location_from_point(39.78, -89.65, online=False)
    db.ensure_reference_sensor("lsr:ilx", KIND_OFFICIAL, "NWS ILX", geo.Location(), 0.95)
    sig = Signal(source="iem_lsr", source_id="p1", sensor_id="lsr:ilx", sensor_kind=KIND_OFFICIAL,
                 topic="weather", metric="tornado", value=1.0, location=loc, quality="reference")
    a = network.process(sig)
    assert a.event and a.push_event and a.event["n_reference"] == 1


def test_trusted_sensor_can_push_alone(make_sensor):
    pro = make_sensor("tg:9", name="Pro", zip_code="62704", trust=0.9)
    a = _contrib(pro, "gust 75mph", "m1").assessments[0]
    assert a.push_event and a.event["mean_trust"] >= 0.8


def test_score_formula():
    assert network.score_event(0.5, 1, 0.5, 0) == 0.5
    assert network.score_event(0.8, 2, 0.6, 0) == pytest.approx(0.8 * (1 + 0.5 * 0.6931) * 1.2, abs=1e-3)
    assert network.score_event(1.0, 1, 0.95, 1) == pytest.approx(1.0 * 1.0 * 1.9 * 1.25, abs=1e-3)
    assert network.score_event(1.0, 1, 0.5, 0, reference="disagree") == 0.7


def test_trust_shrinks_toward_prior():
    assert network.trust_from_record(0, 0) == 0.5
    assert network.trust_from_record(10, 0) == pytest.approx(12 / 14)
    assert network.trust_from_record(0, 10) == pytest.approx(2 / 14)
    assert network.trust_from_record(0, 0, prior=0.9) == 0.9          # admin-vouched
    assert network.trust_from_record(0, 2, prior=0.9) == pytest.approx(3.6 / 6)


def test_mesh_gaps_and_stale_event_close(make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    _contrib(ann, "rain 2in; gust 60", "m1")
    rows = network.mesh_rows(24)
    assert {r["metric_key"] for r in rows} == {"rain_mm", "wind_gust_ms"}
    assert all(r["county_fips"] == "17167" and r["n_human"] == 1 for r in rows)
    gaps = network.coverage_gaps(7)
    assert len(gaps) == 101 and all(c.fips != "17167" for c in gaps)
    assert network.close_stale_events(idle_hours=-0.01) == 2 and not db.open_events()


def test_near_view(make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    _contrib(ann, "rain 0.5in", "m1")              # below the 25 mm event threshold
    view = network.near(geo.location_from_zip("62711"), hours=3)
    assert "rain_mm" in view["readings"] and view["events"] == []
    _contrib(ann, "rain 1.5in", "m2")
    assert network.near(geo.location_from_zip("62711"), hours=3)["events"]


def test_duplicate_source_id_is_ignored(make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    first = _contrib(ann, "rain 1in", "same")
    again = _contrib(ann, "rain 1in", "same")
    assert first.accepted == 1 and again.accepted == 0


def test_rate_limit_and_ban(make_sensor, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "network_rate_limit", 2)
    ann = make_sensor("tg:1", zip_code="62704")
    _contrib(ann, "rain 1in", "a")
    _contrib(ann, "rain 1.1in", "b")
    c = _contrib(ann, "rain 1.2in", "c")
    assert c.rejected and "Slow down" in c.rejected
    db.set_sensor_status("tg:1", "banned")
    c = _contrib(db.get_sensor("tg:1"), "rain 1in", "d")
    assert c.rejected and "suspended" in c.rejected


def test_missing_home_location_is_reported(fresh_db):
    from intelnet.models import Sensor

    s = db.upsert_sensor(Sensor(id="tg:7", name="Nowhere"))
    c = _contrib(s, "rain 1in", "m1")
    assert c.accepted == 0 and any("no location" in e for e in c.errors)
    c = _contrib(s, "rain 1in @62704", "m2")
    assert c.accepted == 1
