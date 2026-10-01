"""The masthead map's day: notable readings in every topic and the news tied to them."""
from __future__ import annotations

import json
from datetime import timedelta

from intelnet import connect, contrib, db, geo, notable, opendata
from intelnet.ingest.base import IngestedItem
from intelnet.models import Signal, utcnow
from intelnet.topics import find_metric


def _at(county: str, lat: float | None = None, lon: float | None = None) -> geo.Location:
    c = geo.county_by_name(county)
    if lat is None:
        return geo.location_from_county(c)
    return geo.Location(lat=lat, lon=lon, county_fips=c.fips, precision="point")


def _report(n: int, county: str, inches: float, lat: float, lon: float, hours_ago: float = 3) -> Signal:
    """An NWS storm report of a rain total, as iem_lsr stores it."""
    return Signal(source="iem_lsr", source_id=f"lsr{n}", sensor_id="lsr:ilx", sensor_kind="official", topic="weather",
                  metric="rain_mm", value=inches * 25.4, observed_at=utcnow() - timedelta(hours=hours_ago),
                  location=_at(county, lat, lon), quality="reference",
                  evidence={"kind": "lsr", "city": f"2 NNW Town{n}", "reporter": "Cocorahs"})


def _station(code: str, county: str, lat: float, lon: float, metric: str, values: list[float], **ev) -> list[Signal]:
    now = utcnow()
    return [Signal(source="iem_asos", source_id=f"{code}{metric}{i}", sensor_id=f"station:{code.lower()}",
                   sensor_kind="station", topic="weather", metric=metric, value=v,
                   observed_at=now - timedelta(hours=len(values) - i), location=_at(county, lat, lon),
                   quality="reference", evidence={"kind": "station", "station": code, "name": code, **ev})
            for i, v in enumerate(values)]


def _gauge(site: str, county: str, lat: float, lon: float, feet: list[float], name: str) -> list[Signal]:
    now = utcnow()
    return [Signal(source="usgs_water", source_id=f"{site}-{i}", sensor_id=f"gauge:{site}", sensor_kind="station",
                   topic="water", metric="stage_m", value=f * 0.3048, observed_at=now - timedelta(hours=len(feet) - i),
                   location=_at(county, lat, lon), quality="reference",
                   evidence={"kind": "gauge", "site": site, "name": name, "url": f"https://waterdata.usgs.gov/{site}"})
            for i, f in enumerate(feet)]


def _story(n: int, title: str, topic: str, relevance: float = 0.9) -> None:
    db.upsert_items([IngestedItem(source="news", source_id=f"s{n}", title=title, url=f"https://ex.test/{n}",
                                  content="...", metadata={"feed": "Google News"})])
    with db.get_conn() as conn:
        item = conn.execute("SELECT id FROM items WHERE source_id = ?", (f"s{n}",)).fetchone()["id"]
    db.update_triage(item, "keep", relevance, topic, "test")


def _place(doc, name):
    return next(p for p in doc["places"] if p["name"] == name)


def test_the_states_headline_readings_are_labelled(fresh_db):
    db.insert_signals([_report(1, "whiteside", 1.5, 41.81, -90.08), _report(2, "knox", 1.1, 40.97, -90.27),
                       _report(3, "peoria", 0.4, 40.82, -89.68)])
    # a station's wettest hour is not a storm total, however wet: it never takes "Rain"
    db.insert_signals(_station("PIA", "peoria", 40.66, -89.69, "rain_mm", [30.0, 2.0, 1.0], period="1h"))
    db.insert_signals(_station("SPI", "sangamon", 39.84, -89.68, "temp_c", [21.0, 28.0, 34.4, 30.0]))
    db.insert_signals(_station("MDH", "jackson", 37.78, -89.25, "temp_c", [19.0, 15.6, 18.0]))
    db.insert_signals(_station("RFD", "winnebago", 42.19, -89.1, "temp_c", [17.0, 16.1, 20.0]))
    doc = notable.build()
    top = _place(doc, "2 mi NNW of Town1")
    assert top["tier"] == 1 and top["label"] == "Rain 1.50 in" and top["source"] == "NWS storm report"
    assert "Highest rainfall total in Illinois in the last 24 hours" in top["why"]
    assert "Reported to the NWS by a CoCoRaHS observer" in top["why"]
    hourly = _place(doc, "PIA")                    # past the threshold as a rate: on the map, but never "the most"
    assert hourly["label"] == "Rain 1.18 in/hr" and hourly["lines"][0]["note"] == "the wettest hour"
    assert not any(w.startswith("Highest") for w in hourly["why"])
    assert _place(doc, "SPI")["label"].startswith("High 93.9") and _place(doc, "MDH")["label"].startswith("Low 60")
    # every place is somewhere in the state, and says when
    assert all(geo.county_by_name(p["county"]) and p["at"] for p in doc["places"])


