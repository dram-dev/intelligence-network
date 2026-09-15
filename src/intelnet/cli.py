"""Click CLI — `intelnet <command>`."""
from __future__ import annotations

import json
import logging
import subprocess
import sys

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from intelnet import db
from intelnet.config import settings

console = Console()

LAUNCHD_LABELS = ("com.dr.intelnet.bot", "com.dr.intelnet.watch", "com.dr.intelnet.daily",
                  "com.dr.intelnet.notify")


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


@click.group()
def main() -> None:
    """Intelligence Network — a decentralized sensor network with a daily digest."""
    _setup_logging()


@main.command(name="init-db")
def init_db_cmd() -> None:
    """Create / migrate the SQLite state."""
    db.init_db()
    console.print(f"[green]✓[/green] DB ready at {settings.db_path}")


@main.command()
def bot() -> None:
    """Run the Telegram listener (long-poll; launchd KeepAlive job)."""
    from intelnet.bot import run_listener

    try:
        run_listener()
    except RuntimeError as exc:
        console.print(f"[yellow]⚠[/yellow] {escape(str(exc))}")
        return


@main.command()
@click.option("--loop", is_flag=True, help="Keep polling every --interval seconds (dev).")
@click.option("--interval", default=300, show_default=True)
@click.option("--only", multiple=True, help="Run only these feeds (nws_alerts, iem_lsr, iem_asos).")
def watch(loop: bool, interval: int, only: tuple[str, ...]) -> None:
    """Poll reference feeds once (launchd every 5 min) and push to subscribers."""
    from intelnet import watch as _watch

    if loop:
        _watch.loop(interval)
        return
    out = _watch.run_once(only=list(only) or None)
    for name, r in out["feeds"].items():
        glyph = "[dim]–[/dim]" if r["skipped"] else ("[green]✓[/green]" if r["status"] == "ok" else "[red]✗[/red]")
        detail = "skipped (cadence)" if r["skipped"] else (
            f"fetched={r['fetched']} new={r['new']} alerts_pushed={r['alerts_pushed']} "
            f"events_pushed={r['events_pushed']}" + (f" error={r['error']}" if r["error"] else ""))
        console.print(f"{glyph} {name}: {escape(detail)}")
    console.print(f"[dim]events closed: {out['events_closed']}[/dim]")


@main.command()
@click.option("--run-type", default="manual", show_default=True, help="daily/manual (daily anchors the window).")
@click.option("--skip-publish", is_flag=True, help="Build the digest but don't upload it to Drive.")
def pipeline(run_type: str, skip_publish: bool) -> None:
    """Full daily run under the cross-digest lock: sweep → news → digest → Drive."""
    from intelnet import pipeline as _pipeline

    def _waiting(holder: str) -> None:
        console.print("[yellow]⏳ waiting for the other digest run to finish[/yellow] "
                      f"[dim]({escape(holder or 'holder unknown')})[/dim]")

    try:
        summary = _pipeline.run(run_type, skip_publish, console=console, on_wait=_waiting)
    except _pipeline.PipelineLockTimeout as exc:
        console.print(f"[red]✗[/red] {escape(str(exc))}")
        raise SystemExit(1) from exc
    console.rule("[bold]done")
    console.print(escape(json.dumps({k: v for k, v in summary.items() if k != "watch"}, default=str)))
    if summary.get("publish_error"):
        raise SystemExit(1)


@main.command()
@click.option("--force", is_flag=True, help="Ignore quiet hours.")
def notify(force: bool) -> None:
    """Telegram ping for the latest digest to `digest` subscribers (08:00 job)."""
    from intelnet.pipeline import notify_digest

    out = notify_digest(force=force)
    console.print(escape(json.dumps(out)))


