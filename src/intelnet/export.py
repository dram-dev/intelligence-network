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

import html
import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from intelnet import ahead, db, geo, network, notable, opendata, rivers, story_brief, trust
from intelnet.config import CONFIG_DIR, PROJECT_ROOT, settings
from intelnet.models import KIND_BOT, KIND_HUMAN, REFERENCE_KINDS, local_time, public_handle, utcnow
from intelnet.topics import find_metric, topics

SITE_DIR = PROJECT_ROOT / "site"
FRAGMENT = SITE_DIR / "index.fragment.html"
ASSETS_DIR = SITE_DIR / "assets"
DOCS_DIR = PROJECT_ROOT / "docs"
DATA_MARK = "/*__NETWORK_DATA__*/null"
COUNTY_TEMPLATE = SITE_DIR / "county.fragment.html"
COUNTY_MARK = "/*__COUNTY_DATA__*/null"
HUMAN_KINDS = (KIND_HUMAN, KIND_BOT)

logger = logging.getLogger(__name__)


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
                "event": m.event, "event_direction": m.event_direction, "display_words": m.display_words,
                "display": ({"unit": m.display_unit["unit"], "expr": _display_expr(m)}
                            if m.display_unit else None),
                "convert": _convert_table(m),
            } for m in t.metrics.values()],
            # the report buttons (the chat keyboard and the Mini App's composer)
            "quick_reports": [{k: q[k] for k in ("id", "button", "ask", "choices", "send", "keyboard", "visual",
                                                 "example") if k in q}
                              for q in t.quick_reports],
            # how NWS impact tags read on cards and in the app (header, chip or tag)
            "alert_parameters": t.mapping("alert_parameters"),
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
            "status", "opened_at", "updated_at", "source_url",
        )} | {"verified": bool(s["verified"])})
    return out


def _map_reports(hours: float = 24) -> list[dict[str, Any]]:
    """People's readings and NWS storm reports of the last day, for the app's map. People
    appear as a handle at their ZIP's centre (the site's rule); storm reports are public
    and keep their point. Notes, photos and exact places of people never leave."""
    out = []
    for s in db.recent_signals(hours, kinds=("human", "bot", "official"), limit=400):
        if s.metric.startswith("alert."):
            continue
        m = find_metric(s.metric)
        person = s.sensor_kind != "official"
        pt = opendata.public_point(s.location.zip5, s.location.county_fips) if person \
            else (round(s.location.lat, 4), round(s.location.lon, 4)) if s.location.has_point else None
        if pt is None or m is None:
            continue
        out.append({"metric": s.metric, "label": m.label,
                    "value": "" if m.is_flag or s.value is None else _short(m, s.value),
                    "who": handle(s.sensor_id) if person else "NWS storm report",
                    "official": not person, "quality": s.quality, "agree": s.reference_agreement,
                    "grid": (s.evidence.get("grid") or {}).get("verdict"), "zip5": s.location.zip5,
                    "lat": pt[0], "lon": pt[1], "at": s.observed_at.isoformat(timespec="minutes")})
    return out


def _gauges() -> list[dict[str, Any]]:
    """The latest reading of each river gauge (USGS), for the app's Gauges layer."""
    latest: dict[str, dict[str, Any]] = {}
    for s in db.recent_signals(6, kinds=("station",), limit=5000):
        site = s.evidence.get("site")
        if s.source != "usgs_water" or not site or s.metric not in ("stage_m", "discharge_cms"):
            continue
        g = latest.setdefault(site, {"site": site, "name": s.evidence.get("name"), "url": s.evidence.get("url"),
                                     "lat": round(s.location.lat, 4), "lon": round(s.location.lon, 4), "at": None})
        m = find_metric(s.metric)
        if m is not None and s.metric not in g:
            g[s.metric] = _short(m, s.value) if s.value is not None else None
            g["at"] = max(g["at"] or "", s.observed_at.isoformat(timespec="minutes"))
    return [g for g in latest.values() if "stage_m" in g or "discharge_cms" in g]


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
        **_standing(r["id"]),
    } for r in db.leaderboard(days, limit=15)]


