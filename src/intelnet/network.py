"""The network engine — what makes many sensors worth more than their sum.

A mesonet's value is not any one station; it is that neighboring readings
check, sharpen and fill in for each other. This module does that for every
contribution:

* **Corroboration** — a new reading is compared with readings of the same
  metric from OTHER sensors nearby (the metric's `radius_km` / `window_min`)
  and judged compatible or not under the metric's tolerance. Compatible
  neighbors corroborate it; it corroborates them back (a raw report becomes
  corroborated the moment a second sensor agrees). Reference sensors —
  ASOS stations, NWS storm reports, and active NWS alerts that support the
  metric — settle agreement outright.
* **Trust** — each human sensor carries a credibility that moves with its
  corroboration record (shrunk toward the 0.5 prior with weight K, so a
  newcomer neither starts trusted nor is condemned by one bad report).
* **Events** — a reading past the metric's event threshold opens, or joins,
  an event for that county; the event's score multiplies severity by how
  many independent sensors, how trusted they are, and whether an official
  source agrees. Only corroborated / reference-backed / trusted events are
  pushed to `events` subscribers; everything still reaches `reports`.
* **Mesh / coverage** — county-level aggregates, coverage gaps ("sensors
  wanted") and a contributor leaderboard for the digest.

Nothing here knows about weather: metrics, tolerances and thresholds come
from the topic pack.
"""
from __future__ import annotations

import logging
import math
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from intelnet import db, geo
from intelnet.config import settings
from intelnet.models import (
    KIND_HUMAN,
    QUALITY_CORROBORATED,
    QUALITY_FLAGGED,
    QUALITY_RAW,
    QUALITY_REFERENCE,
    QUALITY_REJECTED,
    REFERENCE_KINDS,
    Signal,
    iso,
    utcnow,
)
from intelnet.topics import Metric, find_metric, get_topic, topics

logger = logging.getLogger(__name__)

TRUST_PRIOR = 0.5
TRUST_PRIOR_WEIGHT = 4.0          # K: how many outcomes it takes to move off the prior
TRUSTED_SENSOR = 0.8              # a sensor this trusted can push an event alone
EVENT_IDLE_CLOSE_HOURS = 6.0      # an event with no new signal this long is closed
ESCALATION_FACTOR = 1.4           # re-push when the score grows this much


@dataclass
class Assessment:
    signal: Signal
    metric: Metric | None
    corroborating: list[Signal] = field(default_factory=list)
    contradicting: list[Signal] = field(default_factory=list)
    reference: str = "none"                  # agree | disagree | none
    reference_signal: Signal | None = None
    quality: str = QUALITY_RAW
    event: dict[str, Any] | None = None
    event_opened: bool = False
    push_event: bool = False
    push_reason: str = ""
    trust_after: float | None = None

    @property
    def n_corroborating(self) -> int:
        return len({s.sensor_id for s in self.corroborating})

    @property
    def n_contradicting(self) -> int:
        return len({s.sensor_id for s in self.contradicting})


# ── trust ─────────────────────────────────────────────────────────────────

def trust_from_record(n_corroborated: int, n_contradicted: int, prior: float = TRUST_PRIOR) -> float:
    """Shrunk credibility: (K·prior + agreements) / (K + agreements + disagreements).

    `prior` is 0.5 for everyone unless the admin vouched (`/admin trust`), in
    which case the record shrinks toward that instead.
    """
    return (TRUST_PRIOR_WEIGHT * prior + n_corroborated) / (
        TRUST_PRIOR_WEIGHT + n_corroborated + n_contradicted
    )


# ── corroboration ─────────────────────────────────────────────────────────

def _closest_per_sensor(signals: list[Signal], ref: Signal) -> dict[str, Signal]:
    """One reading per neighboring sensor: the one nearest in time to `ref`."""
    best: dict[str, Signal] = {}
    for s in signals:
        cur = best.get(s.sensor_id)
        if cur is None or abs((s.observed_at - ref.observed_at).total_seconds()) < abs(
            (cur.observed_at - ref.observed_at).total_seconds()
        ):
            best[s.sensor_id] = s
    return best


def _alert_supports(metric: Metric, signal: Signal) -> Signal | None:
    """An active NWS alert over the signal's county that supports this metric."""
    topic = get_topic(metric.topic)
    slugs = topic.alert_support.get(metric.key) or []
    if not slugs or not signal.location.county_fips:
        return None
    for a in db.active_alert_signals(signal.location.county_fips, now=signal.observed_at):
        if a.metric.removeprefix("alert.") in slugs:
            return a
    return None


