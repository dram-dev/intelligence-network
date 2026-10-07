"""The day's notable readings and the news tied to them: the masthead map on the site
(site/index.fragment.html) and data/map.json.

The weather service draws its own warnings. What the network adds is where something it
measured stood out, in every topic, and which stories those readings bear on. What stands
out comes from the packs, not from here:

- a reading past its metric's event threshold (`event`);
- a metric's headline reading (`headline`): the state's highest or lowest of the day, or
  the biggest rise at one site, with the short name the map labels it by;
- what people reported, and the NWS's storm reports;
- a reading in a place a kept story names, of a measure the story is about (connect.py).

Each place appears once, with everything notable it measured; the strongest few of each
topic are labelled on the map and the rest are dots. People appear by handle at their
ZIP's centre (the site's rule), never by name, note or exact point. A source that reports
daily or weekly (a soil station) shows its latest day for up to `SLOW_DAYS`; a reading
for a whole county past its threshold (the Drought Monitor) is an area, not a point.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from itertools import pairwise
from typing import Any

from intelnet import connect, db, geo, network, opendata
from intelnet.config import PROJECT_ROOT, settings
from intelnet.models import (
    KIND_BOT,
    KIND_HUMAN,
    Signal,
    iso,
    local_time,
    parse_iso,
    public_handle,
    utcnow,
)
from intelnet.topics import Metric, find_metric, topic_of_metric, topics

HOURS = 24
SLOW_DAYS = 8                 # a daily or weekly source shows its latest day this long
LABELLED = 14                 # places labelled on the map …
PER_TOPIC = 3                 # … at most this many of one topic
DOTS = 80                     # the rest, as dots
STORIES = 7                   # places the news names (numbered on the map)
STATEWIDE = 3                 # stories that name no place
LINKS = 4                     # readings drawn to one story
RELEVANCE = 0.8               # what triage must have scored a story for the map
KINDS = ("human", "bot", "station", "official")
SPARK_POINTS = 48

# What ranks a place: its strongest reading's event severity (0–1) plus these
W_HEADLINE = 0.35             # the state's highest or lowest
W_RISE = (0.35, 0.25, 0.2)    # the three biggest rises
W_PEOPLE, W_CHECKED = 0.3, 0.1
W_REPORT = 0.15
W_NEWS = 0.4
MIN_LABELLED = 0.3
TWIN_KM = 50                  # a second label for the same measure needs this much room
MIN_FIELD = 3                 # sites a "highest in Illinois" needs to beat to mean anything
HEADLINE_KINDS = ("max", "min", "rise")
# The order a place's reasons are told in: "Highest rainfall total in Illinois" says more than
# "past the network's event threshold" when both hold; ties: the stronger kind leads.
ORDER = ("max", "min", "event", "rise", "people", "report", "news")


@dataclass
class Why:
    """One reason a place is on the map."""
    kind: str                 # event | rise | max | min | people | report | news
    metric: str
    weight: float
    text: str
    value: float | None = None          # the reading (a rise: how far)
    at: datetime | None = None


@dataclass
class Place:
    key: tuple[Any, ...]
    lat: float
    lon: float
    county: str | None
    source: str               # what measured it: "USGS stream gauge"
    name: str
    url: str | None = None
    person: bool = False
    zip5: str | None = None
    series: dict[str, list[Signal]] = field(default_factory=dict)     # metric → readings, oldest first
    whys: list[Why] = field(default_factory=list)
    news: list[str] = field(default_factory=list)
    id: str = ""

    @property
    def score(self) -> float:
        best: dict[str, float] = {}
        for w in self.whys:                     # two storm reports at one place don't count twice
            best[w.kind] = max(best.get(w.kind, 0.0), w.weight)
        return sum(best.values())

    @property
    def lead(self) -> Why | None:
        """The reason the map labels it by: what it measured, ahead of a story about it."""
        own = [w for w in self.whys if w.kind != "news"] or self.whys
        return max(own, key=lambda w: (w.weight, -ORDER.index(w.kind)), default=None)

    @property
    def topic(self) -> str:
        t = topic_of_metric(self.lead.metric) if self.lead else None
        return t.name if t else "weather"


# ── where things are ──────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _state_shapes() -> list[tuple[tuple[float, float, float, float], list[list[list[float]]]]]:
    """(bbox, rings) for each county, from the site's county shapes."""
    path = PROJECT_ROOT / "site" / "assets" / f"{settings.geo_state.lower()}-counties.geojson"
    try:
        features = json.loads(path.read_text(encoding="utf-8"))["features"]
    except (OSError, ValueError, KeyError):
        return []
    out = []
    for f in features:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for rings in polys:
            xs = [p[0] for p in rings[0]]
            ys = [p[1] for p in rings[0]]
            out.append(((min(xs), min(ys), max(xs), max(ys)), rings))
    return out


