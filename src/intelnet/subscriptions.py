"""Subscriptions — category × area, and the fan-out of pushes to chats.

A subscription is `(chat, category, area)`:

* **category** comes from a topic pack — `weather.warnings`, `weather.alerts`,
  `weather.events`, `weather.reports`, `weather.digest` (bare `warnings` is
  accepted when unambiguous).
* **area** is a key in the geo hierarchy — `il`, `il.cook`, `il.zip.60601`,
  `il.zip.60601-1234`. A signal carries every key it sits inside, so a
  county subscriber gets everything in the county and a ZIP+4 subscriber only
  their block. NWS alerts are the exception: they cover whole counties, so
  they also reach every ZIP and ZIP+4 subscription inside an alerted county.

Every push is queued once per (message key, chat) in the outbox and delivered
from there (`delivery.py`), so a chat subscribed at both county and ZIP level
is told once, and a push Telegram refuses is retried rather than lost. An NWS
alert is one card per chat, edited as the alert changes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from intelnet import db, delivery, geo
from intelnet.config import settings
from intelnet.models import Signal, iso, utcnow
from intelnet.telegram import bot, esc, href, tg_time
from intelnet.topics import all_categories, expand_category, find_metric, get_topic

logger = logging.getLogger(__name__)


@dataclass
class ParsedSubscription:
    category: str
    area: str
    area_label: str


def parse_area(text: str | None, default: geo.Location | None = None) -> tuple[str, str] | None:
    """Area text → (area key, label). Empty → the sensor's county, else the state."""
    st = settings.geo_state.lower()
    if not text or not text.strip():
        if default and default.county_fips:
            c = geo.county(default.county_fips)
            if c:
                return f"{st}.{c.slug}", c.label
        return st, settings.geo_state
    t = text.strip().lower()
    if t in (st, "state", "all", "*"):
        return st, settings.geo_state
    loc = geo.parse_location(t, online=False)
    if loc is None:
        return None
    if loc.zip9:
        return f"{st}.zip.{loc.zip9}", loc.zip9
    if loc.zip5 and loc.precision == "zip5":
        return f"{st}.zip.{loc.zip5}", loc.zip5
    c = geo.county(loc.county_fips)
    if c:
        return f"{st}.{c.slug}", c.label
    return None


def parse_subscriptions(args: str, default: geo.Location | None = None) -> list[ParsedSubscription] | str:
    """'/subscribe <category> [area]' args → subscriptions, or an error string.

    `events` means the default topic's events; `soil.events` another pack's;
    `*.events` (or `all.events`) every pack's — one subscription each.
    """
    parts = args.strip().split(maxsplit=1)
    if not parts:
        return "Usage: /subscribe <category> [area]\n" + categories_help()
    cats = expand_category(parts[0])
    if not cats:
        return f"Unknown category “{esc(parts[0])}”.\n" + categories_help()
    area = parse_area(parts[1] if len(parts) > 1 else None, default)
    if area is None:
        return (f"Unknown area “{esc(parts[1])}”. Use a county (cook), a ZIP (60601), "
                f"a ZIP+4 (60601-1234) or “{settings.geo_state.lower()}” for the whole state.")
    return [ParsedSubscription(c, area[0], area[1]) for c in cats]


def parse_subscription(args: str, default: geo.Location | None = None) -> ParsedSubscription | str:
    """Single-subscription form of `parse_subscriptions` (first expansion)."""
    out = parse_subscriptions(args, default)
    return out if isinstance(out, str) else out[0]


def categories_help() -> str:
    from intelnet.topics import topics

    lines = ["<b>Categories</b> (topic.category)"]
    for t in topics().values():
        lines.append(f"<b>{esc(t.label)}</b>: " + " · ".join(
            f"<code>{esc(t.category_key(c))}</code>" for c in t.categories))
    lines.append("Bare <code>events</code> = weather.events; <code>*.events</code> = every topic.")
    lines.append("\n<b>Areas</b>: cook · 60601 · 60601-1234 · il (whole state). "
                 "Default = your home county.")
    return "\n".join(lines)


def area_label(area: str) -> str:
    st = settings.geo_state.lower()
    if area == st:
        return settings.geo_state
    if area.startswith(f"{st}.zip."):
        return area[len(st) + 5:]
    c = geo.county_by_name(area[len(st) + 1:]) if area.startswith(f"{st}.") else None
    return c.label if c else area


