"""The daily run — under the cross-digest lock, ending in a Google Doc.

    reference sweep → news ingest → triage (LLM) → housekeeping →
    build digest → narrative (LLM) → publish to Drive → record

Holds `digest_core.runlock.pipeline_serialize`, so it queues behind the PC
and macro digests on the shared Ollama/MLX servers (launchd fires it at
01:10, after macro's 01:00 and PC's 01:05). The Telegram digest ping is a
separate 08:00 job (`notify`) because 01:00 sits inside quiet hours.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from digest_core.cli.base import discover_ingestors, run_ingest
from digest_core.runlock import PipelineLockTimeout, pipeline_serialize

from intelnet import db, digest, llm, network, subscriptions, watch
from intelnet.config import settings

logger = logging.getLogger(__name__)

LOCK_HOLDER = "intelligence-network"


class _Quiet:
    """Console stand-in when the CLI didn't pass one."""

    def rule(self, *_a: Any, **_k: Any) -> None: ...
    def print(self, *_a: Any, **_k: Any) -> None: ...


def triage_news(hours: int, console: Any) -> dict[str, int]:
    counts = {"pending": 0, "kept": 0, "dropped": 0, "untriaged": 0}
    rows = db.items_needing_triage(hours)
    counts["pending"] = len(rows)
    for row in rows:
        verdict = llm.triage_item(dict(row))
        if verdict is None:
            # No LLM: keep it, unranked — the reading list caps at 15 by recency.
            db.update_triage(row["id"], "keep", None, "weather", "untriaged (LLM unavailable)")
            counts["untriaged"] += 1
            continue
        db.update_triage(row["id"], verdict["decision"], verdict["relevance"], verdict["topic"],
                         verdict["reason"])
        counts["kept" if verdict["decision"] == "keep" else "dropped"] += 1
    console.print(f"  triage: {counts}")
    return counts


def _run(run_type: str, skip_publish: bool, console: Any) -> dict[str, Any]:
    t0 = time.perf_counter()
    db.init_db()
    summary: dict[str, Any] = {"run_type": run_type}
    hours = db.lookback_hours(24)
    summary["window_hours"] = hours

    console.rule("[bold cyan]stage 1: reference sweep")
    summary["watch"] = watch.run_once(run_type=run_type)
    console.print(f"  feeds: { {k: v['new'] for k, v in summary['watch']['feeds'].items()} }")

    console.rule("[bold cyan]stage 2: news")
    if settings.news_enabled:
        ingestors = discover_ingestors("intelnet.ingest")
        fetched, new = run_ingest(ingestors, list(ingestors), run_type, console, per_source_rule=False)
        summary["news"] = {"fetched": fetched, "new": new}
        summary["triage"] = triage_news(hours, console)
    else:
        console.print("  news disabled")

    console.rule("[bold cyan]stage 3: housekeeping")
    summary["events_closed"] = network.close_stale_events()
    summary["pruned"] = db.prune_reference_signals(settings.reference_retention_days)
    console.print(f"  closed {summary['events_closed']} idle event(s), pruned {summary['pruned']} old reference rows")

    console.rule("[bold cyan]stage 4: digest")
    model = digest.build(hours=max(24.0, float(hours)))
    model.narrative = llm.narrative(model.to_json())
    console.print(f"  {model.headline}" + ("" if model.narrative else "  (no narrative)"))
    html = digest.render_html(model)
    summary["digest"] = {"date": model.date, "events": len(model.events), "alerts": len(model.alerts)}

    links: dict[str, str | None] = {"doc_id": None, "doc_url": None, "latest_url": None, "folder_url": None}
    if skip_publish:
        console.print("  publish skipped")
    elif not settings.gdrive_enabled:
        console.print("  Google Drive disabled (GDRIVE_ENABLED=false)")
    else:
        from intelnet.gdrive import DriveNotConfigured, publisher

        try:
            links = publisher.publish(model.date, html)
            publisher.sync_readers()
            console.print(f"  published → {links['doc_url']}")
        except DriveNotConfigured as exc:
            # Not configured is a setup state, not a failed run: the digest is
            # recorded locally and the run stays green until Drive is authorized.
            console.print(f"  [yellow]⚠[/yellow] not published: {exc}")
            summary["publish_skipped"] = str(exc)
            _warn_admin_drive(str(exc), model.date)
        except Exception as exc:  # noqa: BLE001
            logger.exception("pipeline: Drive publish failed")
            console.print(f"  [red]✗[/red] publish failed: {exc}")
            summary["publish_error"] = f"{type(exc).__name__}: {exc}"
    db.record_digest(
        model.date, drive_file_id=links.get("doc_id"), drive_url=links.get("doc_url"),
        latest_url=links.get("latest_url"), folder_url=links.get("folder_url"),
        n_events=len(model.events), n_signals=int(model.vitals.get("signals_24h_human", 0)),
        n_sensors=int(model.vitals.get("sensors_active_24h", 0)),
    )
    summary["links"] = links
    summary["headline"] = model.headline

    console.rule("[bold cyan]stage 5: site snapshot")
    try:
        from intelnet import export

        exported = export.export_all(days=14)
        summary["export"] = {"json": len(exported["json"]), "site": bool(exported.get("site"))}
        console.print(f"  wrote {len(exported['json'])} JSON files" + (" + docs/index.html" if exported.get("site") else ""))
        if settings.site_auto_push:
            pushed = export.git_push_docs()
            summary["export"]["pushed"] = pushed
            console.print("  pushed docs/ to GitHub" if pushed else "  nothing to push")
    except Exception as exc:  # noqa: BLE001 — the site is best-effort
        logger.exception("pipeline: export failed")
        console.print(f"  [yellow]⚠[/yellow] export skipped: {exc}")
        summary["export_error"] = str(exc)
    db.log_run(run_type=run_type, source="pipeline", items_fetched=0,
               items_new=int(model.vitals.get("signals_24h_human", 0)),
               duration_ms=int((time.perf_counter() - t0) * 1000),
               status="ok" if "publish_error" not in summary else "error",
               error=summary.get("publish_error"))
    return summary


