"""Subscriptions — category × area, and the fan-out of pushes to chats.

A subscription is `(chat, category, area)`:

* **category** comes from a topic pack — `weather.warnings`, `weather.alerts`,
  `weather.events`, `weather.reports`, `weather.digest` (bare `warnings` is
  accepted when unambiguous).
* **area** is a key in the geo hierarchy — `il`, `il.cook`, `il.zip.60601`,
  `il.zip.60601-1234`. A signal carries every key it sits inside, so a
  county subscriber gets everything in the county and a ZIP+4 subscriber only
  their block.

Every push is deduplicated per (message key, chat) through `notify_log`, so a
chat subscribed at both county and ZIP level is told once.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timezone
from typing import Any

from intelnet import db, geo
from intelnet.config import settings
from intelnet.models import Signal
from intelnet.telegram import bot, esc, href
from intelnet.topics import all_categories, find_metric, get_topic, resolve_category

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


def parse_subscription(args: str, default: geo.Location | None = None) -> ParsedSubscription | str:
    """'/subscribe <category> [area]' args → ParsedSubscription, or an error string."""
    parts = args.strip().split(maxsplit=1)
    if not parts:
        return "Usage: /subscribe <category> [area]\n" + categories_help()
    cat = resolve_category(parts[0])
    if cat is None:
        return f"Unknown category “{esc(parts[0])}”.\n" + categories_help()
    area = parse_area(parts[1] if len(parts) > 1 else None, default)
    if area is None:
        return (f"Unknown area “{esc(parts[1])}”. Use a county (cook), a ZIP (60601), "
                f"a ZIP+4 (60601-1234) or “{settings.geo_state.lower()}” for the whole state.")
    return ParsedSubscription(cat, area[0], area[1])


def categories_help() -> str:
    lines = ["<b>Categories</b>"]
    for key, desc in all_categories().items():
        lines.append(f"• <code>{esc(key)}</code> — {esc(desc)}")
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

def _when(sig: Signal) -> str:
    return sig.observed_at.astimezone(timezone.utc).strftime("%H:%MZ")


_SEVERITY_EMOJI = {"Extreme": "🚨", "Severe": "⚠️", "Moderate": "🔶", "Minor": "🔹", "Unknown": "•"}


def format_alert(sig: Signal, county_labels: list[str]) -> str:
    ev = sig.evidence
    sev = str(ev.get("severity") or "Unknown")
    head = f"{_SEVERITY_EMOJI.get(sev, '•')} <b>{esc(ev.get('event') or sig.metric)}</b>"
    lines = [head, esc(", ".join(county_labels) if county_labels else sig.location.describe())]
    if ev.get("nws_headline"):
        lines.append(f"<i>{esc(str(ev['nws_headline'])[:200])}</i>")
    if sig.expires_at:
        lines.append(f"Until {sig.expires_at.astimezone(timezone.utc).strftime('%a %H:%MZ')} · "
                     f"{esc(sev)} · {esc(ev.get('sender') or 'NWS')}")
    if ev.get("instruction"):
        lines.append(esc(str(ev["instruction"])[:280]))
    link = href(ev.get("url"))
    if link:
        lines.append(f'<a href="{link}">NWS alert</a>')
    return "\n".join(lines)


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


def format_report(sig: Signal, sensor_name: str) -> str:
    m = find_metric(sig.metric, get_topic(sig.topic))
    what = f"{m.label}: {m.display(sig.value)}" if m else f"{sig.metric}: {sig.value}"
    lines = [f"📝 <b>{esc(what)}</b>", f"{esc(sig.location.describe())} · {_when(sig)} · "
             f"{esc(sensor_name)} · {esc(sig.quality)}"]
    if sig.text:
        lines.append(f"<i>{esc(sig.text[:200])}</i>")
    return "\n".join(lines)


# ── fan-out ───────────────────────────────────────────────────────────────

def push(category: str, area_keys: list[str], key: str, text: str, *,
         exclude_chat: str | None = None) -> int:
    """Send `text` once to every chat subscribed to category at any area key."""
    sent = 0
    for chat_id in db.matching_chat_ids(category, area_keys):
        if exclude_chat is not None and str(chat_id) == str(exclude_chat):
            continue
        if db.already_notified(key, chat_id):
            continue
        if bot.send_to(chat_id, text):
            db.record_notification(key, chat_id)
            sent += 1
    return sent


def fanout_alert(sig: Signal, siblings: list[Signal]) -> int:
    """One NWS alert may cover several counties: push per county subscription."""
    topic = get_topic(sig.topic)
    sev = str(sig.evidence.get("severity") or "Unknown")
    cats = topic.alert_routing.get(sev) or ["alerts"]
    labels = [geo.county(s.location.county_fips).label for s in siblings
              if geo.county(s.location.county_fips)]
    text = format_alert(sig, labels)
    total = 0
    for s in siblings or [sig]:
        for cat in cats:
            total += push(topic.category_key(cat), s.location.area_keys(),
                          f"alert:{sig.group_key or sig.key}", text)
    return total


def fanout_event(ev: dict[str, Any], reason: str, area_keys: list[str]) -> int:
    topic = get_topic(ev["topic"])
    key = f"event:{ev['id']}:{'esc' if reason == 'escalated' else 'new'}:{ev.get('pushed_score')}"
    return push(topic.category_key("events"), area_keys, key, format_event(ev, reason))


def fanout_report(sig: Signal, sensor_name: str) -> int:
    topic = get_topic(sig.topic)
    return push(topic.category_key("reports"), sig.location.area_keys(), f"report:{sig.key}",
                format_report(sig, sensor_name), exclude_chat=_chat_of(sig.sensor_id))


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
