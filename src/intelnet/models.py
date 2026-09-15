"""Shared shapes: Signal (the unit of the common data language), Sensor.

A Signal is ONE observation from ONE sensor: a metric + canonical value (or a
flag), where and when, with provenance and the network's assessment of it.
Every source — a person on Telegram, an ASOS station, an NWS alert, a storm
report — produces the same shape, which is what lets the corroboration engine
and the digest treat them uniformly.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelnet.geo import Location

# quality lifecycle: raw → corroborated | flagged | rejected ; reference = official feed
QUALITY_RAW = "raw"
QUALITY_CORROBORATED = "corroborated"
QUALITY_FLAGGED = "flagged"
QUALITY_REJECTED = "rejected"
QUALITY_REFERENCE = "reference"

# sensor kinds
KIND_HUMAN = "human"          # a person on Telegram (or the CLI)
KIND_BOT = "bot"              # an automated contributor posting JSON
KIND_STATION = "station"      # ASOS/AWOS observation station
KIND_OFFICIAL = "official"    # NWS storm report / product
KIND_AUTHORITY = "authority"  # NWS alert issuer

REFERENCE_KINDS = (KIND_STATION, KIND_OFFICIAL, KIND_AUTHORITY)


def public_handle(sensor_id: str) -> str:
    """Stable public handle for a sensor (`s-3f9a1`) — what anything public shows."""
    return "s-" + hashlib.sha1(sensor_id.encode()).hexdigest()[:5]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Sensor:
    id: str                      # 'tg:<chat_id>' | 'station:KSPI' | 'lsr:ILX' | 'nws:alerts'
    kind: str = KIND_HUMAN
    name: str = ""
    chat_id: str | None = None
    username: str | None = None
    location: Location = field(default_factory=Location)
    trust: float = 0.5
    trust_prior: float = 0.5     # admin-settable; the record shrinks toward this
    n_signals: int = 0
    n_corroborated: int = 0
    n_contradicted: int = 0
    status: str = "active"       # active | banned
    registered_at: str | None = None
    last_seen_at: str | None = None

    @property
    def is_reference(self) -> bool:
        return self.kind in REFERENCE_KINDS


@dataclass
class Signal:
    source: str                  # 'telegram' | 'cli' | 'nws_alerts' | 'iem_lsr' | 'iem_asos'
    source_id: str               # unique within source
    sensor_id: str
    sensor_kind: str
    topic: str
    metric: str                  # canonical metric key (or 'alert.<slug>')
    value: float | None = None
    unit: str = ""
    text: str | None = None      # free-text note / headline
    observed_at: datetime = field(default_factory=utcnow)
    received_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None
    location: Location = field(default_factory=Location)
    confidence: float = 1.0      # sensor-declared (0–1)
    quality: str = QUALITY_RAW
    corroboration_n: int = 0
    contradiction_n: int = 0
    reference_agreement: str = "none"   # agree | disagree | none
    group_key: str | None = None        # one alert → many county rows share this
    evidence: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    event_id: int | None = None
    id: int | None = None

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.source}::{self.source_id}".encode()).hexdigest()[:16]

    def to_row(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "source_id": self.source_id,
            "sensor_id": self.sensor_id,
            "sensor_kind": self.sensor_kind,
            "topic": self.topic,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "text": self.text,
            "observed_at": iso(self.observed_at),
            "received_at": iso(self.received_at),
            "expires_at": iso(self.expires_at),
            "lat": self.location.lat,
            "lon": self.location.lon,
            "geohash": self.location.geohash,
            "county_fips": self.location.county_fips,
            "zip5": self.location.zip5,
            "zip9": self.location.zip9,
            "precision": self.location.precision,
            "confidence": self.confidence,
            "quality": self.quality,
            "corroboration_n": self.corroboration_n,
            "contradiction_n": self.contradiction_n,
            "reference_agreement": self.reference_agreement,
            "group_key": self.group_key,
            "evidence_json": json.dumps(self.evidence, default=str),
            "raw_json": json.dumps(self.raw, default=str),
            "event_id": self.event_id,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Signal":
        r = dict(row)
        loc = Location(
            lat=r.get("lat"), lon=r.get("lon"), county_fips=r.get("county_fips"),
            zip5=r.get("zip5"), zip9=r.get("zip9"), precision=r.get("precision") or "unknown",
        )
        return cls(
            id=r.get("id"),
            source=r["source"], source_id=r["source_id"], sensor_id=r["sensor_id"],
            sensor_kind=r["sensor_kind"], topic=r["topic"], metric=r["metric"],
            value=r.get("value"), unit=r.get("unit") or "", text=r.get("text"),
            observed_at=parse_iso(r.get("observed_at")) or utcnow(),
            received_at=parse_iso(r.get("received_at")) or utcnow(),
            expires_at=parse_iso(r.get("expires_at")),
            location=loc, confidence=r.get("confidence") or 1.0,
            quality=r.get("quality") or QUALITY_RAW,
            corroboration_n=r.get("corroboration_n") or 0,
            contradiction_n=r.get("contradiction_n") or 0,
            reference_agreement=r.get("reference_agreement") or "none",
            group_key=r.get("group_key"),
            evidence=json.loads(r.get("evidence_json") or "{}"),
            raw=json.loads(r.get("raw_json") or "{}"),
            event_id=r.get("event_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["observed_at"] = iso(self.observed_at)
        d["received_at"] = iso(self.received_at)
        d["expires_at"] = iso(self.expires_at)
        d["location"] = self.location.to_dict()
        return d
