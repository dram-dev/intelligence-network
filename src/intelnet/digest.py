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

import html
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelnet import db, geo, network
from intelnet.config import settings
from intelnet.feeds.nws_alerts import SEVERITY_RANK
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
    model.leaderboard = [
        {"name": r["name"] or r["username"] or r["id"], "trust": r["trust"], "n": r["n"],
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


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "<p><i>none</i></p>"
    th = "".join(f"<th>{_e(h)}</th>" for h in headers)
    trs = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table border='1' cellpadding='4' cellspacing='0'><tr>{th}</tr>{trs}</table>"


def render_html(m: DigestModel) -> str:
    v = m.vitals
    parts = [
        f"<h1>{_e(m.network_name)} — {_e(m.state)} environmental digest — {_e(m.date)}</h1>",
        f"<p><i>Generated {_e(m.generated_at)} · window {m.hours:g}h · "
        f"{_e(m.headline)}</i></p>",
    ]
    if m.narrative:
        for para in m.narrative.split("\n\n"):
            parts.append(f"<p>{_e(para.strip())}</p>")

    parts.append("<h2>Network vitals</h2>")
    parts.append(_table(["Metric", "Value"], [
        ["Sensors (joined / active 24h / active 7d)",
         f"{v.get('sensors_total', 0)} / {v.get('sensors_active_24h', 0)} / {v.get('sensors_active_7d', 0)}"],
        ["Readings 24h (human / reference)", f"{v.get('signals_24h_human', 0)} / {v.get('signals_24h_reference', 0)}"],
        ["Corroboration rate, 7d", f"{v.get('corroboration_rate_7d', 0):.0%}"],
        ["Counties with a human sensor, 7d", f"{v.get('counties_covered_7d', 0)} / {v.get('counties_total', 0)}"],
        ["Counties with any data, 7d", f"{v.get('counties_reference_7d', 0)} / {v.get('counties_total', 0)}"],
        ["Events (open / 24h)", f"{v.get('events_open', 0)} / {v.get('events_24h', 0)}"],
        ["Active NWS alerts", str(v.get("alerts_active", 0))],
        ["Subscribers", str(v.get("subscribers", 0))],
    ]))

    parts.append("<h2>Events (ranked by score)</h2>")
    parts.append(_table(
        ["Score", "Topic", "Event", "Peak", "Sensors", "Official", "Verified", "Updated"],
        [[f"{e.get('score') or 0:.2f}", e.get("topic"), e["title"], e["peak_display"], e.get("n_sensors"),
          "yes" if e.get("n_reference") else "no", "yes" if e["verified"] else "no",
          e.get("updated_at")] for e in m.events],
    ))

    parts.append("<h2>Official alerts (NWS)</h2>")
    parts.append(_table(
        ["Severity", "Alert", "Counties", "Issued", "Until", "Issuer"],
        [[a["severity"], a["event"], ", ".join(a["counties"][:8]) + ("…" if len(a["counties"]) > 8 else ""),
          a["sent"], a["expires"], a["sender"]] for a in m.alerts],
    ))

    parts.append("<h2>Contributions by county (human sensors)</h2>")
    parts.append(_table(
        ["County", "Topic", "Metric", "Readings", "Sensors", "Mean", "Peak"],
        [[r["county"], r.get("topic"), r["metric"], r["n_human"], r["n_sensors"], r["mean"], r["max"]]
         for r in m.contributions],
    ))

    parts.append("<h2>Station extremes (weather, gauges, soil)</h2>")
    parts.append(_table(["Metric", "", "Value", "County"],
                        [[x["metric"], x["how"], x["value"], x["county"]] for x in m.extremes]))

    parts.append("<h2>Contributors this week</h2>")
    parts.append(_table(["Sensor", "County", "Readings", "Corroborated", "Trust"],
                        [[r["name"], r["county"], r["n"], r["n_corr"], f"{r['trust']:.2f}"]
                         for r in m.leaderboard]))

    parts.append("<h2>Reading</h2>")
    if m.reading:
        items = []
        for r in m.reading:
            link = f'<a href="{_e(r["url"])}">{_e(r["title"])}</a>' if r.get("url") else _e(r["title"])
            meta = " · ".join(x for x in [r.get("feed"), f"relevance {r['relevance']:.2f}" if r.get("relevance") is not None else None] if x)
            why = f" — {_e(r['reason'])}" if r.get("reason") else ""
            items.append(f"<li>{link} <i>({_e(meta)})</i>{why}</li>")
        parts.append("<ul>" + "".join(items) + "</ul>")
    else:
        parts.append("<p><i>nothing kept in this window</i></p>")

    parts.append("<h2>Sensors wanted</h2>")
    parts.append(f"<p>{m.gap_count} of {v.get('counties_total', 0)} counties had no human reading this "
                 f"week{': ' + _e(', '.join(m.gaps)) + ('…' if m.gap_count > len(m.gaps) else '') if m.gaps else ''}.</p>")

    parts.append("<h2>How to contribute</h2>")
    parts.append(
        "<p>Message the network's Telegram bot. Type what you see — "
        "<code>rain 1.2in</code>, <code>hail quarter @62704-1234</code>, <code>gust 60mph</code>, "
        "<code>trees down</code> — or a plain sentence. Set your home once with "
        "<code>/home 62704-1234</code> or share your location. Subscribe with "
        "<code>/subscribe warnings cook</code>, <code>/subscribe events 62704</code>, "
        "<code>/subscribe digest</code>. Readings are checked against neighbours and official "
        "sources; corroborated readings raise your trust and become events.</p>"
    )
    if m.subscriptions:
        parts.append("<p><i>Subscriptions: " + ", ".join(f"{k} {n}" for k, n in m.subscriptions.items()) + "</i></p>")
    return "\n".join(parts)


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
