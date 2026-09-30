"""Nightly copies of the live database.

SQLite's online backup API copies a consistent snapshot while the bot, the alert
loop and the watch keep writing (WAL mode), so nothing stops for it. The copy is
gzipped into BACKUP_DIR as network-YYYY-MM-DD.db.gz and the oldest beyond
BACKUP_KEEP are removed. Point BACKUP_DIR at a synced folder (iCloud Drive,
Dropbox) and the copies leave this machine too. Restore: gunzip the file and set
DB_PATH to it (or copy it over data/network.db with the jobs stopped).
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
from pathlib import Path

from intelnet.config import settings
from intelnet.models import local_time, utcnow


def run(dest_dir: Path | None = None, keep: int | None = None, source: Path | None = None) -> Path:
    dest_dir = Path(dest_dir or settings.backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    keep = keep if keep is not None else settings.backup_keep
    stamp = local_time(utcnow(), "%Y-%m-%d")
    raw = dest_dir / f"network-{stamp}.db"
    src = sqlite3.connect(source or settings.db_path, timeout=30)
    dst = sqlite3.connect(raw)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    final = raw.with_suffix(".db.gz")
    with raw.open("rb") as fin, gzip.open(final, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    raw.unlink()
    for old in sorted(dest_dir.glob("network-*.db.gz"))[:-keep or None]:
        old.unlink()
    return final
