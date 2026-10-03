"""Digest model + renderers on a seeded network."""
from __future__ import annotations


import json
import re

from conftest import load_fixture
from intelnet import contrib, db, digest
from intelnet.feeds import nws_alerts


def _seed(make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    bob = make_sensor("tg:2", name="Bob", zip_code="62711")
    contrib.contribute(ann, "hail golf ball; gust 65", source_id_base="m1", online=False, use_llm=False)
    contrib.contribute(bob, "hail 1.75in", source_id_base="m2", online=False, use_llm=False)
    # as if issued just now, so they sit inside the digest's 24-hour recap
    db.insert_signals(nws_alerts.parse_alerts(
        load_fixture("nws_alerts.json", fresh=True, anchor="oldest")))
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
    text = re.sub(r"data:image/jpeg;base64,[A-Za-z0-9+/=]+", "", digest.render_html(m))   # the map is no text
    assert "Ann" not in text and "Bob" not in text
    # the day's map heads the Doc, with its stories numbered and its stand-outs explained; page 1
    # is the lede, the links, the map, then the numbers (a table can't hold the map off page 1)
    assert 'src="data:image/jpeg;base64,/9j/' in digest.render_html(m) and 'width="384"' in text
    bar = digest.render_html(m, downloads={"PDF": "p"})
    assert bar.index("Also as") < bar.index("<img") < bar.index("official readings</p>") < bar.index("In the news")
    assert "<hr>" not in bar[:bar.index("In the news")] and "<hr>" in bar[bar.index("In the news"):]   # none left alone
    assert 'width="432"' in digest.render_html(m, downloads={"PDF": "p"}, page=True)        # no page on the site
    assert "What stood out" in text and "Hail 1.75 in" in text
    assert "In the news, and what was measured there" in text and "1 · Weather, statewide" in text
    assert "Every story kept today is listed above" in text                  # not twice: listed, then "worth reading"
    assert "Station extremes" not in text and "day" not in json.loads(m.to_json())         # nor in the narrative
    # a phone gets Google's 256-pixel copy of a picture in a Doc: the bar links the map's full-size PNG
    assert m.picture_full[:4] == b"\x89PNG" and "picture_full" not in json.loads(m.to_json())
    linked = digest.render_html(m, downloads={"PDF": "https://drive.google.com/file/d/P/view",
                                              digest.MAP_LABEL: "https://drive.google.com/file/d/M/view"})
    assert re.search(r'Also as <a href="[^"]+/P/view"[^>]*>PDF</a> · <a href="[^"]+/M/view"[^>]*>Full-size map</a>',
                     linked)
    assert m.reading[0]["title"] == "Storms rake central Illinois" and m.reading[0]["feed"] == "Google News"
    assert m.gap_count == 101 and m.subscriptions == {"weather.digest": 1}
    assert "3 readings from 2 people" in m.headline and "Top event: hail size 1.75 in, Sangamon" in m.headline

    html = digest.render_html(m)
    for needle in ("ILLINOIS DAILY DIGEST", "INTELLIGENCE NETWORK", "Network events",
                   "Warnings and advisories", "From people", "Contributors",
                   "Storms rake central Illinois", "Coverage", "TAKE PART", "Hail size"):
        assert needle in html, needle
    assert "<script" not in html
    assert "Also as" not in html                              # no links until the files exist
    linked = digest.render_html(m, downloads={"PDF": "https://drive.google.com/file/d/x/view"})
    assert "Also as" in linked and "file/d/x/view" in linked

    text = digest.render_text(m)
    assert "Events:" in text and "Hail size" in text and "NWS alerts:" in text

    m.narrative = "Para one.\n\nPara two."
    assert "Para one." in digest.render_html(m) and "Para two." in digest.render_html(m)
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


def test_the_lede_counts_alerts_in_effect_like_the_numbers_do():
    # the window's alerts include ended ones: "11 in effect" over a grid saying 4 contradicts itself
    m = digest.DigestModel(date="2026-10-03", generated_at="2026-10-03 1:10 AM CDT", hours=27,
                           network_name="N", state="IL", vitals={"alerts_active": 4},
                           alerts=[{"event": f"Flood Warning {i}"} for i in range(11)])
    assert m.headline.startswith("4 NWS alerts in effect;")
    m.vitals["alerts_active"] = 0
    assert m.headline.startswith("No NWS alerts in effect;")


def test_a_long_first_paragraph_opens_the_body_and_the_facts_lead(make_sensor):
    # page 1 has room for three lines of lede above the map and its numbers
    m = digest.build(hours=24)
    short, long_ = "Floods along the Rock.", "Flood warnings along the Rock River " * 8
    m.narrative = f"{short}\n\nOne person sent a reading."
    html = digest.render_html(m)
    assert html.index(short) < html.index("<img") < html.index("One person sent a reading")
    m.narrative = f"{long_.strip()}\n\nOne person sent a reading."
    html = digest.render_html(m)
    assert html.index(m.headline) < html.index("<img") < html.index("official readings</p>") < html.index(long_[:40])


def test_empty_network_renders(fresh_db):
    m = digest.build(hours=24)
    assert m.events == [] and m.alerts == [] and m.reading == []
    html = digest.render_html(m)
    assert "No events crossed a threshold" in html and "Nothing kept in this window" in html
    assert "Sensors wanted: 102 counties" in digest.render_text(m)


def test_csv_tables_carry_the_same_numbers_as_the_document(make_sensor):
    _seed(make_sensor)
    m = digest.build(hours=24)
    csvs = digest.render_csvs(m)
    assert set(csvs) == {"events", "official-alerts", "county-activity", "station-extremes",
                         "contributors", "reading-list", "network-vitals"}
    events = csvs["events"].splitlines()
    assert events[0].startswith("opened_at,updated_at,topic,metric,metric_label")
    assert any("hail_mm" in line for line in events[1:])
    alerts = csvs["official-alerts"].splitlines()
    assert alerts[0] == "event,severity,counties,sent,expires,sender,headline,url"
    assert any("; " in line for line in alerts[1:])            # a multi-county alert joins with ;
    assert "Ann" not in csvs["contributors"] and "s-" in csvs["contributors"]
    assert "Storms rake central Illinois" in csvs["reading-list"]


def test_an_empty_table_still_has_its_header(fresh_db):
    quiet = digest.DigestModel(date="2026-09-16", generated_at="2026-09-16 03:00Z", hours=24,
                               network_name="Intelligence Network", state="IL")
    assert digest.render_csvs(quiet)["events"].strip() == ",".join(digest.EVENT_COLUMNS)


def test_the_doc_never_highlights_text(make_sensor):
    """Google Docs turns a background on anything but a table cell into a highlight behind
    every line (the white bars on the 30 Sep digest) and ignores the `font:` shorthand."""
    import re

    _seed(make_sensor)
    m = digest.build(hours=24)
    doc = digest.render_html(m)
    assert "<style" not in doc and 'class="sheet"' not in doc
    for tag in re.finditer(r'<(\w+)[^>]*style="[^"]*background[^"]*"', doc):
        assert tag.group(1) == "td", tag.group(0)[:90]
    assert not re.search(r'style="[^"]*(?<![\w-])font:', doc)
    page = digest.render_html(m, page=True)
    assert "<style>" in page and 'class="sheet"' in page
