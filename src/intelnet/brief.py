"""The morning brief — the digest, cut to one person's county, in the chat.

At 08:00 (the notify job, quiet-hours aware) every digest subscriber gets a
short message about their own county, taken from their live location or home
(the whole state when neither is known): what the night brought, NWS alerts in
effect and the ones that ended, the network's events, and a collapsed
statewide summary with the Drive links. The full digest stays in Drive; this is
what reaches the phone, and it goes out without links on a night Drive failed.

Nothing here knows about weather: "what the night brought" is every metric the
county's people reported, or whose readings crossed the pack's event threshold.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from intelnet import db, delivery, geo, network, story_brief
from intelnet.config import settings
from intelnet.feeds.nws_alerts import active_alert_groups
from intelnet.models import local_time, parse_iso, public_handle, utcnow
from intelnet.telegram import MAX_MSG, bot, esc, href, join_within, tg_time
from intelnet.topics import all_categories, find_metric

HOURS = 24
STATE_NAMES = {"IL": "Illinois"}


def _state() -> str:
    return STATE_NAMES.get(settings.geo_state, settings.geo_state)


def _n(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _in_effect(fips: str) -> list[str]:
    """Alerts in effect for the county, one line per kind: river flood warnings come in
    fours (one per gauge), and four identical lines say less than 'Flood Warning ×4'."""
    kinds: dict[str, list[Any]] = {}
    for g in active_alert_groups(fips):
        kinds.setdefault(str(g["signal"].evidence.get("event") or "NWS alert"), []).append(g["signal"])
    lines = []
    for event, sigs in kinds.items():
        ends = [s.expires_at for s in sigs if s.expires_at]
        lines.append(f"⚠️ <b>{esc(event)}</b>" + (f" ×{len(sigs)}" if len(sigs) > 1 else "")
                     + (f" until {tg_time(max(ends))}" if ends else ""))
    return lines


def county_of(chat_id: str) -> str | None:
    """The county a chat's brief is about: live location, else home, else None (the state)."""
    live = db.live_location(chat_id)
    if live is not None and live.county_fips:
        return live.county_fips
    sensor = db.sensor_by_chat(chat_id)
    return sensor.location.county_fips if sensor and sensor.location.county_fips else None


def _night(fips: str) -> list[str]:
    """What the county's readings showed: whatever people reported, and anything notable."""
    every = {r["metric"]: r for r in db.mesh(HOURS) if r["county_fips"] == fips}
    people = {r["metric"]: r for r in db.mesh(HOURS, human_only=True) if r["county_fips"] == fips}
    lines = []
    for key, row in every.items():
        m = find_metric(key)
        if m is None:
            continue
        top = row["min_value"] if m.event_direction == "below" else row["max_value"]
        if key not in people and not (top is not None and m.is_event(top)):
            continue
        if m.is_flag:
            what = f"{m.label}: {row['n']} report{'s' if row['n'] != 1 else ''}"
        else:
            what = f"{m.label}: {'down to' if m.event_direction == 'below' else 'up to'} {m.display(top)}"
        mine = people.get(key)
        if mine is not None and not m.is_flag:
            what += f" · {mine['n_human']} from people"
        lines.append(what)
    return lines


def _ended(fips: str) -> list[str]:
    out = []
    for t in db.alert_threads_ended_since(HOURS):
        if fips in json.loads(t["counties_json"] or "[]"):
            at = parse_iso(t["ended_at"])
            out.append(f"✅ Ended: {esc(t['event'] or 'NWS alert')}" + (f" · {tg_time(at)}" if at else ""))
    return out

NEWS_SHOWN = 4               # the brief's reading list (the Doc carries ten)


def statewide() -> list[str]:
    """The major readings across Illinois, every topic: the station and gauge extremes
    (wind, rain, temperature, rivers, soil…) and the network's verified events. HTML lines."""
    from intelnet import digest

    lines = [f"{esc(x['metric'])}, {'lowest' if x['how'] == 'min' else 'highest'}: <b>{esc(x['value'])}</b> · "
             f"{esc(str(x['county']).removesuffix(' County'))}" for x in digest._extremes(HOURS)]
    events = [network.event_summary(e) for e in db.events_since(HOURS, limit=10)]
    lines += [f"📍 {esc(digest.event_phrase(e)[:1].upper() + digest.event_phrase(e)[1:])}"
              + (" · verified" if e["verified"] else "") for e in events if e["verified"]][:3]
    return lines


def connections() -> tuple[list[str], set[str]]:
    """The news tied to what the network measured where it happened (connect.py), as HTML
    items; and the titles used, so the reading list doesn't repeat them."""
    from intelnet import connect

    items, used = [], set()
    for th in connect.threads():
        heads = " · ".join(
            (f'<a href="{href(s["url"])}">{esc(s["title"])}</a>' if href(s["url"]) else esc(s["title"]))
            + (f" <i>({esc(s['source'])})</i>" if s["source"] else "") for s in th.stories[:2])
        used |= {" ".join(s["title"].lower().split()) for s in th.stories}
        facts = th.readings[:3] + ([" and ".join(th.alerts) + " in force"] if th.alerts else [])
        items.append(f"{heads}<br>↳ <b>{esc(th.place)}</b>: {esc('; '.join(facts))}")
    return items, used


