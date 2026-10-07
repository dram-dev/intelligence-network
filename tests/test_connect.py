"""Connections: news tied to the readings where the story is."""
from __future__ import annotations

from datetime import timedelta

from intelnet import connect, geo, notable
from intelnet.models import Signal, utcnow
from intelnet.topics import find_metric


def test_headlines_name_counties_towns_and_regions():
    rock_island = geo.county_by_name("rockisland").fips
    found, label = connect.places_in("Heavy rain coming to Quad Cities Wednesday and Thursday")
    assert rock_island in found and label == "Quad Cities"
    assert geo.county_by_name("cook").fips in connect.counties_in("Water alert issued for Chicago-area")
    assert connect.counties_in("Midweek soaking rain caused by Hurricane Polo") == []   # Polo, the storm


def _stage(feet: list[float]) -> list[Signal]:
    now = utcnow()
    return [Signal(source="usgs", source_id=f"g{i}", sensor_id="gauge:1", sensor_kind="station", topic="water",
                   metric="stage_m", value=f * 0.3048, observed_at=now - timedelta(hours=len(feet) - i),
                   location=geo.location_from_county(geo.county_by_name("dupage")))
            for i, f in enumerate(feet)]


def test_a_river_that_rose_is_named_with_its_rise():
    m = find_metric("stage_m")
    assert m.display(notable.rise(m, _stage([7.0, 7.4, 8.1, 8.8, 9.3]))) == "2.3 ft"


def test_a_one_step_jump_is_the_gauge_not_the_river():
    assert notable.rise(find_metric("stage_m"), _stage([7.7, 7.7, 7.8, 17.5, 17.4])) is None   # West Branch DuPage, 30 Sep


def test_a_gauges_name_reads_like_a_place():
    from types import SimpleNamespace

    from intelnet import connect

    def name(raw: str) -> str:
        return connect._site(SimpleNamespace(evidence={"name": raw}))

    assert name("SALT CREEK AT 22ND STREET AT OAK BROOK, IL") == "Salt Creek at 22nd Street at Oak Brook"
    assert name("FOX RIVER (TAILWATER) NEAR MCHENRY, IL") == "Fox River (Tailwater) near McHenry"
    assert name("MACKINAW RIVER NR CONGERVILLE, IL") == "Mackinaw River near Congerville"
