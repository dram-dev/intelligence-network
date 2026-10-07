"""AirNow: the EPA's hourly monitor values become the air topic's official readings."""
from __future__ import annotations

from datetime import datetime, timezone

from intelnet import db
from intelnet.feeds import airnow

SITES = "\n".join([
    "StationID|AQSID|FullAQSID|Parameter|MonitorType|SiteCode|SiteName|Status|AgencyID|AgencyName|EPARegion|"
    "Latitude|Longitude|Elevation|GMTOffset|CountryFIPS|CBSA_ID|CBSA_Name|StateAQSCode|StateAbbreviation|"
    "CountyAQSCode|CountyName",
    "170191001|170191001|840170191001|PM2.5|Permanent|1001|BONDVILLE|Active|IL1|Illinois EPA|R5|40.052241|"
    "-88.372549||-6.00|US|||17|IL|019|CHAMPAIGN",
    "170191001|170191001|840170191001|O3|Permanent|1001|BONDVILLE|Active|IL1|Illinois EPA|R5|40.052241|"
    "-88.372549||-6.00|US|||17|IL|019|CHAMPAIGN",
    "171670012|171670012|840171670012|O3|Permanent|0012|SPFD_IB|Active|IL1|Illinois EPA|R5|39.831522|"
    "-89.640926||-6.00|US|||17|IL|167|SANGAMON",
    "180890022|180890022|840180890022|PM2.5|Permanent|0022|Gary|Active|IN1|Indiana|R5|41.606667|-87.304722||"
    "-6.00|US|||18|IN|089|LAKE",
])
HOUR = datetime(2026, 10, 7, 14, tzinfo=timezone.utc)


def _hourly(pm25: str = "62.1") -> str:
    return "\n".join([
        f"10/07/26|14:00|170191001|BONDVILLE|-6|PM2.5|UG/M3|{pm25}|Illinois EPA",
        "10/07/26|14:00|170191001|BONDVILLE|-6|OZONE|PPB|41|Illinois EPA",
        "10/07/26|14:00|170191001|BONDVILLE|-6|CO|PPM|0.1|Illinois EPA",              # not a pack measure
        "10/07/26|14:00|171670012|SPFD_IB|-6|OZONE|PPB|-999|Illinois EPA",             # missing
        "10/07/26|14:00|180890022|Gary|-6|PM2.5|UG/M3|80|Indiana",                     # not Illinois
    ])


def test_the_states_monitors_and_their_pm25_and_ozone():
    known = airnow.parse_sites(SITES, "17")
    assert set(known) == {"170191001", "171670012"} and known["171670012"][0] == "SPFD_IB"
    sigs = airnow.parse_hourly(_hourly(), HOUR, known)
    assert [(s.metric, s.value) for s in sigs] == [("pm25_ugm3", 62.1), ("ozone_ppb", 41.0)]
    pm = sigs[0]
    assert pm.sensor_id == "airnow:170191001" and pm.location.county_fips == "17019"
    assert pm.location.label == "Champaign County air monitor"            # not the agency's code
    assert pm.observed_at == datetime(2026, 10, 7, 15, tzinfo=timezone.utc)          # the hour's end
    assert pm.source_id == "170191001|PM2.5|2026100714" and pm.evidence["kind"] == "monitor"


def test_an_unhealthy_hour_reaches_air_subscribers_and_a_sensitive_one_does_not(fresh_db, sent, monkeypatch):
    db.add_subscription("70", "air.events", "il.champaign")
    monkeypatch.setattr(airnow, "fetch_sites", lambda: SITES)
    monkeypatch.setattr(airnow, "fetch", lambda: (HOUR, _hourly("40.2")))           # sensitive groups
    res = airnow.AirNowFeed().run()
    assert (res.status, res.new, res.events_pushed) == ("ok", 2, 0) and sent == []
    db.kv_set(airnow.KV_LAST_POLL, "")
    later = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(airnow, "fetch", lambda: (later, _hourly("62.1").replace("14:00", "15:00")))
    res = airnow.AirNowFeed().run()
    assert res.events_pushed >= 1 and {c for c, _ in sent} == {"70"}                   # unhealthy: pushed
    assert db.reference_network_sizes().get("airnow") == 1