@main.command()
@click.option("--hours", default=24.0, show_default=True)
@click.option("--html", "html_out", type=click.Path(), help="Write the HTML to this file (preview).")
@click.option("--narrative/--no-narrative", default=False, help="Ask the LLM for the opening.")
def digest(hours: float, html_out: str | None, narrative: bool) -> None:
    """Build and print the digest (no publish)."""
    from intelnet import digest as _digest, llm

    db.init_db()
    model = _digest.build(hours=hours)
    if narrative:
        model.narrative = llm.narrative(model.to_json())
    console.print(_digest.render_text(model))
    if html_out:
        with open(html_out, "w", encoding="utf-8") as f:
            f.write(_digest.render_html(model))
        console.print(f"[green]✓[/green] wrote {html_out}")


@main.group()
def drive() -> None:
    """Google Drive: where the digest lives."""


@drive.command(name="init")
def drive_init() -> None:
    """Authorize (browser on first run), create the folder + Latest doc."""
    from intelnet.gdrive import DriveNotConfigured, publisher

    db.init_db()
    try:
        fid = publisher.ensure_folder()
        lid = publisher.ensure_latest_doc(fid)
    except DriveNotConfigured as exc:
        console.print(f"[red]✗[/red] {escape(str(exc))}")
        raise SystemExit(1) from exc
    console.print(f"[green]✓[/green] folder: {publisher.folder_url(fid)}")
    console.print(f"[green]✓[/green] latest: {publisher.doc_url(lid)}")


@drive.command(name="share")
@click.argument("email")
def drive_share(email: str) -> None:
    """Share the digest folder with an e-mail address (subscribe them)."""
    from intelnet.gdrive import publisher

    db.init_db()
    db.add_email_subscriber(email, None)
    ok = publisher.add_reader(email)
    console.print(("[green]✓[/green] shared with " if ok else "[yellow]⚠[/yellow] recorded, not shared: ") + email)


@drive.command(name="status")
def drive_status() -> None:
    from intelnet.gdrive import publisher

    db.init_db()
    console.print(escape(json.dumps(publisher.status(), indent=2, default=str)))


@main.command()
def sources() -> None:
    """List reference feeds + news ingestors with their last run."""
    from digest_core.catalog import print_sources

    from intelnet.feeds import FEEDS

    db.init_db()
    table = Table(title="Reference feeds", title_style="bold")
    table.add_column("Feed", style="cyan")
    table.add_column("Sensor kind")
    table.add_column("Trust", justify="right")
    table.add_column("Last run", style="dim")
    table.add_column("What it pulls", style="dim")
    with db.get_conn() as conn:
        last = {r["source"]: r for r in conn.execute(
            "SELECT source, run_at, status, items_new FROM run_log WHERE id IN "
            "(SELECT MAX(id) FROM run_log GROUP BY source)").fetchall()}
    for name, cls in FEEDS.items():
        r = last.get(name)
        table.add_row(name, cls.sensor_kind, f"{cls.trust:.2f}",
                      f"{r['run_at']} ({r['status']}, +{r['items_new']})" if r else "never", cls.doc)
    console.print(table)
    print_sources(db.get_conn, "intelnet.ingest", console=console)


@main.command()
def topics() -> None:
    """Show the topic packs (the common data language)."""
    from intelnet import language
    from intelnet.topics import topics as _topics

    for t in _topics().values():
        console.rule(f"[bold]{t.label} ({t.name})")
        console.print(escape(t.description))
        console.print("[bold]categories[/bold]: " + ", ".join(t.category_keys))
        console.print(escape(language.describe_metrics(t)))


