"""The polish wave: numbers as people write them, the alert card's picture of the warned
area (drawn, uploaded once, reused by file id), and what the card says when you're outside."""
from __future__ import annotations

import json

import pytest
from test_alert_threads import BOX, feature, run_feed

from intelnet import cardmap, db, geo, telegram
from intelnet.config import settings
from intelnet.topics import find_metric, fraction, nice_number


@pytest.mark.parametrize(("metric", "value", "shown"), [
    ("visibility_km", 0.1006, "1/16 mi"),        # the 30 Sep digest said "0.0625 mi (0.1006 km)"
    ("visibility_km", 2.414, "1 1/2 mi"),
    ("visibility_km", 16.09, "10 mi"),
    ("pressure_hpa", 1013.2, "29.92 inHg"),
    ("hail_mm", 44.45, "1.75 in"),
    ("discharge_cms", 7843.8, "277,000 cfs"),    # three figures, never 277,002 or 2.77e+05
    ("temp_c", 30.39, "86.7 °F"),
])
def test_values_read_the_way_people_write_them(metric, value, shown):
    assert find_metric(metric).display(value) == shown


def test_number_helpers():
    assert (fraction(0.25), fraction(0.75), fraction(1.0), fraction(2.9)) == ("1/4", "3/4", "1", "2 7/8")
    assert (nice_number(0.01), nice_number(99.94), nice_number(1013.2), nice_number(0.00004)) == \
        ("0.01", "99.9", "1,013", "0.00")


def test_how_far_outside_and_which_side():
    ring = [[-89.8, 39.7], [-89.6, 39.7], [-89.6, 39.9], [-89.8, 39.9], [-89.8, 39.7]]
    km, bearing = geo.polygon_gap(39.8, -89.5, [ring])         # 0.1° east of the east edge
    assert 8 < km < 9 and geo.compass(bearing) == "east"
    assert geo.compass(geo.polygon_gap(40.0, -89.7, [ring])[1]) == "north"


