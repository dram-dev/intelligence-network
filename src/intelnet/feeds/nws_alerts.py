"""NWS active alerts for the state — one signal per (alert, county).

api.weather.gov/alerts/active?area=IL. Each alert carries SAME codes
(0 + county FIPS) so it maps straight onto the county hierarchy; a signal is
emitted per covered county so ZIP/county subscribers match naturally, all
rows sharing `group_key` = the alert id so the digest counts it once.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import requests

from intelnet import db, geo, network, subscriptions
from intelnet.config import settings
from intelnet.feeds.base import FeedResult, ReferenceFeed
from intelnet.models import KIND_AUTHORITY, Signal, parse_iso, utcnow

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}


def slug(event: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (event or "").lower()).strip("_")


def _sender_id(sender: str) -> str:
    return "nws:" + (re.sub(r"[^a-z0-9]+", "_", (sender or "nws").lower()).strip("_") or "nws")


def parse_alerts(payload: dict[str, Any], topic: str = "weather") -> list[Signal]:
    """GeoJSON FeatureCollection → signals (pure; no I/O)."""
    out: list[Signal] = []
    now = utcnow()
    for feat in payload.get("features") or []:
        p = feat.get("properties") or {}
        alert_id = p.get("id") or feat.get("id")
        if not alert_id:
            continue
        same_codes = ((p.get("geocode") or {}).get("SAME")) or []
        fips_list = [f for f in (geo.same_to_fips(s) for s in same_codes) if f]
        if not fips_list:
            continue
        sent = parse_iso(p.get("sent")) or now
        expires = parse_iso(p.get("ends")) or parse_iso(p.get("expires"))
        params = p.get("parameters") or {}
        sev = str(p.get("severity") or "Unknown")
        evidence = {
            "kind": "alert",
            "event": p.get("event"),
            "severity": sev,
            "urgency": p.get("urgency"),
            "certainty": p.get("certainty"),
            "sender": p.get("senderName"),
            "nws_headline": (params.get("NWSheadline") or [None])[0],
            "headline": p.get("headline"),
            "instruction": p.get("instruction"),
            "description": (p.get("description") or "")[:1500],
            "area_desc": p.get("areaDesc"),
            "url": p.get("@id") or f"https://api.weather.gov/alerts/{alert_id}",
            "message_type": p.get("messageType"),
        }
        sender_id = _sender_id(p.get("senderName") or "")
        for fips in dict.fromkeys(fips_list):
            c = geo.county(fips)
            if c is None:
                continue
            out.append(Signal(
                source="nws_alerts", source_id=f"{alert_id}|{fips}", sensor_id=sender_id,
                sensor_kind=KIND_AUTHORITY, topic=topic, metric=f"alert.{slug(p.get('event'))}",
                value=float(SEVERITY_RANK.get(sev, 0)), unit="", text=p.get("headline"),
                observed_at=sent, received_at=now, expires_at=expires,
                location=geo.location_from_county(c), confidence=1.0, quality="reference",
                group_key=str(alert_id), evidence=evidence,
                raw={"sent": p.get("sent"), "effective": p.get("effective"), "onset": p.get("onset")},
            ))
    return out


def fetch(state: str | None = None) -> dict[str, Any]:
    r = requests.get(
        "https://api.weather.gov/alerts/active",
        params={"area": state or settings.geo_state, "status": "actual",
                "message_type": "alert,update"},
        headers={"User-Agent": settings.nws_user_agent, "Accept": "application/geo+json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


class NWSAlertsFeed(ReferenceFeed):
    """NWS watches / warnings / advisories for the state, per county."""

    name = "nws_alerts"
    sensor_kind = KIND_AUTHORITY
    trust = 1.0
    doc = "NWS active alerts (api.weather.gov), one row per covered county"

    def fetch_signals(self) -> list[Signal]:
        signals = parse_alerts(fetch(), topic=self.topic)
        for sender in {s.sensor_id for s in signals}:
            db.ensure_reference_sensor(sender, self.sensor_kind, sender.split(":", 1)[-1].upper(),
                                       geo.Location(precision="state"), self.trust)
        return signals

    def after_store(self, new: list[Signal], res: FeedResult) -> None:
        # Assess (marks reference quality) then push once per alert with all its counties.
        by_group: dict[str, list[Signal]] = {}
        for sig in new:
            network.assess(sig)
            by_group.setdefault(sig.group_key or sig.key, []).append(sig)
        for _gid, sibs in by_group.items():
            res.alerts_pushed += subscriptions.fanout_alert(sibs[0], sibs)


def active_alert_groups(county_fips: str | None = None) -> list[dict[str, Any]]:
    """Active alerts collapsed to one entry per alert with the counties it covers."""
    groups: dict[str, dict[str, Any]] = {}
    for s in db.active_alert_signals(county_fips):
        g = groups.setdefault(s.group_key or s.key, {"signal": s, "counties": []})
        c = geo.county(s.location.county_fips)
        if c:
            g["counties"].append(c.name)
    out = list(groups.values())
    out.sort(key=lambda g: (-(g["signal"].value or 0), g["signal"].observed_at), reverse=False)
    return out


def _fmt_expiry(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%a %H:%MZ") if dt else "—"
