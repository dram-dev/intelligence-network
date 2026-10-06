"""JSON from the public sources the brief and digest read (forecasts, outlooks, rivers):
tried twice, and kept for a while in this process so a fan-out asks each URL once."""
from __future__ import annotations

import time
from typing import Any

from intelnet.config import settings

TIMEOUT = 12
TTL = 30 * 60
_memo: dict[str, tuple[float, Any]] = {}


def get_json(url: str, *, ttl: float = TTL, timeout: float = TIMEOUT) -> Any:
    hit = _memo.get(url)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    import requests

    last: Exception | None = None
    for _ in range(2):
        try:
            r = requests.get(url, timeout=timeout, headers={
                "User-Agent": settings.nws_user_agent, "Accept": "application/geo+json, application/json"})
            r.raise_for_status()
            data = r.json()
            _memo[url] = (time.monotonic(), data)
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
    assert last is not None
    raise last
