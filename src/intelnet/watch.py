"""The watch — poll reference feeds and push what subscribers asked for.

Two cadences, both from launchd:

* `intelnet alert-loop` (KeepAlive): NWS alerts every ALERT_POLL_SECONDS (30 s),
  plus retries of any push Telegram didn't take. Warnings shouldn't wait behind
  gauges and stations. (The IL feed is ~14 KB, and an unchanged one adds nothing.)
* `intelnet watch` (every 5 min): everything else — storm reports every run,
  station and gauge networks on their own slower gates — then idle events are
  closed and due retries sent. It polls NWS alerts too, but only while the alert
  loop's heartbeat is stale, so alerts never depend on the loop being up.

No LLM, no run lock.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from intelnet import db, delivery, feedback, grids, network, subscriptions
from intelnet.config import settings
from intelnet.feeds import FEEDS
from intelnet.models import iso, parse_iso, utcnow

logger = logging.getLogger(__name__)

# Fast products first (alerts, storm reports), then the gated station networks.
FEED_ORDER = ("nws_alerts", "iem_lsr", "usgs_quake", "iem_asos", "usgs_water",
              "nrcs_scan", "usdm", "ams_grain")
ALERT_LOOP_HEARTBEAT = "alert_loop:heartbeat"
ALERT_LOOP_STALE = timedelta(minutes=3)


def alert_loop_alive() -> bool:
    """True while the alert loop has checked in recently."""
    beat = parse_iso(db.kv_get(ALERT_LOOP_HEARTBEAT))
    return beat is not None and utcnow() - beat < ALERT_LOOP_STALE


def _summary(res: Any) -> dict[str, Any]:
    return {"fetched": res.fetched, "new": res.new, "events_pushed": res.events_pushed,
            "alerts_pushed": res.alerts_pushed, "status": res.status, "error": res.error,
            "skipped": res.skipped}


def run_once(run_type: str = "watch", only: list[str] | None = None) -> dict[str, Any]:
    db.init_db()
    out: dict[str, Any] = {"feeds": {}, "events_closed": 0}
    names = [n for n in FEED_ORDER if n in FEEDS] + [n for n in FEEDS if n not in FEED_ORDER]
    loop_has_alerts = alert_loop_alive()
    for name in names:
        if only and name not in only:
            continue
        if name == "nws_alerts" and not only and loop_has_alerts:
            out["feeds"][name] = {"fetched": 0, "new": 0, "events_pushed": 0, "alerts_pushed": 0,
                                  "status": "ok", "error": None, "skipped": True}
            continue
        out["feeds"][name] = _summary(FEEDS[name]().run(run_type=run_type))
    out["grids"] = grid_pass()
    out["events_closed"] = network.close_stale_events()
    out["retries"] = delivery.retry_due()
    return out


def grid_pass() -> dict[str, Any]:
    """Check people's unsettled readings against radar grids (grids.py); push any event a
    confirmation verified, and tell people what the radar said about their reports."""
    try:
        checked = grids.check_pending()
    except Exception:  # noqa: BLE001 — a grid outage can't stop the watch
        logger.exception("watch: grid pass failed")
        return {"checked": 0, "error": True}
    verified = []
    for c in checked:
        a = c.assessment
        if a is not None and a.push_event and a.event:
            n = subscriptions.fanout_event(a.event, a.push_reason, c.signal.location.area_keys())
            verified.append((a, n))
    feedback.confirmed([(c.signal, c.witness) for c in checked if c.verdict == "agree" and c.witness])
    for a, n in verified:
        if a.push_reason == "new":
            feedback.helped(a.event, n)
    counts: dict[str, Any] = {"checked": len(checked), "events_pushed": sum(n for _a, n in verified)}
    for c in checked:
        counts[c.verdict] = counts.get(c.verdict, 0) + 1
    return counts


def alert_pass(run_type: str = "alerts") -> dict[str, Any]:
    """One turn of the alert loop: poll alerts, retry due pushes, check in."""
    res = FEEDS["nws_alerts"]().run(run_type=run_type)
    retries = delivery.retry_due()
    db.kv_set(ALERT_LOOP_HEARTBEAT, iso(utcnow()) or "")
    return {"nws_alerts": _summary(res), "retries": retries}


def alert_loop(interval_sec: int | None = None) -> None:
    """Poll NWS alerts every `interval_sec` (ALERT_POLL_SECONDS) until stopped."""
    interval = max(10, int(interval_sec or settings.alert_poll_seconds))
    db.init_db()
    logger.info("alert loop: polling NWS alerts every %ss", interval)
    while True:
        started = time.monotonic()
        try:
            out = alert_pass()
            if out["nws_alerts"]["new"] or out["nws_alerts"]["alerts_pushed"] or out["retries"]["due"]:
                logger.info("alert loop: %s", out)
        except Exception:  # noqa: BLE001
            logger.exception("alert loop: pass failed")
        time.sleep(max(1.0, interval - (time.monotonic() - started)))


def loop(interval_sec: int = 300) -> None:  # pragma: no cover - dev convenience
    while True:
        try:
            summary = run_once()
            logger.info("watch: %s", summary)
        except Exception:  # noqa: BLE001
            logger.exception("watch: run failed")
        time.sleep(interval_sec)
