"""Bind digest-core's IngestorBase to this project's item store."""
from __future__ import annotations

from digest_core.ingest.base import IngestorBase as _CoreIngestorBase
from digest_core.types import IngestedItem

from intelnet import db

__all__ = ["IngestedItem", "IngestorBase"]


class IngestorBase(_CoreIngestorBase):
    """Persistence bound to `intelnet.db` (upsert_items / log_run)."""

    store = db
