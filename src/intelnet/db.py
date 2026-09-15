"""SQLite state — sensors, signals, events, subscriptions. Raw sqlite3.

The framework base (items / run_log / summarizer_log + connection helpers)
comes from `digest_core.db`; the news ingestor writes `items` through the
`ItemStore` shape (`upsert_items` / `log_run`). Everything the network itself
owns — the sensor registry, the signal log, events, subscriptions, the
notify ledger, the digest ledger — is layered here as MIGRATIONS.

The digest is NOT on disk: it is published to Google Drive (`gdrive.py`) and
only its links are recorded here (`digests`).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from digest_core.db import helpers as core_db
from digest_core.types import IngestedItem

from intelnet import geo
from intelnet.config import settings
from intelnet.models import (
    REFERENCE_KINDS,
    Sensor,
    Signal,
    iso,
    utcnow,
)

logger = logging.getLogger(__name__)

MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS kv (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS sensors (
        id              TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        name            TEXT,
        chat_id         TEXT,
        username        TEXT,
        lat             REAL,
        lon             REAL,
        county_fips     TEXT,
        zip5            TEXT,
        zip9            TEXT,
        precision       TEXT,
        trust           REAL NOT NULL DEFAULT 0.5,
        trust_prior     REAL NOT NULL DEFAULT 0.5,
        n_signals       INTEGER NOT NULL DEFAULT 0,
        n_corroborated  INTEGER NOT NULL DEFAULT 0,
        n_contradicted  INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'active',
        registered_at   TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen_at    TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sensors_chat ON sensors(chat_id)",
    """CREATE TABLE IF NOT EXISTS signals (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        key                 TEXT NOT NULL UNIQUE,
        source              TEXT NOT NULL,
        source_id           TEXT NOT NULL,
        sensor_id           TEXT NOT NULL,
        sensor_kind         TEXT NOT NULL,
        topic               TEXT NOT NULL,
        metric              TEXT NOT NULL,
        value               REAL,
        unit                TEXT,
        text                TEXT,
        observed_at         TEXT NOT NULL,
        received_at         TEXT NOT NULL,
        expires_at          TEXT,
        lat                 REAL,
        lon                 REAL,
        geohash             TEXT,
        county_fips         TEXT,
        zip5                TEXT,
        zip9                TEXT,
        precision           TEXT,
        confidence          REAL NOT NULL DEFAULT 1.0,
        quality             TEXT NOT NULL DEFAULT 'raw',
        corroboration_n     INTEGER NOT NULL DEFAULT 0,
        contradiction_n     INTEGER NOT NULL DEFAULT 0,
        reference_agreement TEXT NOT NULL DEFAULT 'none',
        group_key           TEXT,
        evidence_json       TEXT,
        raw_json            TEXT,
        event_id            INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_signals_metric_time ON signals(metric, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_signals_sensor ON signals(sensor_id, received_at)",
    "CREATE INDEX IF NOT EXISTS idx_signals_county ON signals(county_fips, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_signals_kind_time ON signals(sensor_kind, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_signals_group ON signals(group_key)",
    "CREATE INDEX IF NOT EXISTS idx_signals_event ON signals(event_id)",
    """CREATE TABLE IF NOT EXISTS events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        topic         TEXT NOT NULL,
        metric        TEXT NOT NULL,
        county_fips   TEXT,
        zip5          TEXT,
        lat           REAL,
        lon           REAL,
        opened_at     TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        closed_at     TEXT,
        peak_value    REAL,
        unit          TEXT,
        n_signals     INTEGER NOT NULL DEFAULT 0,
        n_sensors     INTEGER NOT NULL DEFAULT 0,
        n_reference   INTEGER NOT NULL DEFAULT 0,
        mean_trust    REAL,
        severity      REAL,
        score         REAL,
        status        TEXT NOT NULL DEFAULT 'open',
        title         TEXT,
        pushed_at     TEXT,
        pushed_score  REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_county ON events(county_fips, metric, status)",
    """CREATE TABLE IF NOT EXISTS subscriptions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    TEXT NOT NULL,
        category   TEXT NOT NULL,
        area       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(chat_id, category, area)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_subs_cat_area ON subscriptions(category, area)",
    """CREATE TABLE IF NOT EXISTS notify_log (
        key     TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        sent_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (key, chat_id)
    )""",
    """CREATE TABLE IF NOT EXISTS digests (
        date          TEXT PRIMARY KEY,
        drive_file_id TEXT,
        drive_url     TEXT,
        latest_url    TEXT,
        folder_url    TEXT,
        n_events      INTEGER,
        n_signals     INTEGER,
        n_sensors     INTEGER,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS email_subscribers (
        email    TEXT PRIMARY KEY,
        chat_id  TEXT,
        added_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # news items: triage fields beyond BASE_SCHEMA
    "ALTER TABLE items ADD COLUMN triage_reason TEXT",
    "ALTER TABLE items ADD COLUMN relevance REAL",
    "CREATE INDEX IF NOT EXISTS idx_items_triage ON items(triage_decision, ingested_at)",
]


# ── connection ────────────────────────────────────────────────────────────

def init_db(db_path: Path | None = None) -> None:
    core_db.init_db_with_migrations(db_path or settings.db_path, MIGRATIONS)


def get_conn(db_path: Path | None = None) -> AbstractContextManager[sqlite3.Connection]:
    return core_db.get_conn(db_path or settings.db_path)


def utcnow_iso() -> str:
    return iso(utcnow()) or ""


def _since(hours: float) -> str:
    return iso(utcnow() - timedelta(hours=hours)) or ""


# ── kv ────────────────────────────────────────────────────────────────────

def kv_get(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )


# ── ItemStore (news ingestor) ─────────────────────────────────────────────

def upsert_items(items: Iterable[IngestedItem]) -> int:
    with get_conn() as conn:
        return core_db.upsert_items(conn, items)


def log_run(*, run_type: str, source: str, items_fetched: int, items_new: int,
            duration_ms: int, status: str, error: str | None = None) -> None:
    with get_conn() as conn:
        core_db.log_run(conn, run_type, source, items_fetched, items_new, duration_ms, status, error)


def existing_source_ids(source: str) -> set[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT source_id FROM items WHERE source = ?", (source,)).fetchall()
    return {r["source_id"] for r in rows}


def lookback_hours(floor_hours: int = 24) -> int:
    """Window reaching back past the previous scheduled run (digest-core)."""
    with get_conn() as conn:
        return core_db.hours_since_previous_run(conn, floor_hours)


def items_needing_triage(hours: int, limit: int = 200) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT id, source, source_id, title, url, content, published_at, metadata_json
               FROM items
               WHERE triage_decision IS NULL AND ingested_at >= datetime('now', ?)
               ORDER BY published_at DESC, id DESC LIMIT ?""",
            (f"-{hours} hours", limit),
        ).fetchall()


def update_triage(item_id: int, decision: str, relevance: float | None, topic: str | None,
                  reason: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE items SET triage_decision = ?, relevance = ?, triage_score = ?, topic = ?,
               triage_reason = ?, triaged_at = ? WHERE id = ?""",
            (decision, relevance, relevance, topic, reason, utcnow_iso(), item_id),
        )


def kept_items_since(hours: int, limit: int = 30) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT id, source, title, url, published_at, relevance, topic, triage_reason,
                      metadata_json
               FROM items WHERE triage_decision = 'keep' AND triaged_at >= datetime('now', ?)
               ORDER BY relevance DESC, published_at DESC LIMIT ?""",
            (f"-{hours} hours", limit),
        ).fetchall()


# ── sensors ───────────────────────────────────────────────────────────────

def _sensor_from_row(row: sqlite3.Row) -> Sensor:
    r = dict(row)
    return Sensor(
        id=r["id"], kind=r["kind"], name=r.get("name") or "", chat_id=r.get("chat_id"),
        username=r.get("username"),
        location=geo.Location(
            lat=r.get("lat"), lon=r.get("lon"), county_fips=r.get("county_fips"),
            zip5=r.get("zip5"), zip9=r.get("zip9"), precision=r.get("precision") or "unknown",
        ),
        trust=r.get("trust") or 0.5, trust_prior=r.get("trust_prior") or 0.5,
        n_signals=r.get("n_signals") or 0,
        n_corroborated=r.get("n_corroborated") or 0, n_contradicted=r.get("n_contradicted") or 0,
        status=r.get("status") or "active", registered_at=r.get("registered_at"),
        last_seen_at=r.get("last_seen_at"),
    )


def get_sensor(sensor_id: str) -> Sensor | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sensors WHERE id = ?", (sensor_id,)).fetchone()
    return _sensor_from_row(row) if row else None


def sensor_by_chat(chat_id: str | int) -> Sensor | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sensors WHERE chat_id = ?", (str(chat_id),)).fetchone()
    return _sensor_from_row(row) if row else None


def upsert_sensor(s: Sensor) -> Sensor:
    """Insert or update identity/location fields; counters + trust are preserved."""
    loc = s.location
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sensors (id, kind, name, chat_id, username, lat, lon, county_fips, zip5,
                                    zip9, precision, trust, status, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 kind = excluded.kind, name = excluded.name, chat_id = excluded.chat_id,
                 username = excluded.username, lat = excluded.lat, lon = excluded.lon,
                 county_fips = excluded.county_fips, zip5 = excluded.zip5, zip9 = excluded.zip9,
                 precision = excluded.precision, last_seen_at = excluded.last_seen_at""",
            (s.id, s.kind, s.name, s.chat_id, s.username, loc.lat, loc.lon, loc.county_fips,
             loc.zip5, loc.zip9, loc.precision, s.trust, s.status, utcnow_iso()),
        )
    return get_sensor(s.id) or s


def ensure_reference_sensor(sensor_id: str, kind: str, name: str, location: geo.Location,
                            trust: float) -> Sensor:
    """Idempotent registration for stations / official feeds (fixed trust)."""
    existing = get_sensor(sensor_id)
    if existing:
        return existing
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO sensors (id, kind, name, lat, lon, county_fips, zip5, precision,
                                              trust, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sensor_id, kind, name, location.lat, location.lon, location.county_fips,
             location.zip5, location.precision, trust, utcnow_iso()),
        )
    return get_sensor(sensor_id)  # type: ignore[return-value]


def set_sensor_location(sensor_id: str, loc: geo.Location) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE sensors SET lat = ?, lon = ?, county_fips = ?, zip5 = ?, zip9 = ?,
               precision = ?, last_seen_at = ? WHERE id = ?""",
            (loc.lat, loc.lon, loc.county_fips, loc.zip5, loc.zip9, loc.precision,
             utcnow_iso(), sensor_id),
        )


def touch_sensor(sensor_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE sensors SET last_seen_at = ? WHERE id = ?", (utcnow_iso(), sensor_id))


def set_sensor_status(sensor_id: str, status: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("UPDATE sensors SET status = ? WHERE id = ?", (status, sensor_id))
        return cur.rowcount > 0


def set_sensor_trust(sensor_id: str, trust: float) -> bool:
    """Admin override: sets the trust AND the prior the sensor's record shrinks toward."""
    t = max(0.0, min(1.0, trust))
    with get_conn() as conn:
        cur = conn.execute("UPDATE sensors SET trust = ?, trust_prior = ? WHERE id = ?",
                           (t, t, sensor_id))
        return cur.rowcount > 0


def bump_sensor(sensor_id: str, *, signals: int = 0, corroborated: int = 0,
                contradicted: int = 0, trust: float | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE sensors SET n_signals = n_signals + ?, n_corroborated = n_corroborated + ?,
               n_contradicted = n_contradicted + ?, trust = COALESCE(?, trust),
               last_seen_at = ? WHERE id = ?""",
            (signals, corroborated, contradicted, trust, utcnow_iso(), sensor_id),
        )


def list_sensors(kind: str | None = None, active_hours: int | None = None,
                 limit: int = 500) -> list[Sensor]:
    sql = "SELECT * FROM sensors WHERE 1=1"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if active_hours:
        sql += " AND last_seen_at >= ?"
        params.append(_since(active_hours))
    sql += " ORDER BY n_corroborated DESC, n_signals DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_sensor_from_row(r) for r in rows]


def contributions_since(sensor_id: str, minutes: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM signals WHERE sensor_id = ? AND received_at >= ?",
            (sensor_id, iso(utcnow() - timedelta(minutes=minutes))),
        ).fetchone()
    return int(row["n"]) if row else 0


# ── signals ───────────────────────────────────────────────────────────────

_SIGNAL_COLS = (
    "key, source, source_id, sensor_id, sensor_kind, topic, metric, value, unit, text, "
    "observed_at, received_at, expires_at, lat, lon, geohash, county_fips, zip5, zip9, precision, "
    "confidence, quality, corroboration_n, contradiction_n, reference_agreement, group_key, "
    "evidence_json, raw_json, event_id"
)


def insert_signals(signals: Iterable[Signal]) -> list[Signal]:
    """INSERT OR IGNORE each signal; returns the ones that were new (with ids)."""
    new: list[Signal] = []
    cols = [c.strip() for c in _SIGNAL_COLS.split(",")]
    sql = (f"INSERT OR IGNORE INTO signals ({_SIGNAL_COLS}) VALUES "
           f"({', '.join(':' + c for c in cols)})")
    with get_conn() as conn:
        for s in signals:
            cur = conn.execute(sql, s.to_row())
            if cur.rowcount:
                s.id = cur.lastrowid
                new.append(s)
    return new


def insert_signal(signal: Signal) -> Signal | None:
    new = insert_signals([signal])
    return new[0] if new else None


def signal_by_id(signal_id: int) -> Signal | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    return Signal.from_row(row) if row else None


def update_assessment(signal_id: int, *, quality: str, corroboration_n: int, contradiction_n: int,
                      reference_agreement: str, event_id: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE signals SET quality = ?, corroboration_n = ?, contradiction_n = ?,
               reference_agreement = ?, event_id = COALESCE(?, event_id) WHERE id = ?""",
            (quality, corroboration_n, contradiction_n, reference_agreement, event_id, signal_id),
        )


def signals_near(metric: str, lat: float, lon: float, radius_km: float, start: datetime,
                 end: datetime, *, exclude_sensor: str | None = None,
                 kinds: Iterable[str] | None = None, limit: int = 500) -> list[Signal]:
    """Signals of `metric` within radius/time. Bounding box in SQL, haversine after."""
    dlat = radius_km / 111.0
    dlon = radius_km / max(1e-6, 111.0 * math.cos(math.radians(lat)))
    sql = """SELECT * FROM signals
             WHERE metric = ? AND observed_at BETWEEN ? AND ?
               AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
               AND quality != 'rejected'"""
    params: list[Any] = [metric, iso(start), iso(end), lat - dlat, lat + dlat, lon - dlon, lon + dlon]
    if exclude_sensor:
        sql += " AND sensor_id != ?"
        params.append(exclude_sensor)
    if kinds:
        ks = list(kinds)
        sql += f" AND sensor_kind IN ({','.join('?' * len(ks))})"
        params.extend(ks)
    sql += " ORDER BY observed_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        s = Signal.from_row(r)
        if s.location.has_point and geo.haversine_km(lat, lon, s.location.lat, s.location.lon) <= radius_km:
            out.append(s)
    return out


def recent_signals(hours: float, *, topic: str | None = None, kinds: Iterable[str] | None = None,
                   county_fips: str | None = None, metric: str | None = None,
                   metric_prefix: str | None = None, quality: Iterable[str] | None = None,
                   limit: int = 1000) -> list[Signal]:
    sql = "SELECT * FROM signals WHERE observed_at >= ?"
    params: list[Any] = [_since(hours)]
    if topic:
        sql += " AND topic = ?"
        params.append(topic)
    if kinds:
        ks = list(kinds)
        sql += f" AND sensor_kind IN ({','.join('?' * len(ks))})"
        params.extend(ks)
    if county_fips:
        sql += " AND county_fips = ?"
        params.append(county_fips)
    if metric:
        sql += " AND metric = ?"
        params.append(metric)
    if metric_prefix:
        sql += " AND metric LIKE ?"
        params.append(metric_prefix + "%")
    if quality:
        qs = list(quality)
        sql += f" AND quality IN ({','.join('?' * len(qs))})"
        params.extend(qs)
    sql += " ORDER BY observed_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [Signal.from_row(r) for r in rows]


def active_alert_signals(county_fips: str | None = None, now: datetime | None = None) -> list[Signal]:
    """Alert rows (metric 'alert.*') that haven't expired, one per (alert, county)."""
    now_iso = iso(now or utcnow())
    sql = """SELECT * FROM signals WHERE metric LIKE 'alert.%'
             AND (expires_at IS NULL OR expires_at >= ?)"""
    params: list[Any] = [now_iso]
    if county_fips:
        sql += " AND county_fips = ?"
        params.append(county_fips)
    sql += " ORDER BY value DESC, observed_at DESC LIMIT 500"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [Signal.from_row(r) for r in rows]


def signals_for_event(event_id: int) -> list[Signal]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE event_id = ? ORDER BY observed_at", (event_id,)
        ).fetchall()
    return [Signal.from_row(r) for r in rows]


def count_signals(hours: float, kinds: Iterable[str] | None = None,
                  quality: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM signals WHERE observed_at >= ?"
    params: list[Any] = [_since(hours)]
    if kinds:
        ks = list(kinds)
        sql += f" AND sensor_kind IN ({','.join('?' * len(ks))})"
        params.extend(ks)
    if quality:
        sql += " AND quality = ?"
        params.append(quality)
    with get_conn() as conn:
        return int(conn.execute(sql, params).fetchone()["n"])


def prune_reference_signals(days: int) -> int:
    """Drop old reference-sensor rows (stations/alerts/LSR); humans are kept."""
    with get_conn() as conn:
        cur = conn.execute(
            f"""DELETE FROM signals WHERE sensor_kind IN ({','.join('?' * len(REFERENCE_KINDS))})
                AND observed_at < ? AND event_id IS NULL""",
            (*REFERENCE_KINDS, _since(days * 24)),
        )
        return cur.rowcount


# ── events ────────────────────────────────────────────────────────────────

def find_open_event(topic: str, metric: str, county_fips: str | None, around: datetime,
                    within_hours: float) -> sqlite3.Row | None:
    """The open event for (topic, metric, county) live within ±within_hours of `around`."""
    lo = iso(around - timedelta(hours=within_hours))
    hi = iso(around + timedelta(hours=within_hours))
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM events WHERE topic = ? AND metric = ? AND status = 'open'
               AND county_fips IS ? AND updated_at >= ? AND opened_at <= ?
               ORDER BY updated_at DESC LIMIT 1""",
            (topic, metric, county_fips, lo, hi),
        ).fetchone()


def insert_event(**fields: Any) -> int:
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    with get_conn() as conn:
        cur = conn.execute(f"INSERT INTO events ({cols}) VALUES ({marks})", tuple(fields.values()))
        return int(cur.lastrowid)


def update_event(event_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE events SET {sets} WHERE id = ?", (*fields.values(), event_id))


def event_by_id(event_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def open_events() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE status = 'open' ORDER BY score DESC, updated_at DESC"
        ).fetchall()


def events_since(hours: float, limit: int = 50) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE updated_at >= ? ORDER BY score DESC, updated_at DESC LIMIT ?",
            (_since(hours), limit),
        ).fetchall()


def close_stale_events(idle_hours: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE events SET status = 'closed', closed_at = ? WHERE status = 'open' AND updated_at < ?",
            (utcnow_iso(), _since(idle_hours)),
        )
        return cur.rowcount


def event_attach_stats(event_id: int) -> dict[str, Any]:
    """Recompute n_signals / n_sensors / n_reference / mean_trust / peak from rows."""
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS n_signals, COUNT(DISTINCT s.sensor_id) AS n_sensors,
                       SUM(CASE WHEN s.sensor_kind IN ({','.join('?' * len(REFERENCE_KINDS))})
                           THEN 1 ELSE 0 END) AS n_reference,
                       AVG(COALESCE(se.trust, 0.5)) AS mean_trust,
                       MAX(s.value) AS peak_value, MIN(s.value) AS min_value
                FROM signals s LEFT JOIN sensors se ON se.id = s.sensor_id
                WHERE s.event_id = ?""",
            (*REFERENCE_KINDS, event_id),
        ).fetchone()
    return dict(row) if row else {}


# ── subscriptions ────────────────────────────────────────────────────────

def add_subscription(chat_id: str | int, category: str, area: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO subscriptions (chat_id, category, area) VALUES (?, ?, ?)",
            (str(chat_id), category, area),
        )
        return cur.rowcount > 0


def remove_subscription(chat_id: str | int, category: str | None = None,
                        area: str | None = None) -> int:
    sql = "DELETE FROM subscriptions WHERE chat_id = ?"
    params: list[Any] = [str(chat_id)]
    if category:
        sql += " AND category = ?"
        params.append(category)
    if area:
        sql += " AND area = ?"
        params.append(area)
    with get_conn() as conn:
        return conn.execute(sql, params).rowcount


def subscriptions_for(chat_id: str | int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT category, area, created_at FROM subscriptions WHERE chat_id = ? "
            "ORDER BY category, area",
            (str(chat_id),),
        ).fetchall()


def matching_chat_ids(category: str, area_keys: Iterable[str]) -> list[str]:
    """Distinct chats subscribed to `category` at any of the area keys, not banned."""
    keys = list(area_keys)
    if not keys:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT su.chat_id FROM subscriptions su
                LEFT JOIN sensors se ON se.chat_id = su.chat_id
                WHERE su.category = ? AND su.area IN ({','.join('?' * len(keys))})
                  AND COALESCE(se.status, 'active') != 'banned'""",
            (category, *keys),
        ).fetchall()
    return [r["chat_id"] for r in rows]


def subscription_counts() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(DISTINCT chat_id) AS n FROM subscriptions GROUP BY category"
        ).fetchall()
    return {r["category"]: r["n"] for r in rows}


# ── notify ledger ─────────────────────────────────────────────────────────

def already_notified(key: str, chat_id: str | int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM notify_log WHERE key = ? AND chat_id = ?", (key, str(chat_id))
        ).fetchone()
    return row is not None


def record_notification(key: str, chat_id: str | int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO notify_log (key, chat_id) VALUES (?, ?)", (key, str(chat_id))
        )


# ── digests ───────────────────────────────────────────────────────────────

def record_digest(date: str, *, drive_file_id: str | None, drive_url: str | None,
                  latest_url: str | None, folder_url: str | None, n_events: int, n_signals: int,
                  n_sensors: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO digests (date, drive_file_id, drive_url, latest_url, folder_url,
                                    n_events, n_signals, n_sensors)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET drive_file_id = excluded.drive_file_id,
                 drive_url = excluded.drive_url, latest_url = excluded.latest_url,
                 folder_url = excluded.folder_url, n_events = excluded.n_events,
                 n_signals = excluded.n_signals, n_sensors = excluded.n_sensors,
                 created_at = datetime('now')""",
            (date, drive_file_id, drive_url, latest_url, folder_url, n_events, n_signals, n_sensors),
        )


def latest_digest() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM digests ORDER BY date DESC LIMIT 1").fetchone()


def add_email_subscriber(email: str, chat_id: str | int | None) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO email_subscribers (email, chat_id) VALUES (?, ?)",
            (email.strip().lower(), str(chat_id) if chat_id is not None else None),
        )
        return cur.rowcount > 0


