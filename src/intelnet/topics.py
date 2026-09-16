"""Topic packs — the common data language, loaded from config/topics/*.yaml.

A topic pack declares the metrics a sensor can report (aliases, units, sanity
ranges, agreement tolerances, event thresholds) and the subscription
categories the topic exposes. Everything downstream — the parser, the
corroboration engine, subscriptions, the digest — reads the pack rather than
hard-coding weather, so a second topic is a YAML file, not a code change.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from intelnet.config import CONFIG_DIR

_EXPR_ALLOWED = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Pow,
)


def _compile_expr(expr: str):
    """Compile a tiny arithmetic expression in `x` (no names, calls or attrs)."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _EXPR_ALLOWED):
            raise ValueError(f"unsupported unit expression: {expr!r}")
        if isinstance(node, ast.Name) and node.id != "x":
            raise ValueError(f"unit expression may only use x: {expr!r}")
    code = compile(tree, "<unit>", "eval")
    return lambda x: float(eval(code, {"__builtins__": {}}, {"x": float(x)}))  # noqa: S307


@dataclass
class Metric:
    key: str
    topic: str
    label: str
    unit: str = ""
    kind: str = "numeric"                       # numeric | flag
    aliases: tuple[str, ...] = ()
    units: dict[str, Any] = field(default_factory=dict)   # typed unit → callable(x)
    default_unit: str = ""                      # assumed when a bare number is typed
    words: dict[str, float] = field(default_factory=dict)
    range: tuple[float, float] | None = None
    tolerance: dict[str, float] = field(default_factory=dict)
    radius_km: float = 20.0
    window_min: float = 60.0
    event: list[tuple[float, float]] = field(default_factory=list)
    event_direction: str = "above"              # above | below
    display_unit: dict[str, Any] | None = None  # {"unit": str, "fn": callable}

    @property
    def is_flag(self) -> bool:
        return self.kind == "flag"

    def convert(self, value: float, unit: str | None) -> float:
        """Typed value + unit → canonical unit. Unknown unit → ValueError."""
        if self.is_flag:
            return 1.0
        unit = (unit or self.default_unit or "").lower()
        if not unit:
            return float(value)
        fn = self.units.get(unit)
        if fn is None:
            raise ValueError(f"unknown unit {unit!r} for {self.key}")
        return fn(value)

    def in_range(self, value: float) -> bool:
        if self.range is None:
            return True
        lo, hi = self.range
        return lo <= value <= hi

    def compatible(self, a: float, b: float) -> bool:
        """Do two nearby readings agree, within the pack's tolerance?"""
        if self.is_flag:
            return True
        diff = abs(a - b)
        tol_abs = self.tolerance.get("abs")
        tol_rel = self.tolerance.get("rel")
        if tol_abs is None and tol_rel is None:
            return True
        allowed = 0.0
        if tol_abs is not None:
            allowed = max(allowed, tol_abs)
        if tol_rel is not None:
            allowed = max(allowed, tol_rel * max(abs(a), abs(b)))
        return diff <= allowed

    def severity(self, value: float) -> float:
        """0 when below the first event threshold; else the highest matched row."""
        sev = 0.0
        for threshold, s in self.event:
            hit = value <= threshold if self.event_direction == "below" else value >= threshold
            if hit:
                sev = max(sev, s)
        return sev

    def is_event(self, value: float) -> bool:
        return self.severity(value) > 0

    def display(self, value: float | None) -> str:
        """Human-facing rendering: canonical plus the display unit when set."""
        if value is None:
            return "—"
        if self.is_flag:
            return "reported"
        canon = f"{value:.4g} {self.unit}".strip()
        if self.display_unit:
            shown = self.display_unit["fn"](value)
            return f"{shown:.3g} {self.display_unit['unit']} ({canon})"
        return canon


@dataclass
class Topic:
    name: str
    label: str
    description: str = ""
    categories: dict[str, str] = field(default_factory=dict)
    alert_routing: dict[str, list[str]] = field(default_factory=dict)
    metrics: dict[str, Metric] = field(default_factory=dict)
    alert_support: dict[str, list[str]] = field(default_factory=dict)
    lsr_types: dict[str, dict[str, str]] = field(default_factory=dict)
    station_fields: dict[str, dict[str, str]] = field(default_factory=dict)
    # Any other `<source>_<elements|parameters|types|fields|codes>` section: a
    # reference feed's code → {metric, unit} map (usgs_parameters, awdb_elements…).
    mappings: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    def mapping(self, name: str) -> dict[str, dict[str, str]]:
        return self.mappings.get(name, {})

    def category_key(self, category: str) -> str:
        return f"{self.name}.{category}"

    @property
    def category_keys(self) -> list[str]:
        return [self.category_key(c) for c in self.categories]

    def alias_map(self) -> dict[str, Metric]:
        """Every alias (and metric key) → Metric, longest aliases first."""
        out: dict[str, Metric] = {}
        for m in self.metrics.values():
            out[m.key] = m
            for a in m.aliases:
                out[a.lower()] = m
        return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


def _unit_fn(spec: Any):
    if isinstance(spec, dict) and "expr" in spec:
        return _compile_expr(str(spec["expr"]))
    factor = float(spec)
    return lambda x, f=factor: float(x) * f


