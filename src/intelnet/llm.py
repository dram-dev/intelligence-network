"""LLM helpers — free-text parsing, news triage, digest narrative.

All three ride digest-core's backend registry (the same Ollama / MLX servers
the other digests use). Every call is best-effort: a failure returns None or
an empty list and the caller falls back (grammar-only parsing, an un-triaged
reading list, a digest without a narrative). `LLM_ENABLED=false` short-circuits
everything, which is also how the test-suite runs.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from digest_core.summarize.backends import BackendConfig, BackendError, get_backend
from digest_core.summarize.runner import extract_json

from intelnet import geo
from intelnet.config import settings
from intelnet.language import ParsedSignal, ParseResult, describe_metrics
from intelnet.topics import Topic, find_metric

logger = logging.getLogger(__name__)


def backend_config(*, max_tokens: int = 600, temperature: float = 0.1) -> BackendConfig:
    return BackendConfig(
        timeout_sec=settings.summarizer_timeout_sec,
        max_tokens=max_tokens,
        temperature=temperature,
        anthropic_api_key=settings.anthropic_api_key,
        gemini_api_key=settings.gemini_api_key,
        ollama_host=settings.ollama_host,
        ollama_model=settings.ollama_model,
        ollama_think=settings.ollama_think,
        mlx_server_url=settings.mlx_server_url,
        mlx_model=settings.mlx_model,
    )


def call(backend_name: str, system_prompt: str, user_prompt: str, **cfg: Any) -> str | None:
    if not settings.llm_enabled:
        return None
    fn = get_backend(backend_name)
    if fn is None:
        logger.warning("llm: unknown backend %r", backend_name)
        return None
    try:
        return fn(system_prompt, user_prompt, backend_config(**cfg))
    except (BackendError, Exception) as exc:  # noqa: BLE001
        logger.warning("llm: %s call failed: %s", backend_name, exc)
        return None


# ── free-text contribution → signals ───────────────────────────────────────

_PARSE_SYSTEM = """You turn a short eyewitness message into structured observations for a sensor network.
Output ONLY a JSON object: {"signals": [...], "unparsed": "..."}.
Each signal: {"metric": <metric key>, "value": <number or 1 for a flag>, "unit": <unit typed, or "">,
"location": <place/ZIP/county text if the message names one, else null>,
"minutes_ago": <integer if the message says when, else null>, "note": <short quote>, "confidence": 0-1}.
Rules: only report what the message states as observed (not forecasts, fears or questions);
never invent numbers; a flag metric has value 1; if nothing is observable, return an empty list.
Metric vocabulary:
"""


def parse_free_text(text: str, topic: Topic | None = None, *, online: bool | None = None) -> ParseResult:
    """Prose → ParseResult via the parser backend. Empty result when disabled/failed."""
    result = ParseResult()
    if not text.strip() or not settings.llm_enabled:
        return result
    raw = call(settings.parser_backend, _PARSE_SYSTEM + describe_metrics(topic), text,
               max_tokens=600, temperature=0.0)
    if not raw:
        return result
    data = extract_json(raw) or {}
    for item in data.get("signals") or []:
        if not isinstance(item, dict):
            continue
        metric = find_metric(str(item.get("metric", "")), topic)
        if metric is None:
            continue
        try:
            value = 1.0 if metric.is_flag else metric.convert(float(item.get("value")), item.get("unit") or None)
        except (TypeError, ValueError):
            continue
        if not metric.in_range(value):
            result.errors.append(f"{metric.label} {value:g} outside plausible range — not recorded")
            continue
        loc = geo.parse_location(str(item["location"]), online=online) if item.get("location") else None
        observed = None
        if item.get("minutes_ago") is not None:
            from datetime import datetime, timedelta, timezone
            try:
                observed = datetime.now(timezone.utc) - timedelta(minutes=float(item["minutes_ago"]))
            except (TypeError, ValueError):
                observed = None
        try:
            conf = min(1.0, max(0.0, float(item.get("confidence", 0.7))))
        except (TypeError, ValueError):
            conf = 0.7
        result.signals.append(ParsedSignal(
            metric, value, f"{item.get('value')}{item.get('unit') or ''}", location=loc,
            observed_at=observed, note=(item.get("note") or None), confidence=conf,
        ))
    result.leftover = str(data.get("unparsed") or "")
    return result


# ── news triage ────────────────────────────────────────────────────────────

_TRIAGE_SYSTEM = """You are the triage gate for an environmental intelligence digest covering {state}:
weather, water (rivers, lakes, wells, water quality), soil health, agriculture (crops, pests,
drought) and air quality. Keep items that report or explain actual conditions, hazards, warnings,
damage, records, outbreaks or high-impact forecasts in {state}. Drop generic national stories,
lifestyle pieces, and anything not about {state}. Output ONLY JSON:
{{"decision": "keep"|"drop", "relevance": 0-1, "topic": one of {topics}, "reason": "<=25 words"}}"""


def triage_item(item: dict[str, Any]) -> dict[str, Any] | None:
    if not settings.llm_enabled:
        return None
    body = (item.get("content") or "")[:2500]
    prompt = f"Title: {item.get('title')}\nSource: {item.get('source')}\n\n{body}"
    from intelnet.topics import topics as _topics

    raw = call(settings.triage_backend,
               _TRIAGE_SYSTEM.format(state=settings.geo_state, topics=list(_topics())), prompt,
               max_tokens=200, temperature=0.0)
    if not raw:
        return None
    data = extract_json(raw)
    if not data or data.get("decision") not in ("keep", "drop"):
        return None
    try:
        rel = min(1.0, max(0.0, float(data.get("relevance", 0.5))))
    except (TypeError, ValueError):
        rel = 0.5
    return {"decision": data["decision"], "relevance": rel,
            "topic": str(data.get("topic") or "weather"), "reason": str(data.get("reason") or "")[:200]}


# ── digest narrative ──────────────────────────────────────────────────────

_NARRATIVE_SYSTEM = """You write the two-paragraph opening of a daily digest for a citizen
environmental sensor network in {state} (weather, water, soil, agriculture, air). Paragraph 1: what the network observed in the last 24 hours (events,
official alerts, notable readings) — concrete places and numbers, no hype. Paragraph 2: the state of
the network itself (coverage, corroboration, where sensors are needed) in one or two sentences.
Plain text, no markdown, no headings, under 180 words. Only use facts from the data given."""


def narrative(digest_json: str) -> str | None:
    text = call(settings.summarizer_backend, _NARRATIVE_SYSTEM.format(state=settings.geo_state),
                digest_json, max_tokens=400, temperature=0.3)
    if not text:
        return None
    text = text.strip()
    # A model that answers in JSON by habit: unwrap {"text": ...}
    if text.startswith("{"):
        data = extract_json(text) or {}
        text = str(data.get("text") or data.get("narrative") or "")
    return text or None


def probe() -> dict[str, str]:
    """Reachability of each configured backend (for `intelnet health`)."""
    out: dict[str, str] = {}
    if not settings.llm_enabled:
        return {"llm": "disabled"}
    import requests

    try:
        r = requests.get(settings.ollama_host.rstrip("/") + "/api/tags", timeout=3)
        out["ollama"] = f"ok ({settings.ollama_model})" if r.ok else f"http {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        out["ollama"] = f"unreachable ({type(exc).__name__})"
    try:
        r = requests.get(settings.mlx_server_url.rstrip("/") + "/v1/models", timeout=3)
        out["mlx"] = f"ok ({settings.mlx_model})" if r.ok else f"http {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        out["mlx"] = f"unreachable ({type(exc).__name__})"
    return out


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)
