"""The common data language: grammar, units, words, locations, times, JSON."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from intelnet import language
from intelnet.topics import find_metric


def _one(text, **kw):
    r = language.parse(text, online=False, **kw)
    assert r.signals, (text, r.errors, r.leftover)
    return r.signals[0], r


@pytest.mark.parametrize("text, metric, value", [
    ("rain 1.25in", "rain_mm", 31.75),
    ("rain 1.25", "rain_mm", 31.75),            # bare number → default unit (in)
    ("rainfall: 30 mm", "rain_mm", 30.0),
    ("1.5 inches of rain", "rain_mm", 38.1),
    ("gust 62 mph", "wind_gust_ms", 62 * 0.44704),
    ("gusts 45", "wind_gust_ms", 45 * 0.44704),
    ("wind 30 kt", "wind_ms", 30 * 0.514444),
    ("temp 91F", "temp_c", (91 - 32) * 5 / 9),
    ("temperature 32c", "temp_c", 32.0),
    ("temp 91", "temp_c", (91 - 32) * 5 / 9),
    ("humidity 46%", "humidity_pct", 46.0),
    ("pressure 29.91", "pressure_hpa", 29.91 * 33.8639),
    ("snow 4.5 in", "snow_cm", 4.5 * 2.54),
    ("vis 0.25 mi", "visibility_km", 0.25 * 1.609344),
    ("hail 1.75in", "hail_mm", 44.45),
])
def test_numeric_readings_normalize_to_canonical_units(text, metric, value):
    s, _ = _one(text)
    assert s.metric.key == metric
    assert s.value == pytest.approx(value, rel=1e-4)


@pytest.mark.parametrize("text, mm", [
    ("hail quarter", 25), ("quarter size hail", 25), ("quarter-sized hail", 25),
    ("golf ball hail", 44), ("hail the size of golf balls", 44), ("golfball hail", 44),
    ("pea sized hail", 6), ("hail half dollar", 32), ("softball hail!", 114),
])
def test_named_hail_sizes(text, mm):
    s, _ = _one(text)
    assert (s.metric.key, s.value) == ("hail_mm", mm)


@pytest.mark.parametrize("text, key", [
    ("tornado", "tornado"), ("tornado on the ground", "tornado"), ("funnel cloud", "funnel_cloud"),
    ("street flooding", "flooding"), ("flash flood", "flooding"), ("trees down", "wind_damage"),
    ("power out", "power_outage"), ("lightning", "lightning"),
])
def test_flag_reports_are_readings_by_themselves(text, key):
    s, _ = _one(text)
    assert s.metric.key == key and s.value == 1.0


def test_multiple_clauses_and_flags_in_one_message():
    r = language.parse("1.5 inches of rain; gusts 45; trees down and no power", online=False)
    assert [s.metric.key for s in r.signals] == ["rain_mm", "wind_gust_ms", "wind_damage", "power_outage"]


def test_wind_and_gust_are_distinguished():
    r = language.parse("wind 30mph gusting 55", online=False)
    assert {(s.metric.key, round(s.value / 0.44704)) for s in r.signals} == {("wind_ms", 30), ("wind_gust_ms", 55)}


def test_location_forms():
    s, _ = _one("hail quarter @62704-1234")
    assert (s.location.zip5, s.location.zip9, s.location.county_fips) == ("62704", "62704-1234", "17167")
    s, _ = _one("gust 40 @cook")
    assert s.location.county_fips == "17031" and s.location.precision == "county"
    s, _ = _one("gust 40 @st clair county")
    assert s.location.county_fips == "17163"
    s, _ = _one("gust 40 @39.78,-89.65")
    assert s.location.precision == "point" and s.location.county_fips == "17167"
    s, _ = _one("gust 40 @il.jodaviess")
    assert s.location.county_fips == "17085"


def test_unknown_location_is_an_error_but_reading_still_parses():
    r = language.parse("rain 1in @atlantis", online=False)
    assert r.signals and r.signals[0].location is None
    assert any("unknown location" in e for e in r.errors)


def test_times(now):
    s, _ = _one("temp 91F at 3:15pm", now=now)
    assert s.observed_at == datetime(2026, 9, 14, 20, 15, tzinfo=timezone.utc)   # CDT → UTC
    s, _ = _one("gust 50 20 min ago", now=now)
    assert s.observed_at == datetime(2026, 9, 15, 2, 40, tzinfo=timezone.utc)
    s, _ = _one("gust 50 2h ago", now=now)
    assert s.observed_at == datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)
    s, _ = _one("rain 1in 2026-09-14T20:00Z", now=now)
    assert s.observed_at == datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)
    s, _ = _one("temp 70 at 11pm", now=now)     # in the future locally → yesterday
    assert s.observed_at < now


def test_note_and_tags():
    s, _ = _one("tornado @sangamon #storm #spotter -- on the ground west of town")
    assert s.note == "on the ground west of town" and s.tags == ["storm", "spotter"]


def test_bare_metric_asks_for_a_value():
    r = language.parse("rain", online=False)
    assert not r.signals and any("give a value" in e for e in r.errors)


def test_out_of_range_is_rejected_with_a_message():
    r = language.parse("temp 500F", online=False)
    assert not r.signals and any("outside the plausible range" in e for e in r.errors)


def test_prose_becomes_leftover_for_the_llm():
    r = language.parse("it is really windy here and my fence blew over", online=False)
    assert not r.signals and "fence" in r.leftover


def test_json_form_strict():
    r = language.parse_json('{"metric":"rain_mm","value":1.2,"unit":"in","location":"62704-1234",'
                            '"observed_at":"2026-09-14T20:05:00Z","confidence":0.9}', online=False)
    assert r.ok and r.signals[0].value == pytest.approx(30.48)
    assert r.signals[0].location.zip9 == "62704-1234" and r.signals[0].confidence == 0.9
    r = language.parse_json('{"metric":"nope","value":1}', online=False)
    assert not r.signals and "unknown metric" in r.errors[0]
    r = language.parse_json("not json", online=False)
    assert "invalid JSON" in r.errors[0]
    r = language.parse_json('[{"metric":"tornado"},{"metric":"hail","value":1,"unit":"in"}]', online=False)
    assert [s.metric.key for s in r.signals] == ["tornado", "hail_mm"]


def test_metric_lookup_by_alias_and_display():
    m = find_metric("gust")
    assert m is not None and m.key == "wind_gust_ms"
    assert m.display(26.82).startswith("60 mph")
    assert find_metric("no such thing") is None


def test_cheatsheet_and_examples_are_generated_from_the_pack():
    sheet = language.cheatsheet()
    assert "rain" in sheet and "@62704" in sheet
    assert '"metric"' in language.as_json_example()