def in_state(lat: float, lon: float) -> bool:
    """Inside the state's outline (a quake 'near Ridgely, Tennessee' is not, though the
    nearest county centre is Alexander's). True when the shapes are missing."""
    shapes = _state_shapes()
    if not shapes:
        return True
    return any(x0 <= lon <= x1 and y0 <= lat <= y1 and geo.point_in_polygon(lat, lon, rings[:1])
               for (x0, y0, x1, y1), rings in shapes)


# ── what measured it ──────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _casing() -> dict[str, str]:
    """One-word county names as the Census writes them: 'Dupage' → 'DuPage'."""
    return {c.name.lower(): c.name for c in geo.counties().values() if " " not in c.name}


def _title(text: str) -> str:
    return " ".join(_casing().get(w.lower(), w) for w in text.title().split())


def _station(name: str) -> str:
    """'BLOOMINGTON/NORM (BMI)' → 'Bloomington (BMI)'. IEM cuts names at 16 letters, so a
    cut name keeps its whole first part; for 'CHICAGO/AURORA' that's the second (Aurora)."""
    m = re.match(r"(.*?)\s*\((\w+)\)\s*$", name)
    base, code = (m.group(1), m.group(2)) if m else (name, "")
    base = re.sub(r"\s+", " ", base).strip().removesuffix(" IL").strip()
    if base.isupper():
        parts = [p.strip() for p in re.split(r"/| - ", base) if p.strip()]
        if len(parts) > 1:
            base = parts[1] if parts[0] == "CHICAGO" else parts[0]
        elif len(base) >= 16 and " " in base:          # cut mid-word
            base = base.rsplit(" ", 1)[0]
        base = _title(base)
    return f"{base} ({code})" if code else base


def _lsr_place(ev: dict[str, Any]) -> str:
    """'2 NNW Germantown Hills' → '2 mi NNW of Germantown Hills'."""
    city = str(ev.get("city") or "").strip()
    if city.isupper():
        city = _title(city)
    m = re.match(r"^(\d+(?:\.\d+)?)\s+([NSEW]{1,3})\s+(.+)$", city)
    return f"{m.group(1)} mi {m.group(2)} of {m.group(3)}" if m else city


def _observer(name: str, code: str) -> str:
    """A CoCoRaHS station: 'Rantoul 1.4 NNE' → '1.4 mi NNE of Rantoul'; one with no name
    (the name is its number) → 'CoCoRaHS IL-LK-144'."""
    m = re.match(r"^(.+?)\s+(\d+(?:\.\d+)?)\s+([NSEW]{1,3})$", name.strip())
    if m:
        return f"{m.group(2)} mi {m.group(3)} of {m.group(1)}"
    return name if name and name != code else f"CoCoRaHS {code}"


def _reporter(ev: dict[str, Any]) -> str:
    who = str(ev.get("reporter") or "").strip()
    return {"cocorahs": "a CoCoRaHS observer", "co-op observer": "a co-op observer",
            "trained spotter": "a trained spotter", "public": "the public",
            "emergency mngr": "an emergency manager"}.get(who.lower(), who)


def _describe(s: Signal, sensors: dict[str, str]) -> tuple[str, str, str | None]:
    """(what measured it, its name, a page about it) for a reference reading."""
    ev = s.evidence
    kind = ev.get("kind")
    if kind == "gauge":
        return "USGS stream gauge", connect._site(s), ev.get("url")
    if kind == "station":
        code, name = str(ev.get("station") or ""), str(ev.get("name") or "")
        known = sensors.get(s.sensor_id) or (f"{name} ({code})" if name and name != code else None)
        return ("Airport station", _station(known) if known else code,
                f"https://mesonet.agron.iastate.edu/sites/site.php?station={code}&network={settings.geo_state}_ASOS"
                if code else None)
    if kind == "observer":
        return "CoCoRaHS observer", _observer(str(ev.get("name") or ""), str(ev.get("station") or "")), None
    if kind == "lsr":
        return "NWS storm report", _lsr_place(ev) or "Storm report", None
    if kind == "grain_bid":
        return "USDA grain bids", f"{ev.get('district') or 'Illinois'} district", None
    if kind == "scan":
        site = str(ev.get("station") or "").split(":")[0]
        return ("NRCS soil station", f"{ev.get('name') or 'Illinois'} soil station",
                f"https://wcc.sc.egov.usda.gov/nwcc/site?sitenum={site}" if site else None)
    if kind == "quake":
        return "USGS earthquake", str(ev.get("place") or "Earthquake"), ev.get("url")
    c = geo.county(s.location.county_fips)
    name = sensors.get(s.sensor_id)
    return ({"station": "Weather station", "official": "Official report"}.get(s.sensor_kind, "Reference reading"),
            _station(name) if name else (c.label if c else "Illinois"), ev.get("url"))


# ── the readings ──────────────────────────────────────────────────────────

