"""The morning brief's map: the day across the state, as one picture."""
from __future__ import annotations

from io import BytesIO

from PIL import Image

from intelnet import contrib, daymap, notable
from intelnet.config import settings


def _day(make_sensor) -> dict:
    ann = make_sensor("tg:5", zip_code="62704", chat_id="5")
    contrib.contribute(ann, "rain 1.5in; hail golf ball", source_id_base="m1", online=False, use_llm=False)
    return notable.build()


def test_the_picture_is_the_state_portrait_with_its_key(fresh_db, make_sensor):
    day = _day(make_sensor)
    img = Image.open(BytesIO(daymap.render(day, "17167", alerts=[])))
    assert img.format == "JPEG" and img.size == (round(daymap.W * daymap.K), round(daymap.H * daymap.K)) == (2048, 2560)
    # the state sits left of the key, as large as the height allows
    _, left, top, right, *_ = daymap._fit()
    assert right < daymap.W - daymap.RAIL and top <= daymap.PAD + 1
    x, y = daymap.px(39.78, -89.65)                                          # Springfield
    assert left < x < right and top < y < daymap.H - daymap.PAD


def test_a_picture_is_named_by_what_it_shows_and_made_once(fresh_db, make_sensor, monkeypatch):
    day = _day(make_sensor)
    first = daymap.prepare(day, "17167")
    assert first and first.startswith("dm-") and first == daymap.prepare(day, "17167")
    assert daymap.prepare(day, None) != first                                # the county outline is part of it
    from intelnet import cardmap

    assert (cardmap._dir() / f"{first}.jpg").exists()
    assert cardmap.media_for(f'<img src="tg://photo?id={first}"/>')[first][:2] == b"\xff\xd8"
    monkeypatch.setattr(settings, "card_maps", False)
    assert daymap.prepare(day, "17167") is None                              # pictures off: a brief without one


def test_a_story_square_moved_aside_never_sits_on_the_key():
    from intelnet import daymap

    # three stories at the state's north-east corner: the later ones move, but stay on the map
    day = {"news": [{"id": f"s{i}", "n": i, "lat": 42.45, "lon": -87.85} for i in range(1, 4)]}
    spots = daymap._badge_spots(day)
    xs = [x for x, _ in spots.values()]
    assert len(set(spots.values())) == 3 and max(xs) <= daymap.W - daymap.RAIL - 6 - 28


def test_a_dense_picture_keeps_its_layout():
    """Drawn at two pixels a point, a label measures the same in points and its ink lands at
    twice the pixels: the layout stays put while the detail doubles."""
    from intelnet import cardmap
    from PIL import ImageDraw

    font = cardmap._font(24, "SemiBold")
    boxes = []
    for k in (1, 2):
        img = Image.new("RGB", (400 * k, 200 * k))
        labels = cardmap._Labels(cardmap.Dense(ImageDraw.Draw(img), k))
        assert labels.put((50, 100), "Peoria", font, anchor="lm")
        boxes.append(labels.taken[0])
        x0, y0, x1, y1 = img.getbbox()                                         # the ink, in pixels
        assert abs(x0 - k * boxes[-1][0]) <= 2 * k and abs(x1 - k * boxes[-1][2]) <= 2 * k
    assert all(abs(a - b) <= 1 for a, b in zip(*boxes, strict=True))           # the same box, in points


def test_the_pictures_draw_finer_rivers_than_the_app_downloads():
    """config/geo/il-detail.json (~50 m) is what the pictures draw; the Mini App's layer stays light."""
    import json

    from intelnet import cardmap

    def points(ref: dict) -> int:
        return sum(len(line) for r in ref.get("rivers") or [] for line in r["lines"])

    shipped = json.loads((cardmap.ASSETS / "il-reference.json").read_text(encoding="utf-8"))
    assert points(cardmap._reference()) > 3 * points(shipped) > 0


def test_the_state_stands_off_its_ground():
    """Illinois reads against what's around it on either map (dark 1.15:1 and light 1.12:1 sank, 8 Oct)."""
    def luminance(rgb: tuple[int, int, int]) -> float:
        lin = [(c / 255 / 12.92) if c <= 10 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    for pal, least in ((daymap.DARK, 1.4), (daymap.LIGHT, 1.3)):
        a, b = sorted((luminance(pal.land), luminance(pal.ground)))
        assert (b + 0.05) / (a + 0.05) >= least
