"""Every topic pack loads; the language reads all of them at once; feeds map in."""
from __future__ import annotations

import pytest

from conftest import load_fixture
from intelnet import db, language, topics, watch
from intelnet.feeds import FEEDS, nrcs_scan, usdm_drought, usgs_water


def test_all_packs_load_with_unique_aliases():
    packs = topics.topics()
    assert set(packs) == {"weather", "soil", "water", "agriculture", "air"}
    seen: dict[str, tuple[str, str]] = {}
    for t in packs.values():
        assert t.category_keys and all(k.startswith(t.name + ".") for k in t.category_keys)
        for m in t.metrics.values():
            for a in (m.key, *m.aliases):
                assert a not in seen or seen[a] == (t.name, m.key), f"alias {a!r} in {seen[a]} and {(t.name, m.key)}"
                seen[a] = (t.name, m.key)
    assert topics.get_topic("water").mapping("usgs_parameters")["00065"]["metric"] == "stage_m"
    assert topics.get_topic("soil").mapping("awdb_elements")["SMS"]["metric"] == "soil_moisture_pct"
    assert topics.metric_by_key("pm25_ugm3").topic == "air" and topics.topic_of_metric("corn_stage").name == "agriculture"


@pytest.mark.parametrize("text, expect", [
    ("soil temp 62 @cook; soil moisture 18%", [("soil", "soil_temp_c"), ("soil", "soil_moisture_pct")]),
    ("corn at r1 @champaign", [("agriculture", "corn_stage")]),
    ("beans podding @62704", [("agriculture", "soy_stage")]),
    ("stage 7.4 ft @61801; discharge 1200 cfs", [("water", "stage_m"), ("water", "discharge_cms")]),
    ("do 3.1 mg/l -- fish gasping", [("water", "dissolved_oxygen_mgl")]),
    ("pm2.5 62 @60601", [("air", "pm25_ugm3")]),
    ("tar spot showing up @mclean", [("agriculture", "disease")]),
    ("hail quarter; erosion @sangamon", [("weather", "hail_mm"), ("soil", "erosion")]),
    ("drought category d2 @cook", [("agriculture", "drought_category")]),
    ("ph 6.2 @62704", [("soil", "soil_ph")]),
    ("water ph 8.1 @62704", [("water", "water_ph")]),
    ("crop condition good @cook", [("agriculture", "crop_condition")]),
    ("temp 91 @cook", [("weather", "temp_c")]),
    ("tile running and ponding in the low spots @62704", [("water", "tile_flow"), ("water", "ponding")]),
    ("wildfire smoke, aqi 160 @60601", [("air", "smoke"), ("air", "aqi")]),
])
def test_language_reads_every_pack_at_once(text, expect):
    r = language.parse(text, online=False)
    got = [(s.metric.topic, s.metric.key) for s in r.signals]
    assert sorted(got) == sorted(expect), (got, r.errors, r.leftover)


def test_values_and_events_in_new_packs():
    r = language.parse("stage 10 ft", online=False)
    assert r.signals[0].value == pytest.approx(3.048)
    r = language.parse("do 1.5", online=False)
    m = r.signals[0].metric
    assert m.is_event(1.5) and m.severity(1.5) == 0.8 and not m.is_event(6)
    r = language.parse("compaction 350 psi", online=False)
    assert r.signals[0].metric.severity(350) == 0.5
    assert language.parse("corn dent", online=False).signals[0].value == 20
    assert language.parse("drought monitor d4", online=False).signals[0].metric.severity(5) == 1.0


def test_category_resolution_across_packs():
    assert topics.resolve_category("events") == "weather.events"
    assert topics.resolve_category("soil.events") == "soil.events"
    assert sorted(topics.expand_category("*.events")) == [
        "agriculture.events", "air.events", "soil.events", "water.events", "weather.events"]
    assert topics.expand_category("all.digest")[0].endswith(".digest")
    assert topics.expand_category("nope") == []


