"""The Telegram bot — the network's front door, both directions.

PUSH (sensor → network): any message is a contribution. The grammar reads it;
prose goes to the local LLM; a shared location sets the sensor's home; a
photo's caption is the reading and the photo is kept as evidence.

PULL (network → sensor): /near, /alerts, /latest, /network answer from the
network's state; subscriptions (`/subscribe weather.warnings cook`) deliver
official alerts, corroborated events, raw reports and the daily digest to
the categories × areas each chat asked for.

Anyone can /join (optionally gated by NETWORK_JOIN_CODE). The admin chat
(TELEGRAM_ADMIN_CHAT_ID) has /admin. Long-polling, no public endpoint.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Self

from intelnet import asks, contrib, db, feedback, geo, language, metrics, network, subscriptions
from intelnet.config import settings
from intelnet.models import KIND_HUMAN, Sensor
from intelnet.telegram import MAX_MSG, bot, esc, href, join_within, tg_time
from intelnet.topics import all_categories, default_topic, find_metric, topics

logger = logging.getLogger(__name__)

_MAX_BACKOFF = 300
# A reading that reaches us this long after it was sent says so in the reply.
LATE_AFTER = timedelta(minutes=2)
# Telegram's "until I stop" live-location period, and how long we honor one.
_LIVE_UNTIL_STOPPED = 0x7FFFFFFF
LIVE_MAX = timedelta(hours=24)
# A site link: t.me/<bot>?start=sub_<topic>_<category>_<area> (area: county slug, ZIP, ZIP+4, il)
_START_SUB = re.compile(r"^sub_([a-z]+)_([a-z]+)_([a-z0-9-]+)$")

LOCATION_KEYBOARD = {
    "keyboard": [[{"text": "📍 Share my location", "request_location": True}]],
    "resize_keyboard": True, "one_time_keyboard": True,
    "input_field_placeholder": "or type a reading: rain 1.2in",
}
REMOVE_KEYBOARD = {"remove_keyboard": True}


class Reply(str):
    """Reply text that also carries a Telegram keyboard (a plain str everywhere else)."""

    markup: dict | None

    def __new__(cls, text: str, markup: dict | None = None) -> Self:
        obj = super().__new__(cls, text)
        obj.markup = markup
        return obj


# ── the report keyboard (each pack's `quick_reports`) ─────────────────────

def _quick_reports() -> list[dict]:
    """Every pack's report buttons, in pack order, tagged with where they came from."""
    return [dict(q, topic=t.name, index=i) for t in topics().values() for i, q in enumerate(t.quick_reports)]


def report_keyboard() -> dict | None:
    """The persistent report keyboard: three buttons a row."""
    buttons = [{"text": q["button"]} for q in _quick_reports() if q.get("keyboard", True)]
    if not buttons:
        return None
    return {"keyboard": [buttons[i:i + 3] for i in range(0, len(buttons), 3)],
            "resize_keyboard": True, "is_persistent": True,
            "input_field_placeholder": "Tap a button, or type a reading: rain 1.2in"}


def _undo(message_id: int | str, question: int | None = None) -> dict:
    if question is None:
        return {"inline_keyboard": [[{"text": "↩️ Undo", "callback_data": f"u:{message_id}"}]]}
    return {"inline_keyboard": [[{"text": "↩️ Undo", "callback_data": f"u:{message_id}:{question}"},
                                 {"text": "➕ Add another", "callback_data": f"a:{question}"}]]}


def cmd_report(message: dict, sensor: Sensor | None, args: str) -> str:
    kb = report_keyboard()
    if not args.strip() and kb:
        return Reply("Tap what you see. It's recorded where you are: your live location if "
                     "you're sharing one, else your home. Typing works too: <code>rain 1.2in</code>.", kb)
    return _contribute(message, sensor, args)                  # /report rain 1in, as before


def _quick(message: dict, sensor: Sensor | None, q: dict) -> str:
    """A report-keyboard button: open its picker, or record its reading now (with Undo)."""
    if q.get("choices"):
        buttons = [{"text": label, "callback_data": f"q:{q['topic']}:{q['index']}:{j}"}
                   for j, (label, _reading) in enumerate(q["choices"])]
        return Reply(esc(str(q.get("ask") or "Pick one:")),
                     {"inline_keyboard": [buttons[i:i + 3] for i in range(0, len(buttons), 3)]})
    reply = _contribute(message, sensor, str(q["send"]))
    user_id = (message.get("from") or {}).get("id")
    base = f"{message['chat']['id']}:{message.get('message_id')}"
    recorded = db.message_signals("telegram", _sensor_id(user_id), base) if user_id else []
    return Reply(reply, _undo(message.get("message_id"))) if recorded else reply


