"""Follow-ups: telling people what their reports did.

A report goes in and, most of the time, nothing comes back: the check against
neighbors and official sources happens later, out of sight. These notes close
that loop, quietly (no sound) and once per report:

- confirmed: a later reading agreed with yours (a station, an NWS storm report,
  or another person nearby);
- ahead: NWS issued a warning for your area after you reported what it warns of;
- helped: an event your report was part of was verified and pushed out.

At most DAILY_MAX a day per chat; `/followups off` stops them, and the questions
after alerts (asks.py) too. Nothing here knows about weather: what a reading
says comes from its pack's metric.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from intelnet import db, delivery, geo
from intelnet.models import KIND_HUMAN, KIND_OFFICIAL, KIND_STATION, Signal, local_time, utcnow
from intelnet.telegram import bot, esc, tg_time
from intelnet.topics import find_metric, get_topic

logger = logging.getLogger(__name__)

DAILY_MAX = 5
STALE = timedelta(hours=12)
_KINDS = ("confirm:", "ahead:", "helped:")


def enabled_for(chat_id: str | int) -> bool:
    return db.kv_get(f"followups:off:{chat_id}") is None


def set_enabled(chat_id: str | int, on: bool) -> None:
    key = f"followups:off:{chat_id}"
    if on:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM kv WHERE key = ?", (key,))
    else:
        db.kv_set(key, "1")


def _room(chat: str) -> bool:
    """Whether a chat can take another note today."""
    since = utcnow() - timedelta(days=1)
    return sum(db.sent_since(chat, k, since) for k in _KINDS) < DAILY_MAX


def _chat_for(sig: Signal) -> str | None:
    """The chat of the person behind a reading (people only: bots don't read notes)."""
    if sig.sensor_kind != KIND_HUMAN:
        return None
    s = db.get_sensor(sig.sensor_id)
    return s.chat_id if s and s.chat_id and s.status == "active" else None


def describe(sig: Signal, fmt: str = "wt") -> str:
    """'hail size report (1.25 in, Tue 4:52 PM CDT)': what a reading said, in its pack's words."""
    m = find_metric(sig.metric, get_topic(sig.topic))
    label = (m.label[:1].lower() + m.label[1:]) if m else sig.metric
    value = "" if m is None or m.is_flag or sig.value is None else m.display(sig.value).split(" (")[0] + ", "
    return f"{esc(label)} report ({esc(value)}{tg_time(sig.observed_at, fmt)})"


def _distance(a: Signal, b: Signal) -> str:
    if not (a.location.has_point and b.location.has_point):
        return "nearby"
    mi = geo.haversine_km(a.location.lat, a.location.lon, b.location.lat, b.location.lon) * 0.621371
    return "under a mile away" if mi < 1 else f"{mi:.0f} mi away"


def _source(ref: Signal) -> str:
    if ref.sensor_kind == "grid":                      # a radar grid (grids.py), never stored
        m = find_metric(ref.metric, get_topic(ref.topic))
        value = f" of {m.display(ref.value).split(' (')[0]}" if m and ref.value is not None else ""
        return f"the {esc(str(ref.evidence.get('label') or 'radar estimate'))}{esc(value)}"
    if ref.evidence.get("kind") == "observer":         # CoCoRaHS: a volunteer's day total
        m = find_metric(ref.metric, get_topic(ref.topic))
        value = (f", {m.display(ref.value)} over the day to {local_time(ref.observed_at, '%-I %p')}"
                 if m and ref.value is not None else "")
        return f"a CoCoRaHS volunteer's gauge{esc(value)}"
    if ref.evidence.get("kind") == "monitor":
        return "an EPA air monitor"
    if ref.sensor_kind == KIND_OFFICIAL:
        return "an NWS storm report"
    if ref.sensor_kind == KIND_STATION:
        s = db.get_sensor(ref.sensor_id)
        return f"the {esc(s.name)} weather station" if s and s.name else "a weather station"
    if ref.sensor_kind == KIND_HUMAN:
        return "another person's report"
    return "an automated sensor"


def _queue(key: str, chat: str, text: str) -> int | None:
    if not enabled_for(chat) or db.already_notified(key, chat) or not _room(chat):
        return None
    return db.enqueue(key, chat, text, silent=True, priority=6, stale_at=utcnow() + STALE)


def confirmed(pairs: list[tuple[Signal, Signal]]) -> int:
    """Tell people a later reading agreed with theirs. Returns messages sent."""
    if not bot.enabled or not pairs:
        return 0
    ids = []
    for mine, by in pairs:
        chat = _chat_for(mine)
        if chat is None or mine.id is None:
            continue
        s = db.get_sensor(mine.sensor_id)
        n = s.n_corroborated if s else 0
        text = (f"✅ <b>Your report checked out.</b> Your {describe(mine)} matches "
                f"{_source(by)} {_distance(mine, by)}."
                + (f"\n<i>{n} of your reports confirmed so far.</i>" if n > 1 else ""))
        ids.append(_queue(f"confirm:{mine.id}", chat, text))
    return delivery.send_now(ids)


def ahead(pairs: list[tuple[Signal, Signal]]) -> int:
    """Tell people they reported something before NWS warned for it. Returns messages sent."""
    if not bot.enabled or not pairs:
        return 0
    ids = []
    for mine, alert in pairs:
        chat = _chat_for(mine)
        if chat is None or mine.id is None:
            continue
        minutes = max(1, round((alert.observed_at - mine.observed_at).total_seconds() / 60))
        c = geo.county(mine.location.county_fips)
        event = esc(str(alert.evidence.get("event") or "an alert"))
        text = (f"⏱ <b>You were ahead of the warning.</b> Your {describe(mine, 't')} "
                f"came {minutes} min before NWS issued a "
                f"{event} for {esc(c.label if c else 'your area')}. It now counts as confirmed.")
        ids.append(_queue(f"ahead:{mine.id}", chat, text))
    return delivery.send_now(ids)


def helped(event: dict[str, Any], pushes: int, *, exclude_sensor: str | None = None) -> int:
    """Tell the people whose readings made up a newly verified event. Returns messages sent.

    `exclude_sensor` is whoever just sent the reading that tipped it: their own reply
    already says so."""
    if not bot.enabled or not event.get("id"):
        return 0
    title = esc(str(event.get("title") or "an event"))
    reach = (f" It went out to {pushes} subscriber{'s' if pushes != 1 else ''}." if pushes
             else " It's on the network's map.")
    ids, seen = [], set()
    for sig in db.signals_for_event(int(event["id"])):
        if sig.sensor_id == exclude_sensor or sig.sensor_id in seen:
            continue
        seen.add(sig.sensor_id)
        chat = _chat_for(sig)
        if chat is None:
            continue
        text = f"📣 <b>Your report helped verify an event</b>: {title}.{reach}"
        ids.append(_queue(f"helped:{event['id']}", chat, text))
    return delivery.send_now(ids)
