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

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from intelnet import asks, db, delivery, geo, stories
from intelnet.config import settings
from intelnet.models import Signal, iso, parse_iso, utcnow
from intelnet.telegram import bot, esc, href, tg_time
from intelnet.topics import expand_category, find_metric, get_topic

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


INSIDE = "inside the warned area"       # how a card says the warning covers you


@dataclass
class Where:
    """Where a warning stands for one chat: inside or outside its polygon (None when it
    has none, or the chat has no place), when the storm arrives, and how to say it."""
    lines: list[str]
    inside: bool | None = None
    who: str = ""                      # "Your home is" · "You are" · "The center of ZIP 62704 is"
    label: str = ""                    # "Your home" · "You" · "ZIP 62704" (the map caption)
    eta: datetime | None = None
    place: geo.Location | None = None
    ask_location: bool = False


def locate(sig: Signal, chat_id: str) -> Where:
    """Inside or outside the warned polygon, and when the storm reaches them (from the NWS
    storm motion). Uses the live location while it's shared, else home; when home is only
    a ZIP's center, the card says so rather than claiming precision."""
    rings = sig.evidence.get("polygon")
    if not rings:
        return Where([])
    live = db.live_location(chat_id)
    sensor = None if live else db.sensor_by_chat(chat_id)
    place = live or (sensor.location if sensor and sensor.location.has_point else None)
    if place is None or place.lat is None or place.lon is None:
        return Where(["📍 Share your location (or /home 62704) to see whether this warning covers you."],
                     ask_location=True)
    if place.precision == "point":
        who, label = ("You are", "You") if live else ("Your home is", "Your home")
    else:
        who, label = f"The center of ZIP {place.zip5} is", f"ZIP {place.zip5}"
    inside = geo.point_in_polygon(place.lat, place.lon, rings)
    lines = [f"📍 <b>{who} {INSIDE if inside else 'outside the warned area'}.</b>"]
    motion = sig.evidence.get("motion")
    at = parse_iso(motion.get("at")) if motion else None
    eta = None
    if motion and at:
        eta = geo.storm_arrival(at, motion["from_deg"], motion["speed_kt"], motion["points"],
                                place.lat, place.lon)
        if not (eta and eta > utcnow() and (sig.expires_at is None or eta <= sig.expires_at + timedelta(minutes=30))):
            eta = None
        if eta:
            lines.append(f"⏱ The storm reaches you about {tg_time(eta, 't')} ({tg_time(eta, 'r')})")
    return Where(lines, inside, who, label, eta, place)


def where_you_are(sig: Signal, chat_id: str) -> tuple[list[str], bool]:
    """The lines only this chat sees on a storm-based warning, and whether it's inside."""
    w = locate(sig, chat_id)
    return w.lines, bool(w.inside)


def _size_word(spec: dict[str, Any], value: float) -> str:
    """'golf ball' for 1.75 in, when the pack names sizes for the tag's metric."""
    m = find_metric(str(spec.get("metric") or ""))
    if m is None or not m.words:
        return ""
    try:
        canon = m.convert(value, spec.get("unit"))
    except ValueError:
        return ""
    word, size = min(m.words.items(), key=lambda kv: abs(kv[1] - canon))
    return word if abs(size - canon) <= canon * 0.08 else ""


def card_tags(sig: Signal) -> dict[str, list[str]]:
    """NWS impact tags as the card shows them (pack `alert_parameters`, `show`):
    header ('CONSIDERABLE'), chips ('Hail 1.75 in golf ball') and tags ('Radar indicated')."""
    out: dict[str, list[str]] = {"header": [], "chip": [], "tag": []}
    impact = sig.evidence.get("impact") or {}
    for code, spec in get_topic(sig.topic).mapping("alert_parameters").items():
        value = impact.get(code)
        if value in (None, ""):
            continue
        show = spec.get("show", "chip")
        show = show if show in out else "chip"
        if spec.get("levels"):
            v = str(value)
            out[show].append(v.upper() if show == "header" else v.capitalize() if show == "tag"
                             else f"{spec.get('label', code)}: {v.lower()}")
            continue
        word = _size_word(spec, float(value))
        out[show].append(f"{spec.get('chip') or spec.get('label', code)} {float(value):g} {spec.get('unit', '')}".strip()
                         + (f" {word}" if word else ""))
    return out


