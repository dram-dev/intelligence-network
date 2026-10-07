"""The nightly check: real trouble named once in the admin chat, transient trouble left out."""
from __future__ import annotations

from intelnet import db, health
from intelnet.config import settings
from intelnet.ingest.base import IngestedItem


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


def test_a_news_feed_that_went_quiet_is_named(fresh_db):
    db.upsert_items([IngestedItem(source="news", source_id="fw1", title="Harvest permits help cut fuel costs",
                                  url="https://ex.test/fw1", content="", metadata={"feed": "FarmWeekNow"}),
                     IngestedItem(source="news", source_id="ag1", title="Bitter season for sweet corn",
                                  url="https://ex.test/ag1", content="", metadata={"feed": "AgriNews"})])
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET ingested_at = datetime('now', '-30 days') WHERE source_id = 'fw1'")
    assert health.silent_news_feeds() == ["FarmWeekNow"]                # never-heard feeds aren't "silent"


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