def test_a_rise_is_headlined_and_a_one_step_jump_is_not(fresh_db):
    db.insert_signals(_gauge("05536290", "cook", 41.6, -87.6, [5.7, 6.1, 6.6, 7.1, 7.59],
                             "LITTLE CALUMET RIVER AT SOUTH HOLLAND, IL"))
    db.insert_signals(_gauge("05540250", "dupage", 41.75, -88.15, [7.7, 7.7, 7.8, 17.5, 17.4],
                             "WEST BRANCH DUPAGE RIVER NEAR NAPERVILLE, IL"))
    doc = notable.build()
    risen = _place(doc, "Little Calumet River at South Holland")
    assert risen["label"] == "▲ 1.89 ft" and risen["why"][0] == "Biggest rise in Illinois in the last 24 hours"
    assert risen["lines"][0]["note"] == "up 1.89 ft in 24 hours" and risen["spark"] is None     # five readings: no trend line
    assert not any(p["name"].startswith("West Branch") for p in doc["places"])


def test_a_lowest_of_one_says_nothing(fresh_db):
    """A single soil station is not "the lowest in Illinois"; it's on the map only past a threshold."""
    sig = Signal(source="nrcs_scan", source_id="sc1", sensor_id="scan:2004", sensor_kind="station", topic="soil",
                 metric="soil_moisture_pct", value=14.0, observed_at=utcnow() - timedelta(hours=2),
                 location=_at("mason", 40.31, -89.9), quality="reference",
                 evidence={"kind": "scan", "station": "2004:IL:SCAN", "name": "Mason #1"})
    db.insert_signals([sig])
    assert notable.build()["places"] == []
    db.insert_signals([Signal(**{**sig.__dict__, "source_id": "sc2", "value": 8.3, "id": None})])
    [p] = notable.build()["places"]
    assert p["name"] == "Mason #1 soil station" and p["label"] == "Soil moisture 8.3%".replace("%", " %")
    assert p["why"] == ["Soil moisture past the network's event threshold"]


def test_a_daily_source_shows_its_latest_day(fresh_db):
    db.insert_signals([Signal(source="nrcs_scan", source_id="sc1", sensor_id="scan:2004", sensor_kind="station",
                              topic="soil", metric="soil_moisture_pct", value=7.9,
                              observed_at=utcnow() - timedelta(days=2), location=_at("mason", 40.31, -89.9),
                              quality="reference", evidence={"kind": "scan", "name": "Mason #1"})])
    [p] = notable.build()["places"]
    assert p["topic"] == "soil"


def test_people_appear_by_handle_at_their_zip_never_their_point(fresh_db, make_sensor):
    ann = make_sensor("tg:1", name="Ann", zip_code="62704")
    ann.location = geo.location_from_point(39.781234, -89.654321, online=False)
    db.upsert_sensor(ann)
    contrib.contribute(ann, "hail golf ball -- on my deck at 12 Elm St", source_id_base="m1", online=False,
                       use_llm=False)
    doc = notable.build()
    [p] = doc["places"]
    text = json.dumps(doc)
    assert p["person"] and p["name"].startswith("s-") and p["source"] == "Network member"
    assert (p["lat"], p["lon"]) == opendata.public_point(p["zip"], None)          # the ZIP's centre, not the deck
    assert p["label"] == "Hail 1.75 in" and p["status"] == "unverified"
    for leak in ("Ann", "tg:1", "39.7812", "-89.6543", "Elm St"):
        assert leak not in text, leak


def test_a_quake_outside_the_state_stays_off_its_map(fresh_db):
    db.insert_signals([Signal(source="usgs_quake", source_id="q1", sensor_id="quake:usgs", sensor_kind="official",
                              topic="quake", metric="magnitude", value=3.4, observed_at=utcnow() - timedelta(hours=5),
                              location=geo.Location(lat=36.2, lon=-89.45, county_fips="17003", precision="point"),
                              quality="reference", evidence={"kind": "quake", "place": "7 km SSE of Ridgely, Tennessee"})])
    assert notable.in_state(39.8, -89.65) and not notable.in_state(36.2, -89.45)
    doc = notable.build()
    assert doc["places"] == [] and "Earthquakes" in doc["quiet"]


