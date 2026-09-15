"""Illinois geography: counties, ZIP / ZIP+4, hierarchy keys, geohash."""
from __future__ import annotations

from intelnet import geo


def test_vendored_tables_cover_the_state():
    assert len(geo.counties()) == 102
    assert len(geo.zctas()) > 1300
    assert geo.county("17031").name == "Cook"


def test_county_lookup_tolerates_case_punctuation_and_typos():
    assert geo.county_by_name("cook").fips == "17031"
    assert geo.county_by_name("St. Clair County").fips == "17163"
    assert geo.county_by_name("st clair").fips == "17163"
    assert geo.county_by_name("DuPage").fips == "17043"
    assert geo.county_by_name("jo daviess").fips == "17085"
    assert geo.county_by_name("sangamom").fips == "17167"     # close typo
    assert geo.county_by_name("atlantis") is None


def test_zip_and_zip_plus_four():
    loc = geo.location_from_zip("62704-1234")
    assert loc.zip5 == "62704" and loc.zip9 == "62704-1234" and loc.county_fips == "17167"
    assert loc.precision == "zip9" and loc.has_point
    assert geo.location_from_zip("627041234").zip9 == "62704-1234"
    assert geo.location_from_zip("62704").precision == "zip5"
    assert geo.location_from_zip("90210") is None            # not in state


def test_area_keys_form_the_subscription_hierarchy():
    loc = geo.location_from_zip("60601-2001")
    assert loc.area_keys() == ["il", "il.cook", "il.zip.60601", "il.zip.60601-2001"]
    assert geo.location_from_county(geo.county("17163")).area_keys() == ["il", "il.stclair"]


def test_point_resolves_to_nearest_county_offline():
    loc = geo.location_from_point(39.7817, -89.6501, online=False)   # Springfield
    assert loc.county_fips == "17167" and loc.zip5 is not None
    assert geo.nearest_county(41.88, -87.63).name == "Cook"


def test_parse_location_dispatch():
    assert geo.parse_location("62704", online=False).precision == "zip5"
    assert geo.parse_location("@cook", online=False).county_fips == "17031"
    assert geo.parse_location("il.cook", online=False).county_fips == "17031"
    assert geo.parse_location("il.zip.62704", online=False).zip5 == "62704"
    assert geo.parse_location("il", online=False).precision == "state"
    assert geo.parse_location("41.88,-87.63", online=False).county_fips == "17031"
    assert geo.parse_location("Springfield", online=False) is None     # place names need online


def test_same_code_to_fips():
    assert geo.same_to_fips("017031") == "17031"
    assert geo.same_to_fips("048113") is None      # Texas: out of state
    assert geo.same_to_fips("bogus") is None


def test_geohash_and_haversine():
    assert geo.geohash(41.8781, -87.6298, 7) == "dp3wjzt"
    assert geo.haversine_km(41.8781, -87.6298, 39.7817, -89.6501) == 250.0 or \
        abs(geo.haversine_km(41.8781, -87.6298, 39.7817, -89.6501) - 285) < 10


def test_describe():
    assert geo.location_from_zip("62704-1234").describe() == "62704-1234, Sangamon County"
    assert geo.location_from_county(geo.county("17031")).describe() == "Cook County"