def handle_callback(cq: dict) -> None:
    """A tap on an inline button: record the picked reading, or undo one, in place."""
    data = str(cq.get("data") or "")
    user = cq.get("from") or {}
    msg = cq.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    if not user.get("id") or chat is None or mid is None:
        bot.answer_callback(str(cq.get("id")))
        return
    sensor = db.get_sensor(_sensor_id(user["id"]))
    if data.startswith("q:"):
        try:
            _, topic, i, j = data.split(":")
            label, reading = topics()[topic].quick_reports[int(i)]["choices"][int(j)]
        except (ValueError, KeyError, IndexError):
            bot.answer_callback(str(cq["id"]), "That button has expired.")
            return
        # The picker is this reading's message: its readings are the ones Undo removes.
        pick = {"chat": msg["chat"], "from": user, "message_id": mid, "date": int(time.time()),
                "text": reading}
        text = _contribute(pick, sensor, reading)
        recorded = db.message_signals("telegram", _sensor_id(user["id"]), f"{chat}:{mid}")
        bot.answer_callback(str(cq["id"]), str(label))
        bot.edit(chat, mid, text, markup=_undo(mid) if recorded else None)
    elif data.startswith("a:"):
        _answer(cq, data, user, msg, sensor)
    elif data.startswith("u:") and sensor is not None:
        target, _, question = data[2:].partition(":")
        gone = db.message_signals("telegram", sensor.id, f"{chat}:{target}")
        network.withdraw(gone)
        bot.answer_callback(str(cq["id"]), "Removed" if gone else "Nothing to undo")
        q = asks.resolve(f"a:{question}", chat) if question else None
        if q is not None:                                  # back to the question, to answer again
            text, markup = q["show"]
            bot.edit(chat, mid, "↩️ Removed.\n\n" + text, markup=markup)
        elif gone:
            bot.edit(chat, mid, "↩️ Removed. Tap a button to report again.")
    else:
        bot.answer_callback(str(cq["id"]))


def _answer(cq: dict, data: str, user: dict, msg: dict, sensor: Sensor | None) -> None:
    """A button under a question after an alert: open a report, go back, or record the pick."""
    chat, mid = msg["chat"]["id"], msg["message_id"]
    q = asks.resolve(data, chat)
    if q is None:
        bot.answer_callback(str(cq["id"]), "That question has expired.")
        return
    if "show" in q or "pick" in q:
        text, markup = q.get("show") or q["pick"]
        bot.answer_callback(str(cq["id"]))
        bot.edit(chat, mid, text, markup=markup)
        return
    qid = int(q["question"]["id"])
    # Each pick is its own reading under this message, so "Add another" can follow and
    # Undo (which takes back everything from the message) still reaches them all.
    n = len(db.message_signals("telegram", _sensor_id(user["id"]), f"{chat}:{mid}"))
    pick = {"chat": msg["chat"], "from": user, "message_id": f"{mid}:a{n}", "date": int(time.time()),
            "text": q["reading"]}
    text = _contribute(pick, sensor, q["reading"], observed=q["observed"])
    recorded = db.message_signals("telegram", _sensor_id(user["id"]), f"{chat}:{mid}")
    if len(recorded) > n:
        db.answer_question(qid)
    bot.answer_callback(str(cq["id"]), str(q["label"]))
    bot.edit(chat, mid, text, markup=_undo(mid, qid) if recorded else None)


# ── helpers ───────────────────────────────────────────────────────────────

def _sensor_id(user_id: int | str) -> str:
    return f"tg:{user_id}"


def _display_name(user: dict) -> str:
    name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x).strip()
    return name or (f"@{user['username']}" if user.get("username") else f"user {user.get('id')}")


def _is_admin(chat_id: str | int) -> bool:
    return bool(settings.telegram_admin_chat_id) and str(chat_id) == str(settings.telegram_admin_chat_id)


def _message_time(message: dict) -> datetime | None:
    ts = message.get("date")
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


def _register(user: dict, chat_id: str | int) -> Sensor:
    s = Sensor(id=_sensor_id(user["id"]), kind=KIND_HUMAN, name=_display_name(user),
               chat_id=str(chat_id), username=user.get("username"))
    return db.upsert_sensor(s)