def _load_metric(topic: str, key: str, raw: dict[str, Any]) -> Metric:
    units = {str(u).lower(): _unit_fn(spec) for u, spec in (raw.get("units") or {}).items()}
    display = None
    if raw.get("display_unit"):
        du = raw["display_unit"]
        display = {"unit": du["unit"], "fn": _compile_expr(str(du.get("expr", "x")))}
    rng = raw.get("range")
    return Metric(
        key=key,
        topic=topic,
        label=raw.get("label", key),
        unit=str(raw.get("unit", "")),
        kind=str(raw.get("kind", "numeric")),
        aliases=tuple(str(a).lower() for a in raw.get("aliases") or ()),
        units=units,
        default_unit=str(raw.get("default_unit", "")).lower(),
        words={str(w).lower(): float(v) for w, v in (raw.get("words") or {}).items()},
        range=(float(rng[0]), float(rng[1])) if rng else None,
        tolerance={k: float(v) for k, v in (raw.get("tolerance") or {}).items()},
        radius_km=float(raw.get("radius_km", 20)),
        window_min=float(raw.get("window_min", 60)),
        event=[(float(t), float(s)) for t, s in (raw.get("event") or [])],
        event_direction=str(raw.get("event_direction", "above")),
        display_unit=display,
    )


def load_topic(path: Path) -> Topic:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name = str(raw.get("topic") or path.stem)
    metrics = {k: _load_metric(name, k, v or {}) for k, v in (raw.get("metrics") or {}).items()}
    known = {"topic", "label", "description", "categories", "alert_routing", "alert_support",
             "metrics", "lsr_types", "station_fields"}
    mappings = {
        str(k): {str(code): dict(v) for code, v in (val or {}).items()}
        for k, val in raw.items()
        if k not in known and isinstance(val, dict)
        and str(k).rsplit("_", 1)[-1] in ("elements", "parameters", "types", "fields", "codes")
    }
    return Topic(
        name=name,
        label=str(raw.get("label", name.title())),
        description=str(raw.get("description", "")),
        categories={str(k): str(v) for k, v in (raw.get("categories") or {}).items()},
        alert_routing={str(k): list(v) for k, v in (raw.get("alert_routing") or {}).items()},
        metrics=metrics,
        alert_support={str(k): [str(x) for x in v] for k, v in (raw.get("alert_support") or {}).items()},
        lsr_types={str(k): dict(v) for k, v in (raw.get("lsr_types") or {}).items()},
        station_fields={str(k): dict(v) for k, v in (raw.get("station_fields") or {}).items()},
        mappings=mappings,
    )


@lru_cache(maxsize=1)
def topics(config_dir: Path | None = None) -> dict[str, Topic]:
    """All topic packs, keyed by topic name (cached; `reload()` to re-read)."""
    base = (config_dir or CONFIG_DIR) / "topics"
    out: dict[str, Topic] = {}
    for path in sorted(base.glob("*.yaml")):
        t = load_topic(path)
        out[t.name] = t
    return out


def reload() -> None:
    topics.cache_clear()


def get_topic(name: str) -> Topic:
    try:
        return topics()[name]
    except KeyError:
        raise KeyError(f"unknown topic {name!r}; known: {sorted(topics())}") from None


@lru_cache(maxsize=1)
def news_topics() -> dict[str, str]:
    """Reading-list categories that carry no metrics — {name: description}.

    They widen what triage can file an article under (land use, emergency
    response, research) without pretending anyone can report a permit hearing.
    """
    path = CONFIG_DIR / "news_topics.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): " ".join(str(v).split()) for k, v in (raw.get("topics") or {}).items()}


def triage_labels() -> dict[str, str]:
    """Everything triage may file an item under: the packs, then the news-only ones."""
    return {t.name: t.description for t in topics().values()} | news_topics()


def default_topic() -> Topic:
    """The topic used when a sensor doesn't name one (the only one, or 'weather')."""
    all_topics = topics()
    if "weather" in all_topics:
        return all_topics["weather"]
    return next(iter(all_topics.values()))


def find_metric(alias: str, topic: Topic | None = None) -> Metric | None:
    """Resolve a metric by key or alias within a topic (default topic if None)."""
    key = alias.strip().lower()
    key = re.sub(r"\s+", " ", key)
    pool = [topic] if topic else list(topics().values())
    for t in pool:
        m = t.alias_map().get(key)
        if m:
            return m
    return None


def all_categories() -> dict[str, str]:
    """`topic.category` → description across every pack."""
    out: dict[str, str] = {}
    for t in topics().values():
        for c, desc in t.categories.items():
            out[t.category_key(c)] = desc
    return out


def resolve_category(name: str) -> str | None:
    """Accept `weather.alerts` or bare `alerts`.

    A bare name that several packs share (`events`, `reports`, `digest`)
    resolves to the default topic's; `soil.events` names another pack's.
    """
    key = name.strip().lower()
    cats = all_categories()
    if key in cats:
        return key
    matches = [c for c in cats if c.endswith("." + key)]
    if not matches:
        return None
    preferred = default_topic().category_key(key)
    return preferred if preferred in matches else matches[0]


def expand_category(name: str) -> list[str]:
    """`*.events` / `all.events` → every pack's `<topic>.events`; else [resolved]."""
    key = name.strip().lower()
    if key.startswith(("*.", "all.")):
        suffix = key.split(".", 1)[1]
        return [c for c in all_categories() if c.endswith("." + suffix)]
    one = resolve_category(key)
    return [one] if one else []


def metric_by_key(key: str) -> Metric | None:
    for t in topics().values():
        if key in t.metrics:
            return t.metrics[key]
    return None


def topic_of_metric(key: str) -> Topic | None:
    for t in topics().values():
        if key in t.metrics:
            return t
    return None