def news(skip: set[str] | None = None) -> list[str]:
    """The top news the network kept in the last day, across topics: linked title · publisher."""
    from intelnet import digest

    out, seen = [], set()
    for r in db.kept_items_since(HOURS + 6, limit=20):
        item = {"title": r["title"], "url": r["url"],
                "feed": (json.loads(r["metadata_json"] or "{}").get("feed") if r["metadata_json"] else None)}
        title, source = digest._publisher(item)
        source = source.split(" · ")[0]
        key = " ".join(title.lower().split())
        if key in seen or key in (skip or set()):
            continue
        seen.add(key)
        link = href(r["url"])
        head = f'<a href="{link}">{esc(title)}</a>' if link else esc(title)
        out.append(head + (f" · <i>{esc(source)}</i>" if source else ""))
        if len(out) >= NEWS_SHOWN:
            break
    return out


def compose(fips: str | None, links: dict[str, str | None], *, now: datetime | None = None) -> str:
    """The brief for one county (or the whole state when `fips` is None)."""
    now = now or utcnow()
    c = geo.county(fips)
    where = c.label if c else _state()
    lines = [f"☀️ <b>Morning brief</b> · {esc(local_time(now, '%a %-d %b'))} · {esc(where)}"]
    linked, used = connections()
    if linked:
        lines.append("<b>In the news, and what was measured there</b>")
        lines += [f"• {x.replace('<br>', chr(10) + '  ')}" for x in linked]
        lines.append("")
    if c:
        groups = _in_effect(c.fips)
        lines += groups[:4]
        lines += _ended(c.fips)[:3]
        night = _night(c.fips)
        lines += [f"• {esc(x)}" for x in night[:6]]
        storms = [s for s in story_brief.summaries(HOURS) if c.fips in s["fips"]]
        lines += [f"⛈ <b>{esc(s['title'] or 'Storm')}</b>: {esc(s['brief'])}" for s in storms[:2]]
        events = [network.event_summary(e) for e in db.events_since(HOURS) if e["county_fips"] == c.fips]
        lines += [f"📍 {esc(e['title'] or '')} · {_n(e.get('n_sensors') or 0, 'sensor', 'sensors')}"
                  + (" · verified" if e["verified"] else "") for e in events[:3]]
        if not (groups or night or events or storms):
            lines.append(f"A quiet night in {esc(where)}: no NWS alerts, and nothing notable reported.")
    wide = statewide()
    if wide:
        lines.append(f"\n<b>Across {esc(_state())}</b>")
        lines += [f"• {x}" for x in wide]
    reading = news(used)
    if reading:
        lines.append("\n<b>Also worth reading</b>")
        lines += [f"• {x}" for x in reading]
    v = db.vitals()
    state = [f"{_n(v.get('alerts_active', 0), 'NWS alert', 'NWS alerts')} in effect across {esc(_state())}",
             (f"{_n(v.get('signals_24h_human', 0), 'reading', 'readings')} from people · "
              f"{v.get('signals_24h_reference', 0):,} official"),
             f"{_n(len(network.coverage_gaps(7)), 'county', 'counties')} without a sensor this week"]
    top = [network.event_summary(e) for e in db.events_since(HOURS, limit=2)]
    state += [f"Top event: {esc(e['title'] or '')}" for e in top[:1]]
    lines.append("<blockquote expandable>" + "\n".join(state) + "</blockquote>")
    tail = []
    for label, key in (("Full digest", "digest"), ("All digests", "folder")):
        link = href(links.get(key))
        if link:
            tail.append(f'<a href="{link}">{label}</a>')
    page = href(f"{settings.public_site_url}county/{c.slug}.html") if c and settings.public_site_url else None
    if page:
        tail.append(f'<a href="{page}">{esc(c.name)} County page</a>')
    if tail:
        lines.append(" · ".join(tail))
    if not c:
        lines.append("<i>Set your home (/home 62704) and this brief is about your county.</i>")
    return join_within(lines, MAX_MSG)


def _readings_table(fips: str) -> str:
    """The most-reported measurement in the county overnight, reading by reading: who (a
    handle, or the official station), where (ZIP), how it stands, and the value."""
    people = [r for r in db.mesh(HOURS, human_only=True) if r["county_fips"] == fips]
    if not people:
        return ""
    key = max(people, key=lambda r: r["n"])["metric"]
    m = find_metric(key)
    if m is None or m.is_flag:
        return ""
    rows = sorted((s for s in db.recent_signals(HOURS, county_fips=fips, metric=key, limit=200)
                   if s.value is not None and s.sensor_kind in ("human", "bot", "station", "official")),
                  key=lambda s: -(s.value or 0))[:5]
    if not rows:
        return ""
    cells = []
    for s in rows:
        official = s.sensor_kind in ("station", "official")
        if official:
            sensor = db.get_sensor(s.sensor_id)
            who = f"{sensor.name if sensor and sensor.name else s.sensor_id.split(':', 1)[-1].upper()} · official"
        else:
            who = f"{public_handle(s.sensor_id)} · {s.location.zip5 or ''} · " + (
                "corroborated" if s.quality == "corroborated" else "unverified")
        cells.append(f"<tr><td>{esc(who)}</td><td align=\"right\">{esc(m.display(s.value).split(' (')[0])}</td></tr>")
    return (f"<table compact><caption>{esc(m.label)}, last 24 hours</caption>" + "".join(cells) + "</table>")