def _standing(sensor_id: str) -> dict[str, Any]:
    """Trust v2 for the board: the 80% interval, and "new" until there's a record."""
    st = trust.overall(sensor_id)
    return {"trust_low": round(st.low, 2), "trust_high": round(st.high, 2), "checks": st.checks,
            "new": st.is_new}


def _digests() -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM digests ORDER BY date DESC LIMIT 60").fetchall()
    out = [{k: r[k] for k in ("date", "drive_url", "latest_url", "folder_url", "n_events", "n_signals",
                              "n_sensors")} for r in rows]
    if not settings.gdrive_enabled:           # Drive offline: never publish links that won't open
        for d in out:
            d["drive_url"] = d["latest_url"] = d["folder_url"] = None
    return out


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
        "local_tz": settings.local_tz,
        "bot_handle": settings.telegram_bot_handle,
        "group_url": settings.telegram_group_url,
        "github_repo": settings.github_repo,
        "site_url": settings.public_site_url,
        "contact_email": settings.network_contact_email,
        "links": ({"folder": latest["folder_url"], "latest": latest["latest_url"], "digest": latest["drive_url"]}
                  if latest and settings.gdrive_enabled else {"folder": None, "latest": None, "digest": None}),
        "vitals": db.vitals(),
        "feeds": db.feed_freshness(),                   # each source's last good run
        "reference_sizes": db.reference_network_sizes(),
        "subscriptions": db.subscription_counts(),
        "topics": _topics_doc(),
        "counties": _counties(days),
        "events": _events(days),
        "alerts": _alerts(),
        "storms": story_brief.summaries(24),
        "map": notable.build(),                         # the masthead map: notable readings + the news on them
        "reports": _map_reports(24),
        "gauges": _gauges(),
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
    core = {k: v for k, v in snap.items() if k not in ("counties", "events", "alerts", "storms", "map", "reports",
                                                        "gauges", "activity",
                                                        "graph", "leaderboard", "digests", "sources", "topics")}
    for name, payload in (("network", core), ("topics", snap["topics"]), ("counties", snap["counties"]),
                          ("events", snap["events"]), ("alerts", snap["alerts"]),
                          ("storms", snap.get("storms") or []), ("map", snap.get("map") or {}),
                          ("reports", snap.get("reports") or []),
                          ("gauges", snap.get("gauges") or []), ("activity", snap["activity"]),
                          ("graph", snap["graph"]), ("leaderboard", snap["leaderboard"]),
                          ("digests", snap["digests"]), ("sources", snap["sources"])):
        p = out_dir / f"{name}.json"
        p.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        written.append(p)
    return written


def _page_values(snap: dict[str, Any]) -> dict[str, str]:
    """Placeholders every page shares — the link-preview tags need absolute URLs."""
    return {
        "{{NETWORK_NAME}}": str(snap.get("network_name") or "Intelligence Network"),
        "{{STATE}}": {"IL": "Illinois"}.get(str(snap.get("state")), str(snap.get("state") or "")),
        "{{BOT_HANDLE}}": str(snap.get("bot_handle") or "intelligence_network_bot"),
        "{{CONTACT_EMAIL}}": str(snap.get("contact_email") or ""),
        "{{SITE_URL}}": str(snap.get("site_url") or "./"),
        "{{DATASETTE_URL}}": opendata.datasette_url() or "data/network.sqlite",
        "{{COUNTY_LINKS}}": " · ".join(
            f'<a href="county/{c["slug"]}.html">{html.escape(c["name"])}</a>' for c in snap.get("counties") or []),
    }


