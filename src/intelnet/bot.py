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
from datetime import datetime, timezone
from typing import Callable

from intelnet import contrib, db, geo, language, network, subscriptions
from intelnet.config import settings
from intelnet.models import KIND_HUMAN, Sensor
from intelnet.telegram import MAX_MSG, bot, esc, href, join_within
from intelnet.topics import all_categories, default_topic, find_metric

logger = logging.getLogger(__name__)

_MAX_BACKOFF = 300


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
        f"Every reading you send is checked against neighbours and official sources; "
        f"corroborated readings become events and raise your trust.\n\n"
        f"<b>Report</b> (just type it) — weather, soil, water, crops, air:\n"
        f"<code>{esc(language.cheatsheet())}</code>\n"
        f"Plain sentences work too (“golf-ball hail here 5 min ago”). /topics lists every metric.\n\n"
        f"<b>Set up</b>: /join · /home 62704-1234 (or share your location) · /me\n"
        f"<b>Pull</b>: /near [place] · /alerts [county] · /latest · /network\n"
        f"<b>Subscribe</b>: /subscribe warnings cook · /subscribe events 62704 · "
        f"/subscribe soil.events · /subscribe *.events · /subscribe digest · /subs · /unsubscribe all\n"
        f"<b>Digest by email</b>: /digest you@example.com\n"
        f"<b>Automated sensors</b>: /signal {esc(language.as_json_example(topic))}"
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
    return f"🏠 Home set: <b>{esc(loc.describe())}</b> (precision: {esc(loc.precision)})"


def cmd_location(message: dict, sensor: Sensor | None) -> str:
    if sensor is None:
        return "Send /join first, then share your location again."
    p = message["location"]
    loc = geo.location_from_point(float(p["latitude"]), float(p["longitude"]))
    db.set_sensor_location(sensor.id, loc)
    return f"🏠 Home set from your location: <b>{esc(loc.describe())}</b>"


def cmd_me(message: dict, sensor: Sensor | None, args: str) -> str:
    if sensor is None:
        return "You haven't joined yet — send /join."
    subs = db.subscriptions_for(sensor.chat_id or "")
    lines = [
        f"<b>{esc(sensor.name)}</b> · <code>{esc(sensor.id)}</code>",
        f"Home: {esc(sensor.location.describe())}",
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
        until = s.expires_at.astimezone(timezone.utc).strftime("%a %H:%MZ") if s.expires_at else "—"
        counties = ", ".join(g["counties"][:6]) + ("…" if len(g["counties"]) > 6 else "")
        link = href(s.evidence.get("url"))
        title = f"<a href=\"{link}\">{esc(s.evidence.get('event'))}</a>" if link else esc(s.evidence.get("event"))
        lines.append(f"• {title} ({esc(s.evidence.get('severity'))}) — {esc(counties)} · until {until}")
    return join_within(lines, MAX_MSG)


def cmd_latest(message: dict, sensor: Sensor | None, args: str) -> str:
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
    if sub == "broadcast" and len(parts) >= 2:
        text = args.strip()[len("broadcast"):].strip()
        chats = {s.chat_id for s in db.list_sensors(kind=KIND_HUMAN, limit=10000) if s.chat_id}
        n = bot.broadcast(sorted(chats), f"📣 <b>{esc(settings.network_name)}</b>\n{esc(text)}")
        return f"Broadcast to {n} chat(s)."
    return "Usage: /admin stats | sensors | ban <id> | unban <id> | trust <id> <0..1> | broadcast <text>"


COMMANDS: dict[str, Callable[[dict, Sensor | None, str], str]] = {
    "join": cmd_join, "home": cmd_home, "me": cmd_me,
    "subscribe": cmd_subscribe, "unsubscribe": cmd_unsubscribe, "subs": cmd_subs,
    "subscriptions": cmd_subs, "near": cmd_near, "alerts": cmd_alerts, "latest": cmd_latest,
    "digest": cmd_digest_email, "network": cmd_network, "topics": cmd_topics, "admin": cmd_admin,
}


# ── contributions ─────────────────────────────────────────────────────────

def _contribute(message: dict, sensor: Sensor | None, text: str, *, json_mode: bool = False) -> str:
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
    source_id = f"{message['chat']['id']}:{message.get('message_id')}"
    fn = contrib.contribute_json if json_mode else contrib.contribute
    c = fn(sensor, text, source_id_base=source_id, message_time=_message_time(message),
           extra_evidence=evidence or None)
    reply = contrib.ack_text(c, sensor)
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
    if sensor and sensor.status == "banned":
        return None
    text = (message.get("text") or message.get("caption") or "").strip()

    if message.get("location") and not text:
        return cmd_location(message, sensor)

    if text.startswith("/"):
        m = re.match(r"^/([A-Za-z_]+)(?:@\w+)?\s*(.*)$", text, re.DOTALL)
        if not m:
            return help_text()
        cmd, args = m.group(1).lower(), m.group(2).strip()
        if cmd in ("start", "help"):
            return help_text()
        if cmd in ("obs", "report", "r"):
            return _contribute(message, sensor, args)
        if cmd == "signal":
            return _contribute(message, sensor, args, json_mode=True)
        handler = COMMANDS.get(cmd)
        if handler is None:
            return f"Unknown command /{esc(cmd)}.\n\n" + help_text()
        return handler(message, sensor, args)

    if not text:
        return None
    return _contribute(message, sensor, text)


# ── listener ──────────────────────────────────────────────────────────────

def _drain_backlog() -> int | None:
    updates = bot.get_updates(timeout=0)
    if not updates:
        return None
    logger.info("bot: skipped %d backlog update(s)", len(updates))
    return updates[-1]["update_id"] + 1


def run_listener(poll_timeout: int = 30) -> None:
    """Block forever answering messages. Intended for a launchd KeepAlive job."""
    if not bot.enabled:
        raise RuntimeError("Telegram not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_ADMIN_CHAT_ID.")
    db.init_db()
    logger.info("bot: listening as the %s", settings.network_name)
    offset = _drain_backlog()
    backoff = 1
    while True:
        updates = bot.get_updates(offset=offset, timeout=poll_timeout)
        if updates is None:
            time.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)
            continue
        backoff = 1
        for u in updates:
            offset = u["update_id"] + 1
            message = u.get("message") or u.get("edited_message")
            if not message:
                continue
            try:
                bot.typing(message["chat"]["id"])
                reply = handle_message(message)
            except Exception as exc:  # noqa: BLE001
                logger.exception("bot: handler failed")
                reply = f"⚠️ Something went wrong: {esc(str(exc))}"
            if reply:
                bot.send_to(message["chat"]["id"], reply)
        if not updates:
            time.sleep(1)


def categories_text() -> str:
    return "\n".join(f"{k}: {v}" for k, v in all_categories().items())
