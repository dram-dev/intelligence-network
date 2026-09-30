"""The Telegram Mini App's link: the page on GitHub Pages plus the chat's own state.

The app (site/app.fragment.html → docs/app/) has no server to ask who is looking, so
the bot hands it the chat's state in the link's #fragment, which browsers never send
to the server: home, subscriptions, the latest reports and how each was checked,
trust by topic. `focus` opens it on one NWS alert (the alert card's Map button).
"""
from __future__ import annotations

import base64
import json

from intelnet import db, feedback, geo, trust
from intelnet.config import settings
from intelnet.topics import find_metric, get_topic

APP_BUTTON = "🗺 Map · report · settings"
APP_REPORTS = 6


def app_state(chat_id: str | int) -> dict:
    """What the Mini App shows about this chat: home, subscriptions, recent reports and
    how each was checked, trust by topic. It rides in the app URL's #fragment, which the
    browser never sends to the site's server."""
    sensor = db.sensor_by_chat(chat_id)
    home = None
    if sensor and sensor.location.has_point:
        c = geo.county(sensor.location.county_fips)
        home = {"zip": sensor.location.zip5, "fips": sensor.location.county_fips,
                "slug": c.slug if c else None, "label": sensor.location.describe(),
                "lat": round(sensor.location.lat, 3), "lon": round(sensor.location.lon, 3)}
    reports = []
    for s in db.sensor_signals(sensor.id, APP_REPORTS) if sensor else []:
        m = find_metric(s.metric, get_topic(s.topic))
        reports.append({"m": m.label if m else s.metric,
                        "v": "" if m is None or m.is_flag or s.value is None
                        else m.display(s.value).split(" (")[0].replace(" pct", "%"),
                        "t": s.observed_at.isoformat(timespec="minutes"), "q": s.quality,
                        "r": s.reference_agreement, "n": s.corroboration_n,
                        "g": (s.evidence.get("grid") or {}).get("verdict")})
    return {"v": 1, "home": home,
            "subs": [[r["category"], r["area"], _area_label(r["area"])]
                     for r in db.subscriptions_for(str(chat_id))],
            "reports": reports,
            "trust": [[get_topic(st.topic).label, st.label] for st in trust.standings(sensor.id)] if sensor else [],
            "followups": feedback.enabled_for(chat_id)}


def app_url(chat_id: str | int, *, focus: str | None = None, tab: str | None = None) -> str | None:
    """The Mini App's address for this chat (https only: Telegram requires it)."""
    site = settings.public_site_url
    if not site.startswith("https://"):
        return None
    blob = json.dumps(app_state(chat_id), separators=(",", ":"), ensure_ascii=False).encode()
    url = f"{site}app/#s={base64.urlsafe_b64encode(blob).decode().rstrip('=')}"
    if focus:
        url += "&focus=" + focus
    if tab:
        url += "&tab=" + tab
    return url


def _area_label(area: str) -> str:
    from intelnet import subscriptions  # subscriptions uses this module for its Map button

    return subscriptions.area_label(area)