def copy_assets(out_dir: Path | None = None, site_dir: Path | None = None) -> list[Path]:
    """Copy `site/assets/` (icons, web manifest, link-preview card) next to the page."""
    src = (site_dir or SITE_DIR) / "assets"
    if not src.is_dir():
        return []
    dest_dir = (out_dir or DOCS_DIR) / "assets"
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for f in sorted(src.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            shutil.copyfile(f, dest_dir / f.name)
            written.append(dest_dir / f.name)
    return written


def _page_geography() -> dict[str, Any]:
    """Static geography the page draws with: county shapes (site/assets/<st>-counties.geojson,
    Census TIGERweb), the named rivers (the Mini App's reference layer, thinned to the hero
    map's scale) and ZIP → [county FIPS, lat, lon]. Inlined, not fetched, so the map and
    "Near you" also work offline and as an artifact."""
    shapes = ASSETS_DIR / f"{settings.geo_state.lower()}-counties.geojson"
    return {
        "boundaries": json.loads(shapes.read_text(encoding="utf-8")) if shapes.exists() else None,
        "rivers": _rivers(),
        "zips": {z.zip5: [z.county_fips, round(z.lat, 3), round(z.lon, 3)] for z in geo.zctas().values()},
    }


def _rivers(step: float = 0.012) -> list[dict[str, Any]]:
    """Named rivers as [lon, lat] lines, a point kept every ~1 km (a pixel on the masthead
    map) so the page carries a few dozen KB, not the app's full detail."""
    path = ASSETS_DIR / f"{settings.geo_state.lower()}-reference.json"
    try:
        ref = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for r in ref.get("rivers") or []:
        lines = []
        for line in r.get("lines") or []:
            kept = [line[0]]
            for pt in line[1:-1]:
                if abs(pt[0] - kept[-1][0]) + abs(pt[1] - kept[-1][1]) >= step:
                    kept.append(pt)
            if len(line) > 1:
                kept.append(line[-1])
            if len(kept) > 1:
                lines.append([[round(x, 3), round(y, 3)] for x, y in kept])
        if lines:
            out.append({"name": r.get("name"), "lines": lines})
    return out


def _page_data(snap: dict[str, Any]) -> str:
    return json.dumps({**snap, **_page_geography()}, default=str).replace("</", "<\\/")


def build_site(snap: dict[str, Any], fragment: Path | None = None, out: Path | None = None) -> Path:
    """Wrap the fragment as a full HTML document with the snapshot inlined."""
    fragment = fragment or FRAGMENT
    out = out or (DOCS_DIR / "index.html")
    html = fragment.read_text(encoding="utf-8")
    for key, val in _page_values(snap).items():
        html = html.replace(key, val)
    data = _page_data(snap)
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


DIGEST_PAGE = "digest.html"


def write_digest_page(snap: dict[str, Any], out_dir: Path | None = None, hours: float = 24.0) -> Path:
    """Today's digest as a standalone page, for the site to embed and anyone to print.

    The document itself lives in Drive; this is the same rendering served next to
    the site, so the page works even while Drive publishing is off.
    """
    from intelnet import digest as digest_module

    model = digest_module.build(hours=hours)
    links = snap.get("links") or {}
    downloads = {label: url for label, url in (
        ("Google Doc", links.get("latest")), ("All formats", links.get("folder"))) if url}
    body = digest_module.render_html(model, downloads=downloads or None, page=True)
    out = (out_dir or DOCS_DIR) / DIGEST_PAGE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f'<title>{model.network_name} — digest {model.date}</title>\n'
        '<link rel="icon" href="assets/icon.svg" type="image/svg+xml">\n'
        + digest_module.FONTS_LINK + '\n</head>\n<body>\n' + body + "\n</body>\n</html>\n", encoding="utf-8")
    return out


STATIC_PAGES = ("privacy.html", "terms.html")


def render_static_pages(snap: dict[str, Any], out_dir: Path, site_dir: Path | None = None) -> list[Path]:
    """Copy the policy pages next to index.html, filling in the network's details."""
    site_dir = site_dir or FRAGMENT.parent
    values = _page_values(snap)
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
    return html.replace(DATA_MARK, _page_data(snap), 1)


