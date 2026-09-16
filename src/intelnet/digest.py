"""The daily digest — built from the network's state, rendered to HTML.

Two halves, as asked: the classic curated + scored roll-up (events ranked by
score, the official alert recap, a triaged reading list) and the network's
own vital signs (coverage, corroboration, contributor leaderboard, sensors
wanted) — because on this project the network IS the product.

`build()` returns a plain data model; `render_html()` turns it into the HTML
that Google Drive converts into a Google Doc; `render_text()` is the CLI /
Telegram-preview form. No file is written here.
"""
from __future__ import annotations

import csv
import html
import io
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelnet import db, geo, network
from intelnet.config import settings
from intelnet.feeds.nws_alerts import SEVERITY_RANK
from intelnet.models import public_handle
from intelnet.topics import find_metric

EXTREME_METRICS = (("wind_gust_ms", "max"), ("rain_mm", "max"), ("temp_c", "max"),
                   ("temp_c", "min"), ("visibility_km", "min"), ("snow_cm", "max"),
                   ("stage_m", "max"), ("discharge_cms", "max"), ("water_temp_c", "max"),
                   ("soil_moisture_pct", "min"), ("soil_temp_c", "min"))


@dataclass
class DigestModel:
    date: str
    generated_at: str
    hours: float
    network_name: str
    state: str
    vitals: dict[str, Any] = field(default_factory=dict)
    narrative: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    contributions: list[dict[str, Any]] = field(default_factory=list)   # county × metric (human)
    extremes: list[dict[str, Any]] = field(default_factory=list)        # station extremes
    leaderboard: list[dict[str, Any]] = field(default_factory=list)
    reading: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    gap_count: int = 0
    subscriptions: dict[str, int] = field(default_factory=dict)
    links: dict[str, str | None] = field(default_factory=dict)

    @property
    def headline(self) -> str:
        v = self.vitals
        top = self.events[0]["title"] if self.events else None
        bits = [f"{v.get('signals_24h_human', 0)} readings from {v.get('sensors_active_24h', 0)} sensors"]
        if self.alerts:
            bits.append(f"{len(self.alerts)} NWS alerts")
        if top:
            bits.append(f"top event: {top}")
        return " · ".join(bits)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def _alert_recap(hours: float) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for s in db.recent_signals(hours, metric_prefix="alert.", limit=5000):
        g = groups.get(s.group_key or s.key)
        if g is None:
            g = groups[s.group_key or s.key] = {
                "event": s.evidence.get("event") or s.metric, "severity": s.evidence.get("severity"),
                "rank": SEVERITY_RANK.get(str(s.evidence.get("severity")), 0),
                "sender": s.evidence.get("sender"), "counties": [], "sent": s.observed_at,
                "expires": s.expires_at, "url": s.evidence.get("url"),
                "headline": s.evidence.get("nws_headline") or s.evidence.get("headline"),
            }
        c = geo.county(s.location.county_fips)
        if c and c.name not in g["counties"]:
            g["counties"].append(c.name)
    out = sorted(groups.values(), key=lambda g: (-g["rank"], g["sent"]))
    for g in out:
        g["sent"] = g["sent"].strftime("%Y-%m-%d %H:%MZ")
        g["expires"] = g["expires"].strftime("%Y-%m-%d %H:%MZ") if g["expires"] else "—"
    return out


def _extremes(hours: float) -> list[dict[str, Any]]:
    rows = db.mesh(hours, reference_only=True)
    out = []
    for key, how in EXTREME_METRICS:
        m = find_metric(key)
        if m is None:
            continue
        cands = [r for r in rows if r["metric"] == key and r[f"{how}_value"] is not None]
        if not cands:
            continue
        best = max(cands, key=lambda r: r["max_value"]) if how == "max" else min(cands, key=lambda r: r["min_value"])
        c = geo.county(best["county_fips"])
        out.append({"metric": m.label, "how": how, "value": m.display(best[f"{how}_value"]),
                    "county": c.label if c else best["county_fips"], "n": best["n"]})
    return out


