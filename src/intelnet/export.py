"""Public snapshot + site build.

`snapshot()` turns the network's state into a set of plain JSON documents
(vitals, counties, events, alerts, activity, the graph, contributors, the
digest ledger, the topic vocabulary, the public-data catalog). They are what
the site renders and what anyone else may pull — so they are **public by
construction**: human sensors are reduced to a stable handle (`s-3f9a`) plus
their county, no names, no ZIP+4, no coordinates; reference stations keep
their (already public) positions.

`build_site()` wraps `site/index.fragment.html` into `docs/index.html` with
the snapshot inlined, so the page works from GitHub Pages, from a local file,
and as a Claude artifact without fetching anything.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from intelnet import db, geo, network
from intelnet.config import CONFIG_DIR, PROJECT_ROOT, settings
from intelnet.models import KIND_BOT, KIND_HUMAN, REFERENCE_KINDS, public_handle, utcnow
from intelnet.topics import topics

SITE_DIR = PROJECT_ROOT / "site"
FRAGMENT = SITE_DIR / "index.fragment.html"
DOCS_DIR = PROJECT_ROOT / "docs"
DATA_MARK = "/*__NETWORK_DATA__*/null"
HUMAN_KINDS = (KIND_HUMAN, KIND_BOT)


def handle(sensor_id: str) -> str:
    """Stable public handle for a human sensor (no identity leaks)."""
    return public_handle(sensor_id)


# ── sections ──────────────────────────────────────────────────────────────

def _topics_doc() -> list[dict[str, Any]]:
    out = []
    for t in topics().values():
        out.append({
            "name": t.name, "label": t.label, "description": t.description,
            "categories": [{"key": t.category_key(c), "description": d} for c, d in t.categories.items()],
            "metrics": [{
                "key": m.key, "label": m.label, "unit": m.unit, "kind": m.kind,
                "aliases": list(m.aliases), "units": list(m.units), "default_unit": m.default_unit,
                "words": m.words, "range": list(m.range) if m.range else None,
                "event": m.event, "event_direction": m.event_direction,
                "display": ({"unit": m.display_unit["unit"], "expr": _display_expr(m)}
                            if m.display_unit else None),
                "convert": _convert_table(m),
            } for m in t.metrics.values()],
        })
    return out


def _display_expr(m) -> str:
    """The pack's display expression (re-read from YAML; callables don't serialize)."""
    raw = yaml.safe_load((CONFIG_DIR / "topics" / f"{m.topic}.yaml").read_text(encoding="utf-8"))
    du = ((raw.get("metrics") or {}).get(m.key) or {}).get("display_unit") or {}
    return str(du.get("expr", "x"))


def _convert_table(m) -> dict[str, Any]:
    """Typed unit → factor or expression, for the browser-side sandbox."""
    raw = yaml.safe_load((CONFIG_DIR / "topics" / f"{m.topic}.yaml").read_text(encoding="utf-8"))
    units = ((raw.get("metrics") or {}).get(m.key) or {}).get("units") or {}
    out: dict[str, Any] = {}
    for u, spec in units.items():
        out[str(u).lower()] = {"expr": str(spec["expr"])} if isinstance(spec, dict) else float(spec)
    return out


def _activity(days: int) -> list[dict[str, Any]]:
    start = (utcnow() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT substr(observed_at, 1, 10) AS d, topic, sensor_kind, COUNT(*) AS n
               FROM signals WHERE observed_at >= ? AND metric NOT LIKE 'alert.%'
               GROUP BY d, topic, sensor_kind""",
            (start.isoformat(),),
        ).fetchall()
        alerts = conn.execute(
            """SELECT substr(observed_at, 1, 10) AS d, COUNT(DISTINCT group_key) AS n
               FROM signals WHERE observed_at >= ? AND metric LIKE 'alert.%' GROUP BY d""",
            (start.isoformat(),),
        ).fetchall()
    for r in rows:
        key = r["topic"] if r["sensor_kind"] in HUMAN_KINDS else "reference"
        by_day[r["d"]][key] += r["n"]
    for r in alerts:
        by_day[r["d"]]["alerts"] += r["n"]
    out = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        row = {"date": d, **{t: 0 for t in topics()}, "reference": 0, "alerts": 0}
        row.update(by_day.get(d, {}))
        out.append(row)
    return out