def export_all(out_dir: Path | None = None, days: int = 14, *, site: bool = True,
               sample: bool = False) -> dict[str, Any]:
    out_dir = out_dir or DOCS_DIR          # resolved at call time (tests repoint DOCS_DIR)
    snap = snapshot(days, sample=sample)
    files = write_json(snap, out_dir / "data")
    result: dict[str, Any] = {"json": [str(p) for p in files]}
    if site and FRAGMENT.exists():
        result["site"] = str(build_site(snap, out=out_dir / "index.html"))
        result["pages"] = [str(p) for p in render_static_pages(snap, out_dir)]
        result["assets"] = [str(p) for p in copy_assets(out_dir)]
        result["digest_page"] = str(write_digest_page(snap, out_dir))
        counties = render_county_pages(snap, out_dir)
        result["county_pages"] = len(counties)
        app = render_app_page(snap, out_dir)
        result["app"] = str(app) if app else None
        result["opendata"] = opendata.write_all(out_dir)
        result["sitemap"] = str(write_sitemap(snap, out_dir, counties))
    return result


# ── county pages: one per county, for the people who live there and for search ──

def _tokens(fragment: Path | None = None) -> str:
    """The front page's color and type tokens (between its token markers), so every page matches."""
    m = re.search(r"/\* tokens:.*?\*/(.*?)/\* end tokens \*/", (fragment or FRAGMENT).read_text(encoding="utf-8"), re.DOTALL)
    return m.group(1) if m else ""


def _neighbors(c: geo.County, n: int = 6, within_km: float = 90.0) -> list[geo.County]:
    near = sorted((o for o in geo.counties().values() if o.fips != c.fips),
                  key=lambda o: geo.haversine_km(c.lat, c.lon, o.lat, o.lon))
    return [o for o in near[:n] if geo.haversine_km(c.lat, c.lon, o.lat, o.lon) <= within_km]


def _short(metric: Any, value: float) -> str:
    """'71.1 °F' out of '71.1 °F (21.7 degC)'; '96 %' rather than '96 pct'."""
    return metric.display(value).split(" (")[0].replace(" pct", "%")


def _readings(c: geo.County, row: dict[str, Any], mesh: list[Any], events: list[dict[str, Any]], days: int) -> str:
    e = html.escape
    parts = [(f'<p class="muted">{row.get("human", 0):,} readings from people and {row.get("reference", 0):,} '
              f'official readings here in the last {days} days.</p>')]
    trs = []
    for r in mesh:
        m = find_metric(r["metric"])
        if m is None:
            continue
        if m.is_flag:
            value = f'{r["n"]} report{"s" if r["n"] != 1 else ""}'
        else:
            lo, hi = _short(m, r["min_value"]), _short(m, r["max_value"])
            value = hi if lo == hi else f"{lo} to {hi}"
        who = f' · {r["n_human"]} from people' if r["n_human"] else ""
        trs.append(f"<tr><td>{e(m.label)}{e(who)}</td><td>{e(value)}</td></tr>")
    if trs:
        parts.append('<p style="margin:10px 0 4px"><b>The last day</b></p><table class="facts">' + "".join(trs[:12]) + "</table>")
    mine = [x for x in events if x.get("county_fips") == c.fips]
    if mine:
        parts.append('<p style="margin:12px 0 4px"><b>Network events</b></p><ul style="margin:0;padding-left:18px">' + "".join(
            f'<li>{e(x.get("title") or "")}{" · verified" if x.get("verified") else ""}</li>' for x in mine[:8]) + "</ul>")
    return "".join(parts)


AIR_KM = 40                     # a county without a monitor reads the nearest within this


