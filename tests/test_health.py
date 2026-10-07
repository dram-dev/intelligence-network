"""The nightly check: real trouble named once in the admin chat, transient trouble left out."""
from __future__ import annotations

from intelnet import db, health
from intelnet.config import settings


def _runs(source: str, status: str, n: int, hours_ago: float = 1, error: str | None = None) -> None:
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO run_log (run_at, run_type, source, status, error) VALUES (datetime('now', ?), 'watch', ?, ?, ?)",
            [(f"-{hours_ago} hours", source, status, error)] * n)


def test_a_feed_failing_most_runs_is_named_and_a_blip_is_not(fresh_db):
    _runs("usgs_water", "error", 30, error="HTTPError: 503 Server Error:  for url: https://waterservices.usgs.gov/nwis/iv/?x")
    _runs("usgs_water", "ok", 18, hours_ago=0.5)
    _runs("nws_alerts", "error", 174, error="SSLError: HTTPSConnectionPool(host='api.weather.gov', port=443)")
    _runs("nws_alerts", "ok", 2295, hours_ago=0.05)
    assert health.feed_problems() == ["usgs_water: 30 of 48 runs failed in the last day (HTTPError: 503 Server Error)"]


def test_a_feed_quiet_past_its_cadence_is_stale_and_one_never_run_is_not(fresh_db):
    _runs("iem_asos", "ok", 1, hours_ago=5)              # hourly feed: 5 hours quiet
    _runs("cocorahs", "ok", 1, hours_ago=10)             # overnight gap: fine
    _runs("usdm", "ok", 1, hours_ago=24 * 3)             # weekly data: fine
    assert health.feed_problems() == ["iem_asos: no good run for 5 hours"]          # ams_grain: never run, unnamed


def test_a_feed_that_recovered_from_a_blip_is_not_named(fresh_db):
    _runs("usdm", "error", 6, hours_ago=20, error="ConnectionError: HTTPSConnectionPool(host='x')")
    _runs("usdm", "ok", 1, hours_ago=19)                 # 6 of 7 failed, then it came back
    assert health.feed_problems() == []
    _runs("usdm", "error", 1, hours_ago=0.1)             # failing again now: named
    assert health.feed_problems()[0].startswith("usdm: 7 of 8 runs failed")


def test_a_site_feed_empty_three_nights_is_named_and_a_quiet_agency_is_not(fresh_db, monkeypatch):
    from intelnet.ingest import news

    blocked = {"name": "Some Blog", "url": "https://blog.test/feed"}
    search = {"name": "Google News · Illinois blizzard", "url": "https://news.google.com/rss/search?q=x"}
    monkeypatch.setattr(news, "feed_list", lambda: [blocked, search])
    for _ in range(3):
        news.note_empty(blocked, empty=True)
        news.note_empty(search, empty=True)              # a search can rightly come back empty
    assert health.broken_news_feeds() == ["Some Blog"]
    news.note_empty(blocked, empty=False)
    assert health.broken_news_feeds() == []


def test_the_night_is_reported_once_and_only_when_something_is_wrong(fresh_db, sent, monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    assert health.nightly({}, narrative=True, date="2026-10-07") == [] and sent == []
    found = health.nightly({"export": {"pushed": "failed"}}, narrative=False, date="2026-10-07")
    assert found == ["The digest went out without its narrative: both language models failed or ran out of "
                     "time (logs/daily.err.log).", "The site wasn't pushed to GitHub: git push failed."]
    [(chat, text)] = sent
    assert chat == "999" and text.startswith("🩺 <b>Nightly check · 2026-10-07</b>\n• The digest went out")
    health.nightly({"export": {"pushed": "failed"}}, narrative=False, date="2026-10-07")
    assert len(sent) == 1                                                # once a date