def help_text() -> str:
    topic = default_topic()
    return (
        f"👋 <b>{esc(settings.network_name)}</b> — a sensor network you can join from your phone.\n"
        f"Every reading you send is checked against neighbors and official sources; "
        f"corroborated readings become events and raise your trust.\n\n"
        f"<b>Report</b> with buttons: /report · or just type it — weather, soil, water, crops, air:\n"
        f"<code>{esc(language.cheatsheet())}</code>\n"
        f"Plain sentences work too (“golf-ball hail here 5 min ago”). /topics lists every metric.\n\n"
        f"<b>Set up</b>: /join · /home 62704-1234 (or share your location) · /me\n"
        f"<b>Pull</b>: /near [place] · /alerts [county] · /latest · /network\n"
        f"<b>Subscribe</b>: /subscribe warnings cook · /subscribe events 62704 · "
        f"/subscribe soil.events · /subscribe *.events · /subscribe digest · /subs · /unsubscribe all\n"
        f"<b>Digest by email</b>: /digest you@example.com\n"
        f"<b>Automated sensors</b>: /signal {esc(language.as_json_example(topic))}\n"
        f"<b>Follow-ups</b> (a question after an alert, a note when a report is confirmed): "
        f"/followups off\n"
        f"<b>Privacy</b>: /privacy · delete everything about you: /forget"
    )


# ── command handlers: (message, sensor|None, args) → reply text ──────────

def cmd_join(message: dict, sensor: Sensor | None, args: str) -> str:
    user = message.get("from") or {}
    if sensor is not None:
        return ("You're already a sensor here. Set your home with /home <zip> or share your "
                "location, then just send readings.")
    if settings.network_join_code and args.strip() != settings.network_join_code:
        return "This network needs a join code: <code>/join &lt;code&gt;</code>"
    s = _register(user, message["chat"]["id"])
    return (f"✅ Welcome, {esc(s.name)} — you're sensor <code>{esc(s.id)}</code>.\n"
            f"Next: <code>/home 62704-1234</code> (your ZIP+4, ZIP or county) or share your "
            f"location, so readings land on the map. Then just type what you see: "
            f"<code>rain 1.2in</code>, <code>hail quarter</code>, <code>trees down</code>.")


def cmd_home(message: dict, sensor: Sensor | None, args: str) -> str:
    if sensor is None:
        return "Send /join first."
    if not args.strip():
        return ("Usage: /home <ZIP+4 | ZIP | county | lat,lon | place> — or share your location.\n"
                f"Current: {esc(sensor.location.describe())}")
    loc = geo.parse_location(args)
    if loc is None or not loc.has_point:
        return f"Couldn't place “{esc(args)}”. Try a ZIP (62704), ZIP+4 (62704-1234) or a county."
    db.set_sensor_location(sensor.id, loc)
    return Reply(f"🏠 Home set: <b>{esc(loc.describe())}</b> (precision: {esc(loc.precision)})",
                 report_keyboard())


def _live_until(message: dict, p: dict) -> datetime:
    sent = _message_time(message) or datetime.now(timezone.utc)
    period = int(p.get("live_period") or 0)
    return sent + (LIVE_MAX if period >= _LIVE_UNTIL_STOPPED else min(timedelta(seconds=period), LIVE_MAX))


def cmd_location(message: dict, sensor: Sensor | None) -> str:
    """A shared location: a one-off share sets home; a live share is where you are now."""
    if sensor is None:
        if settings.network_join_code:
            return "Join first: <code>/join &lt;code&gt;</code>, then share your location again."
        sensor = _register(message.get("from") or {}, message["chat"]["id"])
    p = message["location"]
    if p.get("live_period"):
        until = _live_until(message, p)
        loc = geo.location_from_point(float(p["latitude"]), float(p["longitude"]), online=False)
        db.set_live_location(message["chat"]["id"], loc, until)
        home = (f"Home stays {esc(sensor.location.describe())}." if sensor.location.has_point
                else "Set a home any time with <code>/home 62704</code>.")
        return Reply(f"📡 Following your live location until {tg_time(until, 't')}. Readings you send "
                     f"land where you are, and alerts check it. {home}",
                     report_keyboard() or REMOVE_KEYBOARD)
    loc = geo.location_from_point(float(p["latitude"]), float(p["longitude"]))
    db.set_sensor_location(sensor.id, loc)
    return Reply(f"🏠 Home set from your location: <b>{esc(loc.describe())}</b>\n"
                 f"Now tap a button below when you see something, or type it: <code>rain 1.2in</code>.",
                 report_keyboard() or REMOVE_KEYBOARD)