def _air(c: geo.County, now: datetime) -> str:
    """The latest PM2.5 and ozone at the county's monitor, else the nearest within AIR_KM, each
    with the county it was read in when that's another: 'PM2.5 7.2 µg/m³ (AQI 40, good) in Cook
    County · Ozone 31 ppb (AQI 29, good)'."""
    bits: list[tuple[str, str]] = []
    for key in ("pm25_ugm3", "ozone_ppb"):
        m = find_metric(key)
        near = [s for s in db.signals_near(key, c.lat, c.lon, AIR_KM, now - timedelta(hours=3), now,
                                           kinds=("station",)) if s.source == "airnow"]
        if m is None or not near:
            continue
        s = min(near, key=lambda x: (geo.haversine_km(c.lat, c.lon, x.location.lat, x.location.lon),
                                     -x.observed_at.timestamp()))
        there = geo.county(s.location.county_fips)
        bits.append((f"{m.short or m.label} {m.display(s.value)}",
                     f" in {there.name} County" if there and there.fips != c.fips else ""))
    places = {where for _, where in bits}
    if len(places) == 1:                              # one place for both: say it once
        return " · ".join(text for text, _ in bits) + places.pop()
    return " · ".join(text + where for text, where in bits)


def _rain(c: geo.County, now: datetime) -> str:
    """The county's CoCoRaHS volunteers' latest day totals: 'up to 1.45 in in the 24 hours to
    7 AM, from 12 volunteers'."""
    m = find_metric("rain_mm")
    rows = [s for s in db.recent_signals(30, county_fips=c.fips, metric="rain_mm", limit=500)
            if s.source == "cocorahs" and s.value is not None]
    if m is None or not rows:
        return ""
    top = max(rows, key=lambda s: s.value)
    n = len({s.sensor_id for s in rows})
    return (f"up to {m.display(top.value)} in the 24 hours to {local_time(top.observed_at, '%-I %p')}, "
            f"from {n} CoCoRaHS volunteer{'s' if n != 1 else ''}")


def _today(c: geo.County, gauges: list[rivers.Gauge], snapshot_at: str, now: datetime) -> str:
    """The county's day on its page: the NWS forecast and outlook risks, rivers running high
    nearby, the air, the volunteers' rain. Static like the page (rebuilt nightly), so it says
    when, and links the live forecast."""
    e = html.escape
    day = ahead.for_point(c.lat, c.lon, now=now)
    lines = [f"<p>{ahead.icon(p)} <b>{e(p.name)}</b>: {e(p.text())}</p>" for p in day.periods]
    lines += [f"<p>{r.emoji} <b>{e(r.name)}</b>: {e(r.text())}</p>" for r in day.risks]
    lines += [f"<p>🌊 <b>{e(g.name)}</b>: {e(g.text())}</p>" for g in rivers.near(gauges, c)[:3]]
    if air := _air(c, now):
        lines.append(f"<p>🌫 <b>Air</b>: {e(air)}</p>")
    if rain := _rain(c, now):
        lines.append(f"<p>🌧 <b>Rain</b>: {e(rain)}</p>")
    if not lines:
        return ""
    live = f"https://forecast.weather.gov/MapClick.php?lat={c.lat:.4f}&lon={c.lon:.4f}"
    return (f'<section class="card today" aria-labelledby="today-h"><h2 id="today-h">Today in {e(c.name)} '
            f'County</h2>{"".join(lines)}<p class="muted src">Forecast and rivers from the National Weather '
            f"Service, air from EPA AirNow, rain from CoCoRaHS volunteers; as of {e(snapshot_at)}. "
            f'<a href="{e(live)}">The latest forecast</a></p></section>')