def _counties(days: int) -> list[dict[str, Any]]:
    human = db.county_activity(days, kinds=HUMAN_KINDS)
    ref = db.county_activity(days, kinds=REFERENCE_KINDS)
    alerts = Counter(s.location.county_fips for s in db.active_alert_signals())
    events = Counter(e["county_fips"] for e in db.open_events())
    top: dict[str, str] = {}
    for r in db.mesh(days * 24, human_only=True):
        top.setdefault(r["county_fips"], r["metric"])
    return [{
        "fips": c.fips, "name": c.name, "slug": c.slug, "lat": c.lat, "lon": c.lon,
        "human": human.get(c.fips, 0), "reference": ref.get(c.fips, 0),
        "alerts": alerts.get(c.fips, 0), "events": events.get(c.fips, 0),
        "top_metric": top.get(c.fips),
    } for c in sorted(geo.counties().values(), key=lambda x: x.name)]


def _events(days: int) -> list[dict[str, Any]]:
    out = []
    for e in db.events_since(days * 24, limit=60):
        s = network.event_summary(e)
        out.append({k: s.get(k) for k in (
            "id", "topic", "metric", "metric_label", "title", "county_fips", "county_label",
            "peak_display", "n_signals", "n_sensors", "n_reference", "severity", "score",
            "status", "opened_at", "updated_at",
        )} | {"verified": bool(s["verified"])})
    return out


def _alerts() -> list[dict[str, Any]]:
    from intelnet.feeds.nws_alerts import active_alert_groups

    out = []
    for g in active_alert_groups():
        s = g["signal"]
        out.append({
            "event": s.evidence.get("event"), "severity": s.evidence.get("severity"),
            "counties": g["counties"], "sender": s.evidence.get("sender"),
            "headline": s.evidence.get("nws_headline") or s.evidence.get("headline"),
            "expires": s.expires_at.isoformat() if s.expires_at else None, "url": s.evidence.get("url"),
        })
    return out


