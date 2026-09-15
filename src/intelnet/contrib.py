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

from intelnet import db, language, llm, network, subscriptions
from intelnet.config import settings
from intelnet.language import ParsedSignal
from intelnet.models import Sensor, Signal, utcnow
from intelnet.network import Assessment
from intelnet.telegram import esc

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
        if a.push_event and a.event:
            c.pushes += subscriptions.fanout_event(a.event, a.push_reason, sig.location.area_keys())
        c.pushes += subscriptions.fanout_report(sig)
    if c.signals:
        db.touch_sensor(sensor.id)
    return c


def contribute_json(sensor: Sensor, payload: str, **kw: Any) -> Contribution:
    return contribute(sensor, payload.strip(), use_llm=False, **kw)


# ── acknowledgement ──────────────────────────────────────────────────────

def _assessment_note(a: Assessment) -> str:
    if a.quality == "corroborated":
        bits = []
        if a.n_corroborating:
            bits.append(f"agrees with {a.n_corroborating} nearby sensor(s)")
        if a.reference == "agree" and a.reference_signal is not None:
            ref = a.reference_signal
            what = ref.evidence.get("event") or ref.sensor_id.split(":", 1)[-1].upper()
            bits.append(f"backed by {what}")
        return "✅ corroborated — " + "; ".join(bits)
    if a.quality == "flagged":
        return (f"⚠️ conflicts with {a.n_contradicting} nearby reading(s)"
                + (" and the official reference" if a.reference == "disagree" else "")
                + " — kept, but marked")
    if a.quality == "rejected":
        return "✖ rejected (outside plausible range)"
    if a.n_contradicting:
        return (f"🕐 differs from {a.n_contradicting} nearby reading(s) — kept as unverified")
    return "🕐 first report here — the network will watch for neighbours"


def ack_text(c: Contribution, sensor: Sensor | None = None) -> str:
    """HTML acknowledgement for Telegram (also printed by the CLI)."""
    if c.rejected:
        return f"⛔ {esc(c.rejected)}"
    lines: list[str] = []
    if c.signals:
        lines.append(f"✅ <b>Recorded {len(c.signals)} reading{'s' if len(c.signals) != 1 else ''}</b>"
                     + (" <i>(parsed from prose)</i>" if c.used_llm else ""))
        for sig, a in zip(c.signals, c.assessments):
            m = a.metric
            what = f"{m.label}: {m.display(sig.value)}" if m else f"{sig.metric}: {sig.value}"
            lines.append(f"• <b>{esc(what)}</b> @ {esc(sig.location.describe())}")
            lines.append(f"  {esc(_assessment_note(a))}")
            if a.event and a.push_event:
                lines.append(f"  📍 event {esc(a.push_reason)}: {esc(a.event.get('title') or '')} "
                             f"(score {a.event.get('score', 0):.2f}) — pushed to subscribers")
            elif a.event and a.event_opened:
                lines.append("  📍 opened an event — unverified until a neighbour or an official "
                             "source agrees")
            elif a.event:
                lines.append(f"  📍 joined event: {esc(a.event.get('title') or '')} "
                             f"(score {a.event.get('score', 0):.2f})")
    for e in c.errors:
        lines.append(f"⚠️ {esc(e)}")
    if c.leftover and not c.signals:
        lines.append(f"🤔 Couldn't read “{esc(c.leftover[:120])}”.\n"
                     f"Try the short form, e.g. <code>rain 1.2in @62704</code> — /help lists the rest.")
    if sensor is not None and c.signals:
        s = db.get_sensor(sensor.id) or sensor
        lines.append(f"<i>Your trust {s.trust:.2f} · {s.n_corroborated} corroborated / "
                     f"{s.n_contradicted} conflicting · {s.n_signals} total</i>")
    if c.pushes:
        lines.append(f"<i>Pushed to {c.pushes} subscriber message(s).</i>")
    return "\n".join(lines) if lines else "Nothing to record."