def _signals(now: datetime) -> list[Signal]:
    """The window's readings, plus the latest day of each metric that reports less often."""
    sigs = [s for s in db.recent_signals(HOURS, kinds=KINDS, limit=200_000)
            if not s.metric.startswith("alert.") and s.quality != "rejected"]
    fresh = {s.metric for s in sigs}
    kinds = ",".join("?" * len(KINDS))
    with db.get_conn() as conn:
        latest = conn.execute(
            f"""SELECT metric, MAX(observed_at) AS at FROM signals
                WHERE observed_at >= ? AND metric NOT LIKE 'alert.%' AND quality != 'rejected'
                  AND sensor_kind IN ({kinds}) GROUP BY metric""",
            (iso(now - timedelta(days=SLOW_DAYS)), *KINDS)).fetchall()
        for r in latest:
            at = parse_iso(r["at"])
            if r["metric"] in fresh or at is None:
                continue
            rows = conn.execute(
                f"""SELECT * FROM signals WHERE metric = ? AND observed_at >= ? AND quality != 'rejected'
                      AND sensor_kind IN ({kinds})""",
                (r["metric"], iso(at - timedelta(hours=HOURS)), *KINDS)).fetchall()
            sigs += [Signal.from_row(x) for x in rows]
    return sigs


def _places(sigs: list[Signal]) -> list[Place]:
    sensors = {x.id: x.name for x in db.list_sensors(limit=100_000) if x.name}
    out: dict[tuple[Any, ...], Place] = {}
    for s in sorted(sigs, key=lambda x: x.observed_at):
        m = find_metric(s.metric)
        if m is None or m.county_wide:                  # a whole county's reading is an area: `_areas`
            continue
        person = s.sensor_kind in (KIND_HUMAN, KIND_BOT)
        if person:
            pt = opendata.public_point(s.location.zip5, s.location.county_fips)
            if pt is None:
                continue
            key: tuple[Any, ...] = ("person", s.sensor_id, pt)
            lat, lon = pt
        else:
            if not s.location.has_point:
                continue
            lat, lon = float(s.location.lat), float(s.location.lon)
            key = (s.sensor_id, round(lat, 3), round(lon, 3))
        p = out.get(key)
        if p is None:
            if not in_state(lat, lon):
                continue
            c = geo.county(s.location.county_fips)
            if person:
                p = Place(key, lat, lon, c.fips if c else None, "Network member", public_handle(s.sensor_id),
                          person=True, zip5=s.location.zip5)
            else:
                source, name, url = _describe(s, sensors)
                p = Place(key, round(lat, 4), round(lon, 4), c.fips if c else None, source, name, url)
            out[key] = p
        p.series.setdefault(s.metric, []).append(s)
    return list(out.values())


def _top(m: Metric, sigs: list[Signal]) -> Signal | None:
    """The reading that stands out: the highest, or the lowest for a measure that matters
    when it falls (visibility, basis)."""
    vals = [s for s in sigs if s.value is not None]
    if not vals:
        return None
    return (min if m.event_direction == "below" else max)(vals, key=lambda s: s.value)


def _period_hours(period: Any) -> float | None:
    """'1h' → 1, '24h' → 24, '30m' → 0.5; None when there's no period."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([hm])\s*", str(period or ""))
    return (float(m.group(1)) / (60 if m.group(2) == "m" else 1)) if m else None


def _hourly(sigs: list[Signal]) -> bool:
    """Amounts over a short period (a station's rain in the last hour) are a rate, not a total;
    a day's amount (a CoCoRaHS observer's 24 hours to 7 AM) is a total."""
    return any((h := _period_hours(s.evidence.get("period"))) is not None and h < 6 for s in sigs)


def _linear(m: Metric) -> bool:
    """A difference reads in the display unit only when zero displays as zero (feet, cfs;
    not °F)."""
    return m.display(0.0).split(" ")[0] in ("0", "0.00")


def rise(m: Metric, sigs: list[Signal]) -> float | None:
    """How far a site's last reading stands above its low of the window, when that's more
    than the measure's tolerance and isn't one jump (a datum shift: the West Branch DuPage
    "rose" 10 ft in an hour on 30 Sep while its flow barely moved)."""
    vals = [s.value for s in sigs if s.value is not None]
    if len(vals) < 3:
        return None
    up = vals[-1] - min(vals)
    steps = [b - a for a, b in pairwise(vals)]
    if up <= float(m.tolerance.get("abs") or 0) or max(steps) > 0.5 * up:
        return None
    return up


def _window(sigs: list[Signal], now: datetime) -> str:
    last = sigs[-1].observed_at
    return " in the last 24 hours" if now - last <= timedelta(hours=HOURS) \
        else f" in the latest reports ({local_time(last, '%b %-d')})"


def _reasons(places: list[Place], now: datetime) -> None:
    """Each place's reasons from its own readings, then the state-wide headline readings."""
    for p in places:
        for key, sigs in p.series.items():
            m = find_metric(key)
            if m is None or not m.scored:
                continue
            top = _top(m, sigs)
            if top is None:
                continue
            sev = m.severity(top.value)
            if sev > 0:
                text = f"{m.label} reported" if m.is_flag else f"{_short(m)} {'well past' if sev >= 0.7 else 'past'}" \
                    " the network's event threshold"
                p.whys.append(Why("event", key, sev, text, top.value, top.observed_at))
            humans = [s for s in sigs if s.sensor_kind in (KIND_HUMAN, KIND_BOT)]
            if humans:
                checked = any(s.quality == "corroborated" for s in humans)
                p.whys.append(Why("people", key, W_PEOPLE + (W_CHECKED if checked else 0),
                                  "Reported by a network member" + (", and corroborated" if checked else ""),
                                  top.value, top.observed_at))
            reports = [s for s in sigs if s.sensor_kind == "official" and s.evidence.get("reporter")]
            if reports:
                p.whys.append(Why("report", key, W_REPORT, f"Reported to the NWS by {_reporter(reports[-1].evidence)}",
                                  top.value, top.observed_at))
    for t in topics().values():
        for m in t.metrics.values():
            for stat in ("max", "min"):
                if stat not in m.headline:
                    continue
                best: tuple[float, Place, Signal] | None = None
                field_ = 0
                for p in places:
                    sigs = p.series.get(m.key)
                    if not sigs or (m.accumulates and _hourly(sigs)):      # compare totals with totals
                        continue
                    vals = [s for s in sigs if s.value is not None]
                    if not vals:
                        continue
                    field_ += 1
                    s = (max if stat == "max" else min)(vals, key=lambda x: x.value)
                    v = s.value if stat == "max" else -s.value
                    if best is None or v > best[0]:
                        best = (v, p, s)
                if best is not None and field_ >= MIN_FIELD:      # "lowest of one" says nothing
                    _, p, s = best
                    what = m.label.lower() + (" total" if m.accumulates else "")
                    p.whys.append(Why(stat, m.key, W_HEADLINE,
                                      f"{'Highest' if stat == 'max' else 'Lowest'} {what} in Illinois"
                                      + _window(p.series[m.key], now), s.value, s.observed_at))
            if "rise" in m.headline and _linear(m):
                ups = sorted(((u, p) for p in places if (sigs := p.series.get(m.key)) and (u := rise(m, sigs))),
                             key=lambda x: -x[0])
                for i, (u, p) in enumerate(ups[:len(W_RISE)]):
                    nth = ("Biggest", "Second-biggest", "Third-biggest")[i]
                    p.whys.append(Why("rise", m.key, W_RISE[i], f"{nth} rise in Illinois"
                                      + _window(p.series[m.key], now), u, p.series[m.key][-1].observed_at))