def _graph(days: int, max_sensors: int = 200) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    links: dict[tuple[str, str, str], int] = defaultdict(int)

    def node(nid: str, **attrs: Any) -> None:
        n = nodes.setdefault(nid, {"id": nid})
        n.update(attrs)

    for t in topics().values():
        node(f"topic:{t.name}", kind="topic", label=t.label, topic=t.name, n=0)
        for m in t.metrics.values():
            node(f"metric:{m.key}", kind="metric", label=m.label, topic=t.name, n=0, flag=m.is_flag)
            links[(f"metric:{m.key}", f"topic:{t.name}", "belongs")] += 1

    sigs = db.recent_signals(days * 24, limit=20000)
    per_sensor: Counter[str] = Counter()
    sensor_home: dict[str, str | None] = {}
    corro: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for s in sigs:
        if s.metric.startswith("alert."):
            continue
        mid = f"metric:{s.metric}"
        if mid in nodes:
            nodes[mid]["n"] += 1
            nodes[f"topic:{s.topic}"]["n"] += 1
        if s.sensor_kind in HUMAN_KINDS:
            per_sensor[s.sensor_id] += 1
            sensor_home.setdefault(s.sensor_id, s.location.county_fips)
            if s.quality == "corroborated" and s.location.county_fips:
                corro[(s.metric, s.location.county_fips, s.observed_at.strftime("%Y-%m-%d"))].add(s.sensor_id)
        else:
            fid = f"feed:{s.source}"
            node(fid, kind="feed", label=s.source, n=nodes.get(fid, {}).get("n", 0) + 1)
            links[(fid, mid, "reports")] += 1
    keep = {sid for sid, _ in per_sensor.most_common(max_sensors)}
    sensors = {s.id: s for s in db.list_sensors(limit=10000) if s.id in keep}
    for sid in keep:
        s = sensors.get(sid)
        h = handle(sid)
        fips = sensor_home.get(sid)
        node(f"sensor:{h}", kind="sensor", label=h, n=per_sensor[sid],
             trust=round(s.trust, 2) if s else None, county=fips)
        if fips:
            c = geo.county(fips)
            node(f"county:{fips}", kind="county", label=c.name if c else fips, n=nodes.get(f"county:{fips}", {}).get("n", 0) + per_sensor[sid])
            links[(f"sensor:{h}", f"county:{fips}", "home")] += 1
    for s in sigs:
        if s.sensor_kind in HUMAN_KINDS and s.sensor_id in keep and not s.metric.startswith("alert."):
            links[(f"sensor:{handle(s.sensor_id)}", f"metric:{s.metric}", "reports")] += 1
    for _key, members in corro.items():
        ms = sorted(members & keep)
        for i, a in enumerate(ms):
            for b in ms[i + 1:]:
                links[(f"sensor:{handle(a)}", f"sensor:{handle(b)}", "corroborated")] += 1
    for e in _events(days):
        eid = f"event:{e['id']}"
        node(eid, kind="event", label=e["title"], topic=e["topic"], n=e["n_signals"], score=e["score"],
             verified=e["verified"])
        links[(eid, f"metric:{e['metric']}", "about")] += 1
        if e["county_fips"]:
            c = geo.county(e["county_fips"])
            node(f"county:{e['county_fips']}", kind="county", label=c.name if c else e["county_fips"])
            links[(eid, f"county:{e['county_fips']}", "in")] += 1
    # drop metric nodes nobody used (keeps the graph readable) unless the pack is tiny
    used = {lk[1] for lk in links if lk[2] in ("reports", "about")} | {lk[0] for lk in links}
    out_nodes = [n for nid, n in nodes.items() if n["kind"] != "metric" or nid in used or n["n"]]
    ids = {n["id"] for n in out_nodes}
    out_links = [{"source": a, "target": b, "kind": k, "weight": w}
                 for (a, b, k), w in links.items() if a in ids and b in ids]
    return {"nodes": out_nodes, "links": out_links}


def _leaderboard(days: int) -> list[dict[str, Any]]:
    return [{
        "handle": handle(r["id"]), "county": (geo.county(r["county_fips"]).name
                                              if geo.county(r["county_fips"]) else None),
        "n": r["n"], "n_corr": r["n_corr"], "trust": round(r["trust"], 2),
    } for r in db.leaderboard(days, limit=15)]


def _digests() -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM digests ORDER BY date DESC LIMIT 60").fetchall()
    return [{k: r[k] for k in ("date", "drive_url", "latest_url", "folder_url", "n_events", "n_signals",
                               "n_sensors")} for r in rows]


def _sources() -> list[dict[str, Any]]:
    path = CONFIG_DIR / "public_sources.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("sources") or [])


def snapshot(days: int = 14, *, sample: bool = False) -> dict[str, Any]:
    latest = db.latest_digest()
    return {
        "generated_at": utcnow().replace(microsecond=0).isoformat(),
        "sample": sample,
        "days": days,
        "network_name": settings.network_name,
        "state": settings.geo_state,
        "bot_handle": settings.telegram_bot_handle,
        "group_url": settings.telegram_group_url,
        "github_repo": settings.github_repo,
        "site_url": settings.public_site_url,
        "contact_email": settings.network_contact_email,
        "links": {"folder": latest["folder_url"] if latest else None,
                  "latest": latest["latest_url"] if latest else None,
                  "digest": latest["drive_url"] if latest else None},
        "vitals": db.vitals(),
        "subscriptions": db.subscription_counts(),
        "topics": _topics_doc(),
        "counties": _counties(days),
        "events": _events(days),
        "alerts": _alerts(),
        "activity": _activity(days),
        "graph": _graph(days),
        "leaderboard": _leaderboard(days),
        "digests": _digests(),
        "sources": _sources(),
    }


# ── writers ───────────────────────────────────────────────────────────────

