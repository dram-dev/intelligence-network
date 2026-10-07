"""Connections: where a news story is, and which measures it's about.

The weather service already says what it says; the network's value is joining things up.
A story names places (counties, towns, regional names like "Quad Cities") and measures
("heavy rain", "flooding"): `places_in` finds the counties and the name it used,
`place_point` where that name sits, and `_named` whether a headline is about a measure.
notable.py ties each story to what the network measured there, for the site's map and
the morning brief: "Heavy rain coming to Quad Cities" next to the 1.40 in a spotter
measured in Henry County and the Flood Watch in force there.

Nothing here knows about weather: places come from the vendored Census tables and the
site's town list (config/geo/aliases.yaml adds regional names), measures from every pack.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

import yaml

from intelnet import geo
from intelnet.config import CONFIG_DIR, PROJECT_ROOT

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


def _site(sig: Any) -> str:
    """'SALT CREEK AT 22ND STREET AT OAK BROOK, IL' → 'Salt Creek at 22nd Street at Oak Brook';
    'NEAR MCHENRY' → 'near McHenry'."""
    name = str(sig.evidence.get("name") or "").rsplit(",", 1)[0].title()
    name = re.sub(r"\bMc([a-z])", lambda m_: "Mc" + m_.group(1).upper(), name)
    name = re.sub(r"\b(\d+)(St|Nd|Rd|Th)\b", lambda m_: m_.group(1) + m_.group(2).lower(), name)   # 22Nd → 22nd
    name = re.sub(r"\bNr\b", "near", re.sub(r"\bAbv\b", "above", re.sub(r"\bBlw\b", "below", name)))
    return re.sub(r"\b(At|Near|Above|Below|Of|The|And)\b", lambda m_: m_.group(1).lower(), name)
