"""The watch — poll reference feeds and push what subscribers asked for.

Runs every few minutes from launchd (`intelnet watch`, no LLM, no lock
needed). Alerts and storm reports every run; station observations on their
own slower cadence (the feed gates itself). Then idle events are closed.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from intelnet import db, network
from intelnet.feeds import FEEDS

logger = logging.getLogger(__name__)

# Fast products first (alerts, storm reports), then the gated station networks.
FEED_ORDER = ("nws_alerts", "iem_lsr", "iem_asos", "usgs_water", "nrcs_scan", "usdm")


def run_once(run_type: str = "watch", only: list[str] | None = None) -> dict[str, Any]:
    db.init_db()
    out: dict[str, Any] = {"feeds": {}, "events_closed": 0}
    names = [n for n in FEED_ORDER if n in FEEDS] + [n for n in FEEDS if n not in FEED_ORDER]
    for name in names:
        if only and name not in only:
            continue
        res = FEEDS[name]().run(run_type=run_type)
        out["feeds"][name] = {
            "fetched": res.fetched, "new": res.new, "events_pushed": res.events_pushed,
            "alerts_pushed": res.alerts_pushed, "status": res.status, "error": res.error,
            "skipped": res.skipped,
        }
    out["events_closed"] = network.close_stale_events()
    return out


def loop(interval_sec: int = 300) -> None:  # pragma: no cover - dev convenience
    while True:
        try:
            summary = run_once()
            logger.info("watch: %s", summary)
        except Exception:  # noqa: BLE001
            logger.exception("watch: run failed")
        time.sleep(interval_sec)
