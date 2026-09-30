"""Weekly measures: is the network earning its people, and is it fast?

The six numbers the review asked for, from the network's own tables:

    activation     joined, then reported within 24 hours
    ask rate       questions after alerts that got an answer
    confirmation   minutes from a person's report to its first corroboration (median)
    coverage       counties with an active human sensor, of 102
    alert speed    seconds from NWS issuing an alert to the card landing (p50 / p95)
    fatigue        messages per subscriber per week, by kind

`intelnet metrics`, `/admin metrics`, and a Monday note to the admin chat.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from intelnet import db, delivery, geo, network
from intelnet.config import settings
from intelnet.models import KIND_HUMAN, iso, local_time, parse_iso, utcnow
from intelnet.telegram import bot, esc

FIRST_FANOUT = timedelta(minutes=30)     # a card queued later went to someone who subscribed late


def _t(value: str | None) -> datetime | None:
    """Both timestamp spellings in the DB: ISO ('…T…+00:00') and SQLite's 'YYYY-MM-DD HH:MM:SS'."""
    if not value:
        return None
    if "T" in value:
        return parse_iso(value)
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _sql_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, max(0, round(q * (len(values) - 1))))]


def weekly(days: int = 7) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    out: dict[str, Any] = {"days": days, "since": iso(since)}

    joined = db.measure_rows(
        """SELECT s.id, s.registered_at, MIN(g.received_at) AS first_at
             FROM sensors s LEFT JOIN signals g ON g.sensor_id = s.id
            WHERE s.kind = ? AND s.registered_at >= ? GROUP BY s.id""",
        (KIND_HUMAN, _sql_time(since)))
    active = [r for r in joined if r["first_at"] and _t(r["first_at"]) - _t(r["registered_at"])
              <= timedelta(hours=24)]
    out["joined"], out["activated"] = len(joined), len(active)

    asked = db.measure_rows(
        """SELECT q.answered_at FROM questions q
             JOIN outbox o ON o.key = q.thread || ':ask' AND o.chat_id = q.chat_id
            WHERE o.status = 'sent' AND q.asked_at >= ?""", (iso(since),))
    out["asked"], out["answered"] = len(asked), sum(1 for r in asked if r["answered_at"])

    readings = db.measure_rows(
        """SELECT received_at, settled_at, quality FROM signals
            WHERE sensor_kind = ? AND received_at >= ? AND metric NOT LIKE 'alert.%'""",
        (KIND_HUMAN, iso(since)))
    waits = [(_t(r["settled_at"]) - _t(r["received_at"])).total_seconds() / 60
             for r in readings if r["quality"] == "corroborated" and r["settled_at"]]
    out["human_readings"] = len(readings)
    out["corroborated"] = len(waits)
    out["confirm_minutes_median"] = round(statistics.median(waits), 1) if waits else None

    total = len(geo.counties())
    out["counties_covered"] = total - len(network.coverage_gaps(days))
    out["counties_total"] = total

    cards = db.measure_rows(
        """SELECT o.created_at, o.sent_at, t.opened_at FROM outbox o
             JOIN alert_threads t ON o.thread = 'alert:' || t.id
            WHERE o.action = 'card' AND o.status = 'sent' AND o.sent_at >= ?""", (iso(since),))
    lags = []
    for r in cards:
        opened, queued, landed = _t(r["opened_at"]), _t(r["created_at"]), _t(r["sent_at"])
        if opened and queued and landed and queued - opened <= FIRST_FANOUT:
            lags.append(max(0.0, (landed - opened).total_seconds()))
    out["alert_cards"] = len(lags)
    out["alert_seconds_p50"] = _pct(lags, 0.5)
    out["alert_seconds_p95"] = _pct(lags, 0.95)

    subscribers = db.measure_rows("SELECT COUNT(DISTINCT chat_id) AS n FROM subscriptions")[0]["n"]
    kinds = db.measure_rows(
        """SELECT CASE WHEN thread LIKE 'alert:%' AND key LIKE '%:ask' THEN 'questions'
                       WHEN thread LIKE 'alert:%' THEN 'alerts'
                       WHEN key LIKE 'digest:%' THEN 'briefs'
                       WHEN key LIKE 'event:%' THEN 'events'
                       WHEN key LIKE 'report:%' THEN 'reports'
                       WHEN key LIKE 'confirm:%' OR key LIKE 'ahead:%' OR key LIKE 'helped:%'
                            THEN 'notes'
                       ELSE 'other' END AS kind, COUNT(*) AS n
             FROM outbox WHERE status = 'sent' AND action != 'edit' AND sent_at >= ?
            GROUP BY kind""", (iso(since),))
    by_kind = {r["kind"]: r["n"] for r in kinds}
    out["subscribers"] = subscribers
    out["messages"] = by_kind
    week = 7 / days
    out["messages_per_subscriber_week"] = (round(sum(by_kind.values()) / subscribers * week, 1)
                                           if subscribers else None)
    return out


def _share(a: int, b: int) -> str:
    return f"{a} of {b} ({a / b:.0%})" if b else "none yet"


def lines(m: dict[str, Any]) -> list[str]:
    """The measures as plain lines (the CLI prints them; the admin message bolds the labels)."""
    p50, p95 = m["alert_seconds_p50"], m["alert_seconds_p95"]
    med = m["confirm_minutes_median"]
    msgs = ", ".join(f"{k} {v}" for k, v in sorted(m["messages"].items(), key=lambda kv: -kv[1]))
    return [
        f"Activation: {_share(m['activated'], m['joined'])} who joined reported within a day",
        f"Ask rate: {_share(m['answered'], m['asked'])} questions after alerts answered",
        f"Confirmation: {m['corroborated']} of {m['human_readings']} readings from people confirmed"
        + (f", median {med:g} min" if med is not None else ""),
        f"Coverage: {m['counties_covered']} of {m['counties_total']} counties have an active sensor",
        "Alert speed: " + (f"{p50:.0f} s median, {p95:.0f} s p95 from NWS to the chat "
                           f"({m['alert_cards']} cards)" if p50 is not None else "no alert cards"),
        "Fatigue: " + (f"{m['messages_per_subscriber_week']:g} messages per subscriber a week"
                       if m["messages_per_subscriber_week"] is not None else "no subscribers")
        + (f" ({msgs})" if msgs else ""),
    ]


def format_html(m: dict[str, Any]) -> str:
    head = f"📊 <b>{esc(settings.network_name)}</b> · the last {m['days']} days"
    body = []
    for ln in lines(m):
        label, _, rest = ln.partition(": ")
        body.append(f"<b>{esc(label)}</b>: {esc(rest)}")
    return "\n".join([head, *body])


def send_weekly(force: bool = False) -> int:
    """Monday's measures to the admin chat, once a week. Returns messages sent."""
    admin = settings.telegram_admin_chat_id
    now = utcnow()
    if not bot.enabled or not admin or (not force and local_time(now, "%a") != "Mon"):
        return 0
    week = local_time(now, "%G-W%V")
    return delivery.send_now([db.enqueue(f"metrics:{week}", admin, format_html(weekly(7)),
                                         silent=True, stale_at=now + timedelta(hours=12))])