def test_cheatsheet_and_metric_descriptions_cover_every_pack():
    sheet = language.cheatsheet()
    for name in ("agriculture", "air", "soil", "water", "weather"):
        assert name + ":" in sheet
    desc = language.describe_metrics()
    assert "[soil]" in desc and "[water]" in desc and "corn_stage" in desc


# ── feeds ──────────────────────────────────────────────────────────────────

def test_usgs_parse():
    sigs = usgs_water.parse_iv(load_fixture("usgs_iv.json"))
    assert len(sigs) == 6 and {s.metric for s in sigs} == {"stage_m", "discharge_cms"}
    s = next(x for x in sigs if x.metric == "stage_m")
    assert s.sensor_id.startswith("gauge:") and s.location.county_fips.startswith("17")
    assert s.unit == "m" and 0 < s.value < 10 and s.evidence["url"].startswith("https://waterdata.usgs.gov/")
    assert s.source_id.count("|") == 2


def test_scan_parse_takes_shallowest_depth():
    payload = load_fixture("nrcs_scan.json")
    stations = {"2004:IL:SCAN": {"name": "Mason #1", "latitude": 40.31314, "longitude": -89.90187,
                                 "countyName": "Mason"}}
    sigs = nrcs_scan.parse_awdb(payload, stations)
    keys = {s.metric: s for s in sigs}
    assert set(keys) == {"soil_moisture_pct", "soil_temp_c"}
    sm = keys["soil_moisture_pct"]
    assert sm.evidence["depth_in"] == -2 and sm.value == pytest.approx(4.3)
    assert sm.location.county_fips == "17125" and sm.sensor_id == "scan:2004"
    assert "-8" in sm.evidence["profile"]
    assert keys["soil_temp_c"].unit == "degC"


def test_usdm_parse_and_category_rule():
    rows = load_fixture("usdm.json")
    sigs = usdm_drought.parse_usdm(rows)
    assert len(sigs) == 1 and sigs[0].metric == "drought_category"
    assert sigs[0].evidence["map_date"] == "2025-09-30" and sigs[0].value == 2.0     # D1 covers 100%
    assert usdm_drought.category_for({"none": 100, "d0": 0}) == 0
    assert usdm_drought.category_for({"d0": 100, "d1": 60, "d2": 20}) == 2
    assert usdm_drought.category_for({"d0": 100, "d1": 100, "d2": 100, "d3": 30, "d4": 5}) == 4


def test_new_feeds_run_through_watch(fresh_db, monkeypatch):
    from intelnet.feeds import iem_asos, iem_lsr, nws_alerts

    monkeypatch.setattr(nws_alerts, "fetch", lambda *a, **k: {"features": []})
    monkeypatch.setattr(iem_lsr, "fetch", lambda *a, **k: {"features": []})
    monkeypatch.setattr(iem_asos, "fetch", lambda *a, **k: {"data": []})
    monkeypatch.setattr(usgs_water, "fetch", lambda *a, **k: load_fixture("usgs_iv.json"))
    monkeypatch.setattr(nrcs_scan, "fetch_stations", lambda *a, **k: {
        "2004:IL:SCAN": {"name": "Mason #1", "latitude": 40.31314, "longitude": -89.90187, "countyName": "Mason"}})
    monkeypatch.setattr(nrcs_scan, "fetch_data", lambda *a, **k: load_fixture("nrcs_scan.json"))
    monkeypatch.setattr(usdm_drought, "fetch", lambda *a, **k: load_fixture("usdm.json"))
    out = watch.run_once()
    f = out["feeds"]
    assert f["usgs_water"]["new"] == 6 and f["nrcs_scan"]["new"] == 2 and f["usdm"]["new"] == 1
    assert all(f[n]["status"] == "ok" for n in ("usgs_water", "nrcs_scan", "usdm"))
    assert db.get_sensor("usdm:drought_monitor").kind == "authority"
    assert db.get_sensor("scan:2004").trust == 0.9
    out2 = watch.run_once()
    assert all(out2["feeds"][n]["skipped"] for n in ("usgs_water", "nrcs_scan", "usdm"))
    assert set(FEEDS) >= {"usgs_water", "nrcs_scan", "usdm"}
