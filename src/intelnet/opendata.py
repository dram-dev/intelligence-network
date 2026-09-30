"""Open data out: the network as a source, not only a consumer.

Every nightly export writes, next to the site:

    feeds/events.geojson   verified network events of the last week (points)
    feeds/storms.geojson   storms (stories.py) of the last week, with their briefs
    feeds/cap.atom         the same events as CAP 1.2 messages in an Atom feed, the
                           format emergency managers' and IEM's tools already read
    data/network.sqlite    a researcher's database: people's readings, NWS storm
                           reports, events, storms, alerts, sensors; open it in the
                           browser with Datasette Lite (no server)
    data/metadata.json     table descriptions for Datasette

Privacy is the same as the site's: people appear as `s-xxxxx` handles with ZIP5 and
county only. A place that came from a person (an event's first reading, a storm's
centre) is published as its ZIP or county centre, never the point itself. Free-text
notes, photos, names, chat ids and ZIP+4 never leave. Station and river-gauge
readings are left to their own publishers (IEM, USGS), which keeps the file small
enough to commit every night.
"""
from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from intelnet import db, geo, network, story_brief
from intelnet.config import settings
from intelnet.models import KIND_OFFICIAL, REFERENCE_KINDS, parse_iso, public_handle, utcnow
from intelnet.topics import find_metric, get_topic, topics

CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"
ATOM_NS = "http://www.w3.org/2005/Atom"
FEED_DAYS = 7
DB_DAYS = 90
MAX_DB_BYTES = 20_000_000


def public_point(zip5: str | None, fips: str | None) -> tuple[float, float] | None:
    """Where the public may see something a person reported: its ZIP's centre, else its county's."""
    z = geo.zcta(zip5) if zip5 else None
    if z is not None:
        return round(z.lat, 4), round(z.lon, 4)
    c = geo.county(fips)
    return (round(c.lat, 4), round(c.lon, 4)) if c else None


def verified_events(days: int = FEED_DAYS) -> list[dict[str, Any]]:
    return [e for e in (network.event_summary(r) for r in db.events_since(days * 24, limit=500)) if e["verified"]]


def _county_page(fips: str | None) -> str | None:
    c = geo.county(fips)
    return f"{settings.public_site_url}county/{c.slug}.html" if c and settings.public_site_url else None


def events_geojson(days: int = FEED_DAYS) -> dict[str, Any]:
    features = []
    for e in verified_events(days):
        pt = public_point(e.get("zip5"), e.get("county_fips"))
        if pt is None:
            continue
        c = geo.county(e.get("county_fips"))
        features.append({"type": "Feature", "id": e["id"],
                         "geometry": {"type": "Point", "coordinates": [pt[1], pt[0]]},
                         "properties": {
                             "topic": e["topic"], "metric": e["metric"], "label": e["metric_label"],
                             "title": e["title"], "peak": e.get("peak_value"), "unit": e.get("unit"),
                             "peak_display": e["peak_display"], "county": c.name if c else None,
                             "county_fips": e.get("county_fips"), "zip5": e.get("zip5"),
                             "opened_at": e.get("opened_at"), "updated_at": e.get("updated_at"),
                             "status": e.get("status"), "sensors": e.get("n_sensors"),
                             "official_sources": e.get("n_reference"), "severity": e.get("severity"),
                             "score": e.get("score"), "story": db.story_of("event", str(e["id"])),
                             "url": _county_page(e.get("county_fips"))}})
    return {"type": "FeatureCollection", "generated_at": utcnow().isoformat(timespec="seconds"),
            "source": settings.public_site_url, "features": features}


def storms_geojson(days: int = FEED_DAYS) -> dict[str, Any]:
    features = []
    for s in story_brief.summaries(days * 24, limit=100):
        pts = []
        for m in db.story_members(s["id"]):
            if m["kind"] == "event":
                ev = db.event_by_id(int(m["ref"]))
                pt = public_point(ev["zip5"], ev["county_fips"]) if ev else None
            else:                                   # an NWS polygon's centre: public already
                pt = (round(m["lat"], 4), round(m["lon"], 4))
            if pt:
                pts.append(pt)
        if not pts:
            continue
        lat, lon = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        features.append({"type": "Feature", "id": s["id"],
                         "geometry": {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]},
                         "properties": {k: s[k] for k in ("topic", "title", "severity", "status", "counties",
                                                          "fips", "opened_at", "updated_at", "brief", "facts")}})
    return {"type": "FeatureCollection", "generated_at": utcnow().isoformat(timespec="seconds"),
            "source": settings.public_site_url, "features": features}