def cmd_start(message: dict, sensor: Sensor | None, args: str) -> str:
    """/start, maybe carrying a site link (sub_<topic>_<category>_<area>): join, subscribe
    and offer the share-location button in one tap."""
    m = _START_SUB.match(args.strip().lower())
    if not m:
        located = sensor is not None and sensor.location.has_point
        return help_text() if located else Reply(help_text(), LOCATION_KEYBOARD)
    if sensor is None:
        if settings.network_join_code:
            return "This network needs a join code: <code>/join &lt;code&gt;</code>, then open the link again."
        sensor = _register(message.get("from") or {}, message["chat"]["id"])
    topic, category, area = m.groups()
    lines = [f"👋 Welcome to the {esc(settings.network_name)}.",
             cmd_subscribe(message, sensor, f"{topic}.{category} {area}")]
    if sensor.location.has_point:
        lines.append("Report what you see by typing it, e.g. <code>rain 1.2in</code>. /help lists the rest.")
        return "\n".join(lines)
    lines.append("Next, tap <b>📍 Share my location</b> below (or send <code>/home 62704</code>) so your "
                 "readings land on the map. Then type what you see, e.g. <code>rain 1.2in</code>.")
    return Reply("\n".join(lines), LOCATION_KEYBOARD)


def cmd_me(message: dict, sensor: Sensor | None, args: str) -> str:
    if sensor is None:
        return "You haven't joined yet — send /join."
    subs = db.subscriptions_for(sensor.chat_id or "")
    live = db.live_location(sensor.chat_id or "")
    lines = [
        f"<b>{esc(sensor.name)}</b> · <code>{esc(sensor.id)}</code>",
        f"Home: {esc(sensor.location.describe())}"
        + (f" · live location on ({esc(live.describe())})" if live else ""),
        f"Trust {sensor.trust:.2f} · {sensor.n_corroborated} corroborated / "
        f"{sensor.n_contradicted} conflicting · {sensor.n_signals} readings",
        "Subscriptions: " + (", ".join(f"{r['category']}@{subscriptions.area_label(r['area'])}"
                                       for r in subs) if subs else "none"),
    ]
    return "\n".join(lines)


def cmd_subscribe(message: dict, sensor: Sensor | None, args: str) -> str:
    parsed = subscriptions.parse_subscriptions(args, sensor.location if sensor else None)
    if isinstance(parsed, str):
        return parsed
    chat_id = message["chat"]["id"]
    lines = []
    for p in parsed:
        if p.category.endswith(".digest"):
            p.area = settings.geo_state.lower()   # digest is state-wide
            p.area_label = settings.geo_state
        added = db.add_subscription(chat_id, p.category, p.area)
        verb = "Subscribed" if added else "Already subscribed"
        lines.append(f"🔔 {verb}: <b>{esc(p.category)}</b> @ {esc(p.area_label)}")
    return "\n".join(lines)


def cmd_unsubscribe(message: dict, sensor: Sensor | None, args: str) -> str:
    chat_id = message["chat"]["id"]
    a = args.strip()
    if not a or a.lower() == "all":
        n = db.remove_subscription(chat_id)
        return f"🔕 Removed {n} subscription(s)."
    parsed = subscriptions.parse_subscriptions(a, sensor.location if sensor else None)
    if isinstance(parsed, str):
        return parsed
    parts = a.split(maxsplit=1)
    n = sum(db.remove_subscription(chat_id, p.category, p.area if len(parts) > 1 else None)
            for p in parsed)
    what = parsed[0].category if len(parsed) == 1 else f"{len(parsed)} categories"
    return f"🔕 Removed {n} subscription(s) for {esc(what)}."


def cmd_subs(message: dict, sensor: Sensor | None, args: str) -> str:
    rows = db.subscriptions_for(message["chat"]["id"])
    if not rows:
        return "No subscriptions yet.\n" + subscriptions.categories_help()
    return "<b>Your subscriptions</b>\n" + "\n".join(
        f"• {esc(r['category'])} @ {esc(subscriptions.area_label(r['area']))}" for r in rows
    )


def _fmt_age(dt: datetime) -> str:
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    return f"{mins}m ago" if mins < 90 else f"{mins // 60}h ago"


