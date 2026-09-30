"""Trust v2: a record per topic that fades, says how sure it is, and counts witnesses once.

    trust = (K·prior + agreements) / (K + agreements + disagreements)

is still the formula (a Beta(K·prior, K·(1−prior)) prior updated by outcomes), with
four changes:

- per topic: a good hail spotter is not automatically a good soil sampler, so each
  (sensor, topic) keeps its own record. `sensors.trust` is the pooled figure;
- it fades: agreements and disagreements lose half their weight every
  HALF_LIFE_DAYS, so last year's record matters less than last month's;
- it says how sure it is: `standing()` gives an 80% interval, and a record with
  fewer than NEW_BELOW outcomes reads "new" instead of a number that looks exact;
- witnesses count once: agreement from someone at the same spot (a household, a
  shared roof), or from someone this sensor already agrees with all the time, is
  worth less than agreement from an independent neighbor (`independence`).

Outcomes are weighted: an official reference counts 1, a radar grid GRID_WEIGHT.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from statistics import NormalDist

from intelnet import db, geo
from intelnet.models import REFERENCE_KINDS, Signal, iso, parse_iso, utcnow

K = 4.0                       # how many outcomes it takes to move off the prior
PRIOR = 0.5
TRUSTED = 0.8                 # a sensor this trusted can push an event alone
HALF_LIFE_DAYS = 180.0
NEW_BELOW = 3                 # fewer outcomes than this: "new", not a number
SAME_PLACE_KM = 0.2           # closer than this: one household, not two witnesses
SAME_PLACE_WEIGHT = 0.3
GRID_WEIGHT = 0.5             # a radar grid is half a reference
_Z80 = NormalDist().inv_cdf(0.9)


def shrunk(agree: float, disagree: float, prior: float = PRIOR) -> float:
    return (K * prior + agree) / (K + agree + disagree)


def _faded(value: float, since: datetime | None, now: datetime) -> float:
    if not since or value <= 0:
        return value
    days = max(0.0, (now - since).total_seconds() / 86400)
    return value * 0.5 ** (days / HALF_LIFE_DAYS)


@dataclass
class Standing:
    topic: str
    trust: float
    low: float
    high: float
    checks: int

    @property
    def is_new(self) -> bool:
        return self.checks < NEW_BELOW

    @property
    def label(self) -> str:
        if self.is_new:
            return f"new · {self.checks} check{'s' if self.checks != 1 else ''}"
        return f"{self.trust:.2f} ({self.low:.2f}–{self.high:.2f})"


def _standing(topic: str, agree: float, disagree: float, checks: int, prior: float) -> Standing:
    a, b = K * prior + agree, K * (1 - prior) + disagree
    mean = a / (a + b)
    sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
    return Standing(topic, mean, max(0.0, mean - _Z80 * sd), min(1.0, mean + _Z80 * sd), checks)


def standing(sensor_id: str, topic: str, *, now: datetime | None = None) -> Standing:
    """How far the network trusts this sensor on this topic, and how sure it is."""
    now = now or utcnow()
    s = db.get_sensor(sensor_id)
    prior = s.trust_prior if s else PRIOR
    row = db.trust_row(sensor_id, topic)
    if row is None:
        return _standing(topic, 0.0, 0.0, 0, prior)
    at = parse_iso(row["updated_at"])
    return _standing(topic, _faded(row["agree"], at, now), _faded(row["disagree"], at, now),
                     int(row["checks"]), prior)


def standings(sensor_id: str) -> list[Standing]:
    """Every topic the sensor has a record in, most-checked first."""
    rows = sorted(db.trust_rows(sensor_id), key=lambda r: -int(r["checks"]))
    return [standing(sensor_id, r["topic"]) for r in rows]


def overall(sensor_id: str, *, now: datetime | None = None) -> Standing:
    """The pooled record across topics (what `sensors.trust` holds)."""
    now = now or utcnow()
    s = db.get_sensor(sensor_id)
    agree = disagree = 0.0
    checks = 0
    for r in db.trust_rows(sensor_id):
        at = parse_iso(r["updated_at"])
        agree += _faded(r["agree"], at, now)
        disagree += _faded(r["disagree"], at, now)
        checks += int(r["checks"])
    return _standing("*", agree, disagree, checks, s.trust_prior if s else PRIOR)


def record(sensor_id: str, topic: str, *, agree: float = 0.0, disagree: float = 0.0,
           now: datetime | None = None) -> float:
    """Add an outcome to a sensor's record on a topic. Returns the topic trust.

    Reference sensors keep the trust their feed gives them; only people and bots
    earn a record."""
    s = db.get_sensor(sensor_id)
    if s is None or s.kind in REFERENCE_KINDS or (agree <= 0 and disagree <= 0):
        return s.trust if s else PRIOR
    now = now or utcnow()
    row = db.trust_row(sensor_id, topic)
    at = parse_iso(row["updated_at"]) if row else None
    a = _faded(row["agree"], at, now) + agree if row else agree
    d = _faded(row["disagree"], at, now) + disagree if row else disagree
    db.save_trust_row(sensor_id, topic, a, d, (int(row["checks"]) if row else 0) + 1, iso(now) or "")
    db.bump_sensor(sensor_id, trust=overall(sensor_id, now=now).trust)
    return shrunk(a, d, s.trust_prior)


def _spot(sig: Signal) -> tuple[float, float] | None:
    """A reading's exact spot, or None when it's only a ZIP / county center: everyone
    who set `/home 62704` shares that center without sharing a roof."""
    loc = sig.location
    return (loc.lat, loc.lon) if loc.precision == "point" and loc.has_point else None


def independence(mine: Signal, other: Signal) -> float:
    """How much another reading's agreement is worth as a second witness (0–1).

    Official and station readings are independent by construction. Two people at
    the same spot are one household; two who agree every time are one voice.
    """
    if other.sensor_kind in REFERENCE_KINDS or mine.sensor_kind in REFERENCE_KINDS:
        return 1.0
    weight = 1.0
    a, b = _spot(mine), _spot(other)
    if a and b and geo.haversine_km(*a, *b) < SAME_PLACE_KM:
        weight = SAME_PLACE_WEIGHT
    together = db.pair_agreements(mine.sensor_id, other.sensor_id)
    return min(weight, 1.0 / (1.0 + together / 3.0))


def witnesses(signals: list[Signal]) -> int:
    """Independent witnesses among these readings: people within SAME_PLACE_KM of each
    other count once; every reference sensor counts."""
    spots: list[tuple[float, float]] = []
    seen: set[str] = set()
    n = 0
    for s in signals:
        if s.sensor_id in seen:
            continue
        seen.add(s.sensor_id)
        here = _spot(s)
        if s.sensor_kind in REFERENCE_KINDS or here is None:
            n += 1
            continue
        if any(geo.haversine_km(*here, *p) < SAME_PLACE_KM for p in spots):
            continue
        spots.append(here)
        n += 1
    return n
