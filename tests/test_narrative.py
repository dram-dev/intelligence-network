"""The digest's narrative: written from a short list of the day's facts, by the summarizer
model, else the parser model (which answers in JSON)."""
from __future__ import annotations

import json

from intelnet import digest, llm
from intelnet.config import settings


def test_the_facts_are_the_days_essentials_not_the_whole_model(make_sensor):
    m = digest.build(hours=24)
    m.alerts = [{"event": "Flood Warning", "severity": "Moderate", "counties": ["Lake"], "expires": "—", "url": None},
                {"event": "Flood Warning", "severity": "Moderate", "counties": ["Pike"],
                 "expires": "2020-01-01T00:00:00Z", "url": None}]                      # ended long ago
    m.reading = [{"title": f"Story {i}", "url": "u", "relevance": 0.9, "reason": "r" * 400, "published_at": "",
                  "feed": "f"} for i in range(15)]
    facts = json.loads(digest.narrative_facts(m))
    assert facts["alerts_in_effect"] == ["Flood Warning: Lake County"] and facts["alerts_ended"] == 1
    assert facts["state"] == "Illinois" and facts["digest_date"].endswith(m.date[:4])
    assert "reading" not in facts and len(digest.narrative_facts(m)) < 3000     # the reading list stays out


def test_the_parser_model_writes_it_when_the_summarizer_fails(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    asked: list[tuple[str, str]] = []

    def call(backend, system, user, **cfg):
        asked.append((backend, system))
        if backend == settings.summarizer_backend:
            return None                                         # timed out
        return '{"text": "Floods along the Rock.\\n\\nSunny today, high 76°F."}'

    monkeypatch.setattr(llm, "call", call)
    assert llm.narrative("{}") == "Floods along the Rock.\n\nSunny today, high 76°F."
    (first, plain), (second, as_json) = asked
    assert (first, second) == (settings.summarizer_backend, settings.parser_backend)
    assert '{"text"' not in plain and '{"text"' in as_json             # only the JSON model is told JSON


def test_thinking_and_wrappers_are_cleaned_off():
    assert llm._clean("<think>hmm</think>\n\nFloods.\n\nSun.") == "Floods.\n\nSun."
    assert llm._clean('{"narrative": "Floods."}') == "Floods."
    assert llm._clean("   ") is None and llm._clean(None) is None