def build(hours: float = 24.0, date: str | None = None) -> DigestModel:
    now = datetime.now(timezone.utc)
    model = DigestModel(
        date=date or now.strftime("%Y-%m-%d"), generated_at=now.strftime("%Y-%m-%d %H:%MZ"),
        hours=hours, network_name=settings.network_name, state=settings.geo_state,
    )
    model.vitals = db.vitals()
    model.events = [network.event_summary(e) for e in db.events_since(hours, limit=15)]
    model.alerts = _alert_recap(hours)
    model.contributions = [r for r in network.mesh_rows(hours) if r["n_human"]][:25]
    model.extremes = _extremes(hours)
    # The digest folder can be anyone-with-link, so people appear by handle only
    # — the same rule as the public site.
    model.leaderboard = [
        {"name": public_handle(r["id"]), "trust": r["trust"], "n": r["n"],
         "n_corr": r["n_corr"], "county": (geo.county(r["county_fips"]).name
                                           if geo.county(r["county_fips"]) else "—")}
        for r in db.leaderboard(7)
    ]
    model.reading = [
        {"title": r["title"], "url": r["url"], "relevance": r["relevance"],
         "reason": r["triage_reason"], "published_at": r["published_at"],
         "feed": (json.loads(r["metadata_json"] or "{}").get("feed") if r["metadata_json"] else None)}
        for r in db.kept_items_since(int(hours) + 6, limit=15)
    ]
    gaps = network.coverage_gaps(7)
    model.gap_count = len(gaps)
    model.gaps = [c.name for c in gaps[:20]]
    model.subscriptions = db.subscription_counts()
    latest = db.latest_digest()
    model.links = {"folder": latest["folder_url"] if latest else None,
                   "latest": latest["latest_url"] if latest else None}
    return model


# ── rendering ─────────────────────────────────────────────────────────────

def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


INK, MUTED, ACCENT = "#1A1F1C", "#5B655F", "#1D6E7A"
LINE, SURFACE, PAPER = "#D3DAD4", "#E9EDE8", "#FFFFFF"
TOPIC_COLORS = {"weather": "#1D6E7A", "water": "#2B5FAD", "soil": "#7A4E24",
                "agriculture": "#4F7F2F", "air": "#6E5E9A", "quake": "#B33A2B",
                "nature": "#8A7A1F", "markets": "#8E4585"}
SEVERITY_COLORS = {"Extreme": "#B3261E", "Severe": "#C4501B", "Moderate": "#B7791F",
                   "Minor": "#4B7B8A", "Unknown": MUTED}
STATE_NAMES = {"IL": "Illinois"}
SERIF = "Georgia, 'Iowan Old Style', serif"
SANS = "'IBM Plex Sans', 'Helvetica Neue', Arial, sans-serif"
MONO = "'IBM Plex Mono', 'SF Mono', Menlo, monospace"

# Google Docs keeps inline colours, borders and table shading but throws the
# stylesheet away, so every rule that matters is inline. The <style> block only
# adds what a browser can do on top: page width, a tinted ground, link colour.
BROWSER_CSS = f"""
  :root {{ color-scheme: light; }}
  body {{ margin: 0; padding: 28px 20px 64px; background: {SURFACE}; }}
  .sheet {{ max-width: 860px; margin: 0 auto; background: {PAPER}; padding: 38px 40px 44px;
            box-shadow: 0 1px 3px rgba(20,32,28,.10); border-radius: 3px; }}
  a {{ color: {ACCENT}; }}
  table {{ width: 100%; }}
  .tw {{ overflow-x: auto; }}
  @media (max-width: 620px) {{
    body {{ padding: 0; }} .sheet {{ padding: 22px 16px 30px; box-shadow: none; }}
    table.vitals, table.vitals tbody {{ display: block; }}
    table.vitals tr {{ display: grid; grid-template-columns: 1fr 1fr; }}
    table.vitals td {{ display: block; width: auto !important; }}
  }}
  @media print {{ body {{ background: {PAPER}; padding: 0; }} .sheet {{ box-shadow: none; padding: 0; }} }}
"""


class _Raw(str):
    """Markup that is already safe — links, chips, coloured numbers."""


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value) if isinstance(value, _Raw) else _e(str(value))


def _link(url: str | None, text: str) -> str:
    return (f'<a href="{_e(url)}" style="color:{ACCENT};text-decoration:none">{_e(text)}</a>'
            if url else _e(text))


def _dot(topic: str | None) -> str:
    """A topic's colour as a small square — survives the Doc conversion as text."""
    return (f'<span style="white-space:nowrap">'
            f'<span style="color:{TOPIC_COLORS.get(topic or "", MUTED)};font-size:13px">■</span> '
            f'{_e(topic or "")}</span>')


