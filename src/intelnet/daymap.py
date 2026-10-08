"""The day across the state in one picture: the morning brief's map, and the digest's.

The site's masthead map (notable.py), drawn where nothing can be hovered: the day's notable
readings as dots in their topic's colour, the stand-outs labelled; the stories as numbered
squares, under the same numbers the brief and the digest list them by, each drawn to the
readings it's about; the NWS alerts in effect, in their alert cards' colours; the reader's
county outlined. Stories about the whole state have no place on it, so they wait in the key
beside it. Two palettes: `DARK`, the alert cards' dark map with the state lifted off its
ground and the site's dark topic colours, reads the same in a light chat or a dark one;
`LIGHT`, the site's light colours on a white ground, sits on the digest's page and prints.

A picture is named by what it shows (`dm-<hash>`) and kept with the alert cards' pictures,
so delivery attaches it like theirs, and a brief that draws the same picture as an earlier
one reuses Telegram's copy (cardmap.media_for).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from io import BytesIO
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from intelnet import cardmap, geo, notable
from intelnet.cardmap import (
    HALO,
    TEXT,
    TEXT_DIM,
    Dense,
    _font,
    _hex,
    _merc,
)
from intelnet.config import settings
from intelnet.models import local_time, parse_iso, utcnow

logger = logging.getLogger(__name__)

W, H = 1080, 1350                 # the layout, in points: 4:5, the tallest a chat shows without scrolling past it
K = 2048 / W                      # pixels a point: 2048 × 2560, the largest picture Telegram keeps whole
SS = 2                            # vector layers drawn twice as large again, then scaled down
PAD = 30
RAIL = 300                        # the key, right of the state
BORDER_KM = 4                     # rivers and lakes drawn this far past the state: border rivers whole
KEEP = timedelta(days=3)
RENDER_VERSION = 5                # part of every name: bump it when the drawing changes (5: the state lifted)


@dataclass(frozen=True)
class Palette:
    ground: tuple[int, int, int]        # around the state
    land: tuple[int, int, int]
    water: tuple[int, int, int]
    river: tuple[int, int, int]
    county: tuple[int, int, int]
    text: tuple[int, int, int]
    dim: tuple[int, int, int]
    halo: tuple[int, int, int]          # behind text and marks
    badge: tuple[int, int, int]         # a story's square, and its number
    badge_text: tuple[int, int, int]
    topics: dict[str, str]              # the site's topic colours for this ground
    alert_alpha: tuple[int, int]        # a watch or advisory's tint, a warning's


# The site's topic colours (site/index.fragment.html tokens), dark and light. The dark ground
# and land are lighter than the cards' (whose radar wants them dark): on the cards' 1.15:1 the
# state sank into its surroundings on a phone; this is 1.5:1, with water, rivers and county
# lines lifted to keep their contrast on it.
DARK = Palette((10, 12, 14), (44, 52, 58), (40, 74, 94), (56, 108, 136), (76, 90, 86),
               TEXT, TEXT_DIM, HALO, TEXT, (16, 20, 23),
               {"weather": "#5FB4C0", "soil": "#C08A5A", "water": "#7FA6E8", "agriculture": "#8BBF63",
                "air": "#B1A2E3", "quake": "#E5786A", "nature": "#D9C45A", "markets": "#D28CC8"}, (46, 86))
LIGHT = Palette((255, 255, 255), (241, 243, 239), (213, 226, 234), (123, 160, 191), (206, 213, 208),
                (26, 31, 28), (91, 101, 95), (255, 255, 255), (26, 31, 28), (255, 255, 255),
                {"weather": "#1D6E7A", "soil": "#7A4E24", "water": "#2B5FAD", "agriculture": "#4F7F2F",
                 "air": "#6E5E9A", "quake": "#B33A2B", "nature": "#8A7A1F", "markets": "#8E4585"}, (52, 92))
# Towns for bearings, largest first; each is written where nothing more important is
CITIES = ("Chicago", "Rockford", "Springfield", "Peoria", "Champaign", "Moline", "Carbondale", "Quincy",
          "Bloomington", "Decatur", "Kankakee", "Effingham", "Mount Vernon")


def _colour(topic: str, pal: Palette = DARK) -> tuple[int, int, int]:
    return _hex(pal.topics.get(topic, "#8C9891"))[:3]


def _topic_name(topic: str | None) -> str:
    """A topic as the key names it, short as the site's layer chips do ("Markets")."""
    short = {"markets": "Markets", "nature": "Nature", "quake": "Quakes", "agriculture": "Crops"}
    return short.get(topic or "") or notable.topic_label(topic)