@main.command()
@click.argument("text")
@click.option("--sensor", "sensor_id", default="cli:local", show_default=True)
@click.option("--home", default=None, help="Location for this sensor if not set (ZIP/ZIP+4/county).")
@click.option("--json", "json_mode", is_flag=True, help="TEXT is the JSON form.")
def signal(text: str, sensor_id: str, home: str | None, json_mode: bool) -> None:
    """Contribute a reading from the terminal: intelnet signal "rain 1.2in @62704"."""
    from intelnet import contrib, geo
    from intelnet.models import Sensor

    db.init_db()
    s = db.get_sensor(sensor_id)
    if s is None:
        s = db.upsert_sensor(Sensor(id=sensor_id, kind="human", name=sensor_id))
    if home:
        loc = geo.parse_location(home)
        if loc is None:
            console.print(f"[red]✗[/red] unknown location {home!r}")
            raise SystemExit(1)
        db.set_sensor_location(s.id, loc)
        s = db.get_sensor(sensor_id) or s
    import uuid

    fn = contrib.contribute_json if json_mode else contrib.contribute
    c = fn(s, text, source="cli", source_id_base=f"cli:{uuid.uuid4().hex[:10]}")
    console.print(contrib.ack_text(c, s), markup=False, highlight=False)


@main.command()
@click.argument("location")
@click.option("--hours", default=3.0, show_default=True)
def near(location: str, hours: float) -> None:
    """What the network sees around a place."""
    from intelnet import bot as _bot, geo

    db.init_db()
    loc = geo.parse_location(location)
    if loc is None:
        console.print(f"[red]✗[/red] unknown location {location!r}")
        raise SystemExit(1)
    msg = {"chat": {"id": 0}, "from": {"id": 0}}
    console.print(_bot.cmd_near(msg, None, f"{location} {hours:g}h"), markup=False)


@main.command()
@click.option("--kind", default=None, help="human | bot | station | official | authority")
def sensors(kind: str | None) -> None:
    """List sensors (people, stations, official feeds)."""
    db.init_db()
    table = Table(title="Sensors", title_style="bold")
    for col in ("id", "kind", "name", "home", "trust", "signals", "corr", "conflict", "status", "last seen"):
        table.add_column(col)
    for s in db.list_sensors(kind=kind, limit=200):
        table.add_row(s.id, s.kind, s.name, s.location.describe(), f"{s.trust:.2f}", str(s.n_signals),
                      str(s.n_corroborated), str(s.n_contradicted), s.status, s.last_seen_at or "")
    console.print(table)


@main.command()
@click.option("--hours", default=48.0, show_default=True)
def events(hours: float) -> None:
    """Events opened/updated in the window, ranked by score."""
    from intelnet.network import event_summary

    db.init_db()
    table = Table(title="Events", title_style="bold")
    for col in ("score", "title", "peak", "sensors", "official", "verified", "status", "updated"):
        table.add_column(col)
    for e in db.events_since(hours):
        s = event_summary(e)
        table.add_row(f"{s.get('score') or 0:.2f}", s["title"] or "", s["peak_display"], str(s.get("n_sensors")),
                      "yes" if s.get("n_reference") else "", "yes" if s["verified"] else "", s["status"],
                      s["updated_at"])
    console.print(table)


@main.command()
def stats() -> None:
    """Network vitals as JSON."""
    db.init_db()
    console.print(escape(json.dumps(db.vitals(), indent=2, default=str)))


@main.command()
def health() -> None:
    """Launchd jobs, DB, LLM reachability, Drive + Telegram status."""
    from intelnet import llm
    from intelnet.gdrive import publisher
    from intelnet.telegram import bot as _bot

    db.init_db()
    console.rule("[bold]launchd")
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        out = ""
    for label in LAUNCHD_LABELS:
        line = next((ln for ln in out.splitlines() if ln.endswith(label)), None)
        if line:
            pid, status, _ = line.split(maxsplit=2)
            state = "running" if pid != "-" else f"loaded (last exit {status})"
            console.print(f"[green]●[/green] {label}: {state}")
        else:
            console.print(f"[dim]○[/dim] {label}: not loaded")
    console.rule("[bold]services")
    console.print(f"telegram: {'enabled' if _bot.enabled else 'not configured'}")
    console.print(f"drive: {escape(json.dumps(publisher.status(), default=str))}")
    for k, v in llm.probe().items():
        console.print(f"{k}: {v}")
    console.rule("[bold]network")
    console.print(escape(json.dumps(db.vitals(), default=str)))


if __name__ == "__main__":  # pragma: no cover
    main()
