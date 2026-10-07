"""AirNow: the EPA's hourly monitor values become the air topic's official readings."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

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


def test_air_reads_on_the_aqi_scale():
    from intelnet.topics import find_metric

    pm, o3 = find_metric("pm25_ugm3"), find_metric("ozone_ppb")
    assert [pm.aqi_of(v) for v in (5, 9.0, 35.5, 45, 62.1, 130)] == [28, 50, 101, 124, 156, 205]   # EPA's examples
    assert pm.display(45) == "45 µg/m³ (AQI 124, unhealthy for sensitive groups)"
    assert pm.display(45, aqi=False) == "45 µg/m³" and o3.display(72) == "72 ppb (AQI 104, unhealthy for sensitive groups)"
    assert find_metric("rain_mm").display(25.4) == "1.00 in"                     # no AQI scale, no words


def test_air_on_the_map_only_when_it_matters(fresh_db):
    from intelnet import notable

    def hour(pm25: str) -> list:
        known = airnow.parse_sites(SITES, "17")
        known["171670013"] = ["SPFLD_PH", 39.80, -89.60]                       # three PM2.5 sites: a field
        known["170310001"] = ["ALSIP", 41.67, -87.73]
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        rows = [f"x|x|170191001|BONDVILLE|-6|PM2.5|UG/M3|{pm25}|EPA", "x|x|171670013|SPFLD_PH|-6|PM2.5|UG/M3|8.2|EPA",
                "x|x|170310001|ALSIP|-6|PM2.5|UG/M3|7.5|EPA"]
        return airnow.parse_hourly("\n".join(rows), now - timedelta(hours=2), known)

    db.insert_signals(hour("15.3"))                                             # moderate: no news
    assert not [p for p in notable.build()["places"] if p["label"].startswith("PM2.5")]
    db.insert_signals([replace(s, source_id=s.source_id + "b") for s in hour("45.0") if s.value == 45.0])
    [air] = [p for p in notable.build()["places"] if p["label"].startswith("PM2.5")]
    assert air["label"] == "PM2.5 45 µg/m³" and air["name"] == "Champaign County air monitor"
    assert any("Highest PM2.5 in Illinois" in w and "(AQI 124, unhealthy for sensitive groups)" in w for w in air["why"])
