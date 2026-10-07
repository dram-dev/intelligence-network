"""High water: NWS river gauges near flood stage or above, with trend and forecast, in the
brief and the digest. The source is faked with the shapes it publishes (the gauge list with
flood categories, a gauge's flood stages, its observed and forecast stage series)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from intelnet import brief, digest, rivers
from intelnet.config import settings
from intelnet.models import utcnow

NOW = utcnow()


def _gauge(lid, name, state, lat, lon, stage, category, forecast=-999, fcat="fcst_not_current"):
    return {"lid": lid, "name": name, "state": {"abbreviation": state}, "latitude": lat, "longitude": lon,
            "status": {"observed": {"primary": stage, "primaryUnit": "ft", "floodCategory": category,
                                    "validTime": (NOW - timedelta(minutes=30)).isoformat()},
                       "forecast": {"primary": forecast, "primaryUnit": "ft", "floodCategory": fcat}}}


GAUGES = {"gauges": [
    _gauge("RUSI2", "Des Plaines River near Russell", "IL", 42.4897, -87.9256, 7.03, "minor", 7.0, "minor"),
    _gauge("CHSI2", "Mississippi River at Chester", "IL", 37.9036, -89.8364, 24.36, "no_flooding", 25.3, "action"),
    _gauge("HNBM7", "Mississippi River at Hannibal", "MO", 39.7084, -91.3585, 16.1, "action"),   # the far bank
    _gauge("CVGI3", "Wabash River at Covington", "IN", 40.1406, -87.3947, 10.1, "action"),       # not the border
    _gauge("MCCI2", "Mainstream Deep Tunnel at McCook Reservoir", "IL", 41.80, -87.83, -95.1, "action"),
    _gauge("QUIET", "Sangamon River at Riverton", "IL", 39.84, -89.55, 9.0, "no_flooding", 9.0, "no_flooding"),
]}


def _series(points):
    return {"data": [{"validTime": (NOW + timedelta(hours=h)).isoformat(), "primary": v} for h, v in points]}


DETAILS = {
    "RUSI2": {"county": "Lake", "flood": {"categories": {"minor": {"stage": 7}, "action": {"stage": 6.5}}}},
    "CHSI2": {"county": "Randolph", "flood": {"categories": {"minor": {"stage": 27}}}},
    "HNBM7": {"county": "Marion", "flood": {"categories": {"minor": {"stage": 17}}}},
}
FLOWS = {
    "RUSI2": {"observed": _series([(-7, 7.03), (-1, 7.03)]),
              "forecast": _series([(6, 7.0), (30, 6.5), (54, 5.9), (24 * 9, 4.0)])},   # beyond 5 days: ignored
    "CHSI2": {"observed": _series([(-7, 23.9), (-1, 24.36)]),
              "forecast": _series([(12, 25.0), (24, 25.3), (48, 25.1)])},
    "HNBM7": {"observed": _series([(-7, 16.4), (-1, 16.1)]), "forecast": {"data": []}},
}


@pytest.fixture
def nwps(monkeypatch):
    monkeypatch.setattr(settings, "ahead_enabled", True)

    def get(url: str):
        if url.startswith("https://api.water.noaa.gov/nwps/v1/gauges?"):
            return GAUGES
        lid = url.split("/gauges/")[1].split("/")[0]
        return FLOWS[lid] if url.endswith("/stageflow") else DETAILS[lid]

    monkeypatch.setattr(rivers, "_get", get)


def test_the_states_gauges_and_the_far_bank_of_a_border_river(nwps):
    hw = rivers.high_water(NOW)
    assert [g.lid for g in hw] == ["RUSI2", "CHSI2", "HNBM7"]       # in flood first; no tunnel, no Indiana
    russell, chester, hannibal = hw
    assert (russell.level, russell.county, russell.trend, russell.floods_at) == (2, "Lake", "steady", 7)
    assert chester.observed == (0, "") and chester.forecast == (1, "near flood stage") and chester.trend == "rising"
    assert hannibal.trend == "falling" and hannibal.crest is None and hannibal.falls_to is None


def test_a_gauge_reads_now_flood_stage_and_the_next_days(nwps):
    russell, chester, _ = rivers.high_water(NOW)
    assert russell.text() == ("7.03 ft, steady, minor flooding (flood stage 7 ft); forecast falling to 5.9 ft by "
                              + rivers.when(NOW + timedelta(hours=54)))
    assert chester.text() == ("24.4 ft, rising (flood stage 27 ft); forecast crest 25.3 ft "
                              + rivers.when(NOW + timedelta(hours=24)) + ", near flood stage")
    assert [rivers.stage_text(x) for x in (7, 6.5, 7.03, 24.36, 427.21)] == [
        "7 ft", "6.5 ft", "7.03 ft", "24.4 ft", "427.2 ft"]


def test_the_brief_has_the_gauges_near_its_county(fresh_db, nwps):
    lake = brief.compose_rich("17097", {}, day={})                       # first, under the heading
    assert "</h4><p>🌊 <b>Des Plaines River near Russell</b>: 7.03 ft, steady, minor flooding" in lake
    assert "Chester" not in lake and "A quiet night" not in lake
    assert "🌊 <b>Mississippi River at Chester</b>" in brief.compose("17157", {}, day={})       # Randolph
    statewide = brief.compose_rich(None, {}, day={})                     # no county: the gauges in flood
    assert "</h4><p>🌊 <b>Des Plaines River near Russell</b>" in statewide and "Chester" not in statewide


def test_the_digest_lists_high_water(make_sensor, nwps):
    m = digest.build(hours=24)
    html = digest.render_html(m)
    assert "High water" in html and ">FLOOD STAGE<" in html and "<b>Mississippi River at Hannibal</b>" in html
    assert "Crest 25.3 ft " in html and "Falling to 5.9 ft by " in html
    assert "High water:" in digest.render_text(m)


def test_off_or_down_means_no_lines(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "ahead_enabled", False)
    assert rivers.high_water() == []
    monkeypatch.setattr(settings, "ahead_enabled", True)

    def down(url):
        raise ConnectionError("offline")

    monkeypatch.setattr(rivers, "_get", down)
    assert rivers.high_water() == [] and brief.high_water(None) == []