def _subscribe(c: geo.County, handle: str) -> tuple[str, str]:
    """The four subscriptions most people want, as cards; every other topic as a chip."""
    e = html.escape
    packs = topics()

    def link(topic: str, cat: str) -> str:
        return e(f"https://t.me/{handle}?start=sub_{topic}_{cat}_{'il' if cat == 'digest' else c.slug}")

    main = [("weather", "warnings", "Warnings", True), ("weather", "alerts", "Every NWS alert", False),
            ("weather", "events", "Weather events", False), ("weather", "digest", "Morning brief", False)]
    cards = "".join(
        f'<a class="sub{" primary" if primary else ""}" href="{link(t, cat)}" target="_blank" rel="noopener">'
        f'<b>{e(label)}</b><span>{e(packs[t].categories[cat])}</span></a>'
        for t, cat, label, primary in main if t in packs and cat in packs[t].categories)
    more = [("weather", "reports", "Every report")] + [
        (t.name, "events", t.label) for t in packs.values() if t.name != "weather" and "events" in t.categories]
    chips = "".join(f'<a href="{link(t, cat)}" target="_blank" rel="noopener" title="{e(packs[t].categories[cat])}">{e(label)}</a>'
                    for t, cat, label in more if t in packs and cat in packs[t].categories)
    return cards, chips


def render_county_pages(snap: dict[str, Any], out_dir: Path, template: Path | None = None) -> list[Path]:
    """docs/county/<slug>.html for every county: live alerts, a map of the county and its
    neighbors with radar, one-tap subscribe links, its readings, and nearby counties."""
    template = template or COUNTY_TEMPLATE
    if not template.exists():
        return []
    page = template.read_text(encoding="utf-8").replace("{{TOKENS}}", _tokens())
    geography = _page_geography()
    shapes = {f["properties"]["fips"]: f for f in ((geography.get("boundaries") or {}).get("features") or [])}
    rows = {r["fips"]: r for r in snap.get("counties") or []}
    mesh: dict[str, list[Any]] = defaultdict(list)
    for r in db.mesh(24):
        mesh[r["county_fips"]].append(r)
    handle = str(snap.get("bot_handle") or "intelligence_network_bot")
    site = str(snap.get("site_url") or "")
    at = datetime.fromisoformat(str(snap.get("generated_at"))) if snap.get("generated_at") else utcnow()
    snapshot_at = local_time(at, "%a %-d %b, %-I:%M %p %Z")
    e = html.escape
    written = []
    gauges = rivers.high_water()                      # one survey for every county's page
    now = utcnow()
    for c in sorted(geo.counties().values(), key=lambda x: x.name):
        near = _neighbors(c)
        cards, chips = _subscribe(c, handle)
        mine = [a for a in snap.get("alerts") or [] if c.name in (a.get("counties") or [])]
        status = (f"No NWS alerts for {c.name} County at the last snapshot." if not mine else
                  f"{len(mine)} NWS alert{'s' if len(mine) != 1 else ''} in effect for {c.name} County at the last snapshot.")
        static_alerts = ("".join(f'<div class="alert {e(str(a.get("severity") or ""))}"><i></i><div><b>{e(str(a.get("event") or ""))}</b></div></div>'
                                 for a in mine) or '<p class="muted">None in effect at the last snapshot.</p>')
        data = {"fips": c.fips, "name": c.name, "slug": c.slug, "local_tz": snap.get("local_tz"),
                "generated_at": snap.get("generated_at"),
                "alerts": [{**a, "fips": [c.fips]} for a in mine],
                "shapes": {"type": "FeatureCollection",
                           "features": [shapes[x.fips] for x in [c, *near] if x.fips in shapes]}}
        values = {
            "{{COUNTY}}": e(c.name), "{{NETWORK_NAME}}": e(str(snap.get("network_name") or "Intelligence Network")),
            "{{DESCRIPTION}}": e(f"Today's forecast, live NWS alerts, weather radar, river levels, air quality and "
                                 f"neighbors' readings for {c.name} County, Illinois. Get {c.name} County "
                                 "warnings in Telegram with one tap."),
            "{{TODAY}}": _today(c, gauges, snapshot_at, now),
            "{{CANONICAL}}": e(f"{site}county/{c.slug}.html"), "{{SITE_URL}}": e(site),
            "{{STATIC_STATUS}}": e(status), "{{SNAPSHOT_AT}}": e(snapshot_at), "{{STATIC_ALERTS}}": static_alerts,
            "{{SUBSCRIBE}}": cards, "{{SUBSCRIBE_MORE}}": chips,
            "{{READINGS}}": _readings(c, rows.get(c.fips, {}), mesh.get(c.fips, []), snap.get("events") or [],
                                      int(snap.get("days") or 14)),
            "{{NEIGHBORS}}": "".join(f'<a href="{x.slug}.html">{e(x.name)}</a>' for x in near),
            # the flyer and the scan box: one QR, straight to this county's warnings
            "{{JOIN_URL}}": e(f"https://t.me/{handle}?start=sub_weather_warnings_{c.slug}"),
            "{{BOT_HANDLE}}": e(handle), "{{COUNTY_SLUG}}": e(c.slug),
            "{{PAGE_URL}}": e(f"{site}county/{c.slug}.html".split("://", 1)[-1] if site else ""),
            "{{TEAR_TABS}}": f"<span><b>{e(c.name)} Co. warnings</b><br>t.me/{e(handle)}</span>" * 8,
        }
        doc = page
        for key, val in values.items():
            doc = doc.replace(key, val)
        doc = doc.replace(COUNTY_MARK, json.dumps(data, default=str).replace("</", "<\\/"), 1)
        dest = out_dir / "county" / f"{c.slug}.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(doc, encoding="utf-8")
        written.append(dest)
    return written