# ── CAP 1.2 in Atom ───────────────────────────────────────────────────────

def _cap_time(dt: datetime | None) -> str:
    """CAP wants an offset, never 'Z': UTC is written -00:00."""
    dt = (dt or utcnow()).astimezone(timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "-00:00"


def _sub(parent: ET.Element, tag: str, text: str | None = None, ns: str = CAP_NS) -> ET.Element:
    el = ET.SubElement(parent, f"{{{ns}}}{tag}")
    if text is not None:
        el.text = str(text)
    return el


def _sender() -> str:
    return settings.network_contact_email or (settings.public_site_url or "intelnet").split("//")[-1].strip("/")


def cap_alert(e: dict[str, Any]) -> ET.Element:
    """One verified network event as a CAP 1.2 message. Each version has its own
    identifier; `incidents` ties the versions of one event together."""
    updated = parse_iso(e.get("updated_at")) or utcnow()
    opened = parse_iso(e.get("opened_at")) or updated
    host = _sender().split("@")[-1]
    alert = ET.Element(f"{{{CAP_NS}}}alert")
    _sub(alert, "identifier", f"{host}.event.{e['id']}.{updated.strftime('%Y%m%dT%H%M%S')}")
    _sub(alert, "sender", _sender())
    _sub(alert, "sent", _cap_time(updated))
    _sub(alert, "status", "Actual")
    _sub(alert, "msgType", "Alert")
    _sub(alert, "scope", "Public")
    _sub(alert, "note", "Citizen sensor network observation, verified by the network. Not an official warning.")
    _sub(alert, "incidents", f"{host}.event.{e['id']}")
    info = _sub(alert, "info")
    _sub(info, "language", "en-US")
    _sub(info, "category", get_topic(e["topic"]).cap_category)
    _sub(info, "event", f"{e['metric_label']} (network observation)")
    _sub(info, "urgency", "Past" if e.get("status") == "closed" else "Unknown")
    sev = float(e.get("severity") or 0)
    _sub(info, "severity", "Severe" if sev >= 0.8 else "Moderate" if sev >= 0.5 else "Minor")
    _sub(info, "certainty", "Observed")
    _sub(info, "effective", _cap_time(opened))
    _sub(info, "onset", _cap_time(opened))
    _sub(info, "expires", _cap_time(updated + timedelta(hours=network.EVENT_IDLE_CLOSE_HOURS)))
    _sub(info, "senderName", settings.network_name)
    _sub(info, "headline", e["title"])
    people = max(0, int(e.get("n_sensors") or 0) - int(e.get("n_reference") or 0))
    official = int(e.get("n_reference") or 0)
    _sub(info, "description", f"{e['metric_label']} peaking at {e['peak_display'].split(' (')[0]}, "
                              f"reported by {people} {'person' if people == 1 else 'people'}"
                              + (f" and {official} official source{'s' if official != 1 else ''}" if official else "")
                              + ", checked against each other and official sources by the network.")
    web = _county_page(e.get("county_fips"))
    if web:
        _sub(info, "web", web)
    for name, value in (("NetworkEventID", e["id"]), ("Metric", e["metric"]),
                        ("Peak", f"{e.get('peak_value')} {e.get('unit') or ''}".strip()),
                        ("Sensors", e.get("n_sensors")), ("OfficialSources", official),
                        ("Score", e.get("score"))):
        p = _sub(info, "parameter")
        _sub(p, "valueName", name)
        _sub(p, "value", value)
    area = _sub(info, "area")
    c = geo.county(e.get("county_fips"))
    _sub(area, "areaDesc", f"{c.name} County, {settings.geo_state}" if c else settings.geo_state)
    pt = public_point(e.get("zip5"), e.get("county_fips"))
    m = find_metric(e["metric"])
    if pt:
        _sub(area, "circle", f"{pt[0]},{pt[1]} {m.radius_km if m else 10}")
    if c:
        for name in ("SAME", "FIPS6"):
            g = _sub(area, "geocode")
            _sub(g, "valueName", name)
            _sub(g, "value", "0" + c.fips)
    return alert


def cap_atom(days: int = 2) -> bytes:
    """The last `days` of verified events as an Atom feed of CAP messages."""
    ET.register_namespace("", ATOM_NS)
    ET.register_namespace("cap", CAP_NS)
    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    site = settings.public_site_url or "./"
    _sub(feed, "id", f"{site}feeds/cap.atom", ATOM_NS)
    _sub(feed, "title", f"{settings.network_name}: verified observations (CAP 1.2)", ATOM_NS)
    _sub(feed, "updated", utcnow().isoformat(timespec="seconds"), ATOM_NS)
    link = _sub(feed, "link", ns=ATOM_NS)
    link.set("rel", "self")
    link.set("href", f"{site}feeds/cap.atom")
    author = _sub(feed, "author", ns=ATOM_NS)
    _sub(author, "name", settings.network_name, ATOM_NS)
    for e in verified_events(days):
        alert = cap_alert(e)
        entry = _sub(feed, "entry", ns=ATOM_NS)
        _sub(entry, "id", f"urn:cap:{alert.find(f'{{{CAP_NS}}}identifier').text}", ATOM_NS)
        _sub(entry, "title", e["title"], ATOM_NS)
        _sub(entry, "updated", (parse_iso(e.get("updated_at")) or utcnow()).isoformat(timespec="seconds"), ATOM_NS)
        content = _sub(entry, "content", ns=ATOM_NS)
        content.set("type", "application/cap+xml")
        content.append(alert)
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


# ── the researcher's database ─────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE readings (id INTEGER PRIMARY KEY, topic TEXT, metric TEXT, value REAL, unit TEXT,
    observed_at TEXT, received_at TEXT, sensor TEXT, sensor_kind TEXT, county_fips TEXT, zip5 TEXT,
    lat REAL, lon REAL, quality TEXT, corroboration_n INTEGER, contradiction_n INTEGER,
    reference_agreement TEXT, event_id INTEGER, grid_verdict TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, topic TEXT, metric TEXT, county_fips TEXT, zip5 TEXT,
    lat REAL, lon REAL, opened_at TEXT, updated_at TEXT, closed_at TEXT, peak_value REAL, unit TEXT,
    n_signals INTEGER, n_sensors INTEGER, n_reference INTEGER, severity REAL, score REAL,
    status TEXT, title TEXT, story_id INTEGER);