def test_the_card_carries_a_picture_uploaded_once_then_reused(fresh_db, sent, monkeypatch, make_sensor):
    make_sensor("tg:1", zip_code="62704", chat_id="1")
    db.set_sensor_location("tg:1", geo.location_from_point(39.78, -89.65, online=False))
    db.add_subscription("1", "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("P1", polygon=BOX))
    [media] = [m for m in sent.media if m]
    [(name, png)] = media.items()
    assert name.startswith("wm-") and png[:2] == b"\xff\xd8" and f"tg://photo?id={name}" in sent.rich[0]
    # Telegram's reply names its copy: the next card showing the same picture sends the file id
    cardmap.remember(sent.rich[0], {"rich_message": {"blocks": [
        {"type": "paragraph"}, {"type": "photo", "photo": [
            {"file_id": "small", "width": 320, "height": 213}, {"file_id": "big", "width": 1080, "height": 720}]}]}})
    assert cardmap.media_for(sent.rich[0]) == {name: "big"}


def test_an_alert_without_a_polygon_is_drawn_as_its_counties(fresh_db):
    from datetime import timedelta

    from intelnet.feeds import nws_alerts
    from intelnet.models import utcnow

    rows = nws_alerts.parse_alerts({"features": [feature("C1", event="Flood Watch", severity="Moderate")]})
    sc = cardmap.scene(rows[0], rows, None)
    assert sc is not None and sc.counties and sc.rings
    jpeg = cardmap.render(sc, radar=False, now=utcnow() - timedelta(minutes=1))
    assert jpeg[:2] == b"\xff\xd8"


def test_no_pictures_when_turned_off(fresh_db, monkeypatch):
    from intelnet.feeds import nws_alerts

    monkeypatch.setattr(settings, "card_maps", False)
    rows = nws_alerts.parse_alerts({"features": [feature("C2", polygon=BOX)]})
    assert cardmap.prepare(rows[0], rows, None) is None


def test_pictures_go_up_as_multipart_and_a_refused_one_is_dropped_not_the_card(monkeypatch):
    calls: list[tuple[str, dict, dict | None]] = []

    def call(method, payload, files=None):
        calls.append((method, payload, files))
        if files:
            return telegram.Sent(False, permanent=True, error="Bad Request: IMAGE_PROCESS_FAILED")
        return telegram.Sent(True, message_id=5)

    monkeypatch.setattr(telegram.bot, "enabled", True)
    monkeypatch.setattr(telegram.bot, "_call", call)
    rich = '<h4>Warning</h4><figure><img src="tg://photo?id=wm-1"/><figcaption>x</figcaption></figure><p>after</p>'
    assert telegram.bot.deliver("1", "plain", rich=rich, media={"wm-1": b"\x89PNG..."}).ok
    (m1, p1, f1), (m2, p2, f2) = calls
    assert m1 == m2 == "sendRichMessage" and f1 == {"wm-1": ("wm-1.jpg", b"\x89PNG...", "image/jpeg")}
    assert p1["rich_message"]["media"] == [{"id": "wm-1", "media": {"type": "photo", "media": "attach://wm-1"}}]
    assert f2 is None and "tg://photo" not in p2["rich_message"]["html"] and "<p>after</p>" in p2["rich_message"]["html"]


def test_multipart_fields_are_strings(monkeypatch):
    posted = {}

    class Requests:
        @staticmethod
        def post(url, json=None, data=None, files=None, timeout=None):
            posted.update(data=data, files=files)

            class R:
                ok, status_code = True, 200

                @staticmethod
                def json():
                    return {"ok": True, "result": {"message_id": 3}}
            return R()

    monkeypatch.setattr(telegram, "requests", Requests)
    monkeypatch.setattr(telegram.bot, "enabled", True)
    sent = telegram.bot.deliver("1", "plain", rich='<img src="tg://photo?id=a"/>', media={"a": b"png"}, silent=True)
    assert sent.ok and posted["files"]["a"][1] == b"png"
    assert posted["data"]["disable_notification"] == "true"
    assert json.loads(posted["data"]["rich_message"])["media"][0]["media"]["media"] == "attach://a"


def test_outside_says_how_far_and_which_way(fresh_db, sent, monkeypatch, make_sensor):
    make_sensor("tg:1", zip_code="62704", chat_id="1")
    db.set_sensor_location("tg:1", geo.location_from_point(39.78, -89.25, online=False))    # 13 km east of BOX
    make_sensor("tg:2", zip_code="62704", chat_id="2")
    db.set_sensor_location("tg:2", geo.location_from_point(39.78, -89.395, online=False))   # across the edge
    for chat in ("1", "2"):
        db.add_subscription(chat, "weather.warnings", "il.sangamon")
    run_feed(monkeypatch, feature("O1", polygon=BOX))
    cards = dict(zip((c for c, _ in sent), sent.rich, strict=True))
    assert "<b>Your home is outside the warned area</b>, about 8 mi east of it." in cards["1"]
    assert "<b>Your home is just outside the warned area.</b>" in cards["2"]


def test_a_time_on_another_day_carries_the_day(fresh_db):
    """The 30 Sep Flood Watch card said 'until 1:00 AM' for Friday 1:00 AM."""
    from datetime import timedelta

    from intelnet import subscriptions
    from intelnet.models import utcnow

    assert 'format="t"' in subscriptions.clock(utcnow())
    assert 'format="wt"' in subscriptions.clock(utcnow() + timedelta(days=2))


def test_a_county_wide_alert_says_when_your_county_is_in_it(fresh_db, sent, monkeypatch, make_sensor):
    make_sensor("tg:1", zip_code="62704", chat_id="1")          # home: a ZIP in Sangamon
    make_sensor("tg:2", zip_code="60601", chat_id="2")          # home in Cook, subscribed to Sangamon
    for chat in ("1", "2"):
        db.add_subscription(chat, "weather.alerts", "il.sangamon")
    run_feed(monkeypatch, feature("W1", event="Flood Watch", severity="Moderate"))
    cards = dict(zip((c for c, _ in sent), sent.rich, strict=True))
    assert "<p><b>Includes Sangamon County, where your home is.</b></p>" in cards["1"]
    assert "Includes" not in cards["2"] and "<p><b>Sangamon</b></p>" in cards["2"]
    assert "SANGAMON" not in cards["1"]                        # (the name is on the picture, not in the HTML)
