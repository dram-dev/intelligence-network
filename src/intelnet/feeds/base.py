"""ReferenceFeed — fetch → store → assess → fan out, for official sources."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from intelnet import db, network, subscriptions
from intelnet.models import Signal

logger = logging.getLogger(__name__)

FEEDS: dict[str, type["ReferenceFeed"]] = {}


@dataclass
class FeedResult:
    name: str
    fetched: int = 0
    new: int = 0
    events_pushed: int = 0
    alerts_pushed: int = 0
    status: str = "ok"
    error: str | None = None
    skipped: bool = False
    new_signals: list[Signal] = field(default_factory=list)


class ReferenceFeed:
    name: str = "base"
    sensor_kind: str = "official"
    trust: float = 0.95
    topic: str = "weather"
    doc: str = ""

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if cls.name != "base":
            FEEDS[cls.name] = cls

    def fetch_signals(self) -> list[Signal]:  # pragma: no cover - abstract
        raise NotImplementedError

    def should_run(self) -> bool:
        """Cadence gate (station feeds poll less often than alerts)."""
        return True

    def run(self, run_type: str = "watch") -> FeedResult:
        res = FeedResult(name=self.name)
        if not self.should_run():
            res.skipped = True
            return res
        t0 = time.perf_counter()
        try:
            signals = self.fetch_signals()
            res.fetched = len(signals)
            new = db.insert_signals(signals)
            res.new = len(new)
            res.new_signals = new
            self.after_store(new, res)
        except Exception as exc:  # noqa: BLE001 — one bad feed can't stop the watch loop
            res.status, res.error = "error", f"{type(exc).__name__}: {exc}"
            logger.exception("[%s] failed", self.name)
        finally:
            db.log_run(run_type=run_type, source=self.name, items_fetched=res.fetched,
                       items_new=res.new, duration_ms=int((time.perf_counter() - t0) * 1000),
                       status=res.status, error=res.error)
        return res

    def after_store(self, new: list[Signal], res: FeedResult) -> None:
        """Default: assess each new signal, push events that qualify."""
        for sig in new:
            a = network.assess(sig)
            if a.push_event and a.event:
                res.events_pushed += subscriptions.fanout_event(
                    a.event, a.push_reason, sig.location.area_keys()
                )
