"""The daily digest — built from the network's state, rendered to HTML.

Two halves, as asked: the classic curated + scored roll-up (events ranked by
score, the official alert recap, a triaged reading list) and the network's
own vital signs (coverage, corroboration, contributor leaderboard, sensors
wanted) — because on this project the network IS the product.

`build()` returns a plain data model; `render_html()` turns it into the HTML
that Google Drive converts into a Google Doc; `render_text()` is the CLI /
Telegram-preview form. No file is written here.
"""
from __future__ import annotations

import base64
import csv
import html
import io
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from intelnet import ahead, daymap, db, geo, network, notable, story_brief, trust
from intelnet.config import settings
from intelnet.feeds.nws_alerts import SEVERITY_RANK, office
from intelnet.models import iso, local_time, parse_iso, public_handle
from intelnet.topics import find_metric

logger = logging.getLogger(__name__)

EXTREME_METRICS = (("wind_gust_ms", "max"), ("rain_mm", "max"), ("temp_c", "max"),
                   ("temp_c", "min"), ("visibility_km", "min"), ("snow_cm", "max"),
                   ("stage_m", "max"), ("discharge_cms", "max"), ("water_temp_c", "max"),
                   ("soil_moisture_pct", "min"), ("soil_temp_c", "min"))


@dataclass
class DigestModel:
    date: str
    generated_at: str
    hours: float
    network_name: str
    state: str
    vitals: dict[str, Any] = field(default_factory=dict)
    narrative: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    storms: list[dict[str, Any]] = field(default_factory=list)          # stories.py, with briefs
    contributions: list[dict[str, Any]] = field(default_factory=list)   # county × metric (human)
    extremes: list[dict[str, Any]] = field(default_factory=list)        # station extremes
    leaderboard: list[dict[str, Any]] = field(default_factory=list)
    reading: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    gap_count: int = 0
    subscriptions: dict[str, int] = field(default_factory=dict)
    links: dict[str, str | None] = field(default_factory=dict)
    day: dict[str, Any] = field(default_factory=dict)                   # notable.py: the map's readings and stories
    picture: bytes | None = None                                        # the day's map (daymap.py, light), JPEG
    picture_full: bytes | None = None                                   # the same map as a PNG, its own file in Drive
    ahead: ahead.Statewide = field(default_factory=ahead.Statewide)     # the day ahead: places' forecasts, risks

    @property
    def headline(self) -> str:
        """One plain sentence or two: the digest's opening when there's no narrative."""
        v = self.vitals
        # alerts in effect now, as the numbers count them (self.alerts is every alert of the window)
        n, k, a = v.get("signals_24h_human", 0), v.get("sensors_active_24h", 0), v.get("alerts_active", 0)
        alerts = f"{a} NWS alert{'s' if a != 1 else ''} in effect" if a else "No NWS alerts in effect"
        people = (f"{n:,} reading{'s' if n != 1 else ''} from {k} {'person' if k == 1 else 'people'}"
                  if n else "no readings from people yet")
        text = f"{alerts}; {people}."
        if self.events:
            text += f" Top event: {event_phrase(self.events[0])}."
        return text

    def to_json(self) -> str:
        """The model for the narrative's prompt: without the map's data and picture."""
        return json.dumps({k: v for k, v in asdict(self).items() if k not in ("day", "picture", "picture_full")},
                          default=str)


def event_phrase(e: dict[str, Any]) -> str:
    """'visibility down to 1/16 mi, Lake County' from an event summary."""
    m = find_metric(str(e.get("metric") or ""))
    what = str(e.get("metric_label") or e.get("title") or "event")
    if m is not None and not m.is_flag:
        what += f" {'down to ' if m.event_direction == 'below' else ''}{e.get('peak_display')}"
    where = e.get("county_label")
    what = what[:1].lower() + what[1:] if what[1:2].islower() else what      # "PM2.5" stays as it is
    return what + (f", {where}" if where else "")