# ── where things are ──────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _fit() -> tuple[float, float, float, float, float, float]:
    """(scale, left, top, the state's right edge, its west and north in Mercator metres):
    the state as large as the picture's height allows, left of the key."""
    xs, ys = [], []
    for rings in cardmap._counties().values():
        for ring in rings:
            for lon, lat in ring:
                x, y = _merc(lat, lon)
                xs.append(x)
                ys.append(y)
    if not xs:                                     # no shapes: a frame around the state's middle
        xs, ys = [_merc(40, -91.5)[0], _merc(40, -87.5)[0]], [_merc(37, -89)[1], _merc(42.5, -89)[1]]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    k = min((W - RAIL - 2 * PAD) / w, (H - 2 * PAD) / h)
    left = PAD + (W - RAIL - 2 * PAD - w * k) / 2
    top = PAD + (H - 2 * PAD - h * k) / 2
    return k, left, top, left + w * k, min(xs), max(ys)


def px(lat: float, lon: float, scale: int = 1) -> tuple[float, float]:
    k, left, top, _, x0, y1 = _fit()
    x, y = _merc(lat, lon)
    return (left + (x - x0) * k) * scale, (top + (y1 - y) * k) * scale


def _ring(ring: list[list[float]], scale: int = SS) -> list[tuple[float, float]]:
    return [px(lat, lon, scale) for lon, lat in ring]


# ── the picture ───────────────────────────────────────────────────────────

def _alerts() -> list[dict[str, Any]]:
    """The alerts in effect: their counties or polygon, in their card's colour, worst last."""
    from intelnet.feeds.nws_alerts import SEVERITY_RANK, active_alert_groups

    out = []
    for g in active_alert_groups():
        s = g["signal"]
        ev = s.evidence
        event, severity = str(ev.get("event") or ""), str(ev.get("severity") or "Unknown")
        fips = sorted({c.fips for c in (geo.county_by_name(n) for n in g["counties"]) if c})
        out.append({"event": event, "rank": SEVERITY_RANK.get(severity, 0), "warning": event.endswith("Warning"),
                    "colour": cardmap._colour(s.topic, event, severity),
                    "polygon": ev.get("polygon"), "counties": fips})
    return sorted(out, key=lambda a: (a["rank"], a["event"]))


class _Labels(cardmap._Labels):
    """Labels kept on the state's side of the picture, clear of the key."""

    def free(self, box: tuple[float, float, float, float]) -> bool:
        x0, y0, x1, y1 = box
        if x0 < 8 or y0 < 8 or x1 > _fit()[3] + 40 or x1 > W - RAIL - 6 or y1 > H - 8:
            return False
        return not any(x0 < b[2] and b[0] < x1 and y0 < b[3] and b[1] < y1 for b in self.taken)


def _badge(d: Dense, x: float, y: float, n: int, size: float,
           pal: Palette = DARK) -> tuple[float, float, float, float]:
    """A story's numbered square, centred on (x, y), at the final scale."""
    w = size * (1.25 if n > 9 else 1)
    box = (x - w / 2, y - size / 2, x + w / 2, y + size / 2)
    d.rounded_rectangle(box, radius=size * .22, fill=pal.badge, outline=pal.halo, width=3)
    d.text((x, y + 1), str(n), font=_font(round(size * .62), "SemiBold"), fill=pal.badge_text, anchor="mm")
    return box


def _arc(a: tuple[float, float], b: tuple[float, float], bend: float = .18) -> list[tuple[float, float]]:
    """A gentle curve from a story to a reading, as points."""
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy) or 1
    lift = min(80 * SS, length * bend)
    cx, cy = (ax + bx) / 2 - dy / length * lift, (ay + by) / 2 + dx / length * lift
    return [((1 - t) ** 2 * ax + 2 * (1 - t) * t * cx + t ** 2 * bx, (1 - t) ** 2 * ay + 2 * (1 - t) * t * cy + t ** 2 * by)
            for t in (i / 24 for i in range(25))]


NUDGES = [(0, 0), (30, -22), (-30, -22), (30, 22), (-30, 22), (0, -48), (0, 48), (-60, 0), (60, 0),
          (-60, -44), (-60, 44), (60, -44), (60, 44)]