def _when(stamp: Any) -> str:
    """2026-09-16T13:00:02Z → 16 Sep 13:00."""
    text = str(stamp or "").replace("T", " ")[:16]
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M").strftime("%-d %b %H:%M")
    except ValueError:
        return text


def _table(headers: list[str], rows: list[list[Any]], *, aligns: tuple[str, ...] = (),
           empty: str = "Nothing in this window.") -> str:
    if not rows:
        return f'<p style="margin:0 0 20px;color:{MUTED};font-style:italic">{_e(empty)}</p>'
    align = lambda i: (aligns[i] if i < len(aligns) else "left")  # noqa: E731
    head = "".join(
        f'<th style="text-align:{align(i)};padding:7px 10px;border-bottom:2px solid {INK};'
        f'font:600 11px/1.3 {SANS};letter-spacing:.06em;text-transform:uppercase;'
        f'color:{MUTED}">{_e(h)}</th>' for i, h in enumerate(headers))
    body = []
    for n, row in enumerate(rows):
        shade = f"background:{SURFACE};" if n % 2 else ""
        cells = "".join(
            f'<td style="{shade}text-align:{align(i)};padding:7px 10px;'
            f'border-bottom:1px solid {LINE};font:400 13.5px/1.45 {SANS};color:{INK};'
            f'vertical-align:top">{_cell(c)}</td>' for i, c in enumerate(row))
        body.append(f"<tr>{cells}</tr>")
    return (f'<div class="tw"><table cellspacing="0" cellpadding="0" style="width:100%;'
            f'border-collapse:collapse;margin:0 0 24px">\n<thead><tr>{head}</tr></thead>\n<tbody>'
            + "".join(body) + "</tbody></table></div>")


def _section(title: str, note: str = "") -> str:
    tail = (f' <span style="font:400 12.5px/1.4 {SANS};color:{MUTED};'
            f'text-transform:none;letter-spacing:0">{_e(note)}</span>' if note else "")
    return (f'<h2 style="margin:30px 0 12px;font:600 13px/1.3 {MONO};letter-spacing:.12em;'
            f'text-transform:uppercase;color:{ACCENT};border-top:2px solid {ACCENT};'
            f'padding-top:9px">{_e(title)}{tail}</h2>')


def _vitals_strip(v: dict[str, Any]) -> str:
    """Six headline numbers across the page — a table, so the Doc keeps the columns."""
    cells = [
        (f"{v.get('signals_24h_human', 0)}", "readings from people, 24h"),
        (f"{v.get('signals_24h_reference', 0)}", "official readings, 24h"),
        (f"{v.get('sensors_active_24h', 0)}/{v.get('sensors_total', 0)}", "sensors active / joined"),
        (f"{v.get('corroboration_rate_7d', 0):.0%}", "corroborated, 7d"),
        (f"{v.get('events_open', 0)}", "events open"),
        (f"{v.get('alerts_active', 0)}", "NWS alerts active"),
    ]
    tds = "".join(
        f'<td style="padding:12px 10px;border:1px solid {LINE};background:{SURFACE};'
        f'text-align:center;width:16.6%">'
        f'<div style="font:600 25px/1.1 {SERIF};color:{INK}">{_e(big)}</div>'
        f'<div style="font:400 10.5px/1.35 {SANS};color:{MUTED};text-transform:uppercase;'
        f'letter-spacing:.05em;padding-top:4px">{_e(label)}</div></td>' for big, label in cells)
    return (f'<table class="vitals" cellspacing="0" cellpadding="0" style="width:100%;'
            f'border-collapse:collapse;margin:22px 0 6px"><tr>{tds}</tr></table>')


def _downloads_bar(downloads: dict[str, str] | None) -> str:
    if not downloads:
        return ""
    links = " &nbsp;·&nbsp; ".join(
        f'<a href="{_e(url)}" style="color:{ACCENT};font-weight:600;text-decoration:none">{_e(label)}</a>'
        for label, url in downloads.items())
    return (f'<table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;'
            f'margin:18px 0 4px"><tr><td style="padding:10px 14px;background:{SURFACE};'
            f'border-left:3px solid {ACCENT};font:400 13px/1.5 {SANS};color:{INK}">'
            f'<span style="font:600 10.5px/1.3 {MONO};letter-spacing:.1em;text-transform:uppercase;'
            f'color:{MUTED}">Also available as</span><br>{links}</td></tr></table>')


