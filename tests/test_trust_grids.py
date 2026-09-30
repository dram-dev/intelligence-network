"""Trust v2 (per topic, fading, sure-or-new, one witness per roof) and radar grids."""
from __future__ import annotations

import gzip
import io
import struct
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from intelnet import bot, contrib, db, geo, grids, network, trust, watch
from intelnet.config import settings
from intelnet.models import Signal, utcnow


def _contrib(sensor, text, base):
    return contrib.contribute(sensor, text, source_id_base=base, online=False, use_llm=False)


def _at(sensor_id: str, lat: float, lon: float, make_sensor, name: str = "P"):
    """A sensor whose home is an exact point (a shared location), not a ZIP center."""
    s = make_sensor(sensor_id, name=name)
    s.location = geo.location_from_point(lat, lon, online=False)
    return db.upsert_sensor(s)


# ── Trust v2 ──────────────────────────────────────────────────────────────

def test_trust_is_kept_per_topic_and_reads_new_until_there_is_a_record(make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    assert trust.standing("tg:1", "weather").label == "new · 0 checks"
    for _ in range(4):
        trust.record(ann.id, "weather", agree=1.0)
    w, s = trust.standing("tg:1", "weather"), trust.standing("tg:1", "soil")
    assert not w.is_new and w.trust == pytest.approx(trust.shrunk(4, 0)) and w.low < w.trust < w.high
    assert s.is_new and s.trust == 0.5                         # a good hail spotter isn't a soil sampler
    assert db.get_sensor("tg:1").trust == pytest.approx(w.trust)
    assert [st.topic for st in trust.standings("tg:1")] == ["weather"]


def test_old_outcomes_fade_with_a_half_life(make_sensor):
    make_sensor("tg:1", zip_code="62704")
    then = utcnow() - timedelta(days=trust.HALF_LIFE_DAYS)
    trust.record("tg:1", "weather", agree=4.0, now=then)
    faded = trust.standing("tg:1", "weather")
    assert faded.trust == pytest.approx(trust.shrunk(2.0, 0.0), abs=1e-3)   # four agreements, worth two now


def test_one_roof_is_one_witness_and_a_pair_that_always_agrees_counts_less(make_sensor):
    a = _at("tg:1", 39.7817, -89.6501, make_sensor)
    b = _at("tg:2", 39.7818, -89.6502, make_sensor, "Roommate")          # ~15 m away
    c = _at("tg:3", 39.8000, -89.6000, make_sensor, "Across town")
    sa, sb, sc = (Signal(source="t", source_id=x.id, sensor_id=x.id, sensor_kind="human", topic="weather",
                         metric="hail_mm", value=25, location=x.location) for x in (a, b, c))
    assert trust.independence(sa, sb) == trust.SAME_PLACE_WEIGHT
    assert trust.independence(sa, sc) == 1.0
    for _ in range(3):
        db.bump_pair("tg:1", "tg:3")
    assert trust.independence(sa, sc) == pytest.approx(0.5)
    assert trust.witnesses([sa, sb, sc]) == 2
    # two people who only set /home 62704 share its center, not a roof
    z1, z2 = make_sensor("tg:4", zip_code="62704"), make_sensor("tg:5", zip_code="62704")
    s4, s5 = (Signal(source="t", source_id=x.id, sensor_id=x.id, sensor_kind="human", topic="weather",
                     metric="hail_mm", value=25, location=x.location) for x in (z1, z2))
    assert trust.independence(s4, s5) == 1.0 and trust.witnesses([s4, s5]) == 2


def test_two_people_under_one_roof_do_not_verify_an_event_alone(make_sensor):
    a = _at("tg:1", 39.7817, -89.6501, make_sensor)
    b = _at("tg:2", 39.7818, -89.6502, make_sensor, "Roommate")
    _contrib(a, "hail golf ball", "m1")
    res = _contrib(b, "hail golf ball", "m2").assessments[0]
    assert res.quality == "corroborated" and not res.push_event
    assert db.get_sensor("tg:2").trust < trust.shrunk(1, 0)            # a roommate's word is worth 0.3
    c = _at("tg:3", 39.80, -89.60, make_sensor, "Neighbor")
    assert _contrib(c, "hail golf ball", "m3").assessments[0].push_event


def test_me_and_the_reading_reply_show_the_record(fresh_db):
    msg = {"message_id": 1, "date": int(utcnow().timestamp()), "chat": {"id": 42, "type": "private"},
           "from": {"id": 42, "first_name": "Cy"}}
    bot.handle_message({**msg, "text": "/join"})
    bot.handle_message({**msg, "text": "/home 62704"})
    r = bot.handle_message({**msg, "message_id": 2, "text": "temp 71"})
    assert "Your weather record: new · 0 checks" in r
    assert "Trust by topic: new: nothing checked yet" in bot.handle_message({**msg, "text": "/me"})


# ── radar grids ───────────────────────────────────────────────────────────

def _grib2(values: list[list[int]], *, la1: float, lo1: float, step: float, valid: datetime,
           ref: float = -30.0, d: int = 1) -> bytes:
    """A real (tiny) MRMS-style GRIB2 message: lat/lon grid, PNG-packed 16-bit values."""
    nj, ni = len(values), len(values[0])
    img = Image.new("I;16", (ni, nj))
    for r, row in enumerate(values):
        for c, v in enumerate(row):
            img.putpixel((c, r), v)
    png = io.BytesIO()
    img.save(png, format="PNG")
    s1 = struct.pack(">IBHHBBBHBBBBBBB", 21, 1, 161, 0, 2, 1, 1, valid.year, valid.month, valid.day,
                     valid.hour, valid.minute, valid.second, 0, 1)
    micro = lambda x: round(x * 1e6)
    s3 = struct.pack(">IBBIBBH", 72, 3, 0, ni * nj, 0, 0, 0) + bytes(16) + struct.pack(
        ">IIIIIIBIIIIB", ni, nj, 0, 0xFFFFFFFF, micro(la1), micro(lo1 % 360), 48,
        micro(la1 - (nj - 1) * step), micro((lo1 + (ni - 1) * step) % 360), micro(step), micro(step), 0)
    s4 = struct.pack(">IBHH", 34, 4, 0, 0) + bytes(25)
    s5 = struct.pack(">IBIHfhhBB", 21, 5, ni * nj, 41, ref, 0, d, 16, 0)
    s6 = struct.pack(">IBB", 6, 6, 255)
    s7 = struct.pack(">IB", 5 + len(png.getvalue()), 7) + png.getvalue()
    body = s1 + s3 + s4 + s5 + s6 + s7 + b"7777"
    return b"GRIB" + bytes(2) + bytes([0, 2]) + struct.pack(">Q", 16 + len(body)) + body


def test_an_mrms_grib2_file_decodes_crops_and_finds_the_nearby_maximum():
    valid = datetime(2026, 9, 30, 21, 40, tzinfo=timezone.utc)
    # 10 × 10 cells of 0.01°, NW corner at 39.85 N, 89.70 W; 20 = covered but nothing, 0 = no radar
    rows = [[20] * 10 for _ in range(10)]
    rows[3][4] = 30 + 445                               # 44.5 mm three cells from the probe
    rows[9][9] = 0
    raw = _grib2(rows, la1=39.85, lo1=-89.70, step=0.01, valid=valid)
    f = grids.parse_grib2(raw)
    assert f.valid == valid and f.image.size == (10, 10)
    assert f.value_at(0, 0) == 0.0 and f.value_at(9, 9) is None and f.value_at(3, 4) == pytest.approx(44.5)
    value, lat, lon = f.max_near(39.82 - 0.03, -89.66, 5)
    assert value == pytest.approx(44.5) and (round(lat, 2), round(lon, 2)) == (39.82, -89.66)
    assert f.max_near(39.80, -89.62, 0)[0] == 0.0
    crop = grids.parse_grib2(gzip.decompress(gzip.compress(raw)), (39.79, -89.67, 39.83, -89.63))
    assert crop.image.size[0] < 10 and crop.max_near(39.82, -89.66, 1)[0] == pytest.approx(44.5)


def test_the_arcgis_reader_takes_the_raw_pixel_for_the_named_product(monkeypatch):
    seen = {}

    class R:
        def raise_for_status(self): ...
        def json(self):
            return {"value": "12.5", "catalogItems": {"features": [
                {"attributes": {"name": "conus_QPE_24H", "idp_validendtime": 1790769600000}}]}}

    def get(url, params=None, headers=None, timeout=None):
        seen.update(params, url=url)
        return R()

    monkeypatch.setattr(grids.requests, "get", get)
    g = grids.grids()["rain_mm"]
    s = grids.sample(g, "24h", 39.78, -89.65)
    assert s.value == 12.5 and s.valid == datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert "conus_QPE_24H" in seen["mosaicRule"] and seen["url"].endswith("/identify")


def test_which_window_judges_a_reading(make_sensor):
    g, hail, rain = grids.grids()["hail_mm"], network.find_metric("hail_mm"), network.find_metric("rain_mm")
    now = utcnow()
    sig = lambda metric, ago, **ev: Signal(source="t", source_id="x", sensor_id="s", sensor_kind="human",
                                          topic="weather", metric=metric, value=30,
                                          observed_at=now - timedelta(minutes=ago), evidence=ev)
    assert grids.pick_window(g, hail, sig("hail_mm", 10), now) == "30m"
    assert grids.pick_window(g, hail, sig("hail_mm", 40), now) == "1h"     # the window starts 15 min early
    assert grids.pick_window(g, hail, sig("hail_mm", 50), now) == "2h"
    assert grids.pick_window(g, hail, sig("hail_mm", -5), now) is None          # the grid hasn't caught up
    r = grids.grids()["rain_mm"]
    assert grids.pick_window(r, rain, sig("rain_mm", 30), now) == "24h"
    assert grids.pick_window(r, rain, sig("rain_mm", 30, period="1h"), now) == "1h"


def test_verdicts():
    g, hail = grids.grids()["hail_mm"], network.find_metric("hail_mm")
    assert grids.verdict(g, hail, 44, 38) == "agree"
    assert grids.verdict(g, hail, 64, 25) == "disagree"          # off, but not three times off
    assert grids.verdict(g, hail, 114, 20) == "far"
    assert grids.verdict(g, hail, 44, 0) == "disagree"           # radar saw nothing; a person saw severe hail
    assert grids.verdict(g, hail, 6, 0) == "quiet"
    assert grids.verdict(g, hail, 44, None) == "nodata"


def _fake(monkeypatch, value_mm: float, valid: datetime | None = None, at=(39.79, -89.66)):
    def reader(grid, product, lat, lon):
        return grids.Sample(value_mm, valid or utcnow(), *at)
    monkeypatch.setitem(grids.READERS, "mrms_grib2", reader)
    monkeypatch.setitem(grids.READERS, "arcgis_image", reader)
    monkeypatch.setattr(settings, "grid_checks_enabled", True)


def test_radar_confirms_hail_tells_the_person_and_verifies_the_event(fresh_db, sent, monkeypatch, make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    db.add_subscription("50", "weather.events", "il.sangamon")
    c = _contrib(ann, "hail golf ball 20m ago", "m1").assessments[0]
    assert c.quality == "raw" and not c.push_event
    _fake(monkeypatch, 40.0)
    out = watch.grid_pass()
    assert out["checked"] == 1 and out["agree"] == 1 and out["events_pushed"] == 1
    [hail] = db.recent_signals(1, kinds=("human",), metric="hail_mm")
    assert (hail.quality, hail.reference_agreement) == ("corroborated", "agree")
    assert hail.evidence["grid"]["verdict"] == "agree" and hail.evidence["grid"]["window"] == "1h"
    notes = [t for chat, t in sent if chat == "1"]
    assert "checked out" in notes[0] and "radar hail estimate (MRMS MESH) of 1.57 in" in notes[0]
    assert "helped verify" in notes[1]
    assert [chat for chat, _ in sent if chat == "50"] == ["50"]           # the event went out
    assert trust.standing("tg:1", "weather").trust == pytest.approx(trust.shrunk(trust.GRID_WEIGHT, 0))
    assert watch.grid_pass()["checked"] == 0                                # judged once


def test_radar_far_off_flags_and_waits_until_it_covers_the_reading(fresh_db, monkeypatch, make_sensor):
    ann = make_sensor("tg:1", zip_code="62704")
    _contrib(ann, "hail softball", "m1")
    _fake(monkeypatch, 20.0, valid=utcnow() - timedelta(minutes=10))        # older than the reading
    assert watch.grid_pass()["checked"] == 0
    assert db.recent_signals(1, kinds=("human",), metric="hail_mm")[0].quality == "raw"
    _fake(monkeypatch, 20.0)
    assert watch.grid_pass()["far"] == 1
    [hail] = db.recent_signals(1, kinds=("human",), metric="hail_mm")
    assert hail.quality == "flagged" and hail.reference_agreement == "disagree"
    assert db.get_sensor("tg:1").n_contradicted == 1


def test_grid_checks_can_be_turned_off(fresh_db, monkeypatch, make_sensor):
    _contrib(make_sensor("tg:1", zip_code="62704"), "hail golf ball", "m1")
    _fake(monkeypatch, 40.0)
    monkeypatch.setattr(settings, "grid_checks_enabled", False)
    assert watch.grid_pass() == {"checked": 0, "events_pushed": 0}
