"""Questions after an alert: what did the people it reached actually see?

When a warning ends (or, for slow hazards, when it's issued), each chat that had
the card and whose place it covered gets one quiet question under the card, with
buttons from the pack's quick reports. A tap is a reading at their place (live
location, else home), timed to when the storm was there: the arrival time from the
NWS storm motion, else the middle of the alert. So the warning gets checked on the
ground, and quiet counties get asked rather than waiting to be heard from.

Which alerts ask what lives in the pack (`alert_questions`). A chat is asked once
per alert, at most once every COOLDOWN, only when there is a place to record the
answer at, and never after `/followups off`.

Callback data: `a:<question>` shows the question, `a:<question>:<i>` opens report i
(or records it, for a one-tap report), `a:<question>:<i>:<j>` records choice j.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from intelnet import db, feedback, geo
from intelnet.models import Signal, parse_iso, utcnow
from intelnet.telegram import esc
from intelnet.topics import Topic, alert_question, topics

COOLDOWN = timedelta(hours=6)


def place_of(chat_id: str | int) -> geo.Location | None:
    """Where an answer from this chat is recorded: the live location, else a home with a point."""
    live = db.live_location(chat_id)
    if live is not None:
        return live
    s = db.sensor_by_chat(chat_id)
    return s.location if s and s.location.has_point else None


def _eligible(sig: Signal, chat: str, place: geo.Location | None) -> bool:
    if place is None or place.lat is None or place.lon is None or not feedback.enabled_for(chat):
        return False
    rings = sig.evidence.get("polygon")
    if rings and not geo.point_in_polygon(place.lat, place.lon, rings):
        return False                                   # the storm missed them: nothing to ask
    last = db.last_question_at(chat)
    return last is None or utcnow() - last >= COOLDOWN


def when_there(sig: Signal, place: geo.Location, start: datetime, end: datetime) -> datetime:
    """When the storm was at this place: its arrival from the NWS storm motion, else mid-alert."""
    motion = sig.evidence.get("motion")
    at = parse_iso(motion.get("at")) if motion else None
    if motion and at and place.lat is not None and place.lon is not None:
        eta = geo.storm_arrival(at, motion["from_deg"], motion["speed_kt"], motion["points"],
                                place.lat, place.lon)
        if eta and start <= eta <= end:
            return eta
    return start + (end - start) / 2


def _reports(topic: Topic, ids: list[str]) -> list[tuple[int, dict[str, Any]]]:
    return [r for r in (topic.quick_report(i) for i in ids) if r is not None]


def question_markup(qid: int, topic: Topic, ids: list[str]) -> dict:
    buttons = [{"text": q["button"], "callback_data": f"a:{qid}:{n}"}
               for n, (_i, q) in enumerate(_reports(topic, ids))]
    return {"inline_keyboard": [buttons[k:k + 3] for k in range(0, len(buttons), 3)]}


def picker_markup(qid: int, n: int, report: dict[str, Any]) -> dict:
    buttons = [{"text": label, "callback_data": f"a:{qid}:{n}:{j}"}
               for j, (label, _reading) in enumerate(report["choices"])]
    rows = [buttons[k:k + 3] for k in range(0, len(buttons), 3)]
    return {"inline_keyboard": [*rows, [{"text": "‹ Back", "callback_data": f"a:{qid}"}]]}


def question_text(ask: str, head: str | None = None) -> str:
    body = (f"❓ <b>{esc(ask)}</b>\nOne tap puts your report on the map. It's how the network "
            f"checks what the warning said.")
    return f"{head}\n\n{body}" if head else body


def queue(sig: Signal, chat: str, thread: str, *, when: str, head: str | None = None,
          ended_at: datetime | None = None, priority: int = 3,
          stale_at: datetime | None = None) -> int | None:
    """Queue this alert's question for a chat, as a quiet reply to its card (with `head`,
    the all-clear, on top). None when there's nothing to ask this chat."""
    found = alert_question(str(sig.evidence.get("event") or ""), when)
    if found is None:
        return None
    topic, q = found
    ids = [str(i) for i in q.get("reports") or []]
    place = place_of(chat)
    if not _reports(topic, ids) or place is None or not _eligible(sig, chat, place):
        return None
    observed = None
    if when == "ended":
        t = db.alert_thread(sig.group_key) if sig.group_key else None
        start = parse_iso(t["opened_at"]) if t else None
        end = ended_at or utcnow()
        observed = when_there(sig, place, min(start or sig.observed_at, end), end)
    ask = str(q.get("ask") or "What did you see?")
    qid = db.add_question(chat, thread, topic.name, ids, ask, observed)
    if qid is None:
        return None
    out = db.enqueue(f"{thread}:ask", chat, question_text(ask, head), action="reply", thread=thread,
                     silent=True, priority=priority, stale_at=stale_at,
                     markup=question_markup(qid, topic, ids))
    if out is None:
        db.drop_question(qid)
    return out


def resolve(data: str, chat: str | int) -> dict[str, Any] | None:
    """What an `a:` button means for this chat, or None if it has expired.

    {question, topic, observed, and one of: show (the question), pick (a report
    with choices to open), reading + label (record this)}."""
    parts = data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    row = db.question(parts[1])
    topic = topics().get(row["topic"]) if row else None
    if row is None or topic is None or str(row["chat_id"]) != str(chat):
        return None
    ids = json.loads(row["reports"])
    out: dict[str, Any] = {"question": row, "topic": topic, "observed": parse_iso(row["observed_at"])}
    if len(parts) == 2:
        out["show"] = (question_text(row["ask"]), question_markup(int(row["id"]), topic, ids))
        return out
    try:
        n = int(parts[2])
        _i, report = _reports(topic, ids)[n]
        if len(parts) == 3:
            if report.get("choices"):
                out["pick"] = (esc(str(report.get("ask") or "Pick one:")),
                               picker_markup(int(row["id"]), n, report))
            else:
                out["label"], out["reading"] = report["button"], str(report["send"])
            return out
        out["label"], out["reading"] = report["choices"][int(parts[3])]
    except (ValueError, IndexError, KeyError):
        return None
    return out
