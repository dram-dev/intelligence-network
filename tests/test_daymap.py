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
    assert img.format == "JPEG" and img.size == (daymap.W, daymap.H)
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