def render_html(m: DigestModel, downloads: dict[str, str] | None = None) -> str:
    """The digest as a page: styled for a browser, and still tidy as a Google Doc.

    `downloads` maps a label to a URL ("PDF", "Word", "CSV tables"…). The publisher
    passes it on a second pass, once the files it names actually exist.
    """
    v = m.vitals
    p = []                                            # noqa: E741 — parts of the page
    p.append(f'<div class="sheet" style="font-family:{SANS};color:{INK}">')

    # ── masthead ──
    p.append(f'<div style="font:600 11px/1.3 {MONO};letter-spacing:.14em;text-transform:uppercase;'
             f'color:{MUTED}">{_e(m.network_name)}</div>')
    p.append(f'<h1 style="margin:6px 0 2px;font:600 34px/1.12 {SERIF};color:{INK};'
             f'letter-spacing:-.01em">{_e(STATE_NAMES.get(m.state, m.state))} environmental digest</h1>')
    p.append(f'<div style="font:400 15px/1.5 {SANS};color:{MUTED};border-bottom:3px solid {INK};'
             f'padding-bottom:14px">{_e(_long_date(m.date))}</div>')
    p.append(f'<p style="margin:16px 0 0;font:400 17px/1.5 {SERIF};color:{INK}">{_e(m.headline)}</p>')
    p.append(_downloads_bar(downloads))

    if m.narrative:
        for para in m.narrative.split("\n\n"):
            if para.strip():
                p.append(f'<p style="margin:14px 0 0;font:400 16px/1.62 {SERIF};color:{INK}">'
                         f'{_e(para.strip())}</p>')

    p.append(_vitals_strip(v))
    p.append(f'<div style="font:400 11.5px/1.4 {SANS};color:{MUTED};margin-bottom:8px">'
             f'Counties with a human reading this week: {v.get("counties_covered_7d", 0)} of '
             f'{v.get("counties_total", 0)} · with any data: {v.get("counties_reference_7d", 0)} · '
             f'subscribers: {v.get("subscribers", 0)}</div>')

    # ── events ──
    p.append(_section("Events", "readings the network corroborated and scored"))
    p.append(_table(
        ["Score", "Topic", "Event", "Peak", "Sensors", "Official", "Status", "Updated"],
        [[_Raw(f'<b style="font:600 14px {MONO}">{e.get("score") or 0:.2f}</b>'),
          _Raw(_dot(e.get("topic"))),
          _Raw(_link(e.get("source_url"),
                     f'{e.get("metric_label") or e["title"]} — {e.get("county_label") or ""}'.strip(" —"))),
          _Raw(f'<span style="white-space:nowrap">{_e(e["peak_display"])}</span>'),
          e.get("n_sensors"), "yes" if e.get("n_reference") else "—",
          _Raw(f'<span style="color:{"#3B7A45" if e["verified"] else MUTED}">'
               f'{"verified" if e["verified"] else "unverified"}</span>'),
          _Raw(f'<span style="font:400 12px {MONO};color:{MUTED};white-space:nowrap">'
               f'{_e(_when(e.get("updated_at")))}</span>')] for e in m.events],
        aligns=("right", "left", "left", "right", "right", "center", "left", "left"),
        empty="No events crossed a threshold in this window."))

    # ── official alerts ──
    p.append(_section("Official alerts", "issued by the National Weather Service"))
    p.append(_table(
        ["Severity", "Alert", "Counties", "Issued", "Until", "Issuer"],
        [[_Raw(f'<b style="color:{SEVERITY_COLORS.get(a["severity"], MUTED)}">{_e(a["severity"])}</b>'),
          _Raw(_link(a.get("url"), a["event"])),
          ", ".join(a["counties"][:8]) + ("…" if len(a["counties"]) > 8 else ""),
          _Raw(f'<span style="font:400 12px {MONO};color:{MUTED};white-space:nowrap">'
               f'{_e(_when(a["sent"]))}</span>'),
          _Raw(f'<span style="font:400 12px {MONO};color:{MUTED};white-space:nowrap">'
               f'{_e(_when(a["expires"]))}</span>'),
          a["sender"]] for a in m.alerts],
        aligns=("left", "left", "left", "left", "left", "left"),
        empty="No active alerts."))

    # ── the network's own week ──
    p.append(_section("Readings by county", "what people reported, grouped"))
    p.append(_table(
        ["County", "Topic", "Metric", "Readings", "Sensors", "Mean", "Peak"],
        [[r["county"], _Raw(_dot(r.get("topic"))), r["metric"], r["n_human"], r["n_sensors"],
          r["mean"], r["max"]] for r in m.contributions],
        aligns=("left", "left", "left", "right", "right", "right", "right"),
        empty="No readings from people in this window — the network is running on official feeds."))

    p.append(_section("Station extremes", "highs and lows across the official network"))
    p.append(_table(["Metric", "", "Value", "County"],
                    [[x["metric"], _Raw(f'<span style="color:{MUTED}">{_e(x["how"])}</span>'),
                      x["value"], x["county"]] for x in m.extremes],
                    aligns=("left", "left", "right", "left")))

    p.append(_section("Contributors", "this week, by handle"))
    p.append(_table(["Sensor", "County", "Readings", "Corroborated", "Trust"],
                    [[_Raw(f'<span style="font:400 13px {MONO}">{_e(r["name"])}</span>'), r["county"],
                      r["n"], r["n_corr"], f"{r['trust']:.2f}"] for r in m.leaderboard],
                    aligns=("left", "left", "right", "right", "right"),
                    empty="No contributors yet — the first reading could be yours."))

    # ── reading list ──
    p.append(_section("Worth reading", "triaged from the network's sources"))
    if m.reading:
        items = []
        for r in m.reading:
            meta = " · ".join(x for x in [r.get("feed"),
                                          f"relevance {r['relevance']:.2f}" if r.get("relevance") is not None else None] if x)
            why = (f'<div style="font:400 13px/1.5 {SANS};color:{MUTED};padding-top:2px">'
                   f'{_e(r["reason"])}</div>' if r.get("reason") else "")
            items.append(
                f'<li style="margin:0 0 12px">'
                f'<span style="font:400 14.5px/1.45 {SANS}">{_link(r.get("url"), r["title"])}</span>'
                f'<div style="font:400 11.5px/1.4 {MONO};color:{MUTED};padding-top:3px">{_e(meta)}</div>'
                f'{why}</li>')
        p.append(f'<ul style="margin:0 0 24px;padding-left:18px">{"".join(items)}</ul>')
    else:
        p.append(f'<p style="margin:0 0 24px;color:{MUTED};font-style:italic">'
                 f'Nothing kept in this window.</p>')

    # ── where the network is thin ──
    p.append(_section("Sensors wanted"))
    gaps = (": " + _e(", ".join(m.gaps)) + ("…" if m.gap_count > len(m.gaps) else "")) if m.gaps else ""
    p.append(f'<p style="margin:0 0 20px;font:400 14px/1.6 {SANS};color:{INK}">'
             f'<b>{m.gap_count}</b> of {v.get("counties_total", 0)} counties had no human reading this '
             f'week{gaps}.</p>')

    # ── how to take part ──
    p.append(f'<table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;'
             f'margin:8px 0 0"><tr><td style="padding:16px 18px;background:{SURFACE};'
             f'border:1px solid {LINE}">'
             f'<div style="font:600 12px/1.3 {MONO};letter-spacing:.1em;text-transform:uppercase;'
             f'color:{ACCENT};padding-bottom:8px">Report something</div>'
             f'<div style="font:400 14px/1.65 {SANS};color:{INK}">'
             f'Message the network on Telegram and type what you see — '
             f'<span style="font-family:{MONO}">rain 1.2in</span>, '
             f'<span style="font-family:{MONO}">hail quarter</span>, '
             f'<span style="font-family:{MONO}">trees down</span> — or just say it in a sentence. '
             f'Set your home once with <span style="font-family:{MONO}">/home 62704</span>, and pick '
             f'what you want sent to you with <span style="font-family:{MONO}">/subscribe warnings cook</span>. '
             f'Your readings are checked against neighbors and official sources; corroborated readings '
             f'raise your trust and become events.</div></td></tr></table>')

    footer = (f'Generated {_e(m.generated_at)} · {m.hours:g}-hour window'
              + (f' · subscriptions: {_e(", ".join(f"{k} {n}" for k, n in m.subscriptions.items()))}'
                 if m.subscriptions else ""))
    p.append(f'<p style="margin:22px 0 0;padding-top:12px;border-top:1px solid {LINE};'
             f'font:400 11.5px/1.5 {MONO};color:{MUTED}">{footer}</p>')
    p.append("</div>")
    return f"<style>{BROWSER_CSS}</style>\n" + "\n".join(x for x in p if x)


