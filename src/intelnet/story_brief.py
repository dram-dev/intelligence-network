"""A storm's brief: two or three sentences, grounded only in its own rows, cited.

A story's facts are numbered: its NWS warnings first (in the order they came),
then its events, most severe first (the top MAX_EVENTS). The storm card lists
them with the same numbers, so "[2]" in the brief points at a line the reader can
see.

The local LLM writes the brief in the watch pass (never on the bot's reply path),
once per version of the story. A brief is kept only if it cites facts that exist
and every number in it appears in the facts; anything else, or no LLM at all,
falls back to a sentence built from the facts directly. Nothing here knows about
weather: the facts are the events' own titles and peaks.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from intelnet import db, llm, stories
from intelnet.config import settings
from intelnet.models import REFERENCE_KINDS, local_time, parse_iso
from intelnet.topics import find_metric

logger = logging.getLogger(__name__)

MAX_EVENTS = 5
MAX_CHARS = 420
_CITE = re.compile(r"\[(\d+)\]")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")       # a clock time is one token: "3:01" never vouches for "3"

_SYSTEM = """You write the brief for one storm tracked by a citizen weather network.
Use ONLY the numbered facts. Two or three short sentences, under 60 words, plain text.
End each sentence with the fact numbers it relies on, like [1] or [2][3].
No advice, no forecasts, no adjectives the facts don't support, no numbers the facts don't contain."""


@dataclass
class Fact:
    key: str                  # "alert:<thread>" | "event:<id>"
    kind: str                 # alert | event
    text: str                 # plain words, for the LLM and the template
    row: dict[str, Any]


def facts(story_id: int) -> list[Fact]:
    """The story's facts in card order: warnings as they came, then events by severity."""
    from intelnet.network import event_summary

    out: list[Fact] = []
    members = db.story_members(story_id)
    for m in (m for m in members if m["kind"] == "alert"):
        t = db.alert_thread(m["ref"])
        event = (t["event"] if t else None) or m["title"] or "NWS alert"
        opened = parse_iso(t["opened_at"]) if t else None
        ended = parse_iso(t["ended_at"]) if t and t["status"] == "ended" else None
        text = f"NWS {event}" + (f", issued {local_time(opened, '%-I:%M %p')}" if opened else "") \
            + (f", ended {local_time(ended, '%-I:%M %p')}" if ended else ", in effect")
        out.append(Fact(f"alert:{m['ref']}", "alert", text, {"event": event, "ended": bool(ended)}))
    events = [dict(e) for e in (db.event_by_id(int(m["ref"])) for m in members if m["kind"] == "event") if e]
    events.sort(key=lambda e: -(e.get("severity") or 0))
    for e in (event_summary(x) for x in events[:MAX_EVENTS]):
        n, ref = int(e.get("n_sensors") or 0), int(e.get("n_reference") or 0)
        who = [f"{n - ref} {'person' if n - ref == 1 else 'people'}" if n - ref else "",
               f"{ref} official source{'s' if ref != 1 else ''}" if ref else ""]
        text = (f"{e['metric_label']} peak {e['peak_display'].split(' (')[0]} in {e['county_label']}, "
                f"from {' and '.join(w for w in who if w) or 'one sensor'}"
                + (", verified" if e["verified"] else ", not yet verified"))
        out.append(Fact(f"event:{e['id']}", "event", text, e))
    return out


def template(fs: list[Fact]) -> str:
    """The brief without an LLM: the peaks, who saw them, the warnings, all cited."""
    events = [(i + 1, f) for i, f in enumerate(fs) if f.kind == "event"]
    alerts = [(i + 1, f) for i, f in enumerate(fs) if f.kind == "alert"]
    parts = []
    if events:
        # one phrase per metric, citing every event of it (most severe first)
        by_metric: dict[str, list[tuple[int, Fact]]] = {}
        for n, f in events:
            by_metric.setdefault(str(f.row["metric"]), []).append((n, f))
        peaks = []
        for metric, group in list(by_metric.items())[:3]:
            top = group[0][1].row
            m = find_metric(metric)
            cites = "".join(f"[{n}]" for n, _f in group)
            what = top["metric_label"].lower()
            peaks.append(f"{what} reported {cites}" if m is not None and m.is_flag
                         else f"{what} up to {top['peak_display'].split(' (')[0]} {cites}")
        seen = {(s.sensor_id, s.sensor_kind in REFERENCE_KINDS)
                for _n, f in events for s in db.signals_for_event(int(f.row["id"]))}
        people = sum(1 for _sid, ref in seen if not ref)                  # each person once
        official = sum(1 for _sid, ref in seen if ref)
        who = " and ".join(x for x in (f"{people} {'person' if people == 1 else 'people'}" if people else "",
                                       f"{official} official source{'s' if official != 1 else ''}"
                                       if official else "") if x)
        sentence = (", ".join(peaks[:-1]) + " and " + peaks[-1] if len(peaks) > 1 else peaks[0])
        parts.append(sentence[:1].upper() + sentence[1:] + (f", from {who}." if who else "."))
    if alerts:
        live = [n for n, f in alerts if not f.row["ended"]]
        done = [n for n, f in alerts if f.row["ended"]]
        bits = [f"{len(live)} NWS warning{'s' if len(live) != 1 else ''} in effect "
                + "".join(f"[{n}]" for n in live) if live else "",
                f"{len(done)} ended " + "".join(f"[{n}]" for n in done) if done else ""]
        parts.append(", ".join(b.strip() for b in bits if b) + ".")
    return " ".join(parts)


