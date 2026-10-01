"""Connections: each news story the network kept, tied to what the network measured where
the story is.

The weather service already says what it says; the network's value is joining things up.
A story names places (counties, towns, regional names like "Quad Cities"); the readings,
events and alerts in those counties over the last day are the evidence that bears on it:
"Heavy rain coming to Quad Cities" next to the 1.4 in a spotter measured in Henry County
and the Flood Watch in force there. A statewide story ("Illinois crop progress") is tied to
the statewide readings of its own topic. Stories about the same places are told together.

Nothing here knows about weather: places come from the vendored Census tables and the
site's town list (config/geo/aliases.yaml adds regional names), readings from every pack.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import pairwise
from typing import Any

import yaml

from intelnet import db, geo
from intelnet.config import CONFIG_DIR, PROJECT_ROOT
from intelnet.topics import find_metric, topic_of_metric

HOURS = 24


@dataclass
class Thread:
    """One connection: the stories, the counties they name, what was measured there."""
    counties: list[str]                                   # FIPS, as named in the stories
    label: str = ""                                       # the name the stories used: "Quad Cities"
    stories: list[dict[str, Any]] = field(default_factory=list)   # title, source, url
    readings: list[str] = field(default_factory=list)     # "rainfall 1.4 in · Henry (spotter)"
    alerts: list[str] = field(default_factory=list)       # "Flood Watch"
    weight: float = 0.0

    @property
    def place(self) -> str:
        if self.label:
            return self.label
        names = [c.name for c in (geo.county(f) for f in self.counties) if c]
        return "Illinois" if not names else names[0] if len(names) == 1 else ", ".join(names[:3])


# ── places named in a headline ───────────────────────────────────────────

@lru_cache(maxsize=1)
def _places() -> list[tuple[re.Pattern[str], tuple[str, ...]]]:
    """(pattern, counties) for every county, town and regional name, longest names first."""
    named: dict[str, set[str]] = {}
    for c in geo.counties().values():
        named.setdefault(c.name, set()).add(c.fips)
        named.setdefault(f"{c.name} County", set()).add(c.fips)
    try:
        ref = json.loads((PROJECT_ROOT / "site" / "assets" / "il-reference.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ref = {}
    for name, lat, lon, *_ in ref.get("places", []):
        loc = geo.location_from_point(float(lat), float(lon), online=False)
        if loc.county_fips and len(name) > 3:
            named.setdefault(name, set()).add(loc.county_fips)
    try:
        aliases = yaml.safe_load((CONFIG_DIR / "geo" / "aliases.yaml").read_text(encoding="utf-8")) or {}
    except OSError:
        aliases = {}
    for name in aliases.pop("not_places", None) or []:          # town names that are mostly other words
        named.pop(str(name), None)
    for alias, counties in aliases.items():
        fips = {c.fips for c in (geo.county_by_name(str(x)) for x in counties) if c}
        named.setdefault(str(alias), set()).update(fips)
    # hyphens as spaces, the way `places_in` reads the headline ("Bloomington-Normal")
    return [(re.compile(rf"(?<!\w){re.escape(n.replace('-', ' '))}(?!\w)"), tuple(sorted(f)))
            for n, f in sorted(named.items(), key=lambda kv: -len(kv[0]))]


def places_in(text: str) -> tuple[list[str], str]:
    """The counties a headline names, and the widest name it used for them (longest names
    win: "Rock Island" before "Rock")."""
    found: list[str] = []
    label, widest = "", 0
    rest = text.replace("-", " ")                     # "Chicago-area" names Chicago
    for pattern, fips in _places():
        hit = pattern.search(rest)
        if hit:
            found += [f for f in fips if f not in found]
            if len(fips) > widest:
                label, widest = hit.group(0), len(fips)
            rest = pattern.sub(" ", rest)
    return found, label


def counties_in(text: str) -> list[str]:
    return places_in(text)[0]


@lru_cache(maxsize=1)
def _points() -> dict[str, tuple[float, float]]:
    """Where each name a headline can use sits, for the site's map: a town at its own point
    (a bare "Peoria" is the city), a county at its centre, a regional name at the centre of
    its first county (the aliases list the core one first: Rock Island for the Quad Cities).
    Keyed as `places_in` reports names: hyphens read as spaces."""
    out: dict[str, tuple[float, float]] = {}
    for c in geo.counties().values():
        out[c.name] = out[f"{c.name} County"] = (c.lat, c.lon)
    try:
        ref = json.loads((PROJECT_ROOT / "site" / "assets" / "il-reference.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ref = {}
    for name, lat, lon, *_ in ref.get("places", []):
        out[name] = (float(lat), float(lon))
    try:
        aliases = yaml.safe_load((CONFIG_DIR / "geo" / "aliases.yaml").read_text(encoding="utf-8")) or {}
    except OSError:
        aliases = {}
    aliases.pop("not_places", None)
    for alias, counties in aliases.items():
        first = next((c for c in (geo.county_by_name(str(x)) for x in counties) if c), None)
        if first is not None:
            out[str(alias)] = (first.lat, first.lon)
    return {name.replace("-", " "): pt for name, pt in out.items()}


def place_point(label: str) -> tuple[float, float] | None:
    """The point a place name from `places_in` stands for, or None."""
    return _points().get(label.replace("-", " "))


# ── what was measured there ──────────────────────────────────────────────

def _named(metric: Any, text: str) -> bool:
    """Does the headline name this measure ("rain" → rainfall; "flood", "river" → gauges)?
    By the pack's `news_words`, and its aliases of more than one word ("soil moisture"). A
    one-word alias is shorthand for a report ("pressure 29.9", "stage 7"), and headlines use
    those words for other things: "Pressure mounts for lawmakers", "as conditions worsen"."""
    flat = " ".join(re.findall(r"[a-z0-9.]+", text.lower()))
    names = [a for a in metric.aliases if " " in a.strip()] + list(getattr(metric, "news_words", []) or [])
    return any(re.search(rf"(?<![a-z]){re.escape(n.lower())}(?:s|es|ing|ed)?(?![a-z])", flat) for n in names)


def _readings(counties: list[str], topic: str | None, text: str = "") -> tuple[list[tuple[float, str]], list[str]]:
    """What bears on a story: in these counties (or statewide, by the story's topic), the
    measures the headline names and anything past its pack's event threshold, strongest
    first; and the alerts in force there."""
    from intelnet.feeds.nws_alerts import active_alert_groups

    rows = db.mesh(HOURS)
    scope = set(counties)
    out: list[tuple[float, str]] = []
    best: dict[str, tuple[float, Any, Any]] = {}
    for r in rows:
        if scope and r["county_fips"] not in scope:
            continue
        m = find_metric(r["metric"])
        if m is None or not m.scored:
            continue
        t = topic_of_metric(m.key)
        if not scope and topic and t and t.name != topic:
            continue
        top = r["min_value"] if m.event_direction == "below" else r["max_value"]
        if top is None:
            continue
        named = _named(m, text)
        weight = m.severity(top) + (0.6 if named else 0) + (0.5 if r["n_human"] else 0)
        if weight <= 0:
            continue
        key = m.key
        rank = (weight, -top if m.event_direction == "below" else top)      # ties: the strongest reading
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r, m)
    for (weight, _), r, m in best.values():
        top = r["min_value"] if m.event_direction == "below" else r["max_value"]
        c = geo.county(r["county_fips"])
        risen = _rise(m, scope or {r["county_fips"]}) if _named(m, text) else None
        if risen:                                  # a gauge: how far it rose says more than its level
            out.append((weight, risen))
            continue
        what = m.label if m.is_flag else f"{m.label} {'down to ' if m.event_direction == 'below' else ''}{m.display(top)}"
        out.append((weight, f"{what} · {c.name if c else ''}" + (" (people)" if r["n_human"] else "")))
    alerts = []
    if scope:
        for g in active_alert_groups():
            ev = str(g["signal"].evidence.get("event") or "")
            covered = {c.fips for c in (geo.county_by_name(n) for n in g["counties"]) if c} & scope
            if covered and ev and ev not in alerts:
                alerts.append(ev)
    return sorted(out, key=lambda x: -x[0]), alerts


def _site(sig: Any) -> str:
    """'SALT CREEK AT 22ND STREET AT OAK BROOK, IL' → 'Salt Creek at 22nd Street at Oak Brook'."""
    name = str(sig.evidence.get("name") or "").rsplit(",", 1)[0].title()
    name = re.sub(r"\bNr\b", "near", re.sub(r"\bAbv\b", "above", re.sub(r"\bBlw\b", "below", name)))
    return re.sub(r"\b(At|Near|Above|Below|Of|The|And)\b", lambda m_: m_.group(1).lower(), name)


def _rise(metric: Any, counties: set[str]) -> str | None:
    """The biggest rise at one named site over the day ('Salt Creek at Oak Brook up 2.4 ft,
    to 49.5 ft'), when it's more than the measure's tolerance; else None."""
    by_site: dict[str, list[Any]] = {}
    for fips in counties:
        for s in db.recent_signals(HOURS, metric=metric.key, county_fips=fips, limit=2000):
            if s.value is not None and s.evidence.get("name"):
                by_site.setdefault(s.sensor_id, []).append(s)
    best = None
    for sigs in by_site.values():
        sigs.sort(key=lambda s: s.observed_at)
        low = min(s.value for s in sigs)
        rise = sigs[-1].value - low
        steps = [b.value - a.value for a, b in pairwise(sigs)]
        # a rise made of one jump is the gauge's data (a datum shift, a bad reading), not the
        # river: the West Branch DuPage "rose" 10 ft in an hour on 30 Sep while its flow
        # went from 46 to 62 cfs
        if not steps or max(steps) > 0.5 * rise:
            continue
        if best is None or rise > best[0]:
            best = (rise, sigs[-1])
    floor = float(metric.tolerance.get("abs") or 0)
    if best is None or best[0] <= floor:
        return None
    rise, last = best
    zero = metric.display(0.0).split(" ")[0]
    up = metric.display(rise + 0.0) if zero in ("0", "0.00") else None
    return f"{_site(last)} up {up}, to {metric.display(last.value)}" if up else None


def threads(limit: int = 4) -> list[Thread]:
    """The day's connections, strongest first: stories joined to the readings where they are.
    A story with nothing measured behind it isn't a connection, and is left to the reading list."""
    from intelnet import digest

    by_place: dict[tuple[str, ...], Thread] = {}
    seen: set[str] = set()
    for r in db.kept_items_since(HOURS + 6, limit=40):
        item = {"title": r["title"], "url": r["url"],
                "feed": (json.loads(r["metadata_json"] or "{}").get("feed") if r["metadata_json"] else None)}
        title, source = digest._publisher(item)
        source = source.split(" · ")[0]                      # the outlet, not the feed's category
        key = " ".join(title.lower().split())
        if key in seen:
            continue
        seen.add(key)
        counties, label = places_in(title)
        place = tuple(sorted(counties))
        th = by_place.get(place)
        if th is None:
            found, alerts = _readings(counties, r["topic"], title)
            readings = [text for _, text in found[:3]]
            if not readings:
                continue
            th = by_place[place] = Thread(list(counties), label, readings=readings, alerts=alerts[:2],
                                          weight=sum(w for w, _ in found[:3]) + (1 if counties else 0))
        if len(th.stories) < 2:
            th.stories.append({"title": title, "source": source, "url": r["url"]})
            th.weight += float(r["relevance"] or 0)
    ranked = sorted(by_place.values(), key=lambda t: (-len(t.counties), -t.weight))
    merged: list[Thread] = []
    for th in ranked:
        home = next((m for m in merged if th.counties and set(th.counties) <= set(m.counties)), None)
        if home is None:
            merged.append(th)
            continue
        home.stories += [s for s in th.stories if len(home.stories) < 3]
        home.readings = (home.readings + [r for r in th.readings if r not in home.readings])[:4]
        home.weight += th.weight
    return sorted(merged, key=lambda t: -t.weight)[:limit]
