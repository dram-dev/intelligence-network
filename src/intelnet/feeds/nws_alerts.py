"""NWS active alerts for the state — one signal per (alert, county), threaded.

api.weather.gov/alerts/active?area=IL, asked with the last ETag (the API answers
200 regardless today; a 304 would skip the work). Each alert carries SAME codes
(0 + county FIPS) so it maps straight onto the county hierarchy; a signal is
emitted per covered county so ZIP/county subscribers match naturally.

Alerts change after they are issued: NWS sends updates (a new CAP id whose
`references` name the earlier ones) and ends some early. Every CAP message is
linked into a **thread** that starts at the first message we saw, and all of a
thread's rows share `group_key` = the thread id, so an alert is counted, listed
and pushed once however often it is updated. A newer version retires the rows
it replaces (they stop counting as active); subscribers' cards are edited in
place and re-notified only when the impact rises (`rises`). `/alerts/active`
never lists cancellations: an alert that ends early simply disappears. So a
thread ends when it expires, or once it has been missing from the feed for
ABSENT_CONFIRM, and its cards are closed.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import requests

from intelnet import db, feedback, geo, network, subscriptions
from intelnet.config import settings
from intelnet.feeds.base import FeedResult, ReferenceFeed
from intelnet.models import KIND_AUTHORITY, Signal, iso, parse_iso, utcnow
from intelnet.topics import get_topic

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}
ETAG_KEY = "nws_alerts:etag"
# Missing from the feed this long (across successful fetches) = ended early.
ABSENT_CONFIRM = timedelta(seconds=90)
# An empty feed while this many alerts are still in force is a glitch, not an all-clear.
SUSPECT_EMPTY = 3


def slug(event: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (event or "").lower()).strip("_")


def _sender_id(sender: str) -> str:
    return "nws:" + (re.sub(r"[^a-z0-9]+", "_", (sender or "nws").lower()).strip("_") or "nws")


def parse_impact(params: dict[str, Any], topic: str = "weather") -> dict[str, Any]:
    """The pack's `alert_parameters` read off one alert: {'maxHailSize': 1.75, …}.

    Numbers come as text ("Up to .75", "70 MPH"); 0 means none and is left out.
    """
    out: dict[str, Any] = {}
    for code, spec in get_topic(topic).mapping("alert_parameters").items():
        raw = (params.get(code) or [None])[0]
        if raw in (None, ""):
            continue
        if spec.get("levels"):
            out[code] = str(raw).strip().upper()
            continue
        m = re.search(r"\d*\.?\d+", str(raw))
        if m and float(m.group(0)) > 0:
            out[code] = float(m.group(0))
    return out


_MOTION = re.compile(r"^(?P<at>[^.]+)\.\.\.[^.]*\.\.\.(?P<deg>\d+)DEG\.\.\.(?P<kt>\d+)KT\.\.\.(?P<pts>.+)$")


def parse_motion(text: str | None) -> dict[str, Any] | None:
    """CAP eventMotionDescription → where the storm was, when, and how it moves.

    '2026-09-30T03:12:00-00:00...storm...262DEG...43KT...33.56,-103.17 33.13,-103.04'
    → {'at': iso, 'from_deg': 262, 'speed_kt': 43, 'points': [[33.56, -103.17], …]}.
    The direction is where the storm comes FROM (a 262° storm moves east).
    """
    m = _MOTION.match((text or "").strip())
    if not m:
        return None
    try:
        at = parse_iso(m["at"])
        points = [[float(a), float(b)] for a, b in (p.split(",") for p in m["pts"].split())]
    except (TypeError, ValueError):
        return None
    if at is None or not points:
        return None
    return {"at": iso(at), "from_deg": float(m["deg"]), "speed_kt": float(m["kt"]), "points": points}


def _rings(geometry: dict[str, Any] | None) -> list[Any] | None:
    """The warned area's outer rings ([lon, lat] pairs), when the alert has a polygon."""
    g = geometry or {}
    coords = g.get("coordinates") or []
    if g.get("type") == "Polygon" and coords:
        return [coords[0]]
    if g.get("type") == "MultiPolygon":
        return [poly[0] for poly in coords if poly] or None
    return None