def valid(text: str, fs: list[Fact]) -> bool:
    """Keep an LLM brief only if it cites real facts and invents no numbers."""
    if not text or len(text) > MAX_CHARS:
        return False
    cites = [int(n) for n in _CITE.findall(text)]
    if not cites or any(not 1 <= n <= len(fs) for n in cites):
        return False
    facts_text = " ".join(f.text for f in fs)
    body = _CITE.sub("", text)
    if not set(_TIME.findall(body)) <= set(_TIME.findall(facts_text)):
        return False
    known = set(_NUMBER.findall(_TIME.sub(" ", facts_text)))
    return all(n in known for n in _NUMBER.findall(_TIME.sub(" ", body)))


def write(story_id: int) -> str | None:
    """Ask the LLM for this story's brief; None when it's off, down or not grounded."""
    fs = facts(story_id)
    if not fs:
        return None
    prompt = "Facts:\n" + "\n".join(f"[{i + 1}] {f.text}" for i, f in enumerate(fs))
    raw = llm.call(settings.summarizer_backend, _SYSTEM, prompt, max_tokens=200, temperature=0.2)
    text = " ".join((raw or "").split())
    if valid(text, fs):
        return text
    if text:
        logger.info("story brief %s rejected (ungrounded): %.120s", story_id, text)
    return None


def current(story_id: int) -> str:
    """The brief to show: the LLM's, if written for this version of the story, else the template."""
    s = db.story(story_id)
    if s is None:
        return ""
    if s["brief"] and s["brief_at"] and s["brief_at"] >= s["updated_at"]:
        return str(s["brief"])
    return template(facts(story_id))


def write_briefs(limit: int = 10) -> list[int]:
    """Write a brief for every open story that changed since its last one (the watch pass).
    Each version is tried once: a failed or ungrounded attempt leaves the template."""
    done = []
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT id, updated_at FROM stories WHERE status = 'open'
                 AND (brief_at IS NULL OR brief_at < updated_at) ORDER BY updated_at DESC LIMIT ?""",
            (limit,)).fetchall()
    for r in rows:
        text = write(int(r["id"])) if settings.llm_enabled else None
        db.update_story(int(r["id"]), brief=text, brief_at=r["updated_at"])
        if text:
            done.append(int(r["id"]))
    return done


def cards_changed(story_ids: list[int]) -> int:
    """Edit the cards of stories whose brief just changed."""
    from intelnet import subscriptions

    return sum(subscriptions.story_changed(stories.Joined(i)) for i in story_ids)


def summaries(hours: float = 24, limit: int = 12) -> list[dict[str, Any]]:
    """Recent storms for the digest, the morning brief and the site: title, counties,
    span, the brief, and the numbered facts it cites. People appear only as counts."""
    from intelnet import geo

    out = []
    for s in db.stories_since(hours, limit):
        fs = facts(int(s["id"]))
        if not fs:
            continue
        names = [c.name for c in (geo.county(f) for f in json.loads(s["counties_json"] or "[]")) if c]
        out.append({"id": int(s["id"]), "topic": s["topic"], "title": s["title"], "severity": s["severity"],
                    "status": s["status"], "counties": names, "fips": json.loads(s["counties_json"] or "[]"),
                    "opened_at": s["opened_at"], "updated_at": s["updated_at"],
                    "lat": round(s["lat"], 3), "lon": round(s["lon"], 3),
                    "brief": current(int(s["id"])), "facts": [f.text for f in fs]})
    return out