def _long_date(date: str) -> str:
    """2026-09-16 → Wednesday, 16 September 2026 (falls back to the raw string)."""
    try:
        return datetime.strptime(date, "%Y-%m-%d").strftime("%A, %-d %B %Y")
    except ValueError:
        return date


def render_text(m: DigestModel) -> str:
    v = m.vitals
    lines = [f"{m.network_name} — {m.state} environmental digest — {m.date}", m.headline, ""]
    if m.narrative:
        lines += [m.narrative, ""]
    lines.append(f"Vitals: sensors {v.get('sensors_total', 0)} (active 24h {v.get('sensors_active_24h', 0)}) · "
                 f"readings 24h {v.get('signals_24h_human', 0)} human / {v.get('signals_24h_reference', 0)} ref · "
                 f"corroboration 7d {v.get('corroboration_rate_7d', 0):.0%} · "
                 f"coverage {v.get('counties_covered_7d', 0)}/{v.get('counties_total', 0)} counties")
    lines.append("")
    lines.append("Events:")
    lines += [f"  {e.get('score') or 0:.2f}  {e['title']}  ({e.get('n_sensors')} sensors"
              f"{', official' if e.get('n_reference') else ''}{', verified' if e['verified'] else ''})"
              for e in m.events] or ["  none"]
    lines.append("")
    lines.append("NWS alerts:")
    lines += [f"  [{a['severity']}] {a['event']} — {', '.join(a['counties'][:5])} (until {a['expires']})"
              for a in m.alerts[:15]] or ["  none"]
    lines.append("")
    lines.append("Station extremes: " + "; ".join(f"{x['metric']} {x['how']} {x['value']} ({x['county']})"
                                                  for x in m.extremes) if m.extremes else "Station extremes: none")
    lines.append("")
    lines.append("Reading:")
    lines += [f"  - {r['title']}" for r in m.reading[:10]] or ["  none"]
    lines.append("")
    lines.append(f"Sensors wanted: {m.gap_count} counties without a human reading this week.")
    return "\n".join(lines)


