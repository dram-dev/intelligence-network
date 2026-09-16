"""Digest model + renderers on a seeded network."""
from __future__ import annotations

from datetime import timedelta

from conftest import load_fixture
from intelnet import contrib, db, digest
from intelnet.feeds import nws_alerts
from intelnet.models import utcnow


def _seed(make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    bob = make_sensor("tg:2", name="Bob", zip_code="62711")
    contrib.contribute(ann, "hail golf ball; gust 65", source_id_base="m1", online=False, use_llm=False)
    contrib.contribute(bob, "hail 1.75in", source_id_base="m2", online=False, use_llm=False)
    alerts = nws_alerts.parse_alerts(load_fixture("nws_alerts.json"))
    now = utcnow()
    for s in alerts:          # the fixture's own times age out of the digest's 24h window
        s.observed_at, s.expires_at = now - timedelta(minutes=30), now + timedelta(hours=2)
    db.insert_signals(alerts)
    db.upsert_items([__import__("intelnet.ingest.base", fromlist=["IngestedItem"]).IngestedItem(
        source="news", source_id="n1", title="Storms rake central Illinois", url="https://ex.test/1",
        content="...", metadata={"feed": "Google News"})])
    with db.get_conn() as conn:
        item_id = conn.execute("SELECT id FROM items").fetchone()["id"]
    db.update_triage(item_id, "keep", 0.8, "weather", "hail + wind damage in Sangamon")
    db.add_subscription("1", "weather.digest", "il")


def test_build_and_render(make_sensor):
    _seed(make_sensor)
    m = digest.build(hours=24)
    assert m.vitals["sensors_total"] == 2 and m.vitals["signals_24h_human"] == 3
    assert m.events and m.events[0]["metric"] == "hail_mm" and m.events[0]["verified"]
    assert m.alerts and m.alerts[0]["counties"]
    assert m.contributions and m.contributions[0]["county"] == "Sangamon County"
    assert m.leaderboard[0]["name"].startswith("s-")          # handles, never names
    assert "Ann" not in digest.render_html(m) and "Bob" not in digest.render_html(m)
    assert m.reading[0]["title"] == "Storms rake central Illinois" and m.reading[0]["feed"] == "Google News"
    assert m.gap_count == 101 and m.subscriptions == {"weather.digest": 1}
    assert "3 readings from 2 sensors" in m.headline and "top event: Hail size" in m.headline

    html = digest.render_html(m)
    for needle in ("<h1>Intelligence Network — IL environmental digest", "Network vitals", "Events (ranked by score)",
                   "Official alerts (NWS)", "Contributions by county", "Contributors this week",
                   "Storms rake central Illinois", "Sensors wanted", "How to contribute", "Hail size"):
        assert needle in html
    assert "<script" not in html

    text = digest.render_text(m)
    assert "Events:" in text and "Hail size" in text and "NWS alerts:" in text

    m.narrative = "Para one.\n\nPara two."
    assert "<p>Para one.</p>" in digest.render_html(m)
    assert "Para one." in digest.render_text(m)
    assert '"vitals"' in m.to_json()


def test_extremes_use_stations_not_humans(make_sensor):
    _seed(make_sensor)
    m = digest.build(hours=24)
    assert m.extremes == []          # no station rows seeded → no extremes from human readings
    from intelnet.feeds import iem_asos
    db.insert_signals(iem_asos.parse_currents(load_fixture("iem_asos.json")))
    m = digest.build(hours=24 * 400)  # fixture obs are in the past relative to test time
    assert any(x["metric"] == "Temperature" for x in m.extremes)


def test_empty_network_renders(fresh_db):
    m = digest.build(hours=24)
    assert m.events == [] and m.alerts == [] and m.reading == []
    html = digest.render_html(m)
    assert "<i>none</i>" in html and "nothing kept" in html
    assert "Sensors wanted: 102 counties" in digest.render_text(m)