def cmd_near(message: dict, sensor: Sensor | None, args: str) -> str:
    hours = 3.0
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*h\b", args)
    if m:
        hours = float(m.group(1))
        args = (args[: m.start()] + args[m.end():]).strip()
    loc = geo.parse_location(args) if args.strip() else (sensor.location if sensor else None)
    if loc is None or not loc.has_point:
        return "Where? <code>/near 62704</code>, <code>/near cook</code>, or set /home first."
    view = network.near(loc, hours=hours)
    lines = [f"📡 <b>Around {esc(loc.describe())}</b> · last {hours:g}h · {view['radius_km']:.0f} km"]
    if view["alerts"]:
        lines.append("<b>Active alerts</b>: " + ", ".join(
            esc(a.evidence.get("event") or a.metric) for a in view["alerts"][:6]))
    for key, rows in view["readings"].items():
        metric = find_metric(key)
        if metric is None:
            continue
        humans = [r for r in rows if r.sensor_kind == "human"]
        refs = [r for r in rows if r.sensor_kind != "human"]
        vals = [r.value for r in rows if r.value is not None]
        if metric.is_flag:
            summary = f"{len(rows)} report(s)"
        else:
            pick = min(vals) if metric.event_direction == "below" else max(vals)
            summary = f"peak {metric.display(pick)}" if vals else ""
        who = f"{len(humans)} sensor(s)" + (f", {len(refs)} station/official" if refs else "")
        latest = max(rows, key=lambda r: r.observed_at)
        lines.append(f"• <b>{esc(metric.label)}</b>: {esc(summary)} · {who} · {_fmt_age(latest.observed_at)}")
    if not view["readings"]:
        lines.append("No readings nearby in that window — be the first: <code>rain 0.3in</code>")
    for ev in view["events"][:5]:
        e = network.event_summary(ev)
        lines.append(f"📍 {esc(e['title'] or '')} · score {e.get('score') or 0:.2f}"
                     f"{' ✅' if e['verified'] else ' (unverified)'}")
    return join_within(lines, MAX_MSG)


def cmd_alerts(message: dict, sensor: Sensor | None, args: str) -> str:
    from intelnet.feeds.nws_alerts import active_alert_groups

    fips = None
    where = settings.geo_state
    if args.strip():
        loc = geo.parse_location(args, online=False)
        if loc is None:
            return f"Unknown area “{esc(args)}”."
        fips = loc.county_fips
        c = geo.county(fips)
        where = c.label if c else where
    elif sensor and sensor.location.county_fips:
        fips = sensor.location.county_fips
        where = geo.county(fips).label  # type: ignore[union-attr]
    groups = active_alert_groups(fips)
    if not groups:
        return f"✅ No active NWS alerts for {esc(where)}."
    lines = [f"<b>Active NWS alerts — {esc(where)}</b>"]
    for g in groups[:12]:
        s = g["signal"]
        until = tg_time(s.expires_at) if s.expires_at else "—"
        counties = ", ".join(g["counties"][:6]) + ("…" if len(g["counties"]) > 6 else "")
        link = href(s.evidence.get("url"))
        title = f"<a href=\"{link}\">{esc(s.evidence.get('event'))}</a>" if link else esc(s.evidence.get("event"))
        lines.append(f"• {title} ({esc(s.evidence.get('severity'))}) — {esc(counties)} · until {until}")
    return join_within(lines, MAX_MSG)


def cmd_latest(message: dict, sensor: Sensor | None, args: str) -> str:
    if not settings.gdrive_enabled:
        return ("📰 The digest's Google Drive home is offline for now. The network is still collecting "
                "readings and sending alerts; digest links will be back here when it returns.")
    row = db.latest_digest()
    if row is None:
        return "No digest published yet — the first one lands after tonight's run."
    lines = [f"📰 <b>Latest digest</b> · {esc(row['date'])}"]
    for label, key in (("Today's digest", "drive_url"), ("Always-current 'Latest' doc", "latest_url"),
                       ("All digests (Drive folder)", "folder_url")):
        link = href(row[key])
        if link:
            lines.append(f'• <a href="{link}">{label}</a>')
    lines.append("Get it pushed daily: /subscribe digest — or by email: /digest you@example.com")
    return "\n".join(lines)


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def cmd_digest_email(message: dict, sensor: Sensor | None, args: str) -> str:
    email = args.strip()
    if not _EMAIL.match(email):
        return "Usage: /digest you@example.com — shares the Drive folder with that address."
    db.add_email_subscriber(email, message["chat"]["id"])
    try:
        from intelnet.gdrive import publisher
        ok = publisher.add_reader(email)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bot: drive share failed: %s", exc)
        ok = False
    if ok:
        return f"📬 Shared the digest folder with <b>{esc(email)}</b> — Google will email the link."
    return (f"📬 Noted <b>{esc(email)}</b>. Drive sharing isn't connected yet; the admin will "
            f"share the folder on the next publish.")


