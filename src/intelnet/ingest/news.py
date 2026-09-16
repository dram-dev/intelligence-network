"""Reading-list RSS (agencies, Extension, the farm press, Google News proxies) → items."""
from __future__ import annotations

import logging
import re
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


def _hits(item: IngestedItem, pattern: str) -> bool:
    return re.search(pattern, f"{item.title}\n{item.content or ''}", re.I) is not None


def apply_filters(items: list[IngestedItem], feeds: list[dict]) -> list[IngestedItem]:
    """Keep only the items their feed's `include` / `exclude` patterns allow.

    Some feeds are worth reading for a narrow slice of what they publish: a
    state agency newsroom that mostly announces fairs but is the authority on
    pesticide cases (`include`), or an air-quality feed that always carries a
    "currently no alerts" placeholder (`exclude`). A feed with neither pattern
    passes everything through.
    """
    rules = {f.get("name", f.get("url")): (f.get("include"), f.get("exclude")) for f in feeds}
    kept: list[IngestedItem] = []
    for item in items:
        include, exclude = rules.get((item.metadata or {}).get("feed"), (None, None))
        if include and not _hits(item, include):
            continue
        if exclude and _hits(item, exclude):
            continue
        kept.append(item)
    if len(kept) < len(items):
        logger.info("news: filters dropped %d of %d items", len(items) - len(kept), len(items))
    return kept


class NewsIngestor(IngestorBase):
    """State-scoped environmental news for the digest's reading list."""

    name = "news"
    tags = ("news",)
    order = 10

    def fetch(self) -> list[IngestedItem]:
        feeds = feed_list()
        return apply_filters(fetch_feeds(feeds, self.name, default_limit=15), feeds)