def email_subscribers() -> list[str]:
    with get_conn() as conn:
        return [r["email"] for r in conn.execute("SELECT email FROM email_subscribers").fetchall()]


# ── network statistics (mesh / coverage / vitals) ─────────────────────────

def mesh(hours: float, topic: str | None = None, *, human_only: bool = False,
         reference_only: bool = False) -> list[sqlite3.Row]:
    """County × metric aggregates over the window (the 'mesh' view)."""
    sql = """SELECT county_fips, metric, COUNT(*) AS n, COUNT(DISTINCT sensor_id) AS n_sensors,
                    SUM(CASE WHEN sensor_kind IN ('human', 'bot') THEN 1 ELSE 0 END) AS n_human,
                    AVG(value) AS mean_value, MAX(value) AS max_value, MIN(value) AS min_value,
                    MAX(observed_at) AS latest
             FROM signals WHERE observed_at >= ? AND county_fips IS NOT NULL
               AND metric NOT LIKE 'alert.%' AND quality != 'rejected'"""
    params: list[Any] = [_since(hours)]
    if topic:
        sql += " AND topic = ?"
        params.append(topic)
    if human_only:
        sql += " AND sensor_kind IN ('human', 'bot')"
    if reference_only:
        sql += " AND sensor_kind = 'station'"
    sql += " GROUP BY county_fips, metric ORDER BY n DESC"
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def county_activity(days: float, kinds: Iterable[str] | None = None) -> dict[str, int]:
    sql = """SELECT county_fips, COUNT(*) AS n FROM signals
             WHERE observed_at >= ? AND county_fips IS NOT NULL AND metric NOT LIKE 'alert.%'"""
    params: list[Any] = [_since(days * 24)]
    if kinds:
        ks = list(kinds)
        sql += f" AND sensor_kind IN ({','.join('?' * len(ks))})"
        params.extend(ks)
    sql += " GROUP BY county_fips"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {r["county_fips"]: r["n"] for r in rows}