def _first_sentences(text: str | None, limit: int = 220) -> str:
    sentences = re.split(r"(?<=\.)\s+", " ".join(str(text or "").split()))
    return " ".join(sentences[:2])[:limit]


def _sender(ev: dict[str, Any]) -> str:
    return re.sub(r" [A-Z]{2}$", "", str(ev.get("sender") or "NWS"))


def format_alert_rich(sig: Signal, county_labels: list[str], *, where: Where | None = None,
                      ended: str | None = None, ended_at: datetime | None = None) -> str:
    """The alert card as a Telegram rich message (Bot API 10.1): the event and damage
    threat as a heading, the reader's own situation as the headline, arrival, impact tags,
    what to do, a map of their place, and a footer. `format_alert` is its plain fallback."""
    ev = sig.evidence
    event = esc(ev.get("event") or sig.metric)
    names = [c.removesuffix(" County") for c in county_labels]
    counties = esc(", ".join(names[:4]) + ("…" if len(names) > 4 else "")) or esc(sig.location.describe())
    if ended:
        head = {"cancelled": "Ended early", "area": "No longer covers your area"}.get(ended, "Ended")
        return (f"<h4>✅ {esc(head.upper())} · {tg_time(ended_at or utcnow(), 't')}</h4><h2><s>{event}</s></h2>"
                + (f"<p>Still in effect for {counties}</p>" if ended == "area" else f"<p>{counties}</p>"))
    tags = card_tags(sig)
    sev = str(ev.get("severity") or "Unknown")
    parts = [f"<h4>{_SEVERITY_EMOJI.get(sev, '•')} {' · '.join([event.upper(), *map(esc, tags['header'])])}</h4>"]
    if where is not None and where.inside is not None:
        parts.append(f"<h2>{esc(where.who)} {'inside' if where.inside else 'outside'} the warning.</h2>")
    else:
        parts.append(f"<h2>{counties}</h2>")
    if where is not None and where.eta:
        parts.append(f"<p>Storm arrives about {tg_time(where.eta, 't')}, <b>{tg_time(where.eta, 'r')}</b></p>")
    elif where is not None and where.ask_location:
        parts.append("<p>📍 Share your location (or /home 62704) to see whether this warning covers you.</p>")
    chips = " ".join(f"<mark>{esc(c)}</mark>" for c in tags["chip"])
    notes = " · ".join(esc(t) for t in tags["tag"])
    if chips or notes:
        parts.append("<p>" + " ".join(x for x in (chips, f"<code>{notes}</code>" if notes else "") if x) + "</p>")
    action = _first_sentences(ev.get("instruction"))
    if action:
        parts.append(f"<blockquote>{esc(action)}</blockquote>")
    if where is not None and where.place is not None and ev.get("polygon"):
        parts.append(f'<figure><tg-map lat="{where.place.lat:.3f}" long="{where.place.lon:.3f}" zoom="9"/>'
                     f"<figcaption>{esc(where.label)} · the warned area is on the Map</figcaption></figure>")
    foot = [esc(_sender(ev))] + ([f"until {tg_time(sig.expires_at, 't')}"] if sig.expires_at else []) + [counties]
    if ev.get("message_type") == "Update":
        foot.append(f"updated {tg_time(sig.observed_at, 't')}")
    link = href(ev.get("url"))
    parts.append("<footer>" + " · ".join(foot) + (f' · <a href="{link}">NWS alert</a>' if link else "") + "</footer>")
    return "".join(parts)


def report_hash(thread: str) -> str:
    """A short, stable key for a card's thread in button data (64-byte limit)."""
    return hashlib.sha1(thread.encode()).hexdigest()[:12]