def corroborate(signal: Signal, metric: Metric) -> Assessment:
    """Compare a reading with its neighbors; does not write anything."""
    a = Assessment(signal=signal, metric=metric)
    if not signal.location.has_point:
        return a
    window = timedelta(minutes=metric.window_min)
    neighbors = db.signals_near(
        metric.key, signal.location.lat, signal.location.lon, metric.radius_km,
        signal.observed_at - window, signal.observed_at + window,
        exclude_sensor=signal.sensor_id,
    )
    for other in _closest_per_sensor(neighbors, signal).values():
        if other.value is None or signal.value is None:
            continue
        (a.corroborating if metric.compatible(signal.value, other.value) else a.contradicting).append(other)

    ref_agree = [s for s in a.corroborating if s.sensor_kind in REFERENCE_KINDS]
    ref_disagree = [s for s in a.contradicting if s.sensor_kind in REFERENCE_KINDS]
    if ref_agree:
        a.reference, a.reference_signal = "agree", ref_agree[0]
    elif ref_disagree:
        a.reference, a.reference_signal = "disagree", ref_disagree[0]
    else:
        alert = _alert_supports(metric, signal)
        if alert is not None:
            a.reference, a.reference_signal = "agree", alert
    return a


def judge_quality(a: Assessment) -> str:
    if a.signal.sensor_kind in REFERENCE_KINDS:
        return QUALITY_REFERENCE
    if a.metric is not None and a.signal.value is not None and not a.metric.in_range(a.signal.value):
        return QUALITY_REJECTED
    if a.n_corroborating >= 1 or a.reference == "agree":
        return QUALITY_CORROBORATED
    if a.reference == "disagree" or (a.n_contradicting >= 2 and a.n_corroborating == 0):
        return QUALITY_FLAGGED
    return QUALITY_RAW


# ── events ────────────────────────────────────────────────────────────────

def score_event(severity: float, n_sensors: int, mean_trust: float, n_reference: int,
                reference: str = "none") -> float:
    """severity × corroboration × trust × reference-agreement."""
    corroboration = 1.0 + 0.5 * math.log1p(max(0, n_sensors - 1))
    trust = min(2.0, max(0.4, (mean_trust or TRUST_PRIOR) / TRUST_PRIOR))
    if n_reference >= 1 or reference == "agree":
        ref = 1.25
    elif reference == "disagree":
        ref = 0.7
    else:
        ref = 1.0
    return round(severity * corroboration * trust * ref, 3)


def _event_title(metric: Metric, peak: float | None, county_fips: str | None) -> str:
    c = geo.county(county_fips)
    where = c.label if c else "unknown county"
    what = metric.label if metric.is_flag else f"{metric.label} {metric.display(peak)}"
    return f"{what} — {where}"


def _attach_to_event(a: Assessment) -> None:
    """Open or join the county event for this metric; rescore; decide on a push."""
    metric, signal = a.metric, a.signal
    if metric is None or signal.value is None or signal.id is None:
        return
    if not metric.is_event(signal.value):
        return
    window_h = max(3.0, metric.window_min / 60.0 * 2)
    fips = signal.location.county_fips
    # Anchor on the reading's own time, not the wall clock: a reading sent late
    # (or backfilled) joins the event that was live when it was observed.
    when = iso(signal.observed_at) or iso(utcnow())
    row = db.find_open_event(metric.topic, metric.key, fips, signal.observed_at, window_h)
    now = iso(utcnow())
    if row is None:
        event_id = db.insert_event(
            topic=metric.topic, metric=metric.key, county_fips=fips, zip5=signal.location.zip5,
            lat=signal.location.lat, lon=signal.location.lon, opened_at=when, updated_at=when,
            peak_value=signal.value, unit=metric.unit, status="open",
            title=_event_title(metric, signal.value, fips),
        )
        a.event_opened = True
    else:
        event_id = int(row["id"])
        when = max(str(row["updated_at"] or ""), when or "")
    db.update_assessment(
        signal.id, quality=a.quality, corroboration_n=a.n_corroborating,
        contradiction_n=a.n_contradicting, reference_agreement=a.reference, event_id=event_id,
    )
    stats = db.event_attach_stats(event_id)
    peak = stats.get("peak_value") if metric.event_direction == "above" else stats.get("min_value")
    peak = signal.value if peak is None else peak
    severity = metric.severity(peak)
    n_sensors = int(stats.get("n_sensors") or 1)
    n_reference = int(stats.get("n_reference") or 0)
    mean_trust = float(stats.get("mean_trust") or TRUST_PRIOR)
    score = score_event(severity, n_sensors, mean_trust, n_reference, a.reference)
    db.update_event(
        event_id, updated_at=when, peak_value=peak, n_signals=int(stats.get("n_signals") or 1),
        n_sensors=n_sensors, n_reference=n_reference, mean_trust=mean_trust, severity=severity,
        score=score, title=_event_title(metric, peak, fips),
    )
    ev = dict(db.event_by_id(event_id) or {})
    a.event = ev

    verified = n_sensors >= 2 or n_reference >= 1 or mean_trust >= TRUSTED_SENSOR or a.reference == "agree"
    pushed_score = ev.get("pushed_score")
    if score >= settings.event_push_min_score and verified:
        if pushed_score is None:
            a.push_event, a.push_reason = True, "new"
        elif score >= float(pushed_score) * ESCALATION_FACTOR:
            a.push_event, a.push_reason = True, "escalated"
    if a.push_event:
        db.update_event(event_id, pushed_at=now, pushed_score=score)
        a.event = dict(db.event_by_id(event_id) or {})