APP_TEMPLATE = SITE_DIR / "app.fragment.html"


def render_app_page(snap: dict[str, Any], out_dir: Path, template: Path | None = None) -> Path | None:
    """docs/app/index.html: the Telegram Mini App (map, report composer, settings). It
    reads ../data/*.json and the NWS live; the chat's own state arrives in the URL's
    #fragment from the bot, and changes go back through Telegram (WebApp.sendData)."""
    template = template or APP_TEMPLATE
    if not template.exists():
        return None
    page = template.read_text(encoding="utf-8").replace("{{TOKENS}}", _tokens())
    for key, val in _page_values(snap).items():
        page = page.replace(key, html.escape(val) if key != "{{COUNTY_LINKS}}" else "")
    dest = out_dir / "app" / "index.html"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(page, encoding="utf-8")
    return dest


def write_sitemap(snap: dict[str, Any], out_dir: Path, county_pages: list[Path]) -> Path | None:
    """sitemap.xml + robots.txt, so search finds the front page and every county page."""
    site = str(snap.get("site_url") or "")
    if not site.startswith("http"):
        return None
    today = utcnow().strftime("%Y-%m-%d")
    paths = ["", *STATIC_PAGES, *(f"county/{p.name}" for p in county_pages)]
    body = "".join(f"<url><loc>{html.escape(site + p)}</loc><lastmod>{today}</lastmod></url>" for p in paths)
    dest = out_dir / "sitemap.xml"
    dest.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>\n', encoding="utf-8")
    (out_dir / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {site}sitemap.xml\n", encoding="utf-8")
    return dest


def git_push_docs(message: str | None = None) -> str:
    """Commit + push docs/ (only when SITE_AUTO_PUSH). Best-effort: "pushed", "nothing" (no
    change), or "failed" (logged; the nightly check tells the admin)."""
    import subprocess

    msg = message or f"site: snapshot {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    try:
        subprocess.run(["git", "add", "docs"], cwd=PROJECT_ROOT, check=True, capture_output=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PROJECT_ROOT)
        if diff.returncode == 0:
            return "nothing"
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=PROJECT_ROOT, check=True, capture_output=True)
        subprocess.run(["git", "push", "-q"], cwd=PROJECT_ROOT, check=True, capture_output=True)
        return "pushed"
    except subprocess.CalledProcessError as exc:
        logger.warning("export: %s failed: %s", " ".join(exc.cmd[:2]), (exc.stderr or b"").decode(errors="replace")[:300])
        return "failed"
    except Exception as exc:  # noqa: BLE001
        logger.warning("export: pushing docs/ failed (%s)", type(exc).__name__)
        return "failed"