# ── CSV ────────────────────────────────────────────────────────────────────

EVENT_COLUMNS = ("opened_at", "updated_at", "topic", "metric", "metric_label", "title", "county_label",
                 "county_fips", "zip5", "lat", "lon", "peak_value", "unit", "peak_display", "score",
                 "severity", "n_signals", "n_sensors", "n_reference", "mean_trust", "verified", "status")
ALERT_COLUMNS = ("event", "severity", "counties", "sent", "expires", "sender", "headline", "url")
COUNTY_COLUMNS = ("county", "county_fips", "topic", "metric", "metric_key", "n", "n_human", "n_sensors",
                  "mean", "max", "latest")
EXTREME_COLUMNS = ("metric", "how", "value", "county", "n")
CONTRIBUTOR_COLUMNS = ("name", "county", "trust", "n", "n_corr")
READING_COLUMNS = ("published_at", "feed", "title", "relevance", "reason", "url")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value)


def _csv(columns: tuple[str, ...], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in columns])
    return buf.getvalue()


def render_csvs(m: DigestModel) -> dict[str, str]:
    """The digest's tables as CSV, one per table — {name: text}.

    The document is for reading; these are for working. Headers stay put even on
    a quiet day, so a spreadsheet or script pointed at a file keeps working.
    """
    return {
        "events": _csv(EVENT_COLUMNS, m.events),
        "official-alerts": _csv(ALERT_COLUMNS, m.alerts),
        "county-activity": _csv(COUNTY_COLUMNS, m.contributions),
        "station-extremes": _csv(EXTREME_COLUMNS, m.extremes),
        "contributors": _csv(CONTRIBUTOR_COLUMNS, m.leaderboard),
        "reading-list": _csv(READING_COLUMNS, m.reading),
        "network-vitals": _csv(("metric", "value"),
                               [{"metric": k, "value": v} for k, v in sorted(m.vitals.items())]),
    }