# ── the news ──────────────────────────────────────────────────────────────

@dataclass
class Story:
    title: str
    source: str
    url: str | None
    at: str | None
    topic: str | None
    relevance: float
    counties: list[str]
    label: str
    measures: set[str]                  # the metrics the headline names


@dataclass
class Group:
    counties: list[str]
    stories: list[Story]
    links: list[tuple[Place, str | None]] = field(default_factory=list)    # (place, the measure it's linked for)

    @property
    def place(self) -> str:
        return max(self.stories, key=lambda s: len(s.counties)).label

    @property
    def point(self) -> tuple[float, float] | None:
        """Where the most specific name among the stories sits: 'Chicago' before 'Chicago area'."""
        for st in sorted(self.stories, key=lambda s: (len(s.counties), -s.relevance)):
            pt = connect.place_point(st.label)
            if pt:
                return pt
        cs = [c for c in (geo.county(f) for f in self.counties) if c]
        return (sum(c.lat for c in cs) / len(cs), sum(c.lon for c in cs) / len(cs)) if cs else None


def _stories() -> list[Story]:
    from intelnet import digest

    metrics = [m for t in topics().values() for m in t.metrics.values()]
    out, seen = [], set()
    for r in db.kept_items_since(HOURS + 6, limit=80):
        if (r["relevance"] or 0) < RELEVANCE:
            continue
        item = {"title": r["title"], "url": r["url"],
                "feed": (json.loads(r["metadata_json"] or "{}").get("feed") if r["metadata_json"] else None)}
        title, source = digest._publisher(item)
        key = " ".join(title.lower().split())
        if key in seen:
            continue
        seen.add(key)
        counties, label = connect.places_in(title)
        out.append(Story(title, source.split(" · ")[0], r["url"], r["published_at"], r["topic"],
                         float(r["relevance"] or 0), counties, label,
                         {m.key for m in metrics if connect._named(m, title)}))
    return out


def _groups(stories: list[Story]) -> list[Group]:
    """Stories about the same place, together. A story about one place joins one about a
    wider place that holds it ('Chicago' into 'Chicago area') when they name a measure in
    common, or both name none and share a topic: the bird count in Will County stays apart
    from the Chicago-area flooding."""
    by: dict[tuple[str, ...], list[Story]] = {}
    for st in stories:
        if st.counties:
            by.setdefault(tuple(sorted(st.counties)), []).append(st)
    merged: list[Group] = []
    for key in sorted(by, key=lambda k: (-len(k), k)):
        sts = by[key]
        measures = set().union(*(s.measures for s in sts))
        topics_ = {s.topic for s in sts}

        def fits(g: Group, measures: set[str] = measures, topics_: set[str | None] = topics_) -> bool:
            theirs = set().union(*(s.measures for s in g.stories))
            return bool(measures & theirs) or (not measures and not theirs and bool(topics_ & {s.topic for s in g.stories}))

        home = next((g for g in merged if set(key) <= set(g.counties) and fits(g)), None)
        if home is None:
            merged.append(Group(list(key), list(sts)))
        else:
            home.stories += sts
    return merged