def cmd_network(message: dict, sensor: Sensor | None, args: str) -> str:
    v = db.vitals()
    return (
        f"🕸 <b>{esc(settings.network_name)}</b>\n"
        f"Sensors: {v['sensors_total']} joined · {v['sensors_active_24h']} active today · "
        f"{v['sensors_active_7d']} this week\n"
        f"Readings 24h: {v['signals_24h_human']} human · {v['signals_24h_reference']} reference\n"
        f"Corroboration (7d): {v['corroboration_rate_7d']:.0%} of human readings\n"
        f"Coverage (7d): {v['counties_covered_7d']}/{v['counties_total']} counties with a human "
        f"sensor · {v['counties_reference_7d']} with any data\n"
        f"Events: {v['events_open']} open · Alerts active: {v['alerts_active']} · "
        f"Subscribers: {v['subscribers']}"
    )


def cmd_topics(message: dict, sensor: Sensor | None, args: str) -> str:
    """/topics [name] — every pack's categories, or one pack's full vocabulary."""
    from intelnet.topics import topics

    name = args.strip().lower()
    if name and name in topics():
        t = topics()[name]
        return (f"<b>{esc(t.label)}</b> — {esc(t.description)}\n"
                f"Categories: {esc(', '.join(t.category_keys))}\n<code>{esc(language.describe_metrics(t))}</code>")
    lines = ["<b>Topics</b> (send /topics &lt;name&gt; for the full vocabulary)"]
    for t in topics().values():
        n_flag = sum(1 for m in t.metrics.values() if m.is_flag)
        lines.append(f"• <b>{esc(t.name)}</b> — {esc(t.description)} "
                     f"<i>({len(t.metrics) - n_flag} metrics, {n_flag} flags)</i>")
    return join_within(lines, MAX_MSG)


def cmd_admin(message: dict, sensor: Sensor | None, args: str) -> str:
    if not _is_admin(message["chat"]["id"]):
        return "Admin only."
    parts = args.strip().split(maxsplit=2)
    sub = parts[0].lower() if parts else "stats"
    if sub == "stats":
        return cmd_network(message, sensor, "") + "\n" + esc(str(db.subscription_counts()))
    if sub == "sensors":
        rows = db.list_sensors(kind=KIND_HUMAN, limit=30)
        return "<b>Sensors</b>\n" + "\n".join(
            f"• {esc(s.id)} {esc(s.name)} · trust {s.trust:.2f} · {s.n_signals} · {esc(s.status)} · "
            f"{esc(s.location.describe())}" for s in rows) or "none"
    if sub in ("ban", "unban") and len(parts) >= 2:
        ok = db.set_sensor_status(parts[1], "banned" if sub == "ban" else "active")
        return f"{'✅' if ok else '✗'} {sub} {esc(parts[1])}"
    if sub == "trust" and len(parts) >= 3:
        try:
            ok = db.set_sensor_trust(parts[1], float(parts[2]))
        except ValueError:
            return "Usage: /admin trust <sensor_id> <0..1>"
        return f"{'✅' if ok else '✗'} trust {esc(parts[1])} = {parts[2]}"
    if sub == "metrics":
        days = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 7
        return metrics.format_html(metrics.weekly(days))
    if sub == "broadcast" and len(parts) >= 2:
        text = args.strip()[len("broadcast"):].strip()
        chats = {s.chat_id for s in db.list_sensors(kind=KIND_HUMAN, limit=10000) if s.chat_id}
        n = bot.broadcast(sorted(chats), f"📣 <b>{esc(settings.network_name)}</b>\n{esc(text)}")
        return f"Broadcast to {n} chat(s)."
    return ("Usage: /admin stats | metrics [days] | sensors | ban <id> | unban <id> | "
            "trust <id> <0..1> | broadcast <text>")


def cmd_followups(message: dict, sensor: Sensor | None, args: str) -> str:
    chat = message["chat"]["id"]
    arg = args.strip().lower()
    if arg in ("off", "stop", "no"):
        feedback.set_enabled(chat, False)
        return ("🔕 Follow-ups off: no questions after alerts, and no notes when your reports are "
                "confirmed. Alerts you subscribed to still arrive. /followups on to undo.")
    if arg in ("on", "start", "yes"):
        feedback.set_enabled(chat, True)
    on = feedback.enabled_for(chat)
    return (f"Follow-ups are <b>{'on' if on else 'off'}</b>: a question after an alert that reached "
            f"you (one tap to say what you saw), and a quiet note when one of your reports is "
            f"confirmed. At most {feedback.DAILY_MAX} notes a day. "
            f"{'/followups off' if on else '/followups on'} to change it.")