def compose_rich(fips: str | None, links: dict[str, str | None], *, now: datetime | None = None) -> str:
    """The brief as a Telegram rich message: one heading naming the county, its alerts and
    storms as lines, the night's readings as a list, the most-reported readings as a table,
    the statewide picture folded away, and the date and links as the footer. `compose`
    stays the plain fallback."""
    now = now or utcnow()
    c = geo.county(fips)
    where = c.label if c else _state()
    parts = [f"<h4>☀️ Morning brief: {esc(where)}</h4>"]
    linked, used = connections()
    if linked:
        parts.append("<p><b>In the news, and what was measured there</b></p><ul>"
                     + "".join(f"<li>{x}</li>" for x in linked) + "</ul>")
    if c and linked:                               # the county's own section, under the connections
        parts.append(f"<p><b>{esc(where)}</b></p>")
    table = ""
    if c:
        lines = _in_effect(c.fips)[:4] + _ended(c.fips)[:3]
        lines += [f"⛈ <b>{esc(s['title'] or 'Storm')}</b>: {esc(s['brief'])}"
                  for s in story_brief.summaries(HOURS) if c.fips in s["fips"]][:2]
        lines += [f"📍 {esc(e['title'] or '')} · {_n(e.get('n_sensors') or 0, 'sensor', 'sensors')}"
                  + (" · verified" if e["verified"] else "")
                  for e in (network.event_summary(e) for e in db.events_since(HOURS) if e["county_fips"] == c.fips)][:3]
        night = _night(c.fips)[:6]
        parts += [f"<p>{x}</p>" for x in lines]
        if night:
            parts.append("<ul>" + "".join(f"<li>{esc(x)}</li>" for x in night) + "</ul>")
        if not (lines or night):
            parts.append(f"<p>A quiet night in {esc(where)}: no NWS alerts, and nothing notable reported.</p>")
        table = _readings_table(c.fips)
    if table:
        parts.append(table)
    wide = statewide()
    if wide:
        parts.append(f"<p><b>Across {esc(_state())}</b></p><ul>" + "".join(f"<li>{x}</li>" for x in wide) + "</ul>")
    reading = news(used)
    if reading:
        parts.append("<p><b>Also worth reading</b></p><ul>" + "".join(f"<li>{x}</li>" for x in reading) + "</ul>")
    v = db.vitals()
    state = [f"{_n(v.get('alerts_active', 0), 'NWS alert', 'NWS alerts')} in effect",
             f"{_n(v.get('signals_24h_human', 0), 'reading', 'readings')} from people · {v.get('signals_24h_reference', 0):,} official",
             f"{_n(len(network.coverage_gaps(7)), 'county', 'counties')} without a sensor this week"]
    parts.append("<details><summary>The network</summary><p>" + "<br>".join(state) + "</p></details>")
    if not c:
        parts.append("<p><i>Set your home (/home 62704) and this brief is about your county.</i></p>")
    tail = [esc(local_time(now, "%a %-d %b"))]
    tail += [f'<a href="{link}">{label}</a>' for label, key in (("Full digest", "digest"), ("All digests", "folder"))
             if (link := href(links.get(key)))]
    page = href(f"{settings.public_site_url}county/{c.slug}.html") if c and settings.public_site_url else None
    if page:
        tail.append(f'<a href="{page}">{esc(c.name)} County page</a>')
    parts.append("<footer>" + " · ".join(tail) + "</footer>")
    return "".join(parts)


def fanout_brief(date: str, links: dict[str, str | None]) -> int:
    """One brief per digest subscriber, about their county. Returns messages sent."""
    if not bot.enabled:
        return 0
    st = settings.geo_state.lower()
    chats = sorted({chat for cat in all_categories() if cat.endswith(".digest")
                    for chat in db.matching_chat_ids(cat, [st])})
    key = f"digest:{date}"
    texts: dict[str | None, tuple[str, str]] = {}
    ids: list[int | None] = []
    for chat in chats:
        if db.already_notified(key, chat):
            continue
        fips = county_of(chat)
        if fips not in texts:
            texts[fips] = (compose(fips, links), compose_rich(fips, links))
        text, rich = texts[fips]
        ids.append(db.enqueue(key, chat, text, priority=5, stale_at=utcnow() + timedelta(hours=12), rich=rich))
    return delivery.send_now(ids)


def links_for(date: str) -> dict[str, Any]:
    """Today's Drive links, when today's digest made it to Drive."""
    row = db.latest_digest()
    if not settings.gdrive_enabled or row is None or row["date"] != date:
        return {}
    return {"digest": row["drive_url"] or row["latest_url"], "folder": row["folder_url"]}