def _rises(m: Metric) -> bool:
    """Does a rise at one site mean something for this measure (a river's height or flow),
    as opposed to a running amount (rain), a reading on an offset scale (°F), or one that
    matters when it falls (visibility clearing isn't news)?"""
    return not m.accumulates and not m.is_flag and m.event_direction == "above" and _linear(m)


def _measure_best(m: Metric, cands: list[Place]) -> Place | None:
    """The place whose reading of this measure says most: its biggest rise, else its peak.
    A running amount counts only as a total (a story about heavy rain isn't borne out by a
    station's wettest hour)."""
    if _rises(m):
        ups = [(u, p) for p in cands if (sigs := p.series.get(m.key)) and (u := rise(m, sigs))]
        if ups:
            return max(ups, key=lambda x: x[0])[1]
    tops = [(t, p) for p in cands if (sigs := p.series.get(m.key)) and not (m.accumulates and _hourly(sigs))
            and (t := _top(m, sigs)) is not None]
    if not tops:
        return None
    pick = (min if m.event_direction == "below" else max)(tops, key=lambda x: x[0].value)
    return pick[1]


def _link(g: Group, places: list[Place], statewide: bool) -> None:
    """The readings a story is about: in its counties (a statewide story: anywhere, among
    the day's notable ones), the best reading of each measure it names, then anything past
    its threshold there."""
    cands = [p for p in places if (statewide and p.whys) or (not statewide and p.county in g.counties)]
    named = Counter(k for s in g.stories for k in s.measures)
    picks: list[tuple[Place, str | None]] = []
    # the measure most stories name first; between equals, one the packs headline (a river's height before its flow)
    for key, _ in sorted(named.items(), key=lambda kv: (-kv[1], not (find_metric(kv[0]) or Metric("", "", "")).headline,
                                                         kv[0])):
        m = find_metric(key)
        best = _measure_best(m, cands) if m else None
        if m is None or best is None or any(best is p for p, _ in picks):
            continue
        own = any(w.metric == key and w.kind != "news" for w in best.whys)
        if statewide and not own:                       # statewide: only what stood out anyway
            continue
        # a level that didn't move or cross a threshold bears on nothing (84 °F under "cooler
        # temperatures"); a rise does, and so does any total of a running amount
        if not (own or m.accumulates or (_rises(m) and rise(m, best.series[key]))):
            continue
        picks.append((best, key))
    if not statewide:
        picks += [(p, None) for p in sorted(cands, key=lambda p: -p.score)
                  if not any(p is q for q, _ in picks) and any(w.kind == "event" for w in p.whys)]
    # what stood out on its own before what merely sits where the story is
    picks.sort(key=lambda pk: not any(w.kind != "news" for w in pk[0].whys))
    g.links = picks[:LINKS]


def _linked(st: Story, places: list[Place]) -> list[tuple[Place, str | None]]:
    g = Group([], [st])
    _link(g, places, statewide=True)
    return g.links


def _alerts(counties: list[str]) -> list[str]:
    from intelnet.feeds.nws_alerts import active_alert_groups

    out: list[str] = []
    scope = set(counties)
    for ag in active_alert_groups():
        ev = str(ag["signal"].evidence.get("event") or "")
        covered = {c.fips for c in (geo.county_by_name(n) for n in ag["counties"]) if c}
        if ev and ev not in out and covered & scope:
            out.append(ev)
    return out


# ── words for people ──────────────────────────────────────────────────────

def tight(label: str) -> str:
    """A label as the map writes it, the site and the brief alike: '93.9 °F' → '93.9°F',
    '-40 ¢' → '−40¢', '▲ 1.86 ft' → '▲1.86 ft'."""
    for unit in ("°F", "°C", "%", "¢"):
        label = label.replace(f" {unit}", unit)
    return re.sub(r"(^|[\s(])-(?=\d)", "\\1\u2212", label).replace("▲ ", "▲")


def topic_label(topic: str | None) -> str:
    """A topic's name for people: its pack's label, or the reading list's own topics."""
    packs = {t.name: t.label for t in topics().values()}
    extra = {"landuse": "Land use", "emergency": "Emergency", "research": "Research"}
    return packs.get(topic or "") or extra.get(topic or "") or (topic or "News").title()


@dataclass
class Listed:
    """One of the day's stories as the brief and the digest list it, under the map's number."""
    n: int
    where: str                      # "Chicago area", or "Agriculture, statewide"
    title: str                      # the lead headline
    url: str | None
    source: str                     # "NBC 5 Chicago · 6 more stories"
    readings: list[str]             # "▲1.27 ft, Du Page River at Shorewood"
    alerts: str = ""                # "Flood Warning and Flood Watch in force"


