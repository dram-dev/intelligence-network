"""What's ahead: the forecast for a place and the outlooks over it, in the brief, the digest
and /forecast. The sources are faked with the shapes they publish (NWS points + forecast,
SPC categorical GeoJSON, WPC excessive-rainfall GeoJSON)."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from intelnet import ahead, bot, brief, db, digest, geo
from intelnet.config import settings

SPI = (39.7817, -89.6501)                       # Springfield, Sangamon County
FORECAST_URL = "https://api.weather.test/gridpoints/ILX/47,56/forecast"
# Everything is dated from tomorrow 6 AM in Illinois, so the forecast is never stale whenever
# the suite runs; NOW is 01:10 that morning (the nightly digest).
BASE = (datetime.now(ZoneInfo("America/Chicago")) + timedelta(days=1)).replace(hour=6, minute=0, second=0,
                                                                             microsecond=0)
NOW = (BASE - timedelta(hours=4, minutes=50)).astimezone(timezone.utc)


def _at(hours: float, *, iso: bool = True) -> str:
    t = (BASE + timedelta(hours=hours)).astimezone(timezone.utc)
    return t.isoformat() if iso else t.strftime("%Y-%m-%d %H:%M:%S")          # the WPC layer's way


def _period(n, name, start, hours, day, temp, short, pop=None, wind="5 mph", direction="S"):
    return {"number": n, "name": name, "startTime": _at(start), "endTime": _at(start + hours), "isDaytime": day,
            "temperature": temp, "temperatureUnit": "F",
            "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": pop},
            "windSpeed": wind, "windDirection": direction, "shortForecast": short}


FORECAST = {"properties": {"periods": [
    _period(1, "Overnight", -5, 5, False, 58, "Mostly Clear", 1),
    _period(2, "Wednesday", 0, 12, True, 86, "Chance Showers And Thunderstorms", 40, "15 to 25 mph", "SW"),
    _period(3, "Wednesday Night", 12, 12, False, 54, "Mostly Clear", None, "1 to 6 mph", "NNW"),
    _period(4, "Thursday", 24, 12, True, 79, "Sunny", 0),
]}}


def _square(lat: float, lon: float, d: float) -> list[list[float]]:
    return [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]


def _spc(label: str, rings: list, valid: str, expire: str) -> dict:
    return {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": [rings]},
            "properties": {"LABEL": label, "VALID_ISO": valid, "EXPIRE_ISO": expire}}


def _ero(outlook: str, ring: list, start: str, end: str) -> dict:
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"outlook": outlook, "start_time": start, "end_time": end}}


SOURCES = {
    # Day 1 is still last night's issuance at 01:10 (valid until 12Z): the day comes from Day 2
    "day1otlk": {"features": [_spc("MRGL", [_square(*SPI, 3)], _at(-10), _at(1))]},
    "day2otlk": {"features": [
        _spc("TSTM", [_square(*SPI, 4)], _at(1), _at(25)),
        _spc("MRGL", [_square(*SPI, 0.6)], _at(1), _at(25)),
        # a slight risk around Springfield with Springfield cut out: the hole counts
        _spc("SLGT", [_square(*SPI, 2), _square(*SPI, 0.3)], _at(1), _at(25)),
    ]},
    "day3otlk": {"features": []},
    "MapServer/0/": {"features": [_ero("Slight (At Least 15%)", _square(*SPI, 0.5), _at(1, iso=False),
                                       _at(25, iso=False))]},
    "MapServer/1/": {"features": []},
    "MapServer/2/": {"features": []},
}


@pytest.fixture
def sources(monkeypatch):
    """The sources, faked; every URL asked is recorded."""
    asked: list[str] = []

    def get(url: str):
        asked.append(url)
        if "/points/" in url:
            return {"properties": {"forecast": FORECAST_URL}}
        if url == FORECAST_URL:
            return FORECAST
        for key, body in SOURCES.items():
            if key in url:
                return body
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(settings, "ahead_enabled", True)
    monkeypatch.setattr(ahead, "_get", get)
    return asked


def test_a_period_reads_as_a_short_line():
    _, day, night, thursday = (ahead._period(x) for x in FORECAST["properties"]["periods"])
    assert day.text() == "Chance showers and thunderstorms (40%), high 86°F, wind SW 15–25 mph"
    assert night.text() == "Mostly clear, low 54°F"                       # light wind, no chance: left out
    assert thursday.text() == "Sunny, high 79°F"
    assert (ahead.icon(day), ahead.icon(night), ahead.icon(thursday)) == ("⛈", "🌙", "☀️")


def test_the_day_is_the_next_daytime_period_and_the_outlook_valid_at_its_noon(fresh_db, sources):
    day = ahead.for_point(*SPI, now=NOW)
    assert [p.name for p in day.periods] == ["Wednesday", "Wednesday Night"]     # "Overnight" is skipped
    assert [(r.name, r.level, r.word, r.day) for r in day.risks] == [
        ("Severe storms", 1, "Marginal", "today"),          # Day 2's marginal; the slight risk's hole spares it
        ("Flash flooding", 2, "Slight", "today")]
    assert day.risks[0].text() == "marginal risk today (level 1 of 5, Storm Prediction Center)"
    assert db.kv_get(f"nwsfc:{geo.geohash(*SPI, 6)}") == FORECAST_URL            # the place's forecast URL is kept
    sources.clear()
    ahead.for_point(*SPI, now=NOW)
    assert not any("/points/" in u for u in sources)


def test_periods_that_ended_are_dropped(fresh_db, sources):
    # the API's cache can serve a forecast hours old: at 7 PM "Wednesday" is over
    evening = (BASE + timedelta(hours=13)).astimezone(timezone.utc)
    assert [p.name for p in ahead.periods(*SPI, now=evening)] == ["Thursday"]
    assert [p.name for p in ahead.periods(*SPI, now=evening, from_daytime=False)] == ["Wednesday Night", "Thursday"]


def test_counties_at_risk_are_named(fresh_db, sources):
    at = ahead.target(NOW, [])
    risks = ahead.county_risks(at, ahead.day_word(NOW, at))
    by = {(r.name, r.word): names for r, names in risks}
    assert "Macon" in by[("Severe storms", "Slight")] and "Sangamon" not in by[("Severe storms", "Slight")]
    assert "Sangamon" in by[("Severe storms", "Marginal")] and "Sangamon" in by[("Flash flooding", "Slight")]
    assert [r.word for r, _ in risks if r.name == "Severe storms"] == ["Slight", "Marginal"]   # highest first
    assert ahead.names(["Henry"]) == "Henry County"
    assert ahead.names(["Henry", "Lee", "Stark"]) == "Henry, Lee and Stark counties"
    assert ahead.names([str(i) for i in range(9)]) == "0, 1, 2, 3, 4, 5 and 3 more counties"


def test_a_failed_source_leaves_its_lines_out(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "ahead_enabled", True)

    def down(url):
        raise ConnectionError("offline")

    monkeypatch.setattr(ahead, "_get", down)
    assert not ahead.for_point(*SPI, now=NOW)
    assert brief.today(geo.county("17167"), NOW) == []
    monkeypatch.setattr(settings, "ahead_enabled", False)
    assert not ahead.for_point(*SPI, now=NOW) and not ahead.statewide(NOW)


def test_the_brief_opens_with_the_countys_day(fresh_db, sources):
    rich = brief.compose_rich("17167", {}, now=NOW, day={})
    head, _, rest = rich.partition("</h4>")
    assert rest.startswith("<p>⛈ <b>Wednesday</b>: Chance showers and thunderstorms (40%), high 86°F, wind SW "
                           "15–25 mph<br>🌙 <b>Wednesday Night</b>: Mostly clear, low 54°F<br>⛈ <b>Severe storms</b>: "
                           "marginal risk today (level 1 of 5, Storm Prediction Center)")
    plain = brief.compose("17167", {}, now=NOW, day={})
    assert plain.split("\n")[1].startswith("⛈ <b>Wednesday</b>: Chance showers")
    statewide = brief.compose_rich(None, {}, now=NOW, day={})               # no county: who's at risk
    assert "<b>Flash flooding</b>: slight risk today (level 2 of 4, Weather Prediction Center) for " in statewide


def test_the_digest_has_the_day_ahead(make_sensor, sources):
    m = digest.build(hours=24)
    assert [name for name, _ in m.ahead.places][:2] == ["Rockford", "Chicago"] and m.ahead.risks
    html = digest.render_html(m)
    assert "The day ahead" in html and ">WEDNESDAY NIGHT<" in html                 # table heads are capitals
    assert "Chance showers and thunderstorms (40%), high 86°F, wind SW 15–25 mph" in html
    # the digest is built on the real clock, so the day reads "today", "tomorrow" or a weekday
    assert re.search(r"<b>Severe storms</b>: \w+ risk \w+ \(level \d of 5, Storm Prediction Center\) for ", html)
    assert html.index("official readings</p>") < html.index("The day ahead") < html.index("Warnings and advisories")
    assert "The day ahead:" in digest.render_text(m)


def msg(text: str, uid: int = 42) -> dict:
    return {"message_id": 1, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
            "from": {"id": uid, "first_name": "Cy", "last_name": "Q", "username": "cyq"}, "text": text}


def test_forecast_answers_for_home_or_a_named_place(fresh_db, sources):
    assert "Where?" in bot.handle_message(msg("/forecast"))
    bot.handle_message(msg("/home 62704"))
    r = bot.handle_message(msg("/forecast"))
    assert r.startswith("🌤 <b>Forecast for ") and "<b>Overnight</b>: Mostly clear, low 58°F" in r
    assert "<b>Thursday</b>" not in r and "National Weather Service" in r          # three periods from now
    assert "<b>Wednesday</b>" in bot.handle_message(msg("/forecast 61602"))