def cmd_privacy(message: dict, sensor: Sensor | None, args: str) -> str:
    base = settings.public_site_url
    lines = ["🔒 <b>Privacy</b>",
             "Other people only ever see you as a handle (like s-3f9a1) with a ZIP code and county — "
             "never your name, username, ZIP+4 or exact location."]
    if base:
        lines.append(f'<a href="{href(base + "privacy.html")}">Privacy Policy</a> · '
                     f'<a href="{href(base + "terms.html")}">Terms of Service</a>')
    lines.append("Delete your sensor record, readings, subscriptions and e-mail: <code>/forget confirm</code>")
    if settings.network_contact_email:
        lines.append(f"Contact: {esc(settings.network_contact_email)}")
    return "\n".join(lines)


def cmd_forget(message: dict, sensor: Sensor | None, args: str) -> str:
    user = message.get("from") or {}
    chat_id = message["chat"]["id"]
    if args.strip().lower() != "confirm":
        return ("This permanently deletes your sensor record, every reading you've sent, your subscriptions "
                "and any digest e-mail address.\nTo go ahead, send <code>/forget confirm</code>")
    out = db.forget_sensor(_sensor_id(user["id"]), chat_id)
    removed = 0
    if out["emails"]:
        try:
            from intelnet.gdrive import publisher

            removed = sum(1 for e in out["emails"] if publisher.remove_reader(e))
        except Exception:  # noqa: BLE001 — deletion from our records already happened
            logger.warning("bot: could not remove digest readers for a forgotten sensor")
    parts = [f"{out['signals']} reading(s)", f"{out['subscriptions']} subscription(s)"]
    if out["emails"]:
        parts.append(f"{len(out['emails'])} e-mail address(es)"
                     + (" (removed from the digest folder)" if removed else ""))
    if not out["sensor"] and not any((out["signals"], out["subscriptions"], out["emails"])):
        return "There's nothing stored about you."
    return "🗑 Deleted " + ", ".join(parts) + " and your sensor record. Send /join any time to start again."


COMMANDS: dict[str, Callable[[dict, Sensor | None, str], str]] = {
    "join": cmd_join, "home": cmd_home, "me": cmd_me,
    "subscribe": cmd_subscribe, "unsubscribe": cmd_unsubscribe, "subs": cmd_subs,
    "subscriptions": cmd_subs, "near": cmd_near, "alerts": cmd_alerts, "latest": cmd_latest,
    "digest": cmd_digest_email, "network": cmd_network, "topics": cmd_topics, "admin": cmd_admin,
    "privacy": cmd_privacy, "forget": cmd_forget, "report": cmd_report, "followups": cmd_followups,
}


# ── contributions ─────────────────────────────────────────────────────────