def _alert_recap(hours: float) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for s in db.recent_signals(hours, metric_prefix="alert.", limit=5000):
        g = groups.get(s.group_key or s.key)
        if g is None:
            g = groups[s.group_key or s.key] = {
                "event": s.evidence.get("event") or s.metric, "severity": s.evidence.get("severity"),
                "rank": SEVERITY_RANK.get(str(s.evidence.get("severity")), 0),
                "sender": s.evidence.get("sender"), "counties": [], "sent": s.observed_at,
                "expires": s.expires_at, "url": s.evidence.get("url"),
                "headline": s.evidence.get("nws_headline") or s.evidence.get("headline"),
            }
        c = geo.county(s.location.county_fips)
        if c and c.name not in g["counties"]:
            g["counties"].append(c.name)
    out = sorted(groups.values(), key=lambda g: (-g["rank"], g["sent"]))
    for g in out:
        # UTC ISO in the data (and the CSVs); rendered as local time by `_when`
        g["sent"] = iso(g["sent"])
        g["expires"] = iso(g["expires"]) if g["expires"] else "—"
    return out


def _extremes(hours: float) -> list[dict[str, Any]]:
    rows = db.mesh(hours, reference_only=True)
    out = []
    for key, how in EXTREME_METRICS:
        m = find_metric(key)
        if m is None:
            continue
        cands = [r for r in rows if r["metric"] == key and r[f"{how}_value"] is not None]
        if not cands:
            continue
        best = max(cands, key=lambda r: r["max_value"]) if how == "max" else min(cands, key=lambda r: r["min_value"])
        c = geo.county(best["county_fips"])
        out.append({"metric": m.label, "how": how, "value": m.display(best[f"{how}_value"]),
                    "county": c.label if c else best["county_fips"], "n": best["n"]})
    return out


def build(hours: float = 24.0, date: str | None = None) -> DigestModel:
    now = datetime.now(UTC)
    model = DigestModel(
        date=date or now.strftime("%Y-%m-%d"), generated_at=local_time(now, "%Y-%m-%d %-I:%M %p %Z"),
        hours=hours, network_name=settings.network_name, state=settings.geo_state,
    )
    model.vitals = db.vitals()
    model.events = [network.event_summary(e) for e in db.events_since(hours, limit=15)]
    model.alerts = _alert_recap(hours)
    model.storms = story_brief.summaries(hours)
    model.contributions = [r for r in network.mesh_rows(hours) if r["n_human"]][:25]
    model.extremes = _extremes(hours)
    model.day = notable.build(now)
    model.ahead = ahead.statewide(now)
    if settings.card_maps:                     # the day on the map, on the page's white
        try:
            img = daymap.render_image(model.day, None, now=now, pal=daymap.LIGHT)
            model.picture, model.picture_full = daymap.encode(img), daymap.encode(img, "PNG")
        except Exception:                      # a digest without its map rather than no digest
            logger.exception("digest: drawing the day's map failed")
    # The digest folder can be anyone-with-link, so people appear by handle only
    # — the same rule as the public site.
    model.leaderboard = [
        {"name": public_handle(r["id"]), "trust": r["trust"], "n": r["n"],
         "trust_label": trust.overall(r["id"]).label,
         "n_corr": r["n_corr"], "county": (geo.county(r["county_fips"]).name
                                           if geo.county(r["county_fips"]) else "—")}
        for r in db.leaderboard(7)
    ]
    model.reading = [
        {"title": r["title"], "url": r["url"], "relevance": r["relevance"],
         "reason": r["triage_reason"], "published_at": r["published_at"],
         "feed": (json.loads(r["metadata_json"] or "{}").get("feed") if r["metadata_json"] else None)}
        for r in db.kept_items_since(int(hours) + 6, limit=15)
    ]
    gaps = network.coverage_gaps(7)
    model.gap_count = len(gaps)
    model.gaps = [c.name for c in gaps[:20]]
    model.subscriptions = db.subscription_counts()
    latest = db.latest_digest()
    model.links = {"folder": latest["folder_url"] if latest else None,
                   "latest": latest["latest_url"] if latest else None}
    return model


# ── rendering ─────────────────────────────────────────────────────────────

def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


INK, MUTED, ACCENT = "#1A1F1C", "#5B655F", "#1D6E7A"
LINE, SHADE, GROUND, PAPER = "#D3DAD4", "#EEF1ED", "#F1F3EF", "#FFFFFF"
TOPIC_COLORS = {"weather": "#1D6E7A", "water": "#2B5FAD", "soil": "#7A4E24",
                "agriculture": "#4F7F2F", "air": "#6E5E9A", "quake": "#B33A2B",
                "nature": "#8A7A1F", "markets": "#8E4585"}
SEVERITY_COLORS = {"Extreme": "#B3261E", "Severe": "#C4501B", "Moderate": "#B7791F",
                   "Minor": "#4B7B8A", "Unknown": MUTED}
STATE_NAMES = {"IL": "Illinois"}