# ── formatting ────────────────────────────────────────────────────────────

_SEVERITY_EMOJI = {"Extreme": "🚨", "Severe": "⚠️", "Moderate": "🔶", "Minor": "🔹", "Unknown": "•"}
_ENDED_HEAD = {"expired": "✅ <b>Ended</b>", "cancelled": "✅ <b>Ended early</b>",
               "area": "✅ <b>No longer includes your area</b>"}


def _impact_line(sig: Signal) -> str:
    """'Hail up to 1.75 in · Wind 70 mph · Tornado: radar indicated' (pack: alert_parameters)."""
    impact = sig.evidence.get("impact") or {}
    parts = []
    for code, spec in get_topic(sig.topic).mapping("alert_parameters").items():
        value = impact.get(code)
        if value in (None, ""):
            continue
        label = spec.get("label", code)
        if spec.get("levels"):
            parts.append(f"{label}: {str(value).lower()}")
        else:
            parts.append(f"{label} {float(value):g} {spec.get('unit', '')}".strip())
    return " · ".join(parts)


def format_alert(sig: Signal, county_labels: list[str], *, ended: str | None = None,
                 ended_at: datetime | None = None) -> str:
    """An alert card. `ended` closes it: 'expired', 'cancelled', or 'area' (left your area)."""
    ev = sig.evidence
    sev = str(ev.get("severity") or "Unknown")
    event = esc(ev.get("event") or sig.metric)
    where = esc(", ".join(county_labels) if county_labels else sig.location.describe())
    link = href(ev.get("url"))
    if ended:
        lines = [f"{_ENDED_HEAD.get(ended, _ENDED_HEAD['expired'])} · {tg_time(ended_at or utcnow())}"]
        lines += [event, f"Still in effect for {where}"] if ended == "area" else [f"<s>{event}</s>", where]
    else:
        lines = [f"{_SEVERITY_EMOJI.get(sev, '•')} <b>{event}</b>", where]
        if ev.get("nws_headline"):
            lines.append(f"<i>{esc(str(ev['nws_headline'])[:200])}</i>")
        impact = _impact_line(sig)
        if impact:
            lines.append(f"<b>{esc(impact)}</b>")
        meta = [f"Until {tg_time(sig.expires_at)}"] if sig.expires_at else []
        meta += [esc(sev), esc(ev.get("sender") or "NWS")]
        if ev.get("message_type") == "Update":
            meta.append(f"updated {tg_time(sig.observed_at, 't')}")
        lines.append(" · ".join(meta))
        if ev.get("instruction"):
            lines.append(esc(str(ev["instruction"])[:280]))
    if link:
        lines.append(f'<a href="{link}">NWS alert</a>')
    return "\n".join(lines)


def format_alert_change(sig: Signal, changes: list[str], *, back: bool = False) -> str:
    """The follow-up (with sound) when an alert someone already has gets worse."""
    event = esc(sig.evidence.get("event") or sig.metric)
    head = (f"⚠️ <b>{event}</b> is back in effect for your area" if back
            else f"⬆️ <b>{event} updated</b>: {esc('; '.join(changes))}")
    return head + (f"\nUntil {tg_time(sig.expires_at)}" if sig.expires_at else "")


def format_all_clear(sig: Signal, county_labels: list[str], ended_at: datetime) -> str:
    event = esc(sig.evidence.get("event") or sig.metric)
    return f"✅ <b>{event}</b> has ended for {esc(', '.join(county_labels))} · {tg_time(ended_at, 't')}"


def format_event(ev: dict[str, Any], reason: str = "new") -> str:
    from intelnet.network import event_summary

    e = event_summary(ev)
    tag = "📈 Escalating" if reason == "escalated" else "📍 Network event"
    verified = "verified" if e["verified"] else "unverified"
    lines = [
        f"{tag}: <b>{esc(e['metric_label'])}</b> — {esc(e['county_label'])}",
        f"Peak {esc(e['peak_display'])} · {e.get('n_sensors', 0)} sensor(s)"
        f"{' incl. official' if (e.get('n_reference') or 0) else ''} · score {e.get('score', 0):.2f} ({verified})",
    ]
    return "\n".join(lines)


