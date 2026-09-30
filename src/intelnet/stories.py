"""Storms as stories: one cluster in space and time per storm, across counties.

An event is keyed on (county, metric); a storm isn't. A line of storms crossing
Sangamon, Menard and Logan makes a hail event, a wind event and a flooding event
in each county, three Severe Thunderstorm Warnings and a dozen storm reports.
A story gathers all of them: each member (an event, an NWS alert thread, a storm
report) joins the open story of its topic whose centroid is within STORY_KM and
which was active within STORY_HOURS of it, or opens one. Two stories that come
to overlap merge (the older survives). A story idle for IDLE_HOURS closes.

The story is what people get (one card per storm, edited as it grows) and what
the brief and the digest tell. Nothing here knows about weather: members carry
their own severity and title.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from intelnet import db, geo
from intelnet.models import iso, parse_iso, utcnow

STORY_KM = 60.0
STORY_HOURS = 3.0
IDLE_HOURS = 6.0


@dataclass
class Joined:
    story_id: int
    opened: bool = False                # this member started the story
    merged: list[int] | None = None     # stories folded into it


def _centroid(members: list) -> tuple[float, float]:
    return (sum(m["lat"] for m in members) / len(members), sum(m["lon"] for m in members) / len(members))


def _refresh(story_id: int) -> None:
    """Recompute a story's centroid, extent, severity and title from its members."""
    members = db.story_members(story_id)
    if not members:
        return
    lat, lon = _centroid(members)
    counties = sorted({c.fips for c in (geo.nearest_county(m["lat"], m["lon"]) for m in members) if c})
    top = max(members, key=lambda m: (m["severity"], m["at"]))
    db.update_story(story_id, lat=lat, lon=lon, n_members=len(members), counties_json=json.dumps(counties),
                    severity=max(m["severity"] for m in members), title=top["title"],
                    updated_at=max(m["at"] for m in members))


def attach(topic: str, kind: str, ref: str | int, lat: float, lon: float, at: datetime,
           *, severity: float = 0.0, title: str | None = None) -> Joined:
    """Put a member (event / alert / signal) in the story it belongs to. Idempotent:
    a member already in a story updates in place (and may pull its story into a merge)."""
    when = iso(at) or iso(utcnow()) or ""
    existing = db.story_of(kind, str(ref))
    out = Joined(story_id=existing or 0)
    if existing is None:
        near = [(geo.haversine_km(lat, lon, s["lat"], s["lon"]), s) for s in
                db.open_stories(topic, at - timedelta(hours=STORY_HOURS))]
        near = [(d, s) for d, s in near if d <= STORY_KM]
        if near:
            out.story_id = int(min(near, key=lambda x: x[0])[1]["id"])
        else:
            out.story_id, out.opened = db.insert_story(topic, lat, lon, when), True
    db.upsert_story_member(out.story_id, kind, str(ref), lat, lon, when, severity, title)
    _refresh(out.story_id)
    out.merged = _merge_neighbors(out.story_id, topic, at) or None
    out.story_id = current(out.story_id)             # it may have been folded into an older one
    return out


def _merge_neighbors(story_id: int, topic: str, at: datetime) -> list[int]:
    """Fold any other open story now within STORY_KM into this one (the older id wins)."""
    merged = []
    me = db.story(story_id)
    for other in db.open_stories(topic, at - timedelta(hours=STORY_HOURS)):
        if me is None or other["id"] == me["id"] or other["id"] in merged:
            continue
        if geo.haversine_km(me["lat"], me["lon"], other["lat"], other["lon"]) > STORY_KM:
            continue
        keep, gone = (me, other) if me["id"] < other["id"] else (other, me)
        db.move_story_members(int(gone["id"]), int(keep["id"]))
        db.update_story(int(gone["id"]), status="closed", closed_at=iso(utcnow()), merged_into=int(keep["id"]))
        _refresh(int(keep["id"]))
        merged.append(int(gone["id"]))
        me = db.story(int(keep["id"]))
    return merged


def close_idle(idle_hours: float = IDLE_HOURS) -> int:
    return db.close_idle_stories(idle_hours)


def current(story_id: int) -> int:
    """Follow merges to the story a member's story became."""
    seen = set()
    row = db.story(story_id)
    while row is not None and row["merged_into"] and row["id"] not in seen:
        seen.add(row["id"])
        row = db.story(int(row["merged_into"]))
    return int(row["id"]) if row else story_id


def span(story_id: int) -> tuple[datetime | None, datetime | None]:
    row = db.story(story_id)
    return (parse_iso(row["opened_at"]), parse_iso(row["updated_at"])) if row else (None, None)