def _contribute(message: dict, sensor: Sensor | None, text: str, *, json_mode: bool = False,
                observed: datetime | None = None) -> str:
    """Record a message's readings. `observed` dates them (an answer about a storm that
    already passed) instead of the message's own time."""
    user = message.get("from") or {}
    if sensor is None:
        if settings.network_join_code:
            return "Join first: <code>/join &lt;code&gt;</code> (ask the admin for the code)."
        sensor = _register(user, message["chat"]["id"])
        prefix = f"✅ Auto-joined you as {esc(sensor.name)}.\n"
    else:
        prefix = ""
    evidence = {}
    photos = message.get("photo") or []
    if photos:
        evidence["photo_file_id"] = photos[-1].get("file_id")
    sent = _message_time(message)
    late = datetime.now(timezone.utc) - sent if sent else timedelta(0)
    if late > LATE_AFTER:
        evidence["received_late_min"] = int(late.total_seconds() // 60)
    live = db.live_location(message["chat"]["id"])
    if live is not None:          # out and about: readings land where they're sent from
        sensor = replace(sensor, location=live)
    source_id = f"{message['chat']['id']}:{message.get('message_id')}"
    fn = contrib.contribute_json if json_mode else contrib.contribute
    c = fn(sensor, text, source_id_base=source_id, message_time=observed or sent,
           extra_evidence=evidence or None)
    reply = contrib.ack_text(c, sensor)
    if sent and late > LATE_AFTER and c.signals:
        reply += (f"\n🕰 This reached the network {evidence['received_late_min']} min after you sent "
                  f"it (the bot was offline). It counts from when you sent it, {tg_time(sent, 't')}.")
    if not sensor.location.has_point and c.errors:
        reply += "\n\nTip: share your location once, or <code>/home 62704-1234</code>."
    return prefix + reply


def handle_message(message: dict) -> str | None:
    """Route one inbound message → reply text (HTML) or None to stay silent."""
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    if not chat.get("id") or not user.get("id"):
        return None
    sensor = db.get_sensor(_sensor_id(user["id"]))
    text = (message.get("text") or message.get("caption") or "").strip()
    if sensor and sensor.status == "banned" and not re.match(r"^/(forget|privacy)\b", text):
        return None     # suspended sensors can still read the policy and delete their data

    if message.get("location") and not text:
        return cmd_location(message, sensor)

    if text.startswith("/"):
        m = re.match(r"^/([A-Za-z_]+)(?:@\w+)?\s*(.*)$", text, re.DOTALL)
        if not m:
            return help_text()
        cmd, args = m.group(1).lower(), m.group(2).strip()
        if cmd == "start":
            return cmd_start(message, sensor, args)
        if cmd == "help":
            return help_text()
        if cmd in ("obs", "r"):
            return _contribute(message, sensor, args)
        if cmd == "signal":
            return _contribute(message, sensor, args, json_mode=True)
        handler = COMMANDS.get(cmd)
        if handler is None:
            return f"Unknown command /{esc(cmd)}.\n\n" + help_text()
        return handler(message, sensor, args)

    if not text:
        return None
    quick = next((q for q in _quick_reports() if q["button"] == text), None)
    if quick is not None:
        return _quick(message, sensor, quick)
    return _contribute(message, sensor, text)


# ── listener ──────────────────────────────────────────────────────────────

def handle_edit(message: dict) -> str | None:
    """An edited message: a live-location tick (kept, silently) or a corrected reading."""
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    if not chat.get("id") or not user.get("id"):
        return None
    sensor = db.get_sensor(_sensor_id(user["id"]))
    if sensor is None or sensor.status == "banned":
        return None
    p = message.get("location")
    if p:
        if p.get("live_period"):
            loc = geo.location_from_point(float(p["latitude"]), float(p["longitude"]), online=False)
            db.set_live_location(chat["id"], loc, _live_until(message, p))
        return None
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text or text.startswith("/"):
        return None
    earlier = db.message_signals("telegram", sensor.id, f"{chat['id']}:{message.get('message_id')}")
    network.withdraw(earlier)
    reply = _contribute(message, sensor, text)
    return ("✏️ <b>Corrected.</b> This replaces what the message said before.\n" + reply) if earlier else reply


def handle_updates(updates: list[dict]) -> int | None:
    """Answer each update in order. Returns the offset that confirms them to Telegram."""
    offset = None
    for u in updates:
        offset = u["update_id"] + 1
        if u.get("callback_query"):
            try:
                handle_callback(u["callback_query"])
            except Exception:  # noqa: BLE001
                logger.exception("bot: button handler failed")
            continue
        message = u.get("message") or u.get("edited_message")
        if not message:
            continue
        try:
            if "message" in u:
                bot.typing(message["chat"]["id"])
                reply = handle_message(message)
            else:
                reply = handle_edit(message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("bot: handler failed")
            reply = f"⚠️ Something went wrong: {esc(str(exc))}"
        if reply:
            bot.send_to(message["chat"]["id"], reply, markup=getattr(reply, "markup", None))
    return offset


def run_listener(poll_timeout: int = 30) -> None:
    """Block forever answering messages. Intended for a launchd KeepAlive job.

    Messages sent while the bot was down wait on Telegram's side (up to 24 h) and
    are answered first, in order: a reading sent during an outage still counts,
    at the time it was sent.
    """
    if not bot.enabled:
        raise RuntimeError("Telegram not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_ADMIN_CHAT_ID.")
    db.init_db()
    logger.info("bot: listening as the %s", settings.network_name)
    offset: int | None = None
    backoff = 1
    while True:
        updates = bot.get_updates(offset=offset, timeout=poll_timeout)
        if updates is None:
            time.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)
            continue
        backoff = 1
        if updates:
            offset = handle_updates(updates)
        else:
            time.sleep(1)


def categories_text() -> str:
    return "\n".join(f"{k}: {v}" for k, v in all_categories().items())