def write_json(snap: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    core = {k: v for k, v in snap.items() if k not in ("counties", "events", "alerts", "activity",
                                                        "graph", "leaderboard", "digests", "sources", "topics")}
    for name, payload in (("network", core), ("topics", snap["topics"]), ("counties", snap["counties"]),
                          ("events", snap["events"]), ("alerts", snap["alerts"]), ("activity", snap["activity"]),
                          ("graph", snap["graph"]), ("leaderboard", snap["leaderboard"]),
                          ("digests", snap["digests"]), ("sources", snap["sources"])):
        p = out_dir / f"{name}.json"
        p.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        written.append(p)
    return written


def build_site(snap: dict[str, Any], fragment: Path | None = None, out: Path | None = None) -> Path:
    """Wrap the fragment as a full HTML document with the snapshot inlined."""
    fragment = fragment or FRAGMENT
    out = out or (DOCS_DIR / "index.html")
    html = fragment.read_text(encoding="utf-8")
    data = json.dumps(snap, default=str).replace("</", "<\\/")
    if DATA_MARK not in html:
        raise ValueError(f"{fragment} has no {DATA_MARK} marker")
    html = html.replace(DATA_MARK, data, 1)
    head_bits = re.findall(r"^\s*(<title>.*?</title>|<link [^>]*>|<meta [^>]*>)\s*$", html, flags=re.M)
    body = html
    for bit in head_bits:
        body = body.replace(bit, "", 1)
    doc = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        + "\n".join(head_bits) + "\n</head>\n<body>\n" + body.strip() + "\n</body>\n</html>\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out


STATIC_PAGES = ("privacy.html", "terms.html")


def render_static_pages(snap: dict[str, Any], out_dir: Path, site_dir: Path | None = None) -> list[Path]:
    """Copy the policy pages next to index.html, filling in the network's details."""
    site_dir = site_dir or FRAGMENT.parent
    values = {
        "{{NETWORK_NAME}}": str(snap.get("network_name") or "Intelligence Network"),
        "{{STATE}}": {"IL": "Illinois"}.get(str(snap.get("state")), str(snap.get("state") or "")),
        "{{BOT_HANDLE}}": str(snap.get("bot_handle") or "intelligence_network_bot"),
        "{{CONTACT_EMAIL}}": str(snap.get("contact_email") or ""),
        "{{SITE_URL}}": str(snap.get("site_url") or "./"),
    }
    written = []
    for name in STATIC_PAGES:
        src = site_dir / name
        if not src.exists():
            continue
        html = src.read_text(encoding="utf-8")
        for key, val in values.items():
            html = html.replace(key, val)
        dest = out_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")
        written.append(dest)
    return written


def artifact_fragment(snap: dict[str, Any], fragment: Path | None = None) -> str:
    """The fragment with data inlined (no html/head/body — the artifact skeleton adds them)."""
    html = (fragment or FRAGMENT).read_text(encoding="utf-8")
    return html.replace(DATA_MARK, json.dumps(snap, default=str).replace("</", "<\\/"), 1)


def export_all(out_dir: Path | None = None, days: int = 14, *, site: bool = True,
               sample: bool = False) -> dict[str, Any]:
    out_dir = out_dir or DOCS_DIR          # resolved at call time (tests repoint DOCS_DIR)
    snap = snapshot(days, sample=sample)
    files = write_json(snap, out_dir / "data")
    result: dict[str, Any] = {"json": [str(p) for p in files]}
    if site and FRAGMENT.exists():
        result["site"] = str(build_site(snap, out=out_dir / "index.html"))
        result["pages"] = [str(p) for p in render_static_pages(snap, out_dir)]
    return result


def git_push_docs(message: str | None = None) -> bool:
    """Commit + push docs/ (only when SITE_AUTO_PUSH). Best-effort."""
    import subprocess

    msg = message or f"site: snapshot {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    try:
        subprocess.run(["git", "add", "docs"], cwd=PROJECT_ROOT, check=True, capture_output=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PROJECT_ROOT)
        if diff.returncode == 0:
            return False
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=PROJECT_ROOT, check=True, capture_output=True)
        subprocess.run(["git", "push", "-q"], cwd=PROJECT_ROOT, check=True, capture_output=True)
        return True
    except Exception:  # noqa: BLE001
        return False