def _badge_spots(day: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Where each located story's square goes: its place, or the nearest spot around it clear of
    the squares before it, never over the key (a Chicago-area square once sat on "Water")."""
    spots: dict[str, tuple[float, float]] = {}
    right = W - RAIL - 6 - 28                          # half a two-digit square, and a margin
    for n in day.get("news") or []:
        if n.get("statewide") or n.get("lat") is None:
            continue
        x0, y0 = px(n["lat"], n["lon"])
        tries = [(min(x0 + dx, right), min(max(y0 + dy, 30), H - 30)) for dx, dy in NUDGES]
        spots[n["id"]] = next((xy for xy in tries
                               if all(math.hypot(xy[0] - a, xy[1] - b) >= 52 for a, b in spots.values())), tries[0])
    return spots


def render_image(day: dict[str, Any], fips: str | None = None, alerts: list[dict[str, Any]] | None = None,
                 *, now: datetime | None = None, pal: Palette = DARK) -> Image.Image:
    now = now or utcnow()
    alerts = _alerts() if alerts is None else alerts
    shapes = cardmap._counties()
    ref = cardmap._reference()
    places = {p["id"]: p for p in day.get("places") or []}

    # the ground: land, water, alerts, county lines, the reader's county
    size = (round(W * K), round(H * K))
    base = Image.new("RGB", (round(W * K * SS), round(H * K * SS)), pal.ground)
    d = Dense(ImageDraw.Draw(base, "RGBA"), K)
    for rings in shapes.values():
        for ring in rings:
            d.polygon(_ring(ring), fill=pal.land)
    _waters(base, shapes, ref, pal)
    for a in alerts:                                   # a warning stronger than a watch or advisory
        for f in a["counties"] if not a["polygon"] else []:
            for ring in shapes.get(f, []):
                d.polygon(_ring(ring), fill=(*a["colour"], pal.alert_alpha[1 if a.get("warning") else 0]))
        for ring in a["polygon"] or []:
            d.polygon(_ring(ring), fill=(*a["colour"], 56))
            d.line(_ring(ring) + _ring(ring[:1]), fill=(*a["colour"], 255), width=6, joint="curve")
    for rings in shapes.values():
        for ring in rings:
            d.line(_ring(ring) + _ring(ring[:1]), fill=pal.county, width=2, joint="curve")
    mine = shapes.get(fips or "", [])
    for ring in mine:
        d.polygon(_ring(ring), fill=(*pal.text, 22))
        d.line(_ring(ring) + _ring(ring[:1]), fill=(*pal.text, 255), width=7, joint="curve")
    img = base.resize(size, Image.Resampling.LANCZOS).convert("RGBA")

    # the marks: arcs from each story to its readings, then the readings, the stories on top
    over = Image.new("RGBA", base.size, (0, 0, 0, 0))
    o = Dense(ImageDraw.Draw(over), K)
    spots = _badge_spots(day)
    for n in day.get("news") or []:
        at = spots.get(n["id"])
        for pid in n.get("links") or [] if at else []:
            p = places.get(pid)
            if p is None:
                continue
            pts = _arc((at[0] * SS, at[1] * SS), px(p["lat"], p["lon"], SS))
            o.line(pts, fill=(*pal.halo, 150), width=9, joint="curve")
            o.line(pts, fill=(*pal.text, 205), width=4, joint="curve")
    for p in sorted(places.values(), key=lambda p: -p["tier"]):         # the small dots first
        x, y = px(p["lat"], p["lon"], SS)
        c = _colour(p["topic"], pal)
        r = (10 if p["tier"] == 1 else 6) * SS
        if p.get("person"):
            o.ellipse((x - r - 11, y - r - 11, x + r + 11, y + r + 11), outline=(*c, 255), width=5)
        o.ellipse((x - r, y - r, x + r, y + r), fill=(*c, 255), outline=(*pal.halo, 255), width=6 if p["tier"] == 1 else 4)
    img.alpha_composite(over.resize(size, Image.Resampling.LANCZOS))

    d = Dense(ImageDraw.Draw(img, "RGBA"), K)
    labels = _Labels(d)
    labels.taken.append((W - RAIL - 6, 0, W, H))
    for p in places.values():
        if p["tier"] == 1:
            x, y = px(p["lat"], p["lon"])
            labels.taken.append((x - 12, y - 12, x + 12, y + 12))
    for n in day.get("news") or []:
        if n["id"] in spots:
            labels.taken.append(_badge(d, *spots[n["id"]], n["n"], 44, pal))
    big = _font(29, "SemiBold")
    for p in (q for q in places.values() if q["tier"] == 1):           # in the order notable ranked them
        _label(d, labels, *px(p["lat"], p["lon"]), notable.tight(p["label"]), big, pal)
    towns = {name: (lat, lon) for name, lat, lon, *_ in ref.get("places", [])}
    for name in CITIES:
        if name in towns:
            x, y = px(*towns[name])
            labels.put((x, y), name, _font(24, "Medium"), fill=pal.dim, halo=pal.halo, anchor="mm", stroke=4)

    _rail(d, day, fips, alerts, now, pal)
    return img.convert("RGB")


def _waters(base: Image.Image, shapes: dict[str, Any], ref: dict[str, Any], pal: Palette) -> None:
    """The rivers and lakes, as far as the state reaches and a little past it (the Mississippi
    and the Wabash whole): drawn further, Indiana's rivers wandered under the key."""
    wet = Image.new("RGBA", base.size, (0, 0, 0, 0))
    w = Dense(ImageDraw.Draw(wet), K)
    for wt in ref.get("water", []):
        for ring in wt.get("rings", []):
            w.polygon(_ring(ring), fill=pal.water)
    for rv in ref.get("rivers", []):
        for line in rv.get("lines", []):
            if len(line) > 1:
                w.line(_ring(line), fill=pal.river, width=3, joint="curve")
    reach = Image.new("L", base.size, 0)
    r = Dense(ImageDraw.Draw(reach), K)
    per_km = _fit()[0] * 1000 / math.cos(math.radians(40)) * SS          # points a km, mid-state, drawn twice as large
    for rings in shapes.values():
        for ring in rings:
            r.polygon(_ring(ring), fill=255)
            r.line(_ring(ring) + _ring(ring[:1]), fill=255, width=2 * BORDER_KM * per_km)
    base.paste(wet, (0, 0), ImageChops.multiply(wet.getchannel("A"), reach))


def _label(d: Dense, labels: _Labels, x: float, y: float, text: str, font: Any,
           pal: Palette = DARK) -> None:
    """A stand-out's label where it fits around its dot. A rise ("▲ 1.86 ft") gets its
    triangle drawn: the font has no glyph for it."""
    rising = text.startswith("▲")
    shown = "    " + text.lstrip("▲ ") if rising else text
    for xy, anchor in cardmap._spots(x, y, 17, 13):
        if labels.put(xy, shown, font, fill=pal.text, halo=pal.halo, anchor=anchor, stroke=5):
            if rising:                      # as tall as the digits beside it, standing on their baseline
                x0 = d.textbbox(xy, shown, font=font, anchor=anchor)[0]
                _, top, _, base = d.textbbox(xy, "0", font=font, anchor=anchor)
                w = (base - top) * 1.1
                tri = [(x0 + 2, base), (x0 + 2 + w, base), (x0 + 2 + w / 2, top)]
                d.polygon(tri, fill=pal.halo, outline=pal.halo, width=7)        # its halo, like the text's
                d.polygon(tri, fill=pal.text)
            return


def _rail(d: Dense, day: dict[str, Any], fips: str | None, alerts: list[dict[str, Any]],
          now: datetime, pal: Palette = DARK) -> None:
    """The key beside the state: the date, what each mark is, the stories without a place."""
    x = W - RAIL + 10
    d.text((x, 44), "ILLINOIS", font=_font(24, "Medium"), fill=pal.dim, anchor="ls")
    d.text((x, 92), local_time(now, "%a %-d %b"), font=_font(42, "SemiBold"), fill=pal.text, anchor="ls")
    d.line([(x, 122), (W - PAD, 122)], fill=pal.county, width=2)
    y = 168
    row = _font(27, "Medium")
    counts: dict[str, int] = {}
    for p in day.get("places") or []:
        counts[p["topic"]] = counts.get(p["topic"], 0) + 1
    for a in day.get("areas") or []:
        counts[a["topic"]] = counts.get(a["topic"], 0) + len(a["counties"])
    for topic in sorted(counts, key=lambda t: -counts[t]):
        d.ellipse((x, y - 11, x + 22, y + 11), fill=_colour(topic, pal), outline=pal.halo, width=2)
        d.text((x + 38, y), _topic_name(topic), font=row, fill=pal.text, anchor="lm")
        y += 46
    if day.get("news"):
        _badge(d, x + 11, y, 1, 26, pal)
        d.text((x + 38, y), "Story below", font=row, fill=pal.text, anchor="lm")
        y += 46
    if alerts:
        worst = alerts[-1]
        d.rounded_rectangle((x, y - 11, x + 22, y + 11), radius=4, fill=(*worst["colour"], 150), outline=worst["colour"])
        d.text((x + 38, y), "NWS alert", font=row, fill=pal.text, anchor="lm")
        y += 46
    if fips:
        d.rounded_rectangle((x, y - 11, x + 22, y + 11), radius=3, outline=pal.text, width=3)
        d.text((x + 38, y), "Your county", font=row, fill=pal.text, anchor="lm")
        y += 46
    wide = [n for n in day.get("news") or [] if n.get("statewide")]
    if wide:
        y += 22
        d.line([(x, y - 30), (W - PAD, y - 30)], fill=pal.county, width=2)
        d.text((x, y + 8), "STATEWIDE", font=_font(24, "Medium"), fill=pal.dim, anchor="ls")
        y += 50
        for n in wide:
            _badge(d, x + 18, y, n["n"], 36, pal)
            d.text((x + 48, y), _topic_name(n["topic"]), font=row, fill=pal.text, anchor="lm")
            y += 52
    as_of = parse_iso(day.get("as_of")) or now
    small = _font(23, "Medium")
    foot = ["Readings and news from", f"the 24 hours to {local_time(as_of, '%-I:%M %p')}"]
    if abs((now - as_of).total_seconds()) > 900:                  # an older day: the alerts are now's
        foot.append(f"Alerts as of {local_time(now, '%-I:%M %p')}")
    for i, line in enumerate(reversed(foot)):
        d.text((x, H - 40 - i * 32), line, font=small, fill=pal.dim, anchor="ls")


def render(day: dict[str, Any], fips: str | None = None, alerts: list[dict[str, Any]] | None = None,
           *, now: datetime | None = None, pal: Palette = DARK) -> bytes:
    return encode(render_image(day, fips, alerts, now=now, pal=pal))


def encode(img: Image.Image, fmt: str = "JPEG") -> bytes:
    """A drawn map as a file: JPEG to embed or send, PNG where the text must stay crisp
    when zoomed (the digest's full-size copy)."""
    out = BytesIO()
    if fmt == "PNG":
        img.save(out, "PNG", optimize=True)
    else:
        img.save(out, "JPEG", quality=90, subsampling=0, optimize=True)
    return out.getvalue()


def name(day: dict[str, Any], fips: str | None, alerts: list[dict[str, Any]], now: datetime) -> str:
    """What the picture shows, as a name: the same day, county and alerts draw the same picture."""
    spec = [RENDER_VERSION, fips, local_time(now, "%Y-%m-%d %H"), day.get("as_of"),
            [(p["id"], p["tier"], p["label"], p["lat"], p["lon"], p["topic"]) for p in day.get("places") or []],
            [(n["n"], n.get("lat"), n.get("lon"), n.get("links"), n.get("topic")) for n in day.get("news") or []],
            [(a["counties"], a["topic"]) for a in day.get("areas") or []],
            [(a["event"], a["counties"], bool(a["polygon"])) for a in alerts]]
    return "dm-" + hashlib.sha1(json.dumps(spec, default=str).encode()).hexdigest()[:20]


def prepare(day: dict[str, Any], fips: str | None = None, *, now: datetime | None = None) -> str | None:
    """Draw (or find) the picture for this county's brief; its name, or None when pictures
    are off or drawing failed (the brief goes without it)."""
    if not settings.card_maps:
        return None
    now = now or utcnow()
    try:
        alerts = _alerts()
        pic = name(day, fips, alerts, now)
        path = cardmap._dir() / f"{pic}.jpg"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(render(day, fips, alerts, now=now))
    except Exception:              # a brief goes out without its map rather than not at all
        logger.exception("daymap: drawing the brief's map failed")
        return None
    _prune()
    return pic


def _prune() -> None:
    cutoff = time.time() - KEEP.total_seconds()
    for p in cardmap._dir().glob("dm-*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