# ── the entry point ───────────────────────────────────────────────────────

def assess(signal: Signal) -> Assessment:
    """Corroborate, judge quality, update trust (both ways), attach to events.

    The signal must already be stored (it has an id). Reference signals skip
    corroboration but still drive events (an official tornado report IS an
    event) and lend their agreement to nearby human reports.
    """
    metric = find_metric(signal.metric, get_topic(signal.topic)) if not signal.metric.startswith("alert.") else None
    if metric is None:
        a = Assessment(signal=signal, metric=None)
        a.quality = QUALITY_REFERENCE if signal.sensor_kind in REFERENCE_KINDS else QUALITY_RAW
        if signal.id is not None:
            db.update_assessment(signal.id, quality=a.quality, corroboration_n=0, contradiction_n=0,
                                 reference_agreement="none")
        return a

    a = corroborate(signal, metric) if signal.sensor_kind not in REFERENCE_KINDS else Assessment(signal, metric)
    a.quality = judge_quality(a)
    if signal.id is not None:
        db.update_assessment(signal.id, quality=a.quality, corroboration_n=a.n_corroborating,
                             contradiction_n=a.n_contradicting, reference_agreement=a.reference)

    if signal.sensor_kind not in REFERENCE_KINDS:
        _update_trust(a)
        _corroborate_back(a)
    else:
        _corroborate_forward(signal, metric)

    _attach_to_event(a)
    return a


def _update_trust(a: Assessment) -> None:
    sensor = db.get_sensor(a.signal.sensor_id)
    if sensor is None:
        return
    corr = 1 if a.quality == QUALITY_CORROBORATED else 0
    contra = 1 if a.quality in (QUALITY_FLAGGED, QUALITY_REJECTED) else 0
    trust = trust_from_record(sensor.n_corroborated + corr, sensor.n_contradicted + contra,
                              sensor.trust_prior)
    db.bump_sensor(sensor.id, signals=1, corroborated=corr, contradicted=contra, trust=trust)
    a.trust_after = trust


def _corroborate_back(a: Assessment) -> None:
    """A raw neighbor becomes corroborated when this reading agrees with it."""
    for other in a.corroborating:
        if other.sensor_kind in REFERENCE_KINDS or other.id is None or other.quality != QUALITY_RAW:
            continue
        db.update_assessment(
            other.id, quality=QUALITY_CORROBORATED, corroboration_n=other.corroboration_n + 1,
            contradiction_n=other.contradiction_n, reference_agreement=other.reference_agreement,
        )
        s = db.get_sensor(other.sensor_id)
        if s and s.kind == KIND_HUMAN:
            db.bump_sensor(s.id, corroborated=1,
                           trust=trust_from_record(s.n_corroborated + 1, s.n_contradicted, s.trust_prior))


def _corroborate_forward(ref: Signal, metric: Metric) -> None:
    """A fresh reference reading settles nearby raw human readings it agrees with."""
    if not ref.location.has_point or ref.value is None:
        return
    window = timedelta(minutes=metric.window_min)
    for other in db.signals_near(
        metric.key, ref.location.lat, ref.location.lon, metric.radius_km,
        ref.observed_at - window, ref.observed_at + window, kinds=(KIND_HUMAN, "bot"),
    ):
        if other.id is None or other.value is None or other.quality != QUALITY_RAW:
            continue
        agree = metric.compatible(ref.value, other.value)
        db.update_assessment(
            other.id,
            quality=QUALITY_CORROBORATED if agree else QUALITY_FLAGGED,
            corroboration_n=other.corroboration_n + (1 if agree else 0),
            contradiction_n=other.contradiction_n + (0 if agree else 1),
            reference_agreement="agree" if agree else "disagree",
        )
        s = db.get_sensor(other.sensor_id)
        if s and s.kind == KIND_HUMAN:
            db.bump_sensor(
                s.id, corroborated=1 if agree else 0, contradicted=0 if agree else 1,
                trust=trust_from_record(s.n_corroborated + (1 if agree else 0),
                                        s.n_contradicted + (0 if agree else 1), s.trust_prior),
            )