# Google Docs builds the Doc from inline styles, and only from longhand ones: the
# `font:` shorthand, text-transform and letter-spacing are dropped, paragraphs start at
# line-height 1, a top border becomes a rule, and a background on anything but a table
# cell turns into a highlight behind every line. So every style here is longhand and in
# points, capitals are typed, spacing is explicit, and only table cells are shaded. Docs
# keeps the first font family it's given, and renders Google Fonts by name: the site's
# Fraunces and IBM Plex, with fallbacks a browser uses when the fonts aren't loaded.
DISPLAY = "font-family:Fraunces,Georgia,serif"
BODY = "font-family:'IBM Plex Sans',Arial,sans-serif"
MONO = "font-family:'IBM Plex Mono',Menlo,monospace"


def _style(font: str, size: float, *, color: str = INK, weight: int = 400, lh: float = 1.45,
           after: float = 0, before: float = 0, extra: str = "") -> str:
    return (f"{font};font-size:{size:g}pt;font-weight:{weight};color:{color};line-height:{lh:g};"
            f"margin:{before:g}pt 0 {after:g}pt 0{';' + extra if extra else ''}")


P_BODY = _style(BODY, 10.5, after=6)
P_SMALL = _style(BODY, 9, color=MUTED, lh=1.4)
P_KICKER = _style(MONO, 8, color=ACCENT, weight=700, lh=1.3, extra="letter-spacing:.08em")

# The browser page adds a sheet on a tinted ground. None of this reaches the Doc: Docs
# would turn the page colour into the Doc's background and the sheet into highlights.
BROWSER_CSS = f"""
  :root {{ color-scheme: light; }}
  body {{ margin: 0; padding: 28px 16px 64px; background: {GROUND}; }}
  .sheet {{ max-width: 720px; margin: 0 auto; background: {PAPER}; padding: 36px 40px 44px;
            box-shadow: 0 1px 3px rgba(20,32,28,.10); border-radius: 4px; }}
  .sheet table {{ width: 100%; }}
  .sheet hr {{ border: 0; border-top: 1px solid {LINE}; margin: 22px 0 0; }}
  .sheet img {{ max-width: 100%; height: auto; }}
  .tw {{ overflow-x: auto; }}
  @media (max-width: 620px) {{
    body {{ padding: 0; background: {PAPER}; }} .sheet {{ padding: 22px 16px 30px; box-shadow: none; }}
  }}
  @media print {{ body {{ background: {PAPER}; padding: 0; }} .sheet {{ box-shadow: none; padding: 0; }} }}
"""
FONTS_LINK = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;'
              '9..144,600&family=IBM+Plex+Sans:ital,wght@0,400;0,600;1,400&family=IBM+Plex+Mono:wght@400;600&display=swap">')


class _Raw(str):
    """Markup that is already safe — links, coloured words."""


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value) if isinstance(value, _Raw) else _e(str(value))


def _link(url: str | None, text: str) -> str:
    return (f'<a href="{_e(url)}" style="color:{ACCENT};text-decoration:none">{_e(text)}</a>'
            if url else _e(text))


def _when(stamp: Any) -> str:
    """2026-09-16T13:00:02Z → Wed 8:00 AM, in the network's local time (LOCAL_TZ)."""
    try:
        dt = parse_iso(str(stamp or ""))
    except ValueError:
        dt = None
    return local_time(dt, "%a %-I:%M %p") if dt else str(stamp or "")