def format_report(sig: Signal, handle: str) -> str:
    """A raw report as other subscribers see it: pseudonymous handle, ZIP5 + county."""
    m = find_metric(sig.metric, get_topic(sig.topic))
    what = f"{m.label}: {m.display(sig.value)}" if m else f"{sig.metric}: {sig.value}"
    lines = [f"📝 <b>{esc(what)}</b>", f"{esc(sig.location.describe_public())} · "
             f"{tg_time(sig.observed_at, 't')} · {esc(handle)} · {esc(sig.quality)}"]
    if sig.text:
        lines.append(f"<i>{esc(sig.text[:200])}</i>")
    return "\n".join(lines)


# ── fan-out ───────────────────────────────────────────────────────────────

# Which queued message goes first (lower is sooner), by category suffix.
_PRIORITY = {"warnings": 0, "alerts": 1, "events": 2, "reports": 3, "digest": 5}
EDIT_PRIORITY = 4
# How long an undelivered message stays worth sending.
_STALE_HOURS = {"events": 6, "reports": 2, "digest": 12}


def _priority(category: str) -> int:
    return _PRIORITY.get(category.rsplit(".", 1)[-1], EDIT_PRIORITY)


def push(category: str, area_keys: list[str], key: str, text: str, *,
         exclude_chat: str | None = None) -> int:
    """Queue `text` once for every chat subscribed to category at any area key, and send it.

    Returns messages sent now. Whatever Telegram doesn't take right away stays
    queued for `delivery.retry_due()`.
    """
    if not bot.enabled:
        return 0
    stale = utcnow() + timedelta(hours=_STALE_HOURS.get(category.rsplit(".", 1)[-1], 6))
    ids: list[int | None] = []
    for chat_id in db.matching_chat_ids(category, area_keys):
        if exclude_chat is not None and str(chat_id) == str(exclude_chat):
            continue
        if db.already_notified(key, chat_id):
            continue
        ids.append(db.enqueue(key, chat_id, text, priority=_priority(category), stale_at=stale))
    return delivery.send_now(ids)


def _county_labels(rows: list[Signal]) -> list[str]:
    return [c.label for c in (geo.county(s.location.county_fips) for s in rows) if c]


def _alert_audience(sig: Signal, siblings: list[Signal]) -> dict[str, int]:
    """chat → priority, for every chat whose subscription covers a county the alert names.

    Each sibling row stands for a whole county, so ZIP and ZIP+4 subscriptions in it
    count too: the same county `/alerts <zip>` looks up.
    """
    topic = get_topic(sig.topic)
    cats = topic.alert_routing.get(str(sig.evidence.get("severity") or "Unknown")) or ["alerts"]
    out: dict[str, int] = {}
    for s in siblings:
        zips = geo.zip5s_in_county(s.location.county_fips)
        for cat in cats:
            category = topic.category_key(cat)
            for chat in db.matching_chat_ids(category, s.location.area_keys(), zip5s=zips):
                out[chat] = min(out.get(chat, EDIT_PRIORITY), _priority(category))
    return out


def _edit_card(thread: str, chat: str, card: Any, text: str, version: str) -> int | None:
    """Queue an edit of a chat's card, unless the card (or a queued edit) already says this."""
    if card["text"] == text:
        return None
    pending = db.pending_in_thread(thread, chat, "edit")
    if pending is not None and pending["text"] == text:
        return int(pending["id"])
    db.supersede_outbox(thread, chat, ("edit",))          # only the newest edit matters
    return db.enqueue(f"{thread}:edit:{version}", chat, text, action="edit", thread=thread,
                      priority=EDIT_PRIORITY, stale_at=utcnow() + timedelta(hours=24))


