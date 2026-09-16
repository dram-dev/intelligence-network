"""The common data language — parse what a sensor types into Signals.

One clause = one reading:

    <metric> <value>[unit] [@<location>] [at <time> | <N>m ago] [#tag] [-- note]

    rain 1.25in                      hail quarter @62704-1234
    gust 62 mph @cook                temp 91F at 3:15pm
    tornado @sangamon -- on the ground west of town
    1.5 inches of rain; gusts 45     (several clauses: ';' or newlines)

Rules of thumb: a bare number takes the metric's `default_unit` (US
contributors type inches / mph / °F); named hail sizes ("golf ball") map to
the NWS chart; a flag metric ("flooding", "trees down") is a report by itself;
`@` introduces a location — ZIP, ZIP+4, county or lat,lon (a place name too,
when online lookup is on); with no `@` the sensor's home location applies.

Automated sensors post JSON instead (`parse_json`): a list of
{metric, value, unit?, location?, observed_at?, note?, confidence?}.

Anything the grammar can't read is returned as `leftover` so the caller can
hand it to the LLM parser (`intelnet.llm.parse_free_text`) — the grammar is
the fast, deterministic path; the model is the fallback for prose.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from intelnet import geo
from intelnet.config import settings
from intelnet.topics import Metric, Topic, default_topic, find_metric  # noqa: F401

_NUM = r"[-+]?\d+(?:\.\d+)?"
_TIME_CLOCK = re.compile(
    r"\b(?:at|@t)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b(?!\s*(?:mph|in|mm|%))",
    re.IGNORECASE,
)
_TIME_AGO = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\s+ago\b",
    re.IGNORECASE,
)
_TIME_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)\b")
_TAG = re.compile(r"(?<!\w)#([A-Za-z0-9_]+)")
_NOTE_SPLIT = re.compile(r"\s(?:--|—)\s")
_CLAUSE_SPLIT = re.compile(r"[;\n]+")
_LOC = re.compile(r"@\s*([^@#]+?)(?=\s+@|\s+#|\s+(?:at|since)\s+\d|\s+\d[\d.]*\s*(?:m|min|h|hr)s?\s+ago|$)",
                  re.IGNORECASE)


@dataclass
class ParsedSignal:
    metric: Metric
    value: float                        # canonical unit
    typed: str                          # what they typed ("1.25in", "quarter")
    location: geo.Location | None = None
    observed_at: datetime | None = None
    note: str | None = None
    tags: list[str] = field(default_factory=list)
    confidence: float = 1.0

    def echo(self) -> str:
        """One line showing how the network understood the reading."""
        return f"{self.metric.label}: {self.metric.display(self.value)}"


@dataclass
class ParseResult:
    signals: list[ParsedSignal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    leftover: str = ""                  # text no clause could read

    @property
    def ok(self) -> bool:
        return bool(self.signals) and not self.errors


# ── regex construction from the topic pack ─────────────────────────────────

def _alt(words: list[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


ALL_TOPICS = "*"


def _metrics_in_scope(scope: str) -> list[Metric]:
    from intelnet.topics import get_topic, topics

    if scope == ALL_TOPICS:
        pool = [m for t in topics().values() for m in t.metrics.values()]
    else:
        pool = list(get_topic(scope).metrics.values())
    # Longest aliases first so 'soil temp' wins over 'temp' and 'wind gust' over 'wind'.
    return sorted(pool, key=lambda x: -max(len(a) for a in (x.key, *x.aliases)))


@lru_cache(maxsize=8)
def _patterns(scope: str) -> list[tuple[Metric, re.Pattern, re.Pattern | None, re.Pattern | None]]:
    """Per metric: (metric, alias-then-value, value-then-alias, words) patterns.

    `scope` is a topic name, or ALL_TOPICS to read every pack at once — the
    normal case for a contribution, since a sensor may report soil and weather
    in one message. Cross-pack alias collisions are a pack-authoring error
    (tests enforce uniqueness).
    """
    out = []
    for m in _metrics_in_scope(scope):
        aliases = _alt([m.key, *m.aliases])
        units = _alt(list(m.units)) if m.units else None
        unit_part = rf"\s*(?P<unit>{units})?(?![A-Za-z])" if units else r"(?P<unit>)"
        if m.is_flag:
            pat_a = re.compile(rf"(?<![A-Za-z])(?P<alias>{aliases})(?![A-Za-z])", re.IGNORECASE)
            out.append((m, pat_a, None, None))
            continue
        # "rain 1.25in", "rain: 1.25 inches", "temp=91F"
        pat_a = re.compile(
            rf"(?<![A-Za-z])(?P<alias>{aliases})(?![A-Za-z])\s*(?:[:=]|of)?\s*(?P<val>{_NUM}){unit_part}",
            re.IGNORECASE,
        )
        # "1.25in of rain", "1.25 inches rain", "62mph gusts"
        pat_b = re.compile(
            rf"(?P<val>{_NUM}){unit_part}\s*(?:of\s+)?(?P<alias>{aliases})(?![A-Za-z])",
            re.IGNORECASE,
        )
        pat_w = None
        if m.words:
            words = _alt(list(m.words))
            # "hail quarter", "quarter-size hail", "hail the size of golf balls"
            pat_w = re.compile(
                rf"(?:(?P<alias1>{aliases})(?![A-Za-z]).{{0,24}}?(?P<word1>{words})s?(?![A-Za-z])"
                rf"|(?P<word2>{words})s?(?:[- ]?sized?)?(?![A-Za-z]).{{0,12}}?(?P<alias2>{aliases})(?![A-Za-z]))",
                re.IGNORECASE,
            )
        out.append((m, pat_a, pat_b, pat_w))
    return out


# ── time ───────────────────────────────────────────────────────────────────

def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.local_tz)


def parse_time(text: str, now: datetime | None = None) -> tuple[datetime | None, str]:
    """Pull an explicit time out of a clause. Returns (utc datetime|None, rest)."""
    now = now or datetime.now(timezone.utc)
    m = _TIME_ISO.search(text)
    if m:
        raw = m.group(1).replace(" ", "T")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_local_tz())
            return dt.astimezone(timezone.utc), (text[: m.start()] + text[m.end():]).strip()
        except ValueError:
            pass
    m = _TIME_AGO.search(text)
    if m:
        n = float(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
        return now - delta, (text[: m.start()] + text[m.end():]).strip()
    m = _TIME_CLOCK.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").replace(".", "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            local_now = now.astimezone(_local_tz())
            dt = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt > local_now + timedelta(minutes=5):   # "at 11pm" said at 9am = yesterday
                dt -= timedelta(days=1)
            return dt.astimezone(timezone.utc), (text[: m.start()] + text[m.end():]).strip()
    return None, text


# ── clause parsing ─────────────────────────────────────────────────────────

def _extract_location(text: str, *, online: bool | None) -> tuple[geo.Location | None, str, str | None]:
    """'… @cook county …' → (Location|None, rest, error|None)."""
    m = _LOC.search(text)
    if not m:
        return None, text, None
    raw = m.group(1).strip()
    tokens = raw.split()
    # Try the longest run of tokens first: '@st clair county', '@jo daviess'.
    for n in range(min(4, len(tokens)), 0, -1):
        cand = " ".join(tokens[:n])
        loc = geo.parse_location(cand, online=online)
        if loc:
            rest = text[: m.start()] + " " + " ".join(tokens[n:]) + " " + text[m.end():]
            return loc, re.sub(r"\s+", " ", rest).strip(), None
    return None, (text[: m.start()] + text[m.end():]).strip(), f"unknown location “{raw}”"


def parse_clause(
    clause: str,
    topic: Topic | None = None,
    *,
    now: datetime | None = None,
    online: bool | None = None,
) -> tuple[list[ParsedSignal], list[str], str]:
    """Parse one clause → (signals, errors, leftover text). No topic = every pack."""
    scope = topic.name if topic else ALL_TOPICS
    text = clause.strip()
    if not text:
        return [], [], ""
    note = None
    parts = _NOTE_SPLIT.split(text, maxsplit=1)
    if len(parts) == 2:
        text, note = parts[0].strip(), parts[1].strip() or None
    tags = [t.lower() for t in _TAG.findall(text)]
    text = _TAG.sub(" ", text)
    observed_at, text = parse_time(text, now=now)
    location, text, loc_err = _extract_location(text, online=online)
    errors = [loc_err] if loc_err else []

    signals: list[ParsedSignal] = []
    consumed: list[tuple[int, int]] = []

    def _free(span: tuple[int, int]) -> bool:
        return all(span[1] <= s or span[0] >= e for s, e in consumed)

    def _reach(entry: tuple[Metric, Any, Any, Any]) -> int:
        """How much of *this* text the metric's longest matching alias covers.

        Packs share words — weather claims a bare "damage", agriculture "crop
        damage", quake "quake damage" — so the metric that matches the most
        words here goes first, whatever alias lengths the packs happen to have.
        """
        metric = entry[0]
        return max((len(a) for a in (metric.key, *metric.aliases)
                    if re.search(rf"(?<![A-Za-z]){re.escape(a)}(?![A-Za-z])", text, re.IGNORECASE)),
                   default=0)

    for metric, pat_a, pat_b, pat_w in sorted(_patterns(scope), key=lambda e: -_reach(e)):
        if pat_w:
            for m in pat_w.finditer(text):
                if not _free(m.span()):
                    continue
                word = (m.group("word1") or m.group("word2")).lower()
                signals.append(ParsedSignal(metric, metric.words[word], word))
                consumed.append(m.span())
        for pat in (pat_a, pat_b):
            if pat is None:
                continue
            for m in pat.finditer(text):
                if not _free(m.span()):
                    continue
                if metric.is_flag:
                    signals.append(ParsedSignal(metric, 1.0, m.group("alias")))
                    consumed.append(m.span())
                    continue
                val = float(m.group("val"))
                unit = (m.group("unit") or "").lower() or None
                try:
                    canon = metric.convert(val, unit)
                except ValueError as exc:
                    errors.append(str(exc))
                    consumed.append(m.span())
                    continue
                if not metric.in_range(canon):
                    errors.append(
                        f"{metric.label} {val:g}{unit or metric.default_unit} is outside the "
                        f"plausible range — not recorded"
                    )
                    consumed.append(m.span())
                    continue
                signals.append(ParsedSignal(metric, canon, f"{val:g}{unit or metric.default_unit}"))
                consumed.append(m.span())

    # A bare alias with no value ("rain" alone) is a question, not a reading.
    for metric, pat_a, _pb, _pw in sorted(_patterns(scope), key=lambda e: -_reach(e)):
        if metric.is_flag or any(s.metric is metric for s in signals):
            continue
        bare = re.compile(rf"(?<![A-Za-z])(?:{_alt([metric.key, *metric.aliases])})(?![A-Za-z])",
                          re.IGNORECASE)
        for m in bare.finditer(text):
            if _free(m.span()):
                errors.append(f"{metric.label}: give a value, e.g. “{metric.aliases[0]} 1.2"
                              f"{metric.default_unit}”")
                consumed.append(m.span())
                break

    leftover = text
    for s, e in sorted(consumed, reverse=True):
        leftover = leftover[:s] + " " + leftover[e:]
    leftover = re.sub(r"\s+", " ", leftover).strip(" ,.")

    for s in signals:
        s.location = location
        s.observed_at = observed_at
        s.note = note
        s.tags = tags
    return signals, errors, leftover


def parse(text: str, topic: Topic | None = None, *, now: datetime | None = None,
          online: bool | None = None) -> ParseResult:
    """Parse a whole message (clauses split on ';' / newlines)."""
    result = ParseResult()
    leftovers: list[str] = []
    for clause in _CLAUSE_SPLIT.split(text or ""):
        if not clause.strip():
            continue
        sigs, errs, left = parse_clause(clause, topic, now=now, online=online)
        result.signals.extend(sigs)
        result.errors.extend(errs)
        # Leftover only matters when the clause produced nothing — a note after
        # a good reading isn't prose to re-parse.
        if not sigs and left:
            leftovers.append(left)
    result.leftover = "; ".join(leftovers)
    return result


# ── JSON form for automated sensors ────────────────────────────────────────

def parse_json(payload: str | dict | list, topic: Topic | None = None, *,
               online: bool | None = None) -> ParseResult:
    """`/signal {...}` or a list of them → ParseResult (strict: no guessing)."""
    result = ParseResult()
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as exc:
        result.errors.append(f"invalid JSON: {exc.msg}")
        return result
    items = data if isinstance(data, list) else [data]
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            result.errors.append(f"item {i}: expected an object")
            continue
        metric = find_metric(str(item.get("metric", "")), topic)
        if metric is None:
            result.errors.append(f"item {i}: unknown metric {item.get('metric')!r}")
            continue
        try:
            value = 1.0 if metric.is_flag else metric.convert(float(item.get("value")), item.get("unit"))
        except (TypeError, ValueError) as exc:
            result.errors.append(f"item {i}: {exc}")
            continue
        if not metric.in_range(value):
            result.errors.append(f"item {i}: {metric.label} {value:g} outside plausible range")
            continue
        loc = None
        if item.get("location"):
            loc_raw = item["location"]
            if isinstance(loc_raw, dict) and "lat" in loc_raw:
                loc = geo.location_from_point(float(loc_raw["lat"]), float(loc_raw["lon"]), online=online)
            else:
                loc = geo.parse_location(str(loc_raw), online=online)
                if loc is None:
                    result.errors.append(f"item {i}: unknown location {loc_raw!r}")
                    continue
        observed = None
        if item.get("observed_at"):
            try:
                observed = datetime.fromisoformat(str(item["observed_at"]).replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
            except ValueError:
                result.errors.append(f"item {i}: bad observed_at")
                continue
        conf = item.get("confidence", 1.0)
        try:
            conf = min(1.0, max(0.0, float(conf)))
        except (TypeError, ValueError):
            conf = 1.0
        result.signals.append(ParsedSignal(
            metric, value, f"{item.get('value')}{item.get('unit') or ''}", location=loc,
            observed_at=observed, note=item.get("note"), confidence=conf,
            tags=[str(t) for t in item.get("tags") or []],
        ))
    return result


def cheatsheet(topic: Topic | None = None) -> str:
    """Compact grammar reference for /help — generated from the packs.

    One topic → its first metrics in full; no topic → a line per pack.
    """
    from intelnet.topics import topics

    if topic is not None:
        numeric = [m for m in topic.metrics.values() if not m.is_flag]
        flags = [m for m in topic.metrics.values() if m.is_flag]
        lines = [f"{m.aliases[0]} <value>[{m.default_unit or m.unit}]" for m in numeric[:6]]
        if numeric[6:]:
            lines.append("…plus: " + ", ".join(m.aliases[0] for m in numeric[6:]))
        if flags:
            lines.append("flags: " + ", ".join(m.aliases[0] for m in flags))
    else:
        lines = []
        for t in topics().values():
            numeric = [m for m in t.metrics.values() if not m.is_flag]
            flags = [m for m in t.metrics.values() if m.is_flag]
            ex = [f"{m.aliases[0]} {_example_value(m)}" for m in numeric[:3]]
            ex += [m.aliases[0] for m in flags[:2]]
            lines.append(f"{t.label.lower()}: " + " · ".join(ex))
    lines.append("location: @62704 · @62704-1234 · @cook · @39.8,-89.6")
    lines.append("time: at 3:15pm · 20 min ago    note: -- text")
    return "\n".join(lines)


def _example_value(m: Metric) -> str:
    if m.words:
        return next(iter(m.words))
    lo, hi = m.range or (0.0, 10.0)
    mid = (lo + hi) / 2
    return f"{mid:g}{m.default_unit or ''}"


def describe_metrics(topic: Topic | None = None) -> str:
    """One line per metric — the vocabulary the LLM parser and /topics show."""
    from intelnet.topics import topics

    packs = [topic] if topic else list(topics().values())
    out = []
    for t in packs:
        for m in t.metrics.values():
            units = "flag" if m.is_flag else f"{m.unit}; typed: {', '.join(list(m.units)[:5])}"
            words = f"; words: {', '.join(list(m.words)[:6])}" if m.words else ""
            out.append(f"{m.key} — {m.label} [{t.name}] ({units}); aliases: {', '.join(m.aliases)}{words}")
    return "\n".join(out)


def as_json_example(topic: Topic | None = None) -> str:
    topic = topic or default_topic()
    m = next(iter(topic.metrics.values()))
    example: dict[str, Any] = {
        "metric": m.key, "value": 1.0, "unit": m.default_unit or m.unit,
        "location": "62704-1234", "observed_at": "2026-09-14T20:05:00Z",
        "note": "optional", "confidence": 0.9,
    }
    return json.dumps(example)