def listing(day: dict[str, Any]) -> tuple[list[Listed], set[str]]:
    """The day's stories under the numbers the map gives them, each with what the network
    measured where it is; and every headline they hold (lower-cased), so a reading list
    after them doesn't repeat one."""
    places = {p["id"]: p for p in day.get("places") or []}
    out: list[Listed] = []
    used: set[str] = set()
    for n in day.get("news") or []:
        lead = n["stories"][0]
        others = len(n["stories"]) - 1 + len(n.get("more") or [])
        source = " · ".join(x for x in (lead["source"], f"{others} more {'story' if others == 1 else 'stories'}"
                                        if others else "") if x)
        used |= {" ".join(t.lower().split()) for t in [s["title"] for s in n["stories"]] + (n.get("more") or [])}
        readings = [f"{tight(p['label'])}, {p['name']}" for p in (places.get(i) for i in n["links"]) if p]
        out.append(Listed(n["n"], n["place"] or f"{topic_label(n['topic'])}, statewide", lead["title"], lead["url"],
                          source, readings, f"{_and(n['alerts'])} in force" if n["alerts"] else ""))
    return out, used


def _and(names: list[str]) -> str:
    """'Flood Warning, Flood Watch and Flood Advisory'."""
    return names[0] if len(names) < 2 else f"{', '.join(names[:-1])} and {names[-1]}"


def standouts(day: dict[str, Any]) -> list[tuple[str, str, str]]:
    """The labelled readings, as text: (label, where, why) — 'Rain 1.50 in', '3 mi E of
    Garden Plain, Whiteside', 'Highest rainfall total in Illinois in the last 24 hours'."""
    numbers = {n["id"]: n["n"] for n in day.get("news") or []}
    out = []
    for p in (p for p in day.get("places") or [] if p["tier"] == 1):
        where = p["name"] + (f", {p['county']}" if p.get("county") and p["county"] not in p["name"] else "")
        stories = [str(numbers[i]) for i in p.get("news") or [] if i in numbers]
        tied = f"Tied to {'story' if len(stories) == 1 else 'stories'} {_and(stories)}" if stories else ""
        why = next((w for w in p["why"] if not w.startswith("In the news")), tied or (p["why"] or [""])[0])
        out.append((tight(p["label"]), where, why))
    return out


# ── the document ──────────────────────────────────────────────────────────

def _spark(m: Metric, sigs: list[Signal]) -> dict[str, Any] | None:
    """The measure's last day at this site, in the display unit: minutes before the last
    reading, and values."""
    pts = [s for s in sigs if s.value is not None]
    if len(pts) < 6:
        return None
    if len(pts) > SPARK_POINTS:                 # evenly spaced, ending on the latest
        pts = [pts[round(i * (len(pts) - 1) / (SPARK_POINTS - 1))] for i in range(SPARK_POINTS)]
    fn = m.display_unit["fn"] if m.display_unit else (lambda x: x)
    last = pts[-1].observed_at
    return {"name": m.label, "unit": m.display_unit["unit"] if m.display_unit else m.unit,
            "t": [round((s.observed_at - last).total_seconds() / 60) for s in pts],
            "v": [round(float(fn(s.value)), 3) for s in pts]}


def _short(m: Metric) -> str:
    """What a label calls a measure: the pack's short name, else its label less any "(…)"."""
    return m.short or re.sub(r"\s*\(.*?\)", "", m.label).strip()


def _label(p: Place) -> str:
    """What the map writes beside a labelled place: 'High 93.9 °F', '▲ 1.86 ft', 'Rain 1.5 in',
    'Tornado'. A headline reading goes by its headline name; anything else by the measure's
    one headline name when it has just one ('Rain'), else its label ('Temperature')."""
    lead = p.lead
    m = find_metric(lead.metric) if lead else None
    if m is None:
        return p.name
    sigs = p.series.get(m.key) or []
    up = lead.value if lead.kind == "rise" else rise(m, sigs) if lead.kind == "news" and _rises(m) else None
    if up:
        return f"▲ {m.display(up)}"
    if m.is_flag:
        return m.label
    names = {k: v for k, v in m.headline.items() if k != "rise"}
    prefix = names.get(lead.kind) or (next(iter(names.values())) if len(names) == 1 else _short(m))
    top = lead.value if lead.value is not None else (t.value if (t := _top(m, sigs)) else None)
    shown = m.display(top) + ("/hr" if m.accumulates and _hourly(sigs) else "")    # a rate, not a storm total
    return shown if shown.endswith(f" {prefix}") else f"{prefix} {shown}"         # "158 AQI", not "AQI 158 AQI"