def _warn_admin_drive(reason: str, date: str) -> None:
    """DM the admin (once a day) when Drive needs re-authorizing.

    While the Google app is in "Testing", its login expires every 7 days; without
    this the digest would quietly stop reaching Drive. Silent when Drive was never
    set up (no OAuth client file) — that's a setup state, not a lapse.
    """
    from pathlib import Path

    from intelnet.telegram import bot, esc

    chat = settings.telegram_admin_chat_id
    if not chat or not Path(settings.gdrive_credentials_path).exists():
        return
    key = f"drive-auth:{date}"
    if db.already_notified(key, chat):
        return
    text = ("⚠️ <b>Today's digest wasn't uploaded to Google Drive</b>\n"
            f"{esc(reason)}\n"
            "On the Mac mini: <code>cd ~/Projects/intelligence-network &amp;&amp; uv run intelnet drive init</code>\n"
            "From your phone: run <code>uv run intelnet drive init --remote</code>, open the link, then "
            "<code>drive init --code '&lt;URL&gt;'</code> with the address Google sends you to.")
    if bot.send_to(chat, text):
        db.record_notification(key, chat)


def run(run_type: str = "daily", skip_publish: bool = False, console: Any = None,
        on_wait: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run the pipeline under the cross-digest lock. Raises PipelineLockTimeout."""
    console = console or _Quiet()
    with pipeline_serialize(LOCK_HOLDER, on_wait=on_wait) as waited:
        if waited:
            console.print(f"  [dim]pipeline lock acquired after {waited / 60:.1f} min[/dim]")
        return _run(run_type, skip_publish, console)


def notify_digest(force: bool = False) -> dict[str, Any]:
    """Telegram ping for the latest digest (quiet-hours aware unless forced)."""
    if not settings.gdrive_enabled:
        return {"sent": 0, "reason": "Drive publishing is off (GDRIVE_ENABLED=false)"}
    row = db.latest_digest()
    if row is None:
        return {"sent": 0, "reason": "no digest"}
    if not (row["drive_url"] or row["latest_url"]):
        # A digest with nothing to open (Drive not authorized yet) — a ping
        # without a link would just be noise.
        return {"sent": 0, "reason": "no link (Drive not configured)", "date": row["date"]}
    if not force and not subscriptions_allowed_now():
        return {"sent": 0, "reason": "quiet hours", "date": row["date"]}
    sent = subscriptions.fanout_digest(row["date"], row["drive_url"] or row["latest_url"],
                                       row["folder_url"])
    return {"sent": sent, "date": row["date"]}


def subscriptions_allowed_now() -> bool:
    from datetime import datetime

    h = datetime.now().hour
    start, end = settings.notify_quiet_start_hour, settings.notify_quiet_end_hour
    if start == end:
        return True
    if end < start:
        return end <= h < start
    return h >= end or h < start


__all__ = ["run", "notify_digest", "PipelineLockTimeout", "LOCK_HOLDER"]
