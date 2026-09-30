"""The card's radar: Level III radials decoded (a synthetic N0B file, built here), drawn
offline, and the loop that replaces a card's picture once delivery gets to it."""
from __future__ import annotations

import bz2
import struct
from datetime import datetime, timezone

import numpy as np
import pytest
from test_alert_threads import BOX, feature, run_feed

from intelnet import cardmap, db, delivery, geo, nexrad
from intelnet.config import settings

SITE = (40.1506, -89.3368)                  # Lincoln (KILX)
KEY = "ILX_N0B_2026_09_30_23_09_24"


def level3(echoes: dict[tuple[int, int], float], *, n_bins: int = 120, gate_km: float = 0.25) -> bytes:
    """A small N0B product: 720 half-degree radials, `echoes` {(radial, bin): dBZ}."""
    levels = np.zeros((720, n_bins), dtype=np.uint8)
    for (r, b), dbz in echoes.items():
        levels[r, b] = round((dbz + 32) * 2) + 2
    packet = struct.pack(">hhhhhhh", 16, 0, n_bins, 256, 280, 999, 720)
    for r in range(720):
        packet += struct.pack(">hhh", n_bins, r * 5, 5) + levels[r].tobytes()
    layer = struct.pack(">hi", -1, len(packet)) + packet
    symbology = struct.pack(">hhih", -1, 1, 10 + len(layer), 1) + layer
    days = (datetime(2026, 9, 30, tzinfo=timezone.utc) - datetime(1969, 12, 31, tzinfo=timezone.utc)).days
    secs = 23 * 3600 + 9 * 60 + 24
    pdb = bytearray(102)
    struct.pack_into(">hii", pdb, 0, -1, round(SITE[0] * 1000), round(SITE[1] * 1000))
    struct.pack_into(">h", pdb, 12, 153)
    struct.pack_into(">hi", pdb, 22, days, secs)
    struct.pack_into(">hhh", pdb, 42, -320, 5, 254)                 # thresholds: −32 dBZ, 0.5 steps
    struct.pack_into(">h", pdb, 82, 1)                               # bzip2
    header = struct.pack(">hhiihhh", 153, days, secs, 0, 0, 0, 3)
    return b"SDUS53 KILX 302309\r\r\nN0BILX\r\r\n" + header + bytes(pdb) + bz2.compress(symbology)


def test_decode_reads_radials_times_and_values():
    raw = level3({(180, 40): 55.0, (180, 41): 20.0})             # due east (90°), 10 km out
    s = nexrad.decode(raw)
    assert (s.site_lat, s.site_lon) == (40.151, -89.337) and s.width == 0.5 and s.data.shape == (720, 120)
    assert s.time == datetime(2026, 9, 30, 23, 9, 24, tzinfo=timezone.utc)
    assert s.data[180, 40] == 55.0 and s.data[180, 41] == 20.0 and np.isnan(s.data[0, 0])
    assert nexrad.key_time(KEY) == s.time


def test_sampling_puts_an_echo_where_the_radar_saw_it():
    s = nexrad.decode(level3({(r, b): 55.0 for r in range(178, 183) for b in range(38, 44)}))
    lat = np.array([[SITE[0]]])
    east = np.array([[SITE[1] + 10.1 / (111.32 * np.cos(np.radians(SITE[0])))]])   # ~10 km east
    west = np.array([[SITE[1] - 10.1 / (111.32 * np.cos(np.radians(SITE[0])))]])
    assert nexrad.sample(s, lat, east)[0, 0] == pytest.approx(55.0, abs=0.5)
    assert nexrad.sample(s, lat, west)[0, 0] < 0                    # nothing west: below anything drawn


HOME = (39.78, -89.65)                     # inside BOX, ~43 km south-southwest of the radar


