"""Telegram bot routing — every command through handle_message."""
from __future__ import annotations

from intelnet import bot, db


def msg(text: str | None = None, uid: int = 42, chat: int | None = None, mid: int = 1, **extra) -> dict:
    m = {"message_id": mid, "date": 1789531200, "chat": {"id": chat or uid, "type": "private"},
         "from": {"id": uid, "first_name": "Cy", "last_name": "Q", "username": "cyq"}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return m


def test_help_join_home_me(fresh_db):
    assert "Intelligence Network" in bot.handle_message(msg("/start"))
    r = bot.handle_message(msg("/join"))
    assert "Welcome, Cy Q" in r and db.get_sensor("tg:42") is not None
    assert "already a sensor" in bot.handle_message(msg("/join"))
    r = bot.handle_message(msg("/home 60601-2001"))
    assert "60601-2001, Cook County" in r
    assert db.get_sensor("tg:42").location.zip9 == "60601-2001"
    assert "Couldn't place" in bot.handle_message(msg("/home atlantis"))
    r = bot.handle_message(msg("/me"))
    assert "tg:42" in r and "Cook County" in r


def test_join_code_gate(fresh_db, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "network_join_code", "secret")
    assert "join code" in bot.handle_message(msg("/join"))
    assert "Join first" in bot.handle_message(msg("rain 1in"))
    assert "Welcome" in bot.handle_message(msg("/join secret"))


def test_plain_text_auto_joins_and_records(fresh_db):
    r = bot.handle_message(msg("rain 1.2in @62704"))
    assert "Auto-joined" in r and "Recorded 1 reading" in r
    assert db.get_sensor("tg:42").n_signals == 1
    r = bot.handle_message(msg("gust 40", mid=2))          # no home, no @ → told how to fix
    assert "no location" in r and "/home" in r


def test_shared_location_sets_home(fresh_db):
    bot.handle_message(msg("/join"))
    r = bot.handle_message(msg(location={"latitude": 39.7817, "longitude": -89.6501}))
    assert "Sangamon County" in r
    assert db.get_sensor("tg:42").location.county_fips == "17167"


def test_photo_caption_is_a_reading_with_evidence(fresh_db):
    bot.handle_message(msg("/join"))
    bot.handle_message(msg("/home 62704"))
    r = bot.handle_message(msg(caption="hail quarter", photo=[{"file_id": "abc"}], mid=3))
    assert "Recorded 1 reading" in r
    sig = db.recent_signals(1, kinds=("human",))[0]
    assert sig.evidence["photo_file_id"] == "abc" and sig.metric == "hail_mm"


def test_json_signal_command(fresh_db):
    bot.handle_message(msg("/join"))
    bot.handle_message(msg("/home 62704"))
    r = bot.handle_message(msg('/signal {"metric":"rain_mm","value":0.5,"unit":"in"}'))
    assert "Rainfall: 0.5 in" in r


def test_subscribe_flow(fresh_db):
    bot.handle_message(msg("/join"))
    bot.handle_message(msg("/home 62704-1234"))
    assert "weather.warnings</b> @ Sangamon County" in bot.handle_message(msg("/subscribe warnings"))
    assert "@ 60601" in bot.handle_message(msg("/subscribe events 60601"))
    assert "@ IL" in bot.handle_message(msg("/subscribe digest cook"))     # digest is state-wide
    assert "Already subscribed" in bot.handle_message(msg("/subscribe warnings"))
    subs = bot.handle_message(msg("/subs"))
    assert "weather.warnings" in subs and "weather.events" in subs and "weather.digest" in subs
    assert "Removed 1" in bot.handle_message(msg("/unsubscribe events 60601"))
    assert "Removed 2" in bot.handle_message(msg("/unsubscribe all"))
    assert "No subscriptions" in bot.handle_message(msg("/subs"))
    assert "Unknown category" in bot.handle_message(msg("/subscribe nope"))
    r = bot.handle_message(msg("/subscribe *.events 62704"))
    assert r.count("Subscribed") == 5 and "soil.events" in r and "air.events" in r
    assert "@ Sangamon County" in bot.handle_message(msg("/subscribe soil.reports"))
    assert "Removed 5" in bot.handle_message(msg("/unsubscribe *.events 62704"))
    assert "Topics" in bot.handle_message(msg("/topics")) and "corn_stage" in bot.handle_message(msg("/topics agriculture"))


def test_near_alerts_latest_network(fresh_db):
    bot.handle_message(msg("/join"))
    assert "Where?" in bot.handle_message(msg("/near"))
    bot.handle_message(msg("/home 62704"))
    bot.handle_message(msg("rain 1in", mid=5))
    r = bot.handle_message(msg("/near 6h"))
    assert "Rainfall" in r and "last 6h" in r
    assert "No active NWS alerts for Sangamon County" in bot.handle_message(msg("/alerts"))
    assert "No active NWS alerts for Cook County" in bot.handle_message(msg("/alerts cook"))
    from intelnet.config import settings

    assert "offline" in bot.handle_message(msg("/latest"))              # Drive off (tests) → no dead links
    settings.gdrive_enabled = True
    try:
        assert "No digest published yet" in bot.handle_message(msg("/latest"))
        db.record_digest("2026-09-15", drive_file_id="d", drive_url="https://docs.google.com/document/d/d/edit",
                         latest_url="https://docs.google.com/document/d/l/edit",
                         folder_url="https://drive.google.com/drive/folders/f", n_events=0, n_signals=0, n_sensors=0)
        r = bot.handle_message(msg("/latest"))
    finally:
        settings.gdrive_enabled = False
    assert "2026-09-15" in r and "docs.google.com/document/d/d" in r
    assert "Sensors: 1 joined" in bot.handle_message(msg("/network"))


def test_digest_email_records_subscriber_even_without_drive(fresh_db):
    assert "Usage" in bot.handle_message(msg("/digest nope"))
    r = bot.handle_message(msg("/digest someone@example.com"))
    assert "someone@example.com" in r and db.email_subscribers() == ["someone@example.com"]


def test_admin_commands_are_gated(fresh_db):
    bot.handle_message(msg("/join"))
    assert bot.handle_message(msg("/admin stats")) == "Admin only."
    admin = msg("/admin ban tg:42", uid=999)
    assert "✅ ban tg:42" in bot.handle_message(admin)
    assert db.get_sensor("tg:42").status == "banned"
    assert bot.handle_message(msg("rain 1in")) is None            # banned sensors are ignored
    assert "✅ unban" in bot.handle_message(msg("/admin unban tg:42", uid=999))
    assert "trust tg:42 = 0.9" in bot.handle_message(msg("/admin trust tg:42 0.9", uid=999))
    assert db.get_sensor("tg:42").trust == 0.9
    assert "Sensors" in bot.handle_message(msg("/admin sensors", uid=999))


def test_unknown_command_and_empty(fresh_db):
    assert "Unknown command /bogus" in bot.handle_message(msg("/bogus"))
    assert bot.handle_message(msg()) is None
    assert bot.handle_message({"chat": {}, "from": {}}) is None


def test_privacy_and_forget(fresh_db, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "network_contact_email", "network@example.org")
    r = bot.handle_message(msg("/privacy"))
    assert "privacy.html" in r and "terms.html" in r and "network@example.org" in r and "/forget confirm" in r
    bot.handle_message(msg("/join"))
    bot.handle_message(msg("/home 62704"))
    bot.handle_message(msg("rain 1in", mid=7))
    bot.handle_message(msg("/subscribe warnings"))
    bot.handle_message(msg("/digest me@example.org"))
    assert "/forget confirm" in bot.handle_message(msg("/forget"))
    assert db.get_sensor("tg:42") is not None                           # nothing deleted without confirm
    r = bot.handle_message(msg("/forget confirm"))
    assert "Deleted 1 reading(s), 1 subscription(s), 1 e-mail address(es)" in r
    assert db.get_sensor("tg:42") is None and db.recent_signals(1) == [] and db.email_subscribers() == []
    assert db.subscriptions_for(42) == []
    assert "nothing stored" in bot.handle_message(msg("/forget confirm"))


def test_suspended_sensor_can_still_forget(fresh_db):
    bot.handle_message(msg("/join"))
    db.set_sensor_status("tg:42", "banned")
    assert bot.handle_message(msg("rain 1in")) is None
    assert "Deleted" in bot.handle_message(msg("/forget confirm"))