def card_markup(sig: Signal, chat: str, thread: str) -> dict[str, Any]:
    """Under each alert card: Map (the app, on this warning) · Report what I see · Mute 1 hr."""
    from intelnet import delivery, miniapp

    h = report_hash(thread)
    db.kv_set(f"rw:{h}", thread)
    row: list[dict[str, Any]] = []
    url = miniapp.app_url(chat, focus=str(sig.evidence.get("alert_id") or ""), tab="now")
    if url:
        row.append({"text": "🗺 Map", "web_app": {"url": url}})
    row.append({"text": "📍 Report what I see", "callback_data": f"rw:{h}"})
    row.append({"text": "🔔 Unmute", "callback_data": "unmute"} if delivery.muted(chat)
               else {"text": "🔕 Mute 1 hr", "callback_data": "mute:60"})
    return {"inline_keyboard": [row]}


def format_alert(sig: Signal, county_labels: list[str], *, ended: str | None = None,
                 ended_at: datetime | None = None, personal: list[str] | None = None) -> str:
    """An alert card. `ended` closes it: 'expired', 'cancelled', or 'area' (left your area).
    `personal` holds the reader's own lines (see `where_you_are`)."""
    ev = sig.evidence
    sev = str(ev.get("severity") or "Unknown")
    event = esc(ev.get("event") or sig.metric)
    where = esc(", ".join(county_labels) if county_labels else sig.location.describe())
    link = href(ev.get("url"))
    if ended:
        lines = [f"{_ENDED_HEAD.get(ended, _ENDED_HEAD['expired'])} · {tg_time(ended_at or utcnow())}"]
        lines += [event, f"Still in effect for {where}"] if ended == "area" else [f"<s>{event}</s>", where]
    else:
        lines = [f"{_SEVERITY_EMOJI.get(sev, '•')} <b>{event}</b>", *(personal or []), where]
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


def format_alert_change(sig: Signal, changes: list[str], *, back: bool = False,
                        now_covers_you: bool = False) -> str:
    """The follow-up (with sound) when an alert someone already has gets worse for them."""
    event = esc(sig.evidence.get("event") or sig.metric)
    if back:
        head = f"⚠️ <b>{event}</b> is back in effect for your area"
    elif changes:
        head = f"⬆️ <b>{event} updated</b>: {esc('; '.join(changes))}"
        if now_covers_you:
            head += ". It now covers your location"
    else:
        head = f"📍 <b>{event}</b> now covers your location"
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
    what = (m.label if m.is_flag else f"{m.label}: {m.display(sig.value)}") if m else f"{sig.metric}: {sig.value}"
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


def _edit_card(thread: str, chat: str, card: Any, text: str, version: str, *,
               rich: str | None = None, markup: dict[str, Any] | None = None) -> int | None:
    """Queue an edit of a chat's card, unless the card (or a queued edit) already says this.
    The rich version and the buttons go with it (an edit without buttons removes them)."""
    if card["text"] == text:
        return None
    pending = db.pending_in_thread(thread, chat, "edit")
    if pending is not None and pending["text"] == text:
        return int(pending["id"])
    db.supersede_outbox(thread, chat, ("edit",))          # only the newest edit matters
    return db.enqueue(f"{thread}:edit:{version}", chat, text, action="edit", thread=thread,
                      priority=EDIT_PRIORITY, stale_at=utcnow() + timedelta(hours=24),
                      rich=rich, markup=markup)


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
    version = str(sig.evidence.get("alert_id") or sig.key)
    stale = sig.expires_at or utcnow() + timedelta(hours=6)
    audience = _alert_audience(sig, siblings)
    have = db.cards(thread)
    ids: list[int | None] = []
    for chat, priority in audience.items():
        w = locate(sig, chat)
        inside = bool(w.inside)
        text = format_alert(sig, labels, personal=w.lines)
        rich = format_alert_rich(sig, labels, where=w)
        markup = card_markup(sig, chat, thread)
        if inside:
            priority = -1                                 # inside the polygon: first out
        card = have.get(chat)
        if card is None:
            pending = db.pending_in_thread(thread, chat, "card")
            if pending is not None:
                db.retext_outbox(pending["id"], text, rich, markup)   # still queued: it leaves up to date
            else:
                ids.append(db.enqueue(thread, chat, text, action="card", thread=thread,
                                      priority=priority, stale_at=stale, rich=rich, markup=markup))
                # slow hazards ask while they're in effect (the pack says which)
                ids.append(asks.queue(sig, chat, thread, when="issued",
                                      priority=_PRIORITY["reports"], stale_at=stale))
            continue
        ids.append(_edit_card(thread, chat, card, text, version, rich=rich, markup=markup))
        back = card["state"] == "ended"
        now_covers_you = inside and INSIDE not in (card["text"] or "")
        if back or changes or now_covers_you:
            db.update_card(thread, chat, state="active")
            ids.append(db.enqueue(f"{thread}:up:{version}", chat,
                                  format_alert_change(sig, changes or [], back=back,
                                                      now_covers_you=now_covers_you),
                                  action="reply", thread=thread, priority=priority, stale_at=stale))
    db.supersede_outbox(thread, actions=("card",), keep_chats=audience)
    closed = None
    for chat, card in have.items():
        if card["state"] == "active" and chat not in audience:
            closed = closed or format_alert(sig, labels, ended="area")
            db.update_card(thread, chat, state="ended")
            ids.append(_edit_card(thread, chat, card, closed, f"gone:{version}",
                                  rich=format_alert_rich(sig, labels, ended="area", ended_at=utcnow())))
    return delivery.send_now(ids)