def _lines(p: Place) -> list[dict[str, Any]]:
    """The place's readings worth showing, the one that put it on the map first; a gauge
    shows its flow beside its height."""
    lead = p.lead
    keys = sorted({w.metric for w in p.whys}, key=lambda k: (k != (lead.metric if lead else ""), k))
    if p.source == "USGS stream gauge":
        keys += [k for k in p.series if k not in keys and k in ("stage_m", "discharge_cms")]
    out = []
    for key in keys:
        m = find_metric(key)
        sigs = p.series.get(key) or []
        if m is None or not sigs:
            continue
        whys = [w for w in p.whys if w.metric == key]
        up = next((w for w in whys if w.kind == "rise"), None)
        shown = next((w for w in sorted(whys, key=lambda w: -w.weight) if w.kind != "rise" and w.value is not None), None)
        last = sigs[-1]
        if up is not None or shown is None:
            value, at = last.value, last.observed_at
        else:
            value, at = shown.value, shown.at
        note = None
        risen = up.value if up is not None else rise(m, sigs) if _rises(m) else None
        if risen:
            note = f"up {m.display(risen)} in 24 hours"
        elif m.accumulates and _hourly(sigs):
            note = "the wettest hour" if m.key == "rain_mm" else "in one hour"
        elif last.evidence.get("delivery_point"):
            note = str(last.evidence["delivery_point"]).lower()
        elif shown is not None and shown.kind in ("max", "min") and len(sigs) > 1:
            note = "the day's high" if shown.kind == "max" else "the day's low"
        out.append({"metric": key, "name": m.label, "value": m.display(value) if not m.is_flag else "reported",
                    "note": note, "at": at.isoformat(timespec="minutes") if at else None,
                    "sev": round(m.severity(value), 2) if value is not None and not m.is_flag else None})
    return out


def _status(p: Place) -> str:
    qs = {s.quality for sigs in p.series.values() for s in sigs}
    if not p.person:
        return "official"
    return "corroborated" if "corroborated" in qs else "flagged" if "flagged" in qs else "unverified"