def parse_alerts(payload: dict[str, Any], topic: str = "weather") -> list[Signal]:
    """GeoJSON FeatureCollection → signals (pure; no I/O). `group_key` = the CAP id
    until `assign_threads` points it at the alert's thread."""
    out: list[Signal] = []
    now = utcnow()
    for feat in payload.get("features") or []:
        p = feat.get("properties") or {}
        alert_id = p.get("id") or feat.get("id")
        if not alert_id:
            continue
        same_codes = ((p.get("geocode") or {}).get("SAME")) or []
        fips_list = [f for f in (geo.same_to_fips(s) for s in same_codes) if f]
        if not fips_list:
            continue
        sent = parse_iso(p.get("sent")) or now
        expires = parse_iso(p.get("ends")) or parse_iso(p.get("expires"))
        params = p.get("parameters") or {}
        sev = str(p.get("severity") or "Unknown")
        evidence = {
            "kind": "alert",
            "alert_id": str(alert_id),
            "event": p.get("event"),
            "severity": sev,
            "urgency": p.get("urgency"),
            "certainty": p.get("certainty"),
            "sender": p.get("senderName"),
            "nws_headline": (params.get("NWSheadline") or [None])[0],
            "headline": p.get("headline"),
            "instruction": p.get("instruction"),
            "description": (p.get("description") or "")[:1500],
            "area_desc": p.get("areaDesc"),
            "url": p.get("@id") or f"https://api.weather.gov/alerts/{alert_id}",
            "message_type": p.get("messageType"),
            "references": [str(r["identifier"]) for r in p.get("references") or []
                           if isinstance(r, dict) and r.get("identifier")],
            "impact": parse_impact(params, topic),
            # storm-based warnings: the warned polygon and the storm's motion
            "polygon": _rings(feat.get("geometry")),
            "motion": parse_motion((params.get("eventMotionDescription") or [None])[0]),
        }
        sender_id = _sender_id(p.get("senderName") or "")
        for fips in dict.fromkeys(fips_list):
            c = geo.county(fips)
            if c is None:
                continue
            out.append(Signal(
                source="nws_alerts", source_id=f"{alert_id}|{fips}", sensor_id=sender_id,
                sensor_kind=KIND_AUTHORITY, topic=topic, metric=f"alert.{slug(p.get('event'))}",
                value=float(SEVERITY_RANK.get(sev, 0)), unit="", text=p.get("headline"),
                observed_at=sent, received_at=now, expires_at=expires,
                location=geo.location_from_county(c), confidence=1.0, quality="reference",
                group_key=str(alert_id), evidence=evidence,
                raw={"sent": p.get("sent"), "effective": p.get("effective"), "onset": p.get("onset")},
            ))
    return out