def fanout_alert_ended(sig: Signal, siblings: list[Signal], reason: str, ended_at: datetime) -> int:
    """Close every card for an alert that ended ('expired' or 'cancelled').

    Severe and Extreme alerts also get a quiet all-clear reply under the card: the
    message most alert services never send. Where the pack has a question for this
    kind of alert, the reply asks it too (asks.py): what did it bring to your place?
    """
    if not bot.enabled:
        return 0
    siblings = siblings or [sig]
    thread = f"alert:{sig.group_key or sig.key}"
    labels = _county_labels(siblings)
    closed = format_alert(sig, labels, ended=reason, ended_at=ended_at)
    closed_rich = format_alert_rich(sig, labels, ended=reason, ended_at=ended_at)
    db.supersede_outbox(thread, actions=("card", "reply"))    # nothing still queued is news now
    severe = str(sig.evidence.get("severity")) in ("Severe", "Extreme")
    stamp = iso(ended_at)
    ids: list[int | None] = []
    for chat, card in db.cards(thread).items():
        if card["state"] != "active":
            continue
        db.update_card(thread, chat, state="ended")
        ids.append(_edit_card(thread, chat, card, closed, f"end:{stamp}", rich=closed_rich))
        clear = format_all_clear(sig, labels, ended_at) if severe else None
        asked = asks.queue(sig, chat, thread, when="ended", head=clear, ended_at=ended_at,
                           priority=_PRIORITY["reports"], stale_at=ended_at + timedelta(hours=6))
        if asked is not None:
            ids.append(asked)
        elif clear:
            ids.append(db.enqueue(f"{thread}:ended:{stamp}", chat, clear, action="reply",
                                  thread=thread, silent=True, priority=_PRIORITY["reports"],
                                  stale_at=ended_at + timedelta(hours=2)))
    return delivery.send_now(ids)


def fanout_event(ev: dict[str, Any], reason: str, area_keys: list[str]) -> int:
    """A verified event goes out as its storm's card (stories.py) when it has one: one
    message per storm, edited as it grows. An event outside any story is its own push."""
    topic = get_topic(ev["topic"])
    story_id = db.story_of("event", str(ev["id"]))
    if story_id is not None:
        return fanout_story(stories.current(story_id), reason, area_keys, topic.category_key("events"), ev)
    key = f"event:{ev['id']}:{'esc' if reason == 'escalated' else 'new'}:{ev.get('pushed_score')}"
    return push(topic.category_key("events"), area_keys, key, format_event(ev, reason))


# ── storm cards (one per story per chat, edited as the storm grows) ──────

