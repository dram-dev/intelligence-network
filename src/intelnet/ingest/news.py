"""Reading-list RSS (agencies, Extension, the farm press, Google News proxies) → items."""
from __future__ import annotations

import logging
import re
import time
from functools import lru_cache

import yaml
from digest_core.ingest.rss import fetch_feeds

from intelnet.config import CONFIG_DIR
from intelnet.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _config() -> dict:
    path = CONFIG_DIR / "news_feeds.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def feed_list() -> list[dict]:
    return list(_config().get("feeds") or [])


def blocked() -> list[str]:
    """Title patterns that are never news, whichever feed carries them (`block`)."""
    return [str(x) for x in _config().get("block") or []]


def title_key(title: str) -> str:
    """The story, not the copy: lowercase words, a Google News ' - Publisher' tail dropped, so the
    same story from two queries (or two nights) is one."""
    head, sep, tail = title.rpartition(" - ")
    text = head if sep and len(head) >= 12 and len(tail) <= 60 else title
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def fresh(items: list[IngestedItem], seen: set[str]) -> list[IngestedItem]:
    """Each story once: not seen in the last days (`seen`, updated) nor earlier in this batch."""
    out = []
    for item in items:
        key = title_key(item.title)
        if key and key in seen:
            continue
        seen.add(key)
        out.append(item)
    if len(out) < len(items):
        logger.info("news: %d repeats of stories already in", len(items) - len(out))
    return out


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
    block = [re.compile(b, re.I) for b in blocked()]
    kept: list[IngestedItem] = []
    for item in items:
        if any(b.search(item.title or "") for b in block):
            continue
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
        from intelnet import db

        feeds = feed_list()
        items: list[IngestedItem] = []
        for f in feeds:                                   # one at a time: an empty feed is tried again
            got = fetch_feeds([f], self.name, default_limit=15)
            if not got and not is_search(f):
                time.sleep(RETRY_AFTER)
                got = fetch_feeds([f], self.name, default_limit=15)
            note_empty(f, empty=not got)
            items += got
        items = apply_filters(items, feeds)
        return fresh(items, {title_key(t) for t in db.recent_item_titles(days=3)})


RETRY_AFTER = 5                # seconds before a second try: FarmWeekNow fails at 01:xx, then answers


def is_search(f: dict) -> bool:
    """A search (Google News) can rightly come back empty; a site's own feed always lists something."""
    return "news.google.com/rss/search" in str(f.get("url") or "")


def note_empty(f: dict, *, empty: bool) -> None:
    """Count a feed's empty runs in a row (kv `news_empty:<name>`), for the nightly check."""
    from intelnet import db

    key = f"news_empty:{f.get('name') or f.get('url')}"
    try:
        db.kv_set(key, str(int(db.kv_get(key) or 0) + 1 if empty else 0))
    except Exception:  # noqa: BLE001 — bookkeeping never stops the ingest
        pass


def empty_feeds(runs: int = 3) -> list[str]:
    """Site feeds that came back empty `runs` times in a row: blocked, moved or down."""
    from intelnet import db

    return [str(f.get("name")) for f in feed_list()
            if not is_search(f) and int(db.kv_get(f"news_empty:{f.get('name') or f.get('url')}") or 0) >= runs]