CREATE TABLE storms (id INTEGER PRIMARY KEY, topic TEXT, status TEXT, opened_at TEXT, updated_at TEXT,
    closed_at TEXT, counties TEXT, severity REAL, title TEXT, brief TEXT);
CREATE TABLE alerts (id TEXT PRIMARY KEY, event TEXT, severity TEXT, counties TEXT, opened_at TEXT,
    ended_at TEXT, ended_reason TEXT, status TEXT);
CREATE TABLE sensors (sensor TEXT PRIMARY KEY, kind TEXT, county_fips TEXT, n_signals INTEGER,
    n_corroborated INTEGER, n_contradicted INTEGER, trust REAL);
CREATE TABLE counties (fips TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL);
CREATE TABLE metrics (key TEXT PRIMARY KEY, topic TEXT, label TEXT, unit TEXT, kind TEXT);
CREATE INDEX readings_metric ON readings(metric, observed_at);
CREATE INDEX readings_county ON readings(county_fips);
"""

METADATA_TABLES = {
    "readings": "One row per reading: people's (sensor = an s-xxxxx handle; ZIP5 and county only, "
                "no coordinates) and official NWS storm reports (exact points). Values are in the "
                "metric's canonical unit (see metrics). quality: raw, corroborated, flagged, rejected, reference.",
    "events": "Readings past a threshold, per county and metric. Places are ZIP or county centres.",
    "storms": "Events and storm-based warnings clustered per storm, with the network's brief.",
    "alerts": "NWS alerts the network tracked (one row per alert, all its updates folded in).",
    "sensors": "Who reported: people as handles with a home county; official sources by id.",
    "counties": "Illinois counties (Census).",
    "metrics": "The common data language: every metric's key, label and canonical unit.",
}


def _handle(sensor_id: str, kind: str) -> str:
    return sensor_id if kind in REFERENCE_KINDS else public_handle(sensor_id)


def write_sqlite(path: Path, days: int = DB_DAYS) -> Path | None:
    """Build the public researcher database from scratch (never a copy of the live one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    since = (utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    out = sqlite3.connect(tmp)
    out.executescript(_SCHEMA)
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM signals WHERE observed_at >= ? AND metric NOT LIKE 'alert.%'
                 AND (sensor_kind IN ('human', 'bot') OR sensor_kind = ?) ORDER BY id""",
            (since, KIND_OFFICIAL)).fetchall()
        for r in rows:
            person = r["sensor_kind"] not in REFERENCE_KINDS
            grid = (json.loads(r["evidence_json"] or "{}").get("grid") or {}).get("verdict")
            out.execute("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r["id"], r["topic"], r["metric"], r["value"], r["unit"], r["observed_at"], r["received_at"],
                _handle(r["sensor_id"], r["sensor_kind"]), r["sensor_kind"], r["county_fips"], r["zip5"],
                None if person else r["lat"], None if person else r["lon"], r["quality"], r["corroboration_n"],
                r["contradiction_n"], r["reference_agreement"], r["event_id"], grid))
        for e in conn.execute("SELECT * FROM events WHERE updated_at >= ?", (since,)).fetchall():
            pt = public_point(e["zip5"], e["county_fips"]) or (None, None)
            out.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                e["id"], e["topic"], e["metric"], e["county_fips"], e["zip5"], pt[0], pt[1], e["opened_at"],
                e["updated_at"], e["closed_at"], e["peak_value"], e["unit"], e["n_signals"], e["n_sensors"],
                e["n_reference"], e["severity"], e["score"], e["status"], e["title"],
                db.story_of("event", str(e["id"]))))
        for s in conn.execute("SELECT * FROM stories WHERE updated_at >= ? AND merged_into IS NULL", (since,)):
            out.execute("INSERT INTO storms VALUES (?,?,?,?,?,?,?,?,?,?)", (
                s["id"], s["topic"], s["status"], s["opened_at"], s["updated_at"], s["closed_at"],
                s["counties_json"], s["severity"], s["title"], story_brief.current(int(s["id"]))))
        for t in conn.execute("SELECT * FROM alert_threads WHERE opened_at >= ?", (since,)):
            out.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?)", (
                t["id"], t["event"], t["severity"], t["counties_json"], t["opened_at"], t["ended_at"],
                t["ended_reason"], t["status"]))
        for s in conn.execute("SELECT * FROM sensors WHERE status = 'active' AND (kind IN ('human', 'bot') OR kind = ?)",
                              (KIND_OFFICIAL,)):
            out.execute("INSERT INTO sensors VALUES (?,?,?,?,?,?,?)", (
                _handle(s["id"], s["kind"]), s["kind"], s["county_fips"], s["n_signals"], s["n_corroborated"],
                s["n_contradicted"], round(s["trust"] or 0.5, 3)))
    out.executemany("INSERT INTO counties VALUES (?,?,?,?)",
                    [(c.fips, c.name, c.lat, c.lon) for c in geo.counties().values()])
    out.executemany("INSERT INTO metrics VALUES (?,?,?,?,?)",
                    [(m.key, t.name, m.label, m.unit, m.kind) for t in topics().values() for m in t.metrics.values()])
    out.commit()
    out.execute("VACUUM")
    out.close()
    if tmp.stat().st_size > MAX_DB_BYTES:            # the repo takes a copy every night; keep it lean
        tmp.unlink()
        return None
    tmp.replace(path)
    return path


def metadata() -> dict[str, Any]:
    site = settings.public_site_url
    return {"title": f"{settings.network_name}: open data",
            "description": "A citizen sensor network's readings, events and storms. People appear only as "
                           "handles with a ZIP and county.",
            "source": settings.network_name, "source_url": site,
            "databases": {"network": {"tables": {k: {"description": v} for k, v in METADATA_TABLES.items()}}}}


def datasette_url() -> str | None:
    site = settings.public_site_url
    if not site.startswith("https://"):
        return None
    return f"https://lite.datasette.io/?url={site}data/network.sqlite&metadata={site}data/metadata.json"


def write_all(out_dir: Path) -> dict[str, Any]:
    """Every open-data file, into the site directory (docs/)."""
    feeds = out_dir / "feeds"
    feeds.mkdir(parents=True, exist_ok=True)
    (feeds / "events.geojson").write_text(json.dumps(events_geojson(), default=str), encoding="utf-8")
    (feeds / "storms.geojson").write_text(json.dumps(storms_geojson(), default=str), encoding="utf-8")
    (feeds / "cap.atom").write_bytes(cap_atom())
    data = out_dir / "data"
    sqlite_path = write_sqlite(data / "network.sqlite")
    (data / "metadata.json").write_text(json.dumps(metadata(), indent=1), encoding="utf-8")
    return {"feeds": 3, "sqlite": str(sqlite_path) if sqlite_path else None}