def leaderboard(days: float, limit: int = 10) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT se.id, se.name, se.username, se.trust, se.county_fips,
                      COUNT(s.id) AS n, SUM(CASE WHEN s.quality = 'corroborated' THEN 1 ELSE 0 END) AS n_corr
               FROM sensors se JOIN signals s ON s.sensor_id = se.id
               WHERE se.kind IN ('human', 'bot') AND s.received_at >= ? AND se.status = 'active'
               GROUP BY se.id ORDER BY n_corr DESC, n DESC, se.trust DESC LIMIT ?""",
            (_since(days * 24), limit),
        ).fetchall()


def vitals() -> dict[str, Any]:
    with get_conn() as conn:
        def one(sql: str, *p: Any) -> Any:
            row = conn.execute(sql, p).fetchone()
            return row[0] if row else 0

        human = "sensor_kind IN ('human', 'bot')"
        out = {
            "sensors_total": one("SELECT COUNT(*) FROM sensors WHERE kind IN ('human','bot')"),
            "sensors_active_24h": one(
                "SELECT COUNT(DISTINCT sensor_id) FROM signals WHERE %s AND received_at >= ?" % human,
                _since(24)),
            "sensors_active_7d": one(
                "SELECT COUNT(DISTINCT sensor_id) FROM signals WHERE %s AND received_at >= ?" % human,
                _since(24 * 7)),
            "signals_24h_human": one(
                "SELECT COUNT(*) FROM signals WHERE %s AND received_at >= ?" % human, _since(24)),
            "signals_24h_reference": one(
                "SELECT COUNT(*) FROM signals WHERE sensor_kind IN ('station','official','authority') "
                "AND received_at >= ?", _since(24)),
            "corroborated_7d": one(
                "SELECT COUNT(*) FROM signals WHERE %s AND quality = 'corroborated' AND received_at >= ?"
                % human, _since(24 * 7)),
            "flagged_7d": one(
                "SELECT COUNT(*) FROM signals WHERE %s AND quality = 'flagged' AND received_at >= ?"
                % human, _since(24 * 7)),
            "human_7d": one(
                "SELECT COUNT(*) FROM signals WHERE %s AND received_at >= ?" % human, _since(24 * 7)),
            "events_open": one("SELECT COUNT(*) FROM events WHERE status = 'open'"),
            "events_24h": one("SELECT COUNT(*) FROM events WHERE updated_at >= ?", _since(24)),
            "alerts_active": one(
                "SELECT COUNT(DISTINCT group_key) FROM signals WHERE metric LIKE 'alert.%' "
                "AND (expires_at IS NULL OR expires_at >= ?)", utcnow_iso()),
            "subscriptions": one("SELECT COUNT(*) FROM subscriptions"),
            "subscribers": one("SELECT COUNT(DISTINCT chat_id) FROM subscriptions"),
        }
    out["counties_total"] = len(geo.counties())
    out["counties_covered_7d"] = len(county_activity(7, kinds=("human", "bot")))
    out["counties_reference_7d"] = len(county_activity(7))
    out["corroboration_rate_7d"] = (
        out["corroborated_7d"] / out["human_7d"] if out["human_7d"] else 0.0
    )
    return out


def stats_json() -> str:
    return json.dumps(vitals(), default=str)


# re-exported for callers that want a raw window helper
def window(hours: float) -> tuple[datetime, datetime]:
    now = utcnow()
    return now - timedelta(hours=hours), now


__all__ = [name for name in dir() if not name.startswith("_")]
