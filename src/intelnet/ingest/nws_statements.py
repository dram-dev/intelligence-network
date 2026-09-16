"""NWS Public Information Statements from the five Illinois offices → items.

Damage surveys, storm totals and record reports: the authoritative account of
what a storm actually did, issued as text products by each Weather Forecast
Office and served by api.weather.gov. They are what a member's tornado or
rainfall report gets checked against, so the digest carries them beside the
reading list.

This replaces the weather.gov office "news" pages (`rss_page.php`), which still
return valid RSS but have carried no items for years. The API keeps roughly a
week of products, so the nightly run sees every statement.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import requests

from intelnet import db
from intelnet.config import settings
from intelnet.ingest.base import IngestedItem, IngestorBase
from intelnet.models import parse_iso

logger = logging.getLogger(__name__)

API = "https://api.weather.gov"
IEM_PRODUCT = "https://mesonet.agron.iastate.edu/p.php?pid="

#: Offices whose county warning areas cover part of Illinois.
OFFICES = {
    "LOT": "Chicago",
    "ILX": "Lincoln",
    "DVN": "Quad Cities",
    "LSX": "St. Louis",
    "PAH": "Paducah",
}
PRODUCT = "PNS"
MAX_PER_OFFICE = 5
MAX_CHARS = 6000


def _get(url: str) -> dict[str, Any]:
    r = requests.get(url, headers={"User-Agent": settings.nws_user_agent,
                                   "Accept": "application/ld+json"}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_listing(office: str) -> list[dict[str, Any]]:
    """Recent statements from one office, newest first (metadata only)."""
    return list(_get(f"{API}/products/types/{PRODUCT}/locations/{office}").get("@graph") or [])


def fetch_text(product_id: str) -> str:
    return str(_get(f"{API}/products/{product_id}").get("productText") or "")


def headline(text: str) -> str | None:
    """The `...NWS Damage Survey for the Toluca Tornado...` title block.

    A product's title sits between triple dots and often wraps across lines, so
    the match spans newlines and the whitespace is collapsed back to one line.
    """
    m = re.search(r"^\.\.\.\s*(.+?)\s*\.\.\.", text, re.M | re.S)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()[:160] or None


def permalink(meta: dict[str, Any], office: str) -> str:
    """Readable rendering of the product on IEM, falling back to the raw API."""
    issued, wmo = parse_iso(meta.get("issuanceTime")), meta.get("wmoCollectiveId")
    sender = meta.get("issuingOffice")
    if issued and wmo and sender:
        pid = f"{issued.strftime('%Y%m%d%H%M')}-{sender}-{wmo}-{PRODUCT}{office}"
        return IEM_PRODUCT + pid
    return f"{API}/products/{meta.get('id')}"


def to_item(office: str, meta: dict[str, Any], text: str) -> IngestedItem:
    name = OFFICES.get(office, office)
    title = headline(text) or str(meta.get("productName") or "Public Information Statement")
    return IngestedItem(
        source=NWSStatementsIngestor.name,
        source_id=str(meta.get("id") or meta.get("@id") or ""),
        title=f"{title} (NWS {name})",
        url=permalink(meta, office),
        author=f"NWS {name}",
        content=text[:MAX_CHARS],
        published_at=parse_iso(meta.get("issuanceTime")),
        metadata={"feed": f"NWS {name} statements", "feed_url": f"{API}/products/types/{PRODUCT}/locations/{office}",
                  "topic_hint": "weather", "office": office, "product": PRODUCT},
    )


class NWSStatementsIngestor(IngestorBase):
    """Public Information Statements (damage surveys, storm totals, records)."""

    name = "nws_statements"
    tags = ("news", "weather")
    order = 20

    def fetch(self) -> list[IngestedItem]:
        seen = db.existing_source_ids(self.name)
        items: list[IngestedItem] = []
        for office in OFFICES:
            try:
                listing = fetch_listing(office)
            except Exception as exc:  # noqa: BLE001 — one office can't sink the rest
                logger.warning("nws_statements: %s listing failed: %s", office, exc)
                continue
            for meta in listing[:MAX_PER_OFFICE]:
                product_id = str(meta.get("id") or "")
                if not product_id or product_id in seen:
                    continue
                try:
                    text = fetch_text(product_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("nws_statements: %s %s failed: %s", office, product_id, exc)
                    continue
                items.append(to_item(office, meta, text))
        return items
