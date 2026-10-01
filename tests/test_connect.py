"""Connections: news tied to the readings where the story is."""
from __future__ import annotations

from datetime import timedelta

from intelnet import connect, db, geo
from intelnet.models import Signal, utcnow


def test_headlines_name_counties_towns_and_regions():
    rock_island = geo.county_by_name("rockisland").fips
    found, label = connect.places_in("Heavy rain coming to Quad Cities Wednesday and Thursday")
    assert rock_island in found and label == "Quad Cities"
    assert geo.county_by_name("cook").fips in connect.counties_in("Water alert issued for Chicago-area")
    assert connect.counties_in("Midweek soaking rain caused by Hurricane Polo") == []   # Polo, the storm


def _gauge(values: list[float], *, name="SALT CREEK AT OAK BROOK, IL"):
    now = utcnow()
    fips = geo.county_by_name("dupage").fips
    rows = [Signal(source="usgs", source_id=f"g{i}", sensor_id="gauge:1", sensor_kind="station", topic="water",
                   metric="stage_m", value=v * 0.3048, observed_at=now - timedelta(hours=len(values) - i),
                   location=geo.location_from_county(geo.county(fips)), evidence={"name": name})
            for i, v in enumerate(values)]
    db.insert_signals(rows)
    return fips


def test_a_river_that_rose_is_named_with_its_rise(fresh_db):
    from intelnet.topics import find_metric

    fips = _gauge([7.0, 7.4, 8.1, 8.8, 9.3])
    assert connect._rise(find_metric("stage_m"), {fips}) == "Salt Creek at Oak Brook up 2.3 ft, to 9.3 ft"


def test_a_one_step_jump_is_the_gauge_not_the_river(fresh_db):
    from intelnet.topics import find_metric

    fips = _gauge([7.7, 7.7, 7.8, 17.5, 17.4])          # the West Branch DuPage on 30 Sep
    assert connect._rise(find_metric("stage_m"), {fips}) is None