def fanout_alert(sig: Signal, siblings: list[Signal], changes: list[str] | None = None) -> int:
    """Bring every subscriber's card for this alert up to date. Returns messages sent.

    One card per alert per chat (the thread is `sig.group_key`). A newer version edits
    the card in place, which is silent; a follow-up with sound goes out only when
    `changes` says the impact rose, or the alert is back in effect for that chat. A chat
    the alert no longer covers gets its card closed.
    """
    if not bot.enabled:
        return 0
    siblings = siblings or [sig]
    thread = f"alert:{sig.group_key or sig.key}"
    labels = _county_labels(siblings)
    text = format_alert(sig, labels)
    version = str(sig.evidence.get("alert_id") or sig.key)
    stale = sig.expires_at or utcnow() + timedelta(hours=6)
    audience = _alert_audience(sig, siblings)
    have = db.cards(thread)
    ids: list[int | None] = []
    for chat, priority in audience.items():
        card = have.get(chat)
        if card is None:
            pending = db.pending_in_thread(thread, chat, "card")
            if pending is not None:
                db.retext_outbox(pending["id"], text)     # still queued: it leaves up to date
            else:
                ids.append(db.enqueue(thread, chat, text, action="card", thread=thread,
                                      priority=priority, stale_at=stale))
            continue
        ids.append(_edit_card(thread, chat, card, text, version))
        back = card["state"] == "ended"
        if back or changes:
            db.update_card(thread, chat, state="active")
            ids.append(db.enqueue(f"{thread}:up:{version}", chat,
                                  format_alert_change(sig, changes or [], back=back),
                                  action="reply", thread=thread, priority=priority, stale_at=stale))
    db.supersede_outbox(thread, actions=("card",), keep_chats=audience)
    closed = None
    for chat, card in have.items():
        if card["state"] == "active" and chat not in audience:
            closed = closed or format_alert(sig, labels, ended="area")
            db.update_card(thread, chat, state="ended")
            ids.append(_edit_card(thread, chat, card, closed, f"gone:{version}"))
    return delivery.send_now(ids)


def fanout_alert_ended(sig: Signal, siblings: list[Signal], reason: str, ended_at: datetime) -> int:
    """Close every card for an alert that ended ('expired' or 'cancelled').

    Severe and Extreme alerts also get a quiet all-clear reply under the card: the
    message most alert services never send.
    """
    if not bot.enabled:
        return 0
    siblings = siblings or [sig]
    thread = f"alert:{sig.group_key or sig.key}"
    labels = _county_labels(siblings)
    closed = format_alert(sig, labels, ended=reason, ended_at=ended_at)
    db.supersede_outbox(thread, actions=("card", "reply"))    # nothing still queued is news now
    severe = str(sig.evidence.get("severity")) in ("Severe", "Extreme")
    stamp = iso(ended_at)
    ids: list[int | None] = []
    for chat, card in db.cards(thread).items():
        if card["state"] != "active":
            continue
        db.update_card(thread, chat, state="ended")
        ids.append(_edit_card(thread, chat, card, closed, f"end:{stamp}"))
        if severe:
            ids.append(db.enqueue(f"{thread}:ended:{stamp}", chat,
                                  format_all_clear(sig, labels, ended_at), action="reply",
                                  thread=thread, silent=True, priority=_PRIORITY["reports"],
                                  stale_at=ended_at + timedelta(hours=2)))
    return delivery.send_now(ids)


def fanout_event(ev: dict[str, Any], reason: str, area_keys: list[str]) -> int:
    topic = get_topic(ev["topic"])
    key = f"event:{ev['id']}:{'esc' if reason == 'escalated' else 'new'}:{ev.get('pushed_score')}"
    return push(topic.category_key("events"), area_keys, key, format_event(ev, reason))


def fanout_report(sig: Signal) -> int:
    from intelnet.models import public_handle

    topic = get_topic(sig.topic)
    return push(topic.category_key("reports"), sig.location.area_keys(), f"report:{sig.key}",
                format_report(sig, public_handle(sig.sensor_id)), exclude_chat=_chat_of(sig.sensor_id))


def _chat_of(sensor_id: str) -> str | None:
    m = re.match(r"^tg:(-?\d+)$", sensor_id)
    return m.group(1) if m else None


def fanout_digest(date: str, url: str | None, folder_url: str | None, headline: str | None = None) -> int:
    key = f"digest:{date}"
    topics_with_digest = [t for t in all_categories() if t.endswith(".digest")]
    lines = [f"📰 <b>{esc(settings.network_name)} digest</b> · {esc(date)}"]
    if headline:
        lines.append(esc(headline[:300]))
    link = href(url)
    if link:
        lines.append(f'<a href="{link}">Open today’s digest</a>')
    flink = href(folder_url)
    if flink:
        lines.append(f'<a href="{flink}">All digests (Drive folder)</a>')
    text = "\n".join(lines)
    st = settings.geo_state.lower()
    total = 0
    for cat in topics_with_digest:
        # digest subscriptions are state-wide by construction (area = state key)
        total += push(cat, [st], key, text)
    return total