def _polar(lat: float, lon: float) -> tuple[int, int]:
    """(radial, bin) of a point as the radar sees it."""
    import math

    p1, p2, dl = math.radians(SITE[0]), math.radians(lat), math.radians(lon - SITE[1])
    az = math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                 math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360
    return int(az / 0.5), int(geo.haversine_km(SITE[0], SITE[1], lat, lon) / 0.25)


@pytest.fixture
def offline_radar(monkeypatch):
    r0, b0 = _polar(*HOME)
    raw = level3({(r, b): 50.0 for r in range(r0 - 6, r0 + 7) for b in range(b0 - 12, b0 + 13)}, n_bins=b0 + 40)
    monkeypatch.setattr(settings, "card_map_radar", True)
    monkeypatch.setattr(cardmap, "radar_keys", lambda topic, f, minutes: [KEY])
    monkeypatch.setattr(nexrad, "fetch", lambda key, cache=None: raw if "_N0B_" in key else None)
    cardmap._LAYERS.clear()
    return raw


def test_the_picture_draws_the_radar_under_the_warning(fresh_db, offline_radar):
    from intelnet.feeds import nws_alerts

    rows = nws_alerts.parse_alerts({"features": [feature("R9", polygon=BOX)]})
    sc = cardmap.scene(rows[0], rows, None)
    img = cardmap.render_image(sc, radar_key=KEY)
    f = cardmap.frame_for(cardmap._points(sc))
    x, y = f.px(*HOME)
    r, g, _ = img.getpixel((int(x), int(y)))
    assert img.size == (cardmap.W, cardmap.H) and r > 180 and g < 110     # a 50 dBZ red, not the dark map
    far_x, far_y = f.px(HOME[0] - 0.25, HOME[1] - 0.2)                  # no echo there
    assert max(img.getpixel((int(far_x), int(far_y)))) < 90


def test_a_card_gets_its_picture_first_then_its_loop(fresh_db, sent, monkeypatch, make_sensor, offline_radar):
    monkeypatch.setattr(settings, "card_map_loop", True)
    monkeypatch.setattr(cardmap, "loop_possible", lambda picture: True)
    monkeypatch.setattr(cardmap, "render_loop", lambda sc, **kw: b"\x00\x00\x00\x18ftypmp42 loop")
    make_sensor("tg:1", zip_code="62704", chat_id="1")
    db.set_sensor_location("tg:1", geo.location_from_point(39.78, -89.65, online=False))
    db.add_subscription("1", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("L1", polygon=BOX))
    assert "tg://photo?id=wm-" in sent.rich[0] and not sent.edits           # the card: its picture, at once
    delivery.retry_due()                                                    # the next pass of the outbox
    [(chat, _, _)] = sent.edits
    rich, (media, fallback) = sent.edit_rich[-1], sent.edit_media[-1]
    [loop] = [k for k in media if k.startswith("wl-")]
    assert chat == "1" and f'<video src="tg://video?id={loop}"></video>' in rich and "tg://photo" not in rich
    assert media[loop].startswith(b"\x00\x00\x00\x18ftyp") and fallback is False   # refused → the picture stays


def test_a_loop_that_cant_be_made_is_never_sent(fresh_db, sent, monkeypatch, make_sensor, offline_radar):
    monkeypatch.setattr(settings, "card_map_loop", True)
    monkeypatch.setattr(cardmap, "loop_possible", lambda picture: True)
    monkeypatch.setattr(cardmap, "render_loop", lambda sc, **kw: None)     # e.g. too few sweeps
    make_sensor("tg:1", zip_code="62704", chat_id="1")
    db.add_subscription("1", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("L2", polygon=BOX))
    delivery.retry_due()
    assert not sent.edits


def test_pictures_and_loops_are_stripped_together():
    from intelnet.telegram import strip_pictures

    rich = ('<h4>W</h4><figure><video src="tg://video?id=wl-1"></video><figcaption>c</figcaption></figure>'
            '<figure><img src="tg://photo?id=wm-1"/></figure><p>after</p>')
    assert strip_pictures(rich) == "<h4>W</h4><p>after</p>"