def _severity_mark(severity: float) -> str:
    return "🟥" if severity >= 0.8 else "🟧" if severity >= 0.5 else "🟨"


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def format_story(story_id: int) -> str:
    """A storm card: what it is, where, since when, its brief, and its numbered facts
    (warnings, then events by severity) that the brief cites."""
    from intelnet import story_brief

    s = db.story(story_id)
    if s is None:
        return ""
    members = db.story_members(story_id)
    names = [c.name for c in (geo.county(f) for f in json.loads(s["counties_json"] or "[]")) if c]
    opened, updated = parse_iso(s["opened_at"]), parse_iso(s["updated_at"])
    lines = [f"{_severity_mark(s['severity'])} <b>{esc(s['title'] or 'Storm')}</b>"]
    fresh = updated and (not opened or (updated - opened).total_seconds() >= 60)
    when = [f"since {tg_time(opened, 't')}" if opened else "", f"updated {tg_time(updated, 't')}" if fresh else ""]
    where = f"{_names(names)} {'County' if len(names) == 1 else 'counties'}" if names else ""
    lines.append(" · ".join(x for x in [esc(where), *when] if x))
    fs = story_brief.facts(story_id)
    brief = story_brief.current(story_id)
    if brief:
        lines.append(f"<i>{esc(brief)}</i>")
    for n, f in enumerate(fs, 1):
        if f.kind == "alert":
            event = esc(f.row["event"])
            lines.append(f"✅ <s>{event}</s> ended [{n}]" if f.row["ended"] else f"⚠️ {event} [{n}]")
        else:
            e = f.row
            k = e.get("n_sensors") or 0
            lines.append(f"• {esc(e['title'] or '')} · {k} sensor{'s' if k != 1 else ''}"
                         + (" · verified" if e["verified"] else "") + f" [{n}]")
    more = sum(1 for m in members if m["kind"] == "event") - story_brief.MAX_EVENTS
    if more > 0:
        lines.append(f"• and {more} more")
    return "\n".join(lines)


def _story_version(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def fanout_story(story_id: int, reason: str, area_keys: list[str], category: str,
                 ev: dict[str, Any] | None = None) -> int:
    """Bring a storm's cards up to date: a new card for each subscriber of `category` in
    the event's area, a silent edit for everyone who has one, and a reply with sound when
    the event `ev` escalated. Returns messages sent (edits don't count)."""
    if not bot.enabled:
        return 0
    thread = f"story:{story_id}"
    text = format_story(story_id)
    version = _story_version(text)
    have = db.cards(thread)
    ids: list[int | None] = []
    audience = set(db.matching_chat_ids(category, area_keys))
    for chat in audience:
        card = have.get(chat)
        if card is None:
            pending = db.pending_in_thread(thread, chat, "card")
            if pending is not None:
                db.retext_outbox(pending["id"], text)
            else:
                ids.append(db.enqueue(thread, chat, text, action="card", thread=thread,
                                      priority=_PRIORITY["events"], stale_at=utcnow() + timedelta(hours=6)))
            continue
        ids.append(_edit_card(thread, chat, card, text, version))
        if reason == "escalated" and ev:
            ids.append(db.enqueue(f"{thread}:esc:{ev.get('id')}:{ev.get('pushed_score')}", chat,
                                  f"📈 <b>Escalating</b>: {esc(ev.get('title') or '')}", action="reply",
                                  thread=thread, priority=_PRIORITY["events"],
                                  stale_at=utcnow() + timedelta(hours=6)))
    for chat, card in have.items():
        if chat not in audience:
            ids.append(_edit_card(thread, chat, card, text, version))
    return delivery.send_now(ids)


def story_changed(joined: Any) -> int:
    """A story grew (or swallowed another): hand merged stories' cards to it, then edit
    every card it has, silently. New cards only ever come from a verified event."""
    if joined is None or not bot.enabled:
        return 0
    for gone in joined.merged or []:
        db.move_thread(f"story:{gone}", f"story:{joined.story_id}")
    thread = f"story:{joined.story_id}"
    have = db.cards(thread)
    if not have:
        return 0
    text = format_story(joined.story_id)
    version = _story_version(text)
    return delivery.send_now([_edit_card(thread, chat, card, text, version) for chat, card in have.items()])


def fanout_report(sig: Signal) -> int:
    from intelnet.models import public_handle

    topic = get_topic(sig.topic)
    return push(topic.category_key("reports"), sig.location.area_keys(), f"report:{sig.key}",
                format_report(sig, public_handle(sig.sensor_id)), exclude_chat=_chat_of(sig.sensor_id))


def _chat_of(sensor_id: str) -> str | None:
    m = re.match(r"^tg:(-?\d+)$", sensor_id)
    return m.group(1) if m else None
