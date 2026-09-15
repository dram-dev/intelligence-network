"""Topic packs are the data language: a second pack must load with no code change."""
from __future__ import annotations

from pathlib import Path

import pytest

from intelnet import topics


def test_weather_pack_shape():
    t = topics.get_topic("weather")
    assert t.category_keys == ["weather.warnings", "weather.alerts", "weather.events",
                               "weather.reports", "weather.digest"]
    assert t.alert_routing["Extreme"] == ["warnings", "alerts"]
    m = t.metrics["hail_mm"]
    assert m.words["golf ball"] == 44 and m.default_unit == "in"
    assert m.is_event(25) and not m.is_event(24) and m.severity(70) == 1.0
    assert t.metrics["visibility_km"].is_event(0.3) and not t.metrics["visibility_km"].is_event(5)
    assert t.metrics["tornado"].is_flag and t.metrics["tornado"].convert(0, None) == 1.0
    assert t.lsr_types["HAIL"]["metric"] == "hail_mm" and t.station_fields["tmpf"]["unit"] == "f"
    assert "tornado_warning" in t.alert_support["tornado"]


def test_compatibility_tolerances():
    t = topics.get_topic("weather")
    temp, gust = t.metrics["temp_c"], t.metrics["wind_gust_ms"]
    assert temp.compatible(20, 22.9) and not temp.compatible(20, 24)
    assert gust.compatible(30, 20) and not gust.compatible(30, 15)    # rel 35% of the max
    assert gust.compatible(2, 5)                                        # abs floor 4 m/s


def test_unit_expression_sandbox():
    with pytest.raises(ValueError):
        topics._compile_expr("__import__('os')")
    with pytest.raises(ValueError):
        topics._compile_expr("y * 2")
    assert topics._compile_expr("(x - 32) * 5 / 9")(212) == 100


def test_resolve_category_shorthand():
    assert topics.resolve_category("warnings") == "weather.warnings"
    assert topics.resolve_category("weather.digest") == "weather.digest"
    assert topics.resolve_category("nope") is None


def test_a_second_pack_loads_from_yaml(tmp_path: Path):
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "river.yaml").write_text(
        "topic: river\nlabel: River\ncategories: {events: e, digest: d}\n"
        "metrics:\n  stage_m: {label: Stage, unit: m, aliases: [stage, river stage],\n"
        "    units: {m: 1, ft: 0.3048}, default_unit: ft, range: [0, 30], event: [[6, 0.5]]}\n",
        encoding="utf-8",
    )
    packs = topics.topics.__wrapped__(tmp_path)
    assert list(packs) == ["river"]
    m = packs["river"].metrics["stage_m"]
    assert m.convert(10, None) == pytest.approx(3.048) and m.is_event(6.5)
