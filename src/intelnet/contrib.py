"""Accepting a contribution — the one path every human/bot reading takes.

    text ──grammar──▶ ParsedSignals ──(prose? LLM)──▶ Signals ──store──▶ assess ──▶ fan-out
                                                                                  └──▶ ack

Used by the Telegram bot and the `intelnet signal` CLI alike, so both get the
same rate limiting, the same location defaulting (the sensor's home), the
same corroboration and the same acknowledgement text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from intelnet import db, feedback, language, llm, network, subscriptions, trust
from intelnet.config import settings
from intelnet.language import ParsedSignal
from intelnet.models import Sensor, Signal, utcnow
from intelnet.network import Assessment
from intelnet.telegram import esc
from intelnet.topics import get_topic

logger = logging.getLogger(__name__)

RATE_WINDOW_MIN = 10


@dataclass
class Contribution:
    signals: list[Signal] = field(default_factory=list)
    assessments: list[Assessment] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    leftover: str = ""
    rejected: str | None = None          # whole-message rejection reason
    used_llm: bool = False
    pushes: int = 0

    @property
    def accepted(self) -> int:
        return len(self.signals)


def _to_signals(parsed: list[ParsedSignal], sensor: Sensor, *, source: str, source_id_base: str,
                message_time: datetime | None, extra_evidence: dict[str, Any] | None,
                errors: list[str]) -> list[Signal]:
    out: list[Signal] = []
    for i, p in enumerate(parsed):
        loc = p.location or sensor.location
        if not loc.has_point:
            errors.append(
                f"{p.metric.label}: no location — set your home with /home 62704-1234 "
                "(or share your location), or add @<zip|county> to the reading."
            )
            continue
        evidence: dict[str, Any] = {
            "kind": p.metric.kind, "typed": p.typed, "tags": p.tags, "confidence": p.confidence,
        }
        if extra_evidence:
            evidence.update(extra_evidence)
        out.append(Signal(
            source=source, source_id=f"{source_id_base}:{i}", sensor_id=sensor.id,
            sensor_kind=sensor.kind, topic=p.metric.topic, metric=p.metric.key,
            value=p.value, unit=p.metric.unit, text=p.note,
            observed_at=p.observed_at or message_time or utcnow(), received_at=utcnow(),
            location=loc, confidence=p.confidence, evidence=evidence,
        ))
    return out


def contribute(sensor: Sensor, text: str, *, source: str = "telegram", source_id_base: str,
               message_time: datetime | None = None, extra_evidence: dict[str, Any] | None = None,
               online: bool | None = None, use_llm: bool = True) -> Contribution:
    """Parse, store, assess and fan out one message from `sensor`."""
    c = Contribution()
    if sensor.status == "banned":
        c.rejected = "This sensor has been suspended by the network admin."
        return c
    if db.contributions_since(sensor.id, RATE_WINDOW_MIN) >= settings.network_rate_limit:
        c.rejected = (f"Slow down — more than {settings.network_rate_limit} readings in "
                      f"{RATE_WINDOW_MIN} minutes. Try again shortly.")
        return c

    if text.strip().startswith(("{", "[")):
        parsed = language.parse_json(text, online=online)
    else:
        parsed = language.parse(text, online=online)
        if not parsed.signals and parsed.leftover and use_llm and settings.llm_enabled:
            llm_parsed = llm.parse_free_text(parsed.leftover, online=online)
            if llm_parsed.signals or llm_parsed.errors:
                c.used_llm = True
                parsed.signals.extend(llm_parsed.signals)
                parsed.errors.extend(llm_parsed.errors)
                parsed.leftover = llm_parsed.leftover
    c.errors.extend(parsed.errors)
    c.leftover = parsed.leftover

    signals = _to_signals(parsed.signals, sensor, source=source, source_id_base=source_id_base,
                          message_time=message_time, extra_evidence=extra_evidence, errors=c.errors)
    for sig in signals:
        a = network.process(sig)
        if a is None:        # duplicate source_id (message re-delivered)
            continue
        c.signals.append(sig)
        c.assessments.append(a)
        n = 0
        if a.push_event and a.event:
            n = subscriptions.fanout_event(a.event, a.push_reason, sig.location.area_keys())
            c.pushes += n
        if a.metric is None or a.metric.scored:       # "nothing here" isn't news to push
            c.pushes += subscriptions.fanout_report(sig)
        subscriptions.story_changed(a.story)           # its storm's cards, edited quietly
        feedback.confirmed(a.settled)                  # neighbors this reading agreed with
        if a.push_event and a.event and a.push_reason == "new":
            feedback.helped(a.event, n, exclude_sensor=sensor.id)
    if c.signals:
        db.touch_sensor(sensor.id)
    return c


def contribute_json(sensor: Sensor, payload: str, **kw: Any) -> Contribution:
    return contribute(sensor, payload.strip(), use_llm=False, **kw)


# ── acknowledgement ──────────────────────────────────────────────────────

def _reference_words(ref: Signal) -> str:
    """'Severe Thunderstorm Warning in effect' · 'matches an NWS storm report' · 'matches the KSPI station'."""
    if ref.evidence.get("event"):
        return f"{ref.evidence['event']} in effect"
    if ref.sensor_kind == "official":
        return "matches an NWS storm report"
    if ref.sensor_kind == "station":
        s = db.get_sensor(ref.sensor_id)
        return f"matches the {s.name if s and s.name else ref.sensor_id.split(':', 1)[-1].upper()} station"
    return "matches an official source"


def _assessment_note(a: Assessment) -> str:
    """The status line under a recorded reading: Corroborated / Waiting / Flagged, and why."""
    if a.metric is not None and not a.metric.scored:
        return "<b>Noted</b>: quiet reports show where a storm didn't reach"
    if a.quality == "corroborated":
        bits = []
        if a.reference == "agree" and a.reference_signal is not None:
            bits.append(_reference_words(a.reference_signal))
        if a.n_corroborating:
            n = a.n_corroborating
            bits.append(f"{n} neighbor{'s' if n != 1 else ''} agree{'s' if n == 1 else ''}")
        return "<b>Corroborated</b>: " + esc("; ".join(bits) or "confirmed")
    if a.quality == "flagged":
        return (f"<b>Flagged</b>: conflicts with {a.n_contradicting} nearby reading{'s' if a.n_contradicting != 1 else ''}"
                + (" and the official reference" if a.reference == "disagree" else "") + ". Kept, but marked")
    if a.quality == "rejected":
        return "<b>Rejected</b>: outside the plausible range"
    if a.n_contradicting:
        return f"<b>Unverified</b>: differs from {a.n_contradicting} nearby reading{'s' if a.n_contradicting != 1 else ''}"
    return "<b>Waiting</b> for a neighbor or an official source to agree"


def ack_text(c: Contribution, sensor: Sensor | None = None, radar: dict[int, str] | None = None) -> str:
    """HTML acknowledgement for Telegram (also printed by the CLI): per reading, what was
    recorded where, and how it stands (with what radar shows there, when known)."""
    if c.rejected:
        return f"⛔ {esc(c.rejected)}"
    lines: list[str] = []
    radar = radar or {}
    if c.signals:
        for sig, a in zip(c.signals, c.assessments):
            m = a.metric
            what = (m.label if m.is_flag else f"{m.label} {m.display(sig.value).split(' (')[0]}") if m else f"{sig.metric} {sig.value}"
            lines.append(f"✅ <b>Recorded:</b> {esc(what)} at {esc(sig.location.describe())}")
            note = radar.get(sig.id or -1)
            lines.append(_assessment_note(a) + (f"; {esc(note)}" if note else ""))
            if a.event and a.push_event:
                lines.append(f"📍 Verified event: {esc(a.event.get('title') or '')}. Sent to subscribers")
            elif a.event and a.event_opened:
                lines.append("📍 Opened an event, unverified until a neighbor or an official source agrees")
            elif a.event:
                lines.append(f"📍 Joined event: {esc(a.event.get('title') or '')}")
        if c.used_llm:
            lines.append("<i>Read from your sentence.</i>")
    for e in c.errors:
        lines.append(f"⚠️ {esc(e)}")
    if c.leftover and not c.signals:
        lines.append(f"🤔 Couldn't read “{esc(c.leftover[:120])}”.\n"
                     f"Try the short form, e.g. <code>rain 1.2in @62704</code> — /help lists the rest.")
    if sensor is not None and c.signals:
        s = db.get_sensor(sensor.id) or sensor
        topic = get_topic(c.signals[0].topic)
        st = trust.standing(sensor.id, topic.name)
        lines.append(f"<i>Your {esc(topic.label.lower())} record: {esc(st.label)} · "
                     f"{s.n_signals} reading{'s' if s.n_signals != 1 else ''}</i>")
    if c.pushes:
        lines.append(f"<i>Pushed to {c.pushes} subscriber message(s).</i>")
    return "\n".join(lines) if lines else "Nothing to record."