def test_a_county_wide_reading_is_an_area(fresh_db):
    rows = [Signal(source="usdm", source_id=f"d{c}", sensor_id="usdm:drought_monitor", sensor_kind="official",
                   topic="agriculture", metric="drought_category", value=v, observed_at=utcnow() - timedelta(days=3),
                   location=_at(c), quality="reference", evidence={"kind": "usdm"})
            for c, v in (("mason", 3.0), ("cass", 3.0), ("cook", 1.0))]
    db.insert_signals(rows)
    doc = notable.build()
    [area] = doc["areas"]
    assert area["label"] == "Severe drought (D2)" and len(area["counties"]) == 2 and doc["places"] == []


def test_the_news_is_drawn_to_the_readings_it_is_about(fresh_db):
    db.insert_signals(_gauge("05536290", "cook", 41.6, -87.6, [5.7, 6.1, 6.6, 7.1, 7.59],
                             "LITTLE CALUMET RIVER AT SOUTH HOLLAND, IL"))
    db.insert_signals([_report(1, "whiteside", 1.5, 41.81, -90.08)])
    _story(1, "Water alert issued for Chicago-area; residents asked to delay showers amid flooding", "water")
    _story(2, "Chicago weather: Rain could bring flooding through Thursday - FOX 32 Chicago", "weather")
    _story(3, "Fall bird migration: a look at who is winging south in Will County", "nature")
    _story(4, "Pressure mounts for lawmakers to regulate new data centers", "landuse")
    _story(5, "Severe weather threat, heavy rain to finish out this week", "weather")
    doc = notable.build()
    news = {n["place"] or n["stories"][0]["title"]: n for n in doc["news"]}
    chicago = news["Chicago area"]
    assert len(chicago["stories"]) == 2 and chicago["lat"] == round(connect.place_point("Chicago")[0], 4)
    gauge = _place(doc, "Little Calumet River at South Holland")
    assert chicago["links"] == [gauge["id"]] and chicago["id"] in gauge["news"]
    assert any(w.startswith("In the news") for w in gauge["why"])
    assert news["Will County"]["links"] == []                 # a bird count is not the Chicago flooding
    statewide = [n for n in doc["news"] if n["statewide"]]
    rain = _place(doc, "2 mi NNW of Town1")
    assert {n["stories"][0]["title"] for n in statewide} == {
        "Pressure mounts for lawmakers to regulate new data centers",
        "Severe weather threat, heavy rain to finish out this week"}
    assert next(n for n in statewide if "rain" in n["stories"][0]["title"])["links"] == [rain["id"]]
    assert next(n for n in statewide if "Pressure" in n["stories"][0]["title"])["links"] == []
    assert [n["n"] for n in doc["news"]] == list(range(1, len(doc["news"]) + 1))


def test_headlines_name_measures_by_their_news_words_not_one_word_aliases():
    def named(title: str) -> set[str]:
        return {m for m in ("pressure_hpa", "crop_condition", "rain_mm", "stage_m", "tornado")
                if connect._named(find_metric(m), title)}

    assert named("Pressure mounts for lawmakers to regulate new data centers") == set()
    assert named("Flood warnings issued as conditions worsen") == {"stage_m"}
    assert named("Rainy stretch for the Quad Cities") == {"rain_mm"}
    assert named("Danville school district to pay for summer tornado damage") == {"tornado"}
    assert connect.place_point("Quad Cities") == (geo.county_by_name("rockisland").lat,
                                                  geo.county_by_name("rockisland").lon)
    assert connect.counties_in("Bloomington-Normal council meets") == [geo.county_by_name("mclean").fips]


def test_scales_read_in_their_own_words():
    assert find_metric("corn_stage").display(19) == "R4" and find_metric("drought_category").display(3) == "D2"
    assert find_metric("crop_condition").display(4) == "good" and find_metric("corn_basis").display(-0.4) == "-40 ¢"
    assert find_metric("rain_mm").display(2.5) == "0.10 in" and find_metric("wind_gust_ms").short == "Gust"
