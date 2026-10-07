"""The nightly check: what went wrong in the last day, sent to the admin chat once, and only
when there is something to say.

A digest without its narrative, a feed failing most of its runs or gone quiet, the site not
pushed, a news feed empty night after night: each surfaces the next morning instead of a week later
(in the week to 6 Oct the narrative timed out twice and CI failed for six days, unseen).
Transient trouble stays out: a feed is named when half or more of its runs failed (at least
MIN_FAILS) and it's still failing or was flaky all day (CHRONIC_RUNS), or when it hasn't had a
good run in longer than its cadence allows (USDM failing through a morning network blip and then
recovering isn't news); a news feed, when its site came back empty three nights running.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from intelnet import db
from intelnet.config import settings
from intelnet.models import parse_iso, utcnow

logger = logging.getLogger(__name__)

# Hours without a good run before a feed counts as stale; the slow ones report daily or weekly,
# and CoCoRaHS only from 7 AM to 8 PM.
STALE_HOURS = {"nws_alerts": 1, "iem_lsr": 1, "usgs_quake": 2, "iem_asos": 3, "airnow": 4, "usgs_water": 3,
               "cocorahs": 26, "ams_grain": 96, "nrcs_scan": 192, "usdm": 192}
FAIL_SHARE = 0.5
MIN_FAILS = 4
CHRONIC_RUNS = 12              # this many runs in a day, half failing: flaky even if the last one worked


def _short(error: str | None) -> str:
    """The gist of an error, URLs dropped: 'HTTPError: 503 Server Error'."""
    text = re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", str(error or "")))
    return re.split(r"\s*\(|:?\s+for url", text)[0].strip(" :")[:70]


def _age(delta: timedelta) -> str:
    hours = round(delta.total_seconds() / 3600)
    return f"{hours} hour{'s' * (hours != 1)}" if hours < 48 else f"{round(hours / 24)} days"


def feed_problems(now: datetime | None = None) -> list[str]:
    """Feeds that failed most of their runs in the last day, or haven't had a good run lately."""
    from intelnet.feeds import FEEDS

    now = now or utcnow()
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT source, SUM(status = 'error') AS bad, COUNT(*) AS n,
                      MAX(CASE WHEN status = 'error' THEN error END) AS err,
                      (SELECT status FROM run_log r2 WHERE r2.source = run_log.source ORDER BY id DESC LIMIT 1) AS last
               FROM run_log WHERE run_at >= datetime('now', '-1 day') GROUP BY source""").fetchall()
    out = []
    for r in rows:
        # failing now, or flaky all day; a feed that failed through a blip and recovered isn't named
        if (r["source"] in FEEDS and r["bad"] >= MIN_FAILS and r["bad"] / r["n"] >= FAIL_SHARE
                and (r["last"] == "error" or r["n"] >= CHRONIC_RUNS)):
            out.append(f"{r['source']}: {r['bad']} of {r['n']} runs failed in the last day ({_short(r['err'])})")
    fresh = db.feed_freshness()
    for name, hours in STALE_HOURS.items():
        if name not in FEEDS:
            continue
        last = parse_iso(fresh.get(name))           # none on record: new, or switched off (no key)
        if last is not None and now - last > timedelta(hours=hours):
            out.append(f"{name}: no good run for {_age(now - last)}")
    return out


def broken_news_feeds() -> list[str]:
    """Site feeds that came back empty three nights running (blocked, moved or down); an agency
    that posts rarely still lists its old items, so it isn't named."""
    from intelnet.ingest.news import empty_feeds

    return empty_feeds()


def problems(summary: dict[str, Any], *, narrative: bool, now: datetime | None = None) -> list[str]:
    """Everything worth the admin's attention after a nightly run."""
    out = []
    if not narrative and settings.llm_enabled:              # off on purpose (LLM_ENABLED=false) is no fault
        out.append("The digest went out without its narrative: both language models failed or ran out "
                   "of time (logs/daily.err.log).")
    if summary.get("publish_error"):
        out.append(f"Drive publish failed: {_short(summary['publish_error'])}")
    if summary.get("export_error"):
        out.append(f"The site wasn't exported: {_short(summary['export_error'])}")
    elif (summary.get("export") or {}).get("pushed") == "failed":
        out.append("The site wasn't pushed to GitHub: git push failed.")
    out += feed_problems(now)
    broken = broken_news_feeds()
    if broken:
        out.append("News feeds empty three nights running: " + ", ".join(broken))
    return out


def nightly(summary: dict[str, Any], *, narrative: bool, date: str) -> list[str]:
    """Send the night's problems to the admin chat, once a date. Returns them."""
    from intelnet.telegram import bot, esc

    found = problems(summary, narrative=narrative)
    chat = settings.telegram_admin_chat_id
    key = f"health:{date}"
    if not found or not chat or db.already_notified(key, chat):
        return found
    text = f"🩺 <b>Nightly check · {esc(date)}</b>\n" + "\n".join(f"• {esc(p)}" for p in found)
    if bot.send_to(chat, text):
        db.record_notification(key, chat)
    return found