def _n(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _table(headers: list[str], rows: list[list[Any]], *, aligns: tuple[str, ...] = (),
           empty: str = "Nothing in this window.") -> str:
    """At most four short columns: a phone shows the Doc reflowed, and a wide table
    squeezes every word onto its own line."""
    if not rows:
        return f'<p style="{_style(BODY, 10, color=MUTED, after=8)};font-style:italic">{_e(empty)}</p>'
    def align(i: int) -> str:
        return aligns[i] if i < len(aligns) else "left"

    # Docs draws every border left unset as a grid line, so each one is set: rules under
    # the rows, and white (invisible) sides.
    sides = f"border-top:1pt solid {PAPER};border-left:1pt solid {PAPER};border-right:1pt solid {PAPER};"
    head = "".join(
        f'<td style="{sides}border-bottom:1pt solid {INK};text-align:{align(i)};padding:4pt 6pt;'
        f'vertical-align:bottom"><p style="{_style(MONO, 7.5, color=MUTED, weight=700, lh=1.2)};'
        f'text-align:{align(i)}">{_e(h.upper())}</p></td>'
        for i, h in enumerate(headers))
    body = []
    for row in rows:
        cells = "".join(
            f'<td style="{sides}border-bottom:1pt solid {LINE};text-align:{align(i)};padding:4pt 6pt;'
            f'vertical-align:top"><p style="{_style(BODY, 9.5, lh=1.35)};text-align:{align(i)}">{_cell(c)}</p></td>'
            for i, c in enumerate(row))
        body.append(f"<tr>{cells}</tr>")
    return (f'<div class="tw"><table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;'
            f'margin:4pt 0 10pt 0"><tr>{head}</tr>' + "".join(body) + "</table></div>")


KEEP = "page-break-after:avoid"     # Docs' "keep with next" (the import's only page control)


def _section(title: str, note: str = "") -> str:
    """A rule, the heading, and a line on what the section is. The heading is a real <h2>,
    so the Doc's outline (and a phone's contents panel) lists the sections; it and its note
    keep with what follows, so no heading ends a page alone."""
    return ("<hr>"
            f'<h2 style="{_style(DISPLAY, 15, weight=700, lh=1.2, before=10, after=2, extra=KEEP)}">{_e(title)}</h2>'
            + (f'<p style="{_style(BODY, 9.5, color=MUTED, lh=1.35, after=8, extra=KEEP)}">{_e(note)}</p>'
               if note else ""))


def _item(head: str, meta: str = "", body: str = "") -> str:
    """One entry of a list-shaped section: a bold first line, a muted line under it."""
    return (f'<p style="{_style(BODY, 10.5, lh=1.35)}">{head}</p>'
            + (f'<p style="{P_SMALL}">{meta}</p>' if meta else "")
            + (f'<p style="{_style(BODY, 10, color=MUTED, lh=1.4)}">{body}</p>' if body else "")
            + f'<p style="{_style(BODY, 4, lh=1)}">&nbsp;</p>')


def _story(st: notable.Listed) -> str:
    """One of the day's stories under its map number: the place, the lead headline, a line
    per reading measured there, the alerts in force."""
    out = [f'<p style="{_style(BODY, 11, weight=700, lh=1.3)}">{st.n} · {_e(st.where)}</p>',
           f'<p style="{_style(BODY, 10.5, lh=1.4)}">{_link(st.url, st.title)}'
           + (f' <span style="color:{MUTED}">({_e(st.source)})</span>' if st.source else "") + "</p>"]
    out += [f'<p style="{_style(BODY, 9.5, lh=1.4)}">↳ {_e(r)}</p>' for r in st.readings]
    if st.alerts:
        out.append(f'<p style="{_style(BODY, 9.5, color=SEVERITY_COLORS["Severe"], weight=700, lh=1.4)}">'
                   f'{_e(st.alerts)}</p>')
    return "".join(out) + f'<p style="{_style(BODY, 4, lh=1)}">&nbsp;</p>'


def _glance(v: dict[str, Any]) -> str:
    """Six numbers in two rows of three: a table, so the Doc keeps the grid on a phone."""
    cells = [
        (f"{v.get('signals_24h_human', 0):,}", "readings from people"),
        (f"{v.get('signals_24h_reference', 0):,}", "official readings"),
        (f"{v.get('sensors_active_24h', 0)} of {v.get('sensors_total', 0)}", "sensors active"),
        (f"{v.get('alerts_active', 0)}", "NWS alerts in effect"),
        (f"{v.get('events_open', 0)}", "network events open"),
        (f"{v.get('corroboration_rate_7d', 0):.0%}", "corroborated, 7 days"),
    ]
    rows = []
    for r in range(2):
        tds = "".join(
            f'<td style="background-color:{SHADE};border:1pt solid {PAPER};padding:6pt 8pt;width:33%;'
            f'vertical-align:top"><p style="{_style(DISPLAY, 17, weight=700, lh=1.1)}">{_e(big)}</p>'
            f'<p style="{_style(BODY, 8, color=MUTED, lh=1.25)}">{_e(label)}</p></td>'
            for big, label in cells[r * 3:r * 3 + 3])
        rows.append(f"<tr>{tds}</tr>")
    return (f'<table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;'
            f'margin:6pt 0 4pt 0">{"".join(rows)}</table>')


def _downloads(downloads: dict[str, str] | None) -> str:
    if not downloads:
        return ""
    links = " · ".join(_link(url, label) for label, url in downloads.items())
    return f'<p style="{_style(BODY, 9, color=MUTED, lh=1.4, after=4)}">Also as {links}</p>'


def _alert_kinds(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per kind of alert: river flood warnings come one per gauge, and four
    lines saying 'Flood Warning' say less than one naming every county it covers."""
    kinds: dict[str, dict[str, Any]] = {}
    for a in alerts:
        k = kinds.setdefault(a["event"], {"event": a["event"], "severity": a["severity"], "n": 0,
                                          "counties": [], "senders": [], "expires": [], "url": a.get("url")})
        k["n"] += 1
        k["counties"] += [c for c in a["counties"] if c not in k["counties"]]
        sender = office(a.get("sender"))
        if sender not in k["senders"]:
            k["senders"].append(sender)
        if a.get("expires") not in (None, "—"):
            k["expires"].append(a["expires"])
    return list(kinds.values())


MAP_LABEL = "Full-size map"    # the downloads bar's name for the map's own file (gdrive.publish)
LEDE_MAX = 200              # characters: three lines of the 13 pt lede across the Doc's page
READING_SHOWN = 10          # the Doc's reading list; the CSV keeps every kept item


def _publisher(r: dict[str, Any]) -> tuple[str, str]:
    """A Google News title ends ' - Publisher': show the publisher, not the search it came from."""
    title, feed = str(r.get("title") or ""), str(r.get("feed") or "")
    head, _, source = title.rpartition(" - ")
    if feed.startswith("Google News") and len(head) >= 12 and 2 <= len(source) <= 60:
        return head, source
    return title, feed


def _names(names: list[str], limit: int = 6) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def render_html(m: DigestModel, downloads: dict[str, str] | None = None, *, page: bool = False) -> str:
    """The digest as a document: what Google Drive turns into the Doc (the default), or
    with `page=True` the same content on a sheet for a browser (the site's copy).

    `downloads` maps a label to a URL ("PDF", "Word", "CSV tables"…). The publisher
    passes it on a second pass, once the files it names actually exist.
    """
    v = m.vitals
    state = STATE_NAMES.get(m.state, m.state)
    p: list[str] = []

    # ── masthead ──
    p.append(f'<p style="{P_KICKER}">{_e(m.network_name.upper())} · {_e(state.upper())} DAILY DIGEST</p>')
    p.append(f'<h1 style="{_style(DISPLAY, 26, weight=400, lh=1.1, before=4, after=2)}">'
             f'{_e(_long_date(m.date))}</h1>')
    made = m.generated_at.split(" ", 1)[-1]                      # "2026-09-30 1:10 AM CDT" → the time
    p.append(f'<p style="{_style(BODY, 9.5, color=MUTED, lh=1.4, after=6)}">The last {m.hours:g} hours '
             f'across {_e(state)}, as of {_e(made)}</p>')
    paras = [x.strip() for x in (m.narrative or "").split("\n\n") if x.strip()]
    # the narrative's first paragraph leads while it fits page 1's three lines; a longer one opens the body
    lede, rest = (paras[0], paras[1:]) if paras and len(paras[0]) <= LEDE_MAX else (m.headline, paras)
    p.append(f'<p style="{_style(DISPLAY, 13, lh=1.45, after=6)}">{_e(lede)}</p>')
    p.append(_downloads(downloads))

    # ── the day on the map, then its numbers: what stood out, and the stories it numbers ──
    # Page 1 is the masthead, a lede of up to three lines, the map and the numbers. Docs' import
    # keeps no page break, moves a picture that doesn't fit to the next page whole and splits a
    # table anywhere, even inside a row: so the map comes before the numbers, sized to leave them room.
    listed, told = notable.listing(m.day)
    if m.picture:
        alt = (f"Map of {state}: the day's notable readings by topic, the stand-outs labelled, "
               "and the stories below by number")
        # no caption under it: the map's own key says what the marks are, and a caption can
        # fall onto the next page alone. A phone gets Google's 256-pixel copy of any picture in
        # a Doc, and the import drops a link on a picture: the bar above links the full-size file
        w, h = (432, 540) if page else (384, 480)                 # 4 × 5 in on the Doc's page
        p.append(f'<p style="text-align:center;margin:4pt 0 2pt 0"><img src="data:image/jpeg;base64,'
                 f'{base64.b64encode(m.picture).decode()}" width="{w}" height="{h}" alt="{_e(alt)}"></p>')
    p.append(_glance(v))
    if rest:                      # Docs drops the space above a paragraph that follows a table
        p.append(f'<p style="{_style(BODY, 7, lh=1)}">&nbsp;</p>')
    p += [f'<p style="{P_BODY}">{_e(x)}</p>' for x in rest]
    sections = len(p)
    if listed:
        p.append(_section("In the news, and what was measured there",
                          "Stories from the network's news sources, each with what was measured where it happened."))
        p += [_story(st) for st in listed]
    stand = notable.standouts(m.day)
    if stand:
        p.append(_section("What stood out", "The readings labelled on the map, and why each is there."))
        p.append(_table(["Reading", "Where", "Why"],
                        [[_Raw(f'<b style="white-space:nowrap">{_e(a)}</b>'), b, c] for a, b, c in stand]))

    # ── the day ahead: the outlooks over the state, then the places' forecasts ──
    if m.ahead:
        p.append(_section("The day ahead", "National Weather Service forecasts, and the outlooks that cover "
                                           f"{state}."))
        p += [f'<p style="{_style(BODY, 10.5, lh=1.4, after=6)}"><b>{_e(r.name)}</b>: {_e(r.text())} for '
              f"{_e(ahead.names(cs))}.</p>" for r, cs in m.ahead.risks]
        if m.ahead.places:
            heads = [x.name for x in m.ahead.places[0][1]]
            p.append(_table(["Place", *heads], [[_Raw(f"<b>{_e(name)}</b>"), *(x.text() for x in ps)]
                                                for name, ps in m.ahead.places if len(ps) == len(heads)]))

    # ── storms ──
    if m.storms:
        p.append(_section("Storms", "Warnings, storm reports and readings, one storm at a time."))
        for st in m.storms:
            facts = "".join(f'<p style="{P_SMALL}">[{i}] {_e(f)}</p>' for i, f in enumerate(st["facts"], 1))
            p.append(f'<p style="{_style(BODY, 11, weight=700, lh=1.3)}">{_e(st["title"] or "Storm")}</p>'
                     f'<p style="{P_SMALL}">{_e(_names(st["counties"]))} · {_e(_when(st["opened_at"]))} – '
                     f'{_e(_when(st["updated_at"]))}</p>'
                     f'<p style="{_style(BODY, 10.5, before=4, after=4)}">{_e(st["brief"])}</p>{facts}'
                     f'<p style="{_style(BODY, 4, lh=1)}">&nbsp;</p>')

    # ── official alerts ──
    p.append(_section("Warnings and advisories", "In effect during the window, from the National Weather Service."))
    kinds = _alert_kinds(m.alerts)
    if kinds:
        for k in kinds:
            colour = SEVERITY_COLORS.get(str(k["severity"]), MUTED)
            head = (f'<b style="color:{colour}">{_e(k["event"])}</b>'
                    + (f' <span style="color:{MUTED}">×{k["n"]}</span>' if k["n"] > 1 else "")
                    + f" — {_e(_names(k['counties']))}")
            until = f"until {_when(max(k['expires']))}" if k["expires"] else ""
            severity = str(k["severity"] or "")
            meta = " · ".join(x for x in [until, ", ".join(k["senders"]),
                                          severity if severity != "Unknown" else ""] if x)
            p.append(_item(head, _e(meta)))
    else:
        p.append(f'<p style="{_style(BODY, 10, color=MUTED, after=8)};font-style:italic">No NWS alerts in this window.</p>')

    # ── the network's events ──
    p.append(_section("Network events", "Readings the network checked against neighbours and official sources."))
    if m.events:
        for e in m.events:
            title = f'{e.get("metric_label") or e["title"]} — {e.get("county_label") or ""}'.strip(" —")
            meta = [_n(e.get("n_sensors") or 0, "sensor", "sensors"),
                    "official source agrees" if e.get("n_reference") else "",
                    "verified" if e["verified"] else "not yet verified",
                    f"updated {_when(e.get('updated_at'))}"]
            p.append(_item(f"<b>{_link(e.get('source_url'), title)}</b> · {_e(e['peak_display'])}",
                           _e(" · ".join(x for x in meta if x))))
    else:
        p.append(f'<p style="{_style(BODY, 10, color=MUTED, after=8)};font-style:italic">'
                 f'No events crossed a threshold in this window.</p>')

    # ── the network's own week ──
    p.append(_section("From people", "What contributors reported, by county."))
    p.append(_table(
        ["County", "Measure", "Readings", "Peak"],
        [[str(r["county"]).removesuffix(" County"), r["metric"],
          f"{r['n_human']} · {_n(r['n_sensors'], 'person', 'people')}", r["max"]] for r in m.contributions],
        aligns=("left", "left", "right", "right"),
        empty="No readings from people in this window. The network ran on official feeds."))

    p.append(_section("Contributors", "This week, by handle. Trust grows as readings are confirmed."))
    p.append(_table(["Contributor", "County", "Readings", "Trust"],
                    [[_Raw(f'<span style="{MONO}">{_e(r["name"])}</span>'), r["county"],
                      f"{r['n']} · {r['n_corr']} confirmed", r.get("trust_label") or f"{r['trust']:.2f}"]
                     for r in m.leaderboard],
                    aligns=("left", "left", "right", "right"),
                    empty="No contributors yet. The first reading could be yours."))

    # ── reading list ──
    p.append(_section("Worth reading", "Picked from the network's news sources."))
    if m.reading:
        seen: set[str] = set()
        shown = 0
        for r in m.reading:
            title, source = _publisher(r)
            key = re.sub(r"\W+", " ", title.lower()).strip()
            if key in seen or " ".join(title.lower().split()) in told:     # one story, two feeds; or listed above
                continue
            seen.add(key)
            p.append(_item(_link(r.get("url"), title), _e(source)))
            shown += 1
            if shown >= READING_SHOWN:
                break
        if not shown:
            p.append(f'<p style="{_style(BODY, 10, color=MUTED, after=8)};font-style:italic">'
                     f'Every story kept today is listed above, with the readings behind it.</p>')
    else:
        p.append(f'<p style="{_style(BODY, 10, color=MUTED, after=8)};font-style:italic">Nothing kept in this window.</p>')

    # ── where the network is thin ──
    p.append(_section("Coverage"))
    total = v.get("counties_total", 0)
    site = settings.public_site_url
    map_link = f" The {_link(site, 'coverage map')} shows where a sensor would help most." if site else ""
    p.append(f'<p style="{P_BODY}"><b>{m.gap_count} of {total}</b> counties had no reading from a person '
             f'this week; {v.get("counties_reference_7d", 0)} had official data.{map_link}</p>')

    # ── how to take part ──
    def code(s: str) -> str:
        return f'<span style="{MONO};font-size:9.5pt">{_e(s)}</span>'

    p.append(f'<table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;margin:14pt 0 0 0">'
             f'<tr><td style="background-color:{SHADE};border:1pt solid {SHADE};padding:10pt 12pt">'
             f'<p style="{P_KICKER}">TAKE PART</p>'
             f'<p style="{_style(BODY, 10, lh=1.5, before=4)}">Message the network on Telegram and say what you see: '
             f'{code("rain 1.2in")}, {code("hail quarter")}, {code("trees down")}, or a sentence. Set your home '
             f'once with {code("/home 62704")} and choose what reaches you with {code("/subscribe warnings cook")}. '
             f'Readings are checked against neighbours and official sources; confirmed ones build your trust '
             f'and become events.</p></td></tr></table>')

    footer = f"{m.hours:g}-hour window · made {_e(m.generated_at)}" + (
        f" · subscriptions: {_e(', '.join(f'{k} {n}' for k, n in m.subscriptions.items()))}" if m.subscriptions else "")
    p.append(f'<p style="{_style(MONO, 7.5, color=MUTED, lh=1.4, before=14)}">{footer}</p>')

    # the first section goes without its rule: the numbers close page 1, and Docs' import makes a
    # rule a line of its own that can stay at the foot of page 1 while the heading starts page 2
    first = next((i for i in range(sections, len(p)) if p[i].startswith("<hr>")), None)
    if first is not None:
        p[first] = p[first].removeprefix("<hr>")
    body = "\n".join(x for x in p if x)
    if not page:
        return body
    return f"<style>{BROWSER_CSS}</style>\n<div class=\"sheet\">\n{body}\n</div>"


def _long_date(date: str) -> str:
    """2026-09-16 → Wednesday, 16 September 2026 (falls back to the raw string)."""
    try:
        return datetime.strptime(date, "%Y-%m-%d").strftime("%A, %-d %B %Y")
    except ValueError:
        return date


def render_text(m: DigestModel) -> str:
    v = m.vitals
    lines = [f"{m.network_name} — {m.state} environmental digest — {m.date}", m.headline, ""]
    if m.narrative:
        lines += [m.narrative, ""]
    lines.append(f"Vitals: sensors {v.get('sensors_total', 0)} (active 24h {v.get('sensors_active_24h', 0)}) · "
                 f"readings 24h {v.get('signals_24h_human', 0)} human / {v.get('signals_24h_reference', 0)} ref · "
                 f"corroboration 7d {v.get('corroboration_rate_7d', 0):.0%} · "
                 f"coverage {v.get('counties_covered_7d', 0)}/{v.get('counties_total', 0)} counties")
    lines.append("")
    if m.storms:
        lines.append("Storms:")
        for st in m.storms:
            lines.append(f"  {st['title']} — {', '.join(st['counties'])}")
            lines.append(f"    {st['brief']}")
            lines += [f"    [{i}] {f}" for i, f in enumerate(st["facts"], 1)]
        lines.append("")
    lines.append("Events:")
    lines += [f"  {e.get('score') or 0:.2f}  {e['title']}  ({e.get('n_sensors')} sensors"
              f"{', official' if e.get('n_reference') else ''}{', verified' if e['verified'] else ''})"
              for e in m.events] or ["  none"]
    lines.append("")
    if m.ahead:
        lines.append("The day ahead:")
        lines += [f"  {r.name}: {r.text()} for {ahead.names(cs)}" for r, cs in m.ahead.risks]
        lines += [f"  {name}: " + " · ".join(f"{x.name}: {x.text()}" for x in ps) for name, ps in m.ahead.places]
        lines.append("")
    lines.append("NWS alerts:")
    lines += [f"  [{a['severity']}] {a['event']} — {', '.join(a['counties'][:5])} (until {_when(a['expires'])})"
              for a in m.alerts[:15]] or ["  none"]
    lines.append("")
    listed, _ = notable.listing(m.day)
    if listed:
        lines.append("In the news, and what was measured there:")
        for st in listed:
            lines.append(f"  {st.n} · {st.where}: {st.title}" + (f" ({st.source})" if st.source else ""))
            lines += [f"      ↳ {r}" for r in st.readings] + ([f"      {st.alerts}"] if st.alerts else [])
        lines.append("")
    stand = notable.standouts(m.day)
    lines.append("What stood out: " + "; ".join(f"{a} ({b})" for a, b, _ in stand) if stand else "What stood out: nothing")
    lines.append("")
    lines.append("Reading:")
    lines += [f"  - {r['title']}" for r in m.reading[:10]] or ["  none"]
    lines.append("")
    lines.append(f"Sensors wanted: {m.gap_count} counties without a human reading this week.")
    return "\n".join(lines)


# ── CSV ────────────────────────────────────────────────────────────────────

EVENT_COLUMNS = ("opened_at", "updated_at", "topic", "metric", "metric_label", "title", "county_label",
                 "county_fips", "zip5", "lat", "lon", "peak_value", "unit", "peak_display", "score",
                 "severity", "n_signals", "n_sensors", "n_reference", "mean_trust", "verified", "status")
ALERT_COLUMNS = ("event", "severity", "counties", "sent", "expires", "sender", "headline", "url")
COUNTY_COLUMNS = ("county", "county_fips", "topic", "metric", "metric_key", "n", "n_human", "n_sensors",
                  "mean", "max", "latest")
EXTREME_COLUMNS = ("metric", "how", "value", "county", "n")
CONTRIBUTOR_COLUMNS = ("name", "county", "trust", "n", "n_corr")
READING_COLUMNS = ("published_at", "feed", "title", "relevance", "reason", "url")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value)


def _csv(columns: tuple[str, ...], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in columns])
    return buf.getvalue()


def render_csvs(m: DigestModel) -> dict[str, str]:
    """The digest's tables as CSV, one per table — {name: text}.

    The document is for reading; these are for working. Headers stay put even on
    a quiet day, so a spreadsheet or script pointed at a file keeps working.
    """
    return {
        "events": _csv(EVENT_COLUMNS, m.events),
        "official-alerts": _csv(ALERT_COLUMNS, m.alerts),
        "county-activity": _csv(COUNTY_COLUMNS, m.contributions),
        "station-extremes": _csv(EXTREME_COLUMNS, m.extremes),
        "contributors": _csv(CONTRIBUTOR_COLUMNS, m.leaderboard),
        "reading-list": _csv(READING_COLUMNS, m.reading),
        "network-vitals": _csv(("metric", "value"),
                               [{"metric": k, "value": v} for k, v in sorted(m.vitals.items())]),
    }