def process(signal: Signal) -> Assessment | None:
    """Store a signal (dedup by source/source_id) and assess it. None if duplicate."""
    stored = db.insert_signal(signal)
    if stored is None:
        return None
    return assess(stored)


def close_stale_events(idle_hours: float = EVENT_IDLE_CLOSE_HOURS) -> int:
    return db.close_stale_events(idle_hours)


# ── views ─────────────────────────────────────────────────────────────────

def near(location: geo.Location, hours: float = 3.0, radius_km: float = 30.0) -> dict[str, Any]:
    """What the network sees around a point: readings, alerts, open events."""
    out: dict[str, Any] = {"location": location, "hours": hours, "radius_km": radius_km,
                           "readings": {}, "alerts": [], "events": []}
    if not location.has_point:
        return out
    start, end = db.window(hours)
    seen_keys: set[str] = set()
    for topic in topics().values():
        for metric in topic.metrics.values():
            rows = db.signals_near(metric.key, location.lat, location.lon, radius_km, start, end)
            if rows:
                out["readings"][metric.key] = rows
    if location.county_fips:
        for a in db.active_alert_signals(location.county_fips):
            if a.group_key not in seen_keys:
                seen_keys.add(a.group_key or a.key)
                out["alerts"].append(a)
    for ev in db.open_events():
        if ev["lat"] is not None and ev["lon"] is not None and geo.haversine_km(
            location.lat, location.lon, ev["lat"], ev["lon"]
        ) <= radius_km * 2:
            out["events"].append(dict(ev))
    return out


def coverage_gaps(days: float = 7) -> list[geo.County]:
    """Counties with no human contribution in the window — 'sensors wanted'."""
    active = db.county_activity(days, kinds=(KIND_HUMAN, "bot"))
    return [c for c in sorted(geo.counties().values(), key=lambda c: c.name) if c.fips not in active]


def mesh_rows(hours: float = 24, topic: str | None = None) -> list[dict[str, Any]]:
    """County × metric aggregates with display strings for the digest."""
    out = []
    for r in db.mesh(hours, topic):
        m = find_metric(r["metric"])
        c = geo.county(r["county_fips"])
        out.append({
            "county": c.label if c else r["county_fips"],
            "county_fips": r["county_fips"],
            "topic": m.topic if m else None,
            "metric": m.label if m else r["metric"],
            "metric_key": r["metric"],
            "n": r["n"], "n_sensors": r["n_sensors"], "n_human": r["n_human"],
            "mean": m.display(r["mean_value"]) if m and not m.is_flag else "—",
            "max": m.display(r["max_value"]) if m and not m.is_flag else f"{r['n']} reports",
            "latest": r["latest"],
        })
    return out


def source_link(row: sqlite3.Row | None) -> str | None:
    """Where to read more about a signal — its own URL, or its station's page.

    NWS alerts, the Drought Monitor and USGS gauges carry a URL. Storm reports and
    airport stations don't, so we point at the Iowa State pages that show them.
    """
    if row is None:
        return None
    if row["url"]:
        return str(row["url"])
    evidence = json.loads(row["evidence_json"] or "{}")
    if row["source"] == "iem_lsr" and evidence.get("wfo"):
        return f"https://mesonet.agron.iastate.edu/lsr/#{evidence['wfo']}"
    if row["source"] == "iem_asos" and evidence.get("station"):
        return ("https://mesonet.agron.iastate.edu/sites/site.php?station="
                f"{evidence['station']}&network={settings.geo_state}_ASOS")
    if row["source"] == "nrcs_scan" and evidence.get("station"):
        site = str(evidence["station"]).split(":")[0]      # station ids arrive as "2004:IL:SCAN"
        return f"https://wcc.sc.egov.usda.gov/nwcc/site?sitenum={site}"
    return None


def event_summary(ev: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    e = dict(ev)
    m = find_metric(e["metric"])
    c = geo.county(e.get("county_fips"))
    return {
        **e,
        "metric_label": m.label if m else e["metric"],
        "peak_display": m.display(e.get("peak_value")) if m else str(e.get("peak_value")),
        "county_label": c.label if c else "—",
        "verified": (e.get("n_sensors") or 0) >= 2 or (e.get("n_reference") or 0) >= 1
        or (e.get("mean_trust") or 0) >= TRUSTED_SENSOR,
        "source_url": source_link(db.event_source_signal(e["id"])) if e.get("id") else None,
    }