def fetch(state: str | None = None, *, etag: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Active alerts for the state → (payload, ETag); payload is None when unchanged since `etag`."""
    headers = {"User-Agent": settings.nws_user_agent, "Accept": "application/geo+json"}
    if etag:
        headers["If-None-Match"] = etag
    r = requests.get(
        "https://api.weather.gov/alerts/active",
        params={"area": state or settings.geo_state, "status": "actual",
                "message_type": "alert,update"},
        headers=headers,
        timeout=30,
    )
    if r.status_code == 304:
        return None, etag
    r.raise_for_status()
    return r.json(), r.headers.get("ETag")


# ── threads ───────────────────────────────────────────────────────────────

def _by_message(signals: list[Signal]) -> dict[str, list[Signal]]:
    """CAP id → its county rows."""
    out: dict[str, list[Signal]] = {}
    for s in signals:
        out.setdefault(str(s.evidence.get("alert_id") or s.group_key or s.key), []).append(s)
    return out


def assign_threads(signals: list[Signal]) -> None:
    """Point each CAP message's rows at its thread: the thread of any id it
    references, or a new thread named for the message itself."""
    for alert_id, rows in _by_message(signals).items():
        known = db.alert_threads_for([alert_id, *(rows[0].evidence.get("references") or [])])
        for s in rows:
            s.group_key = known[0] if known else alert_id


@dataclass
class Version:
    """What one new CAP message did to its thread."""

    thread: str
    new: bool = False                                   # first message of this alert we've seen
    changes: list[str] = field(default_factory=list)    # impact that rose, in words
    over: bool = False                                  # already past its end when it arrived


def _level(levels: list[str], value: Any) -> int:
    return levels.index(value) if value in levels else -1


def rises(prev: Any, ev: dict[str, Any], topic: str = "weather") -> list[str]:
    """What got worse between a thread's previous message and this one, in words."""
    out: list[str] = []
    rank_new = SEVERITY_RANK.get(str(ev.get("severity")), 0)
    rank_old = SEVERITY_RANK.get(str(prev["severity"]), 0)
    if ev.get("event") and prev["event"] and ev["event"] != prev["event"] and rank_new >= rank_old:
        out.append(f"now {ev['event']} (was {prev['event']})")
    elif rank_new > rank_old:
        out.append(f"severity {str(ev.get('severity')).lower()} (was {str(prev['severity']).lower()})")
    old = json.loads(prev["impact_json"] or "{}")
    new = ev.get("impact") or {}
    for code, spec in get_topic(topic).mapping("alert_parameters").items():
        a, b = old.get(code), new.get(code)
        if b is None:
            continue
        label = str(spec.get("label", code)).lower()
        levels = spec.get("levels")
        if levels:
            if a is None or _level(levels, b) > _level(levels, a):
                out.append(f"{label}: {str(b).lower()}" + (f" (was {str(a).lower()})" if a else ""))
        elif a is None or float(b) > float(a):
            unit = f" {spec['unit']}" if spec.get("unit") else ""
            out.append(f"{label} {float(b):g}{unit}"
                       + (f" (was {float(a):g}{unit})" if a is not None else ""))
    return out


def advance_thread(rows: list[Signal], now: datetime, topic: str = "weather") -> Version:
    """Record a new CAP message on its thread and retire the rows it replaces."""
    first = rows[0]
    ev = first.evidence
    alert_id = str(ev.get("alert_id") or first.group_key)
    thread = first.group_key or alert_id
    refs = ev.get("references") or []
    # Fold in whatever this message continues: another thread it references, or rows
    # stored before threading existed (their group_key is their own CAP id).
    for other in {*db.alert_threads_for(refs), *refs} - {thread}:
        db.merge_alert_threads(into=thread, other=other)
        db.move_thread(f"alert:{other}", f"alert:{thread}")
    prev = db.alert_thread(thread)
    over = first.expires_at is not None and first.expires_at <= now
    db.save_alert_thread(
        thread, current_id=alert_id, event=ev.get("event"), severity=ev.get("severity"),
        impact_json=json.dumps(ev.get("impact") or {}),
        counties_json=json.dumps([s.location.county_fips for s in rows]),
        expires_at=iso(first.expires_at), updated_at=iso(first.observed_at),
        status="ended" if over else "active", missing_since=None,
        ended_at=iso(now) if over else None, ended_reason="expired" if over else None,
        **({} if prev else {"opened_at": iso(first.observed_at)}),
    )
    db.link_alert_ids(thread, [alert_id, *refs])
    db.expire_alert_rows(thread, first.observed_at, keep_alert_id=alert_id)
    return Version(thread, new=prev is None, over=over,
                   changes=rises(prev, ev, topic) if prev is not None else [])


def _expired(thread: Any, now: datetime) -> bool:
    ends = parse_iso(thread["expires_at"])
    return ends is not None and ends <= now


def settle_threads(present: set[str] | None, now: datetime) -> list[tuple[str, str]]:
    """End threads that expired, or have been missing from the feed for ABSENT_CONFIRM.

    `present` holds the CAP ids in this fetch, or None when the feed was unchanged
    (after a 304, whatever was missing is still missing). Returns (thread, reason).
    """
    active = db.active_alert_threads()
    if present is not None:
        in_force = [t for t in active if not _expired(t, now)]
        if not present and len(in_force) >= SUSPECT_EMPTY:
            logger.warning("nws_alerts: empty feed while %d alerts are in force; "
                           "not treating it as an all-clear", len(in_force))
        else:
            db.retire_unthreaded_alert_rows(present, now)
            for t in active:
                if t["current_id"] in present:
                    if t["missing_since"]:
                        db.save_alert_thread(t["id"], missing_since=None)
                elif not t["missing_since"]:
                    db.save_alert_thread(t["id"], missing_since=iso(now))
            active = db.active_alert_threads()
    ended: list[tuple[str, str]] = []
    for t in active:
        missing = parse_iso(t["missing_since"])
        if _expired(t, now):
            reason = "expired"
        elif missing is not None and now - missing >= ABSENT_CONFIRM:
            reason = "cancelled"
        else:
            continue
        db.save_alert_thread(t["id"], status="ended", ended_at=iso(now), ended_reason=reason,
                             missing_since=None)
        db.expire_alert_rows(t["id"], now)
        ended.append((t["id"], reason))
    return ended


def _close(thread: str, reason: str, now: datetime) -> int:
    """Close subscribers' cards for a thread that ended."""
    t = db.alert_thread(thread)
    rows = db.alert_rows(thread, t["current_id"]) if t else []
    return subscriptions.fanout_alert_ended(rows[0], rows, reason, now) if rows else 0


class NWSAlertsFeed(ReferenceFeed):
    """NWS watches / warnings / advisories for the state, per county, threaded."""

    name = "nws_alerts"
    sensor_kind = KIND_AUTHORITY
    trust = 1.0
    doc = "NWS active alerts (api.weather.gov), one row per covered county"

    def __init__(self) -> None:
        self.present: set[str] | None = None
        self.etag: str | None = None

    def fetch_signals(self) -> list[Signal]:
        payload, self.etag = fetch(etag=db.kv_get(ETAG_KEY))
        if payload is None:                  # unchanged since the last feed we processed
            self.present = None
            return []
        signals = parse_alerts(payload, topic=self.topic)
        self.present = {str(s.evidence["alert_id"]) for s in signals}
        assign_threads(signals)
        for sender in {s.sensor_id for s in signals}:
            db.ensure_reference_sensor(sender, self.sensor_kind, sender.split(":", 1)[-1].upper(),
                                       geo.Location(precision="state"), self.trust)
        return signals

    def after_store(self, new: list[Signal], res: FeedResult) -> None:
        # Assess (marks reference quality), move each thread forward, then close the ended.
        now = utcnow()
        for sig in new:
            network.assess(sig)
        for rows in sorted(_by_message(new).values(), key=lambda rs: rs[0].observed_at):
            v = advance_thread(rows, now, self.topic)
            if v.over:
                res.alerts_pushed += _close(v.thread, "expired", now)
            else:
                res.alerts_pushed += subscriptions.fanout_alert(rows[0], rows, v.changes)
            if v.new:
                # people who reported it before NWS warned: confirmed now, and told so
                feedback.ahead(network.settle_by_alert(rows))
        for thread, reason in settle_threads(self.present, now):
            res.alerts_pushed += _close(thread, reason, now)
        if self.present is not None:
            # Saved only now: a run that dies before this point sees the same feed again.
            db.kv_set(ETAG_KEY, self.etag or "")


def active_alert_groups(county_fips: str | None = None) -> list[dict[str, Any]]:
    """Active alerts collapsed to one entry per alert with the counties it covers."""
    groups: dict[str, dict[str, Any]] = {}
    for s in db.active_alert_signals(county_fips):
        g = groups.setdefault(s.group_key or s.key, {"signal": s, "counties": []})
        c = geo.county(s.location.county_fips)
        if c:
            g["counties"].append(c.name)
    out = list(groups.values())
    out.sort(key=lambda g: (-(g["signal"].value or 0), g["signal"].observed_at), reverse=False)
    return out
