"""Reference feeds: parsers on real payloads, and the run() loop with stubs."""
from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import load_fixture
from intelnet import db, watch
from intelnet.feeds import FEEDS, iem_asos, iem_lsr, nrcs_scan, nws_alerts, usdm_drought, usgs_water
from intelnet.models import utcnow


@pytest.fixture(autouse=True)
def _quiet_new_feeds(monkeypatch):
    """The water/soil/drought feeds are covered in test_packs; keep them offline here."""
    monkeypatch.setattr(usgs_water, "fetch", lambda *a, **k: {"value": {"timeSeries": []}})
    monkeypatch.setattr(nrcs_scan, "fetch_stations", lambda *a, **k: {})
    monkeypatch.setattr(usdm_drought, "fetch", lambda *a, **k: [])


def test_alerts_parse_one_row_per_county_sharing_a_group():
    sigs = nws_alerts.parse_alerts(load_fixture("nws_alerts.json"))
    assert len(sigs) >= 3
    heat = [s for s in sigs if s.metric == "alert.heat_advisory"]
    assert len({s.group_key for s in heat}) == 1 and len(heat) > 1
    s = heat[0]
    assert s.sensor_kind == "authority" and s.quality == "reference" and s.value == 2.0
    assert s.location.county_fips.startswith("17") and s.location.precision == "county"
    assert s.expires_at is not None and s.evidence["severity"] == "Moderate"
    assert s.evidence["url"].startswith("https://api.weather.gov/alerts/")
    assert nws_alerts.slug("Severe Thunderstorm Warning") == "severe_thunderstorm_warning"


def test_lsr_parse_maps_types_units_and_counties():
    sigs = iem_lsr.parse_lsr(load_fixture("iem_lsr.json"))
    by_metric = {}
    for s in sigs:
        by_metric.setdefault(s.metric, []).append(s)
    assert "rain_mm" in by_metric and "tornado" in by_metric and "funnel_cloud" in by_metric
    rain = by_metric["rain_mm"][0]
    assert rain.value == pytest.approx(25.4, rel=0.05) or rain.value > 25    # inches → mm
    assert rain.location.county_fips == "17195" and rain.sensor_id == "lsr:dvn"
    assert rain.quality == "reference" and "Rain" in rain.text
    assert by_metric["tornado"][0].value == 1.0


def test_asos_parse_one_signal_per_metric_per_station():
    sigs = iem_asos.parse_currents(load_fixture("iem_asos.json"))
    stations = {s.sensor_id for s in sigs}
    assert len(stations) == 3
    spi = [s for s in sigs if s.sensor_id == "station:spi"]
    keys = {s.metric for s in spi}
    assert {"temp_c", "dewpoint_c", "humidity_pct", "wind_ms", "pressure_hpa", "visibility_km"} <= keys
    t = next(s for s in spi if s.metric == "temp_c")
    assert -40 < t.value < 50 and t.location.county_fips == "17167"
    assert t.source_id.startswith("SPI|") and t.source_id.endswith("|temp_c")


def test_feed_run_stores_assesses_and_pushes(fresh_db, sent, monkeypatch):
    monkeypatch.setattr(nws_alerts, "fetch", lambda *a, **k: load_fixture("nws_alerts.json"))
    monkeypatch.setattr(iem_lsr, "fetch", lambda *a, **k: load_fixture("iem_lsr.json"))
    monkeypatch.setattr(iem_asos, "fetch", lambda *a, **k: load_fixture("iem_asos.json"))
    db.add_subscription("77", "weather.alerts", "il")
    db.add_subscription("78", "weather.events", "il")
    out = watch.run_once()
    f = out["feeds"]
    assert f["nws_alerts"]["new"] > 0 and f["nws_alerts"]["alerts_pushed"] == 2   # two alerts, one chat
    assert f["iem_lsr"]["new"] == 5 and f["iem_lsr"]["events_pushed"] >= 1          # official tornado
    assert f["iem_asos"]["new"] > 10 and f["iem_asos"]["status"] == "ok"
    assert {c for c, _ in sent} == {"77", "78"}
    # second run: nothing new, and the station feed is on its cadence gate
    out2 = watch.run_once()
    assert out2["feeds"]["nws_alerts"]["new"] == 0 and out2["feeds"]["iem_asos"]["skipped"]
    assert db.get_sensor("nws:nws_chicago_il") or db.list_sensors(kind="authority")
    assert db.list_sensors(kind="station")[0].trust == 0.9


def test_feed_errors_are_isolated_and_logged(fresh_db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nws down")

    monkeypatch.setattr(nws_alerts, "fetch", boom)
    monkeypatch.setattr(iem_lsr, "fetch", lambda *a, **k: {"features": []})
    monkeypatch.setattr(iem_asos, "fetch", lambda *a, **k: {"data": []})
    out = watch.run_once()
    assert out["feeds"]["nws_alerts"]["status"] == "error" and "nws down" in out["feeds"]["nws_alerts"]["error"]
    assert out["feeds"]["iem_lsr"]["status"] == "ok"
    with db.get_conn() as conn:
        rows = conn.execute("SELECT source, status FROM run_log ORDER BY id").fetchall()
    assert [(r["source"], r["status"]) for r in rows][:3] == [
        ("nws_alerts", "error"), ("iem_lsr", "ok"), ("iem_asos", "ok")]


def test_station_cadence_gate(fresh_db, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "station_poll_minutes", 60)
    feed = FEEDS["iem_asos"]()
    assert feed.should_run()
    db.kv_set(iem_asos.KV_LAST_POLL, (utcnow() - timedelta(minutes=10)).isoformat())
    assert not feed.should_run()
    db.kv_set(iem_asos.KV_LAST_POLL, (utcnow() - timedelta(minutes=61)).isoformat())
    assert feed.should_run()


def test_active_alert_groups_and_prune(fresh_db, monkeypatch):
    monkeypatch.setattr(nws_alerts, "fetch", lambda *a, **k: load_fixture("nws_alerts.json"))
    FEEDS["nws_alerts"]().run()
    groups = nws_alerts.active_alert_groups()
    assert groups and all(g["counties"] for g in groups)
    cook = nws_alerts.active_alert_groups("17031")
    assert all("Cook" in g["counties"] for g in cook)
    assert db.prune_reference_signals(days=30) == 0          # nothing that old
