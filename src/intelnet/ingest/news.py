"""Reading-list RSS (Google News proxies + NWS office news) → items."""
from __future__ import annotations

import logging
from functools import lru_cache

import yaml
from digest_core.ingest.rss import fetch_feeds

from intelnet.config import CONFIG_DIR
from intelnet.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def feed_list() -> list[dict]:
    path = CONFIG_DIR / "news_feeds.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("feeds") or [])


class NewsIngestor(IngestorBase):
    """State-scoped weather news for the digest's reading list."""

    name = "news"
    tags = ("news",)
    order = 10

    def fetch(self) -> list[IngestedItem]:
        return fetch_feeds(feed_list(), self.name, default_limit=15)