def _event(p: Place, events: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    ids = {s.event_id for sigs in p.series.values() for s in sigs if s.event_id}
    evs = [events[i] for i in ids if i in events]
    if not evs:
        return None
    e = max(evs, key=lambda e: e.get("score") or 0)
    return {"title": e.get("title"), "verified": bool(e["verified"]), "score": round(e.get("score") or 0, 2)}


def _place_doc(p: Place, events: dict[int, dict[str, Any]]) -> dict[str, Any]:
    lead = p.lead
    m = find_metric(lead.metric) if lead else None
    c = geo.county(p.county)
    sigs = p.series.get(lead.metric) if lead else None
    spark = _spark(m, sigs) if m and sigs and not p.person and p.source in (
        "USGS stream gauge", "Airport station", "NRCS soil station") else None
    whys = sorted(p.whys, key=lambda w: (ORDER.index(w.kind), -w.weight))
    seen: set[str] = set()
    once = {"people", "report", "news"}               # who reported it is one line, however many readings
    why_text = [w.text for w in whys
                if not (w.text in seen or (w.kind in once and w.kind in seen) or seen.add(w.text) or seen.add(w.kind))]
    return {
        "id": p.id, "topic": p.topic, "source": p.source, "name": p.name,
        "county": c.name if c else None, "zip": p.zip5 if p.person else None,
        "lat": round(p.lat, 4), "lon": round(p.lon, 4), "person": p.person,
        "label": _label(p),
        "lines": _lines(p), "why": why_text[:3], "status": _status(p),
        "at": max(s.observed_at for sigs in p.series.values() for s in sigs).isoformat(timespec="minutes"),
        "url": p.url, "spark": spark, "event": _event(p, events), "news": p.news,
    }


def _areas(sigs: list[Signal]) -> list[dict[str, Any]]:
    """Readings for a whole county past their threshold (the Drought Monitor's D2 or
    worse): one area per measure and level, the latest reading of each county."""
    latest: dict[tuple[str, str], Signal] = {}
    for s in sorted(sigs, key=lambda x: x.observed_at):
        m = find_metric(s.metric)
        if m is not None and m.county_wide and s.location.county_fips and s.value is not None:
            latest[(s.metric, s.location.county_fips)] = s
    by: dict[tuple[str, float], list[Signal]] = {}
    for (key, _), s in latest.items():
        m = find_metric(key)
        if m is not None and m.severity(s.value) > 0:
            by.setdefault((key, s.value), []).append(s)
    out = []
    for (key, value), ss in sorted(by.items(), key=lambda kv: -find_metric(kv[0][0]).severity(kv[0][1])):
        m = find_metric(key)
        names = [w for w, v in m.words.items() if v == value]
        label = (f"{max(names, key=len).capitalize()} ({min(names, key=len).upper()})" if len(names) > 1
                 else m.display(value))
        out.append({"topic": m.topic, "metric": key, "name": m.label, "label": label,
                    "counties": sorted(s.location.county_fips for s in ss), "sev": m.severity(value),
                    "at": max(s.observed_at for s in ss).isoformat(timespec="minutes"),
                    "url": ss[0].evidence.get("url")})
    return out


def _pick(places: list[Place]) -> tuple[list[Place], list[Place]]:
    """The labelled few: the state's headline readings first (its high, its low, its
    biggest rise), then one of each other measure, then the strongest; at most `PER_TOPIC`
    of a topic. The rest are dots."""
    ranked = sorted((p for p in places if p.whys), key=lambda p: -p.score)
    labelled: list[Place] = []
    per_topic: Counter[str] = Counter()
    seen: set[tuple[str, str, str]] = set()

    def kind(p: Place) -> str:
        lead = p.lead
        return lead.kind if lead and lead.kind in HEADLINE_KINDS else "other"

    for stage in ("headline", "diverse", "rest"):
        for p in ranked:
            lead = p.lead
            if len(labelled) >= LABELLED or lead is None:
                break
            what = (p.topic, lead.metric, kind(p))
            if p in labelled or p.score < MIN_LABELLED or per_topic[p.topic] >= PER_TOPIC:
                continue
            if stage == "headline" and not any(w.kind in HEADLINE_KINDS and w.weight >= W_HEADLINE for w in p.whys):
                continue
            if stage == "diverse" and what in seen:
                continue
            if stage == "rest" and any(q.lead and q.lead.metric == lead.metric
                                       and geo.haversine_km(p.lat, p.lon, q.lat, q.lon) < TWIN_KM for q in labelled):
                continue                                # two "AQI 158" labels side by side say it once
            labelled.append(p)
            per_topic[p.topic] += 1
            seen.add(what)
    dots = [p for p in ranked if p not in labelled][:DOTS]
    linked = [p for p in ranked if p.news and p not in labelled and p not in dots]
    return labelled, dots + linked


def build(now: datetime | None = None) -> dict[str, Any]:
    """The map document: places, stories, areas, and the topics with nothing to show."""
    now = now or utcnow()
    sigs = _signals(now)
    places = _places(sigs)
    _reasons(places, now)

    stories = _stories()
    located = _groups(stories)
    for g in located:
        _link(g, places, statewide=False)
    located.sort(key=lambda g: -(max(s.relevance for s in g.stories) + (0.3 if g.links else 0)
                                + 0.05 * (len(g.stories) - 1)))
    located = [g for g in located if g.point][:STORIES]
    # statewide: stories that say "Illinois" and bear on a reading before the rest
    state = {"IL": "illinois"}.get(settings.geo_state, settings.geo_state.lower())
    wide_links = {id(st): bool(_linked(st, places)) for st in stories if not st.counties}
    pool = sorted((s for s in stories if not s.counties),
                  key=lambda s: -(s.relevance + (0.2 if state in s.title.lower() else 0) + (0.3 if wide_links[id(s)] else 0)))
    picked: list[Story] = []
    for first in (True, False):           # one story per subject first: a new topic, naming new measures
        for st in pool:
            fresh = st.topic not in {x.topic for x in picked} and not st.measures & set().union(
                *(x.measures for x in picked))
            if len(picked) < STATEWIDE and st not in picked and (fresh or not first):
                picked.append(st)
    wide = [Group([], [st]) for st in sorted(picked, key=lambda s: -s.relevance)]
    for g in wide:
        _link(g, places, statewide=True)

    for i, g in enumerate(located + wide, start=1):
        for p, key in g.links:
            if not any(w.kind == "news" for w in p.whys):
                key = key or (p.lead.metric if p.lead else next(iter(p.series)))
                m = find_metric(key)
                top = _top(m, p.series[key]) if m and key in p.series else None
                p.whys.append(Why("news", key, W_NEWS, f"In the news: “{g.stories[0].title}”",
                                  top.value if top else None, top.observed_at if top else None))
            p.news.append(f"n{i}")

    labelled, dots = _pick(places)
    for i, p in enumerate(labelled + dots, start=1):
        p.id = f"p{i}"
    events = {e["id"]: e for e in (network.event_summary(r) for r in db.events_since(HOURS, limit=200))}
    docs = [_place_doc(p, events) | {"tier": 1} for p in labelled] + [_place_doc(p, events) | {"tier": 2} for p in dots]

    news = []
    for i, g in enumerate(located + wide, start=1):
        pt = g.point if g.counties else None
        news.append({
            "id": f"n{i}", "n": i, "topic": max(g.stories, key=lambda s: s.relevance).topic,
            "place": g.place if g.counties else None, "statewide": not g.counties,
            "lat": round(pt[0], 4) if pt else None, "lon": round(pt[1], 4) if pt else None,
            "counties": g.counties,
            "stories": [{"title": s.title, "source": s.source, "url": s.url, "at": s.at}
                        for s in sorted(g.stories, key=lambda s: -s.relevance)[:3]],
            "more": [s.title for s in sorted(g.stories, key=lambda s: -s.relevance)[3:13]],   # the rest, by title
            "links": [p.id for p, _ in g.links if p.id],
            "alerts": _alerts(g.counties) if g.counties else [],
        })

    areas = _areas(sigs)
    shown = {d["topic"] for d in docs} | {a["topic"] for a in areas}
    return {
        "as_of": now.replace(microsecond=0).isoformat(), "hours": HOURS,
        "places": docs, "news": news, "areas": areas,
        "quiet": [t.label for t in topics().values() if t.name not in shown],
    }
