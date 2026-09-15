"""Demo seed — a plausible fortnight on the network, run through the real engine.

Everything goes through `network.process`, so corroboration, trust, events
and scores are the engine's own output on synthetic inputs (a central-Illinois
storm on day −3, ordinary readings around it, a few soil / water / crop / air
reports, and synthetic official rows: a Tornado Warning, storm reports, a
station gust). Used to build the site's sample snapshot and to try the
digest without waiting for real sensors. Deterministic (seeded).
"""
from __future__ import annotations

import random
from datetime import timedelta

from intelnet import db, geo, network
from intelnet.models import KIND_AUTHORITY, KIND_HUMAN, KIND_OFFICIAL, KIND_STATION, Sensor, Signal, utcnow

# (handle, zip5, first name) — ZIPs chosen across central / northern / southern IL
SENSORS = [
    ("tg:1001", "62704", "Ann"), ("tg:1002", "62711", "Bo"), ("tg:1003", "62702", "Cy"),
    ("tg:1004", "62629", "Dee"), ("tg:1005", "61801", "Eli"), ("tg:1006", "61820", "Fay"),
    ("tg:1007", "61761", "Gil"), ("tg:1008", "61701", "Hal"), ("tg:1009", "62526", "Ida"),
    ("tg:1010", "62521", "Jo"), ("tg:1011", "61614", "Kim"), ("tg:1012", "61604", "Lou"),
    ("tg:1013", "60601", "Max"), ("tg:1014", "60302", "Nia"), ("tg:1015", "60187", "Ole"),
    ("tg:1016", "60540", "Pam"), ("tg:1017", "61108", "Quin"), ("tg:1018", "61032", "Rae"),
    ("tg:1019", "62901", "Sal"), ("tg:1020", "62864", "Tia"), ("tg:1021", "62301", "Uma"),
    ("tg:1022", "61462", "Vic"), ("tg:1023", "62025", "Wes"), ("tg:1024", "62221", "Xia"),
    ("tg:1025", "61401", "Yan"), ("tg:1026", "60435", "Zed"), ("tg:1027", "61350", "Abe"),
    ("tg:1028", "61301", "Bea"), ("tg:1029", "62450", "Cal"), ("tg:1030", "61944", "Dot"),
    ("tg:1031", "60901", "Eve"), ("tg:1032", "61201", "Fin"), ("tg:1033", "62650", "Gus"),
    ("tg:1034", "61938", "Hope"), ("tg:1035", "62294", "Ivy"), ("tg:1036", "60548", "Jax"),
]

STORM_ZIPS = {"62704", "62711", "62702", "62629", "61801", "61820", "62526", "62521", "62650"}


def _sig(sensor: Sensor, metric: str, value: float, when, note: str | None = None, *,
         source: str = "telegram", conf: float = 1.0) -> Signal:
    from intelnet.topics import metric_by_key

    m = metric_by_key(metric)
    return Signal(
        source=source, source_id=f"demo:{sensor.id}:{metric}:{when.isoformat()}", sensor_id=sensor.id,
        sensor_kind=sensor.kind, topic=m.topic if m else "weather", metric=metric, value=value,
        unit=m.unit if m else "", text=note, observed_at=when, received_at=when, location=sensor.location,
        confidence=conf, evidence={"kind": m.kind if m else "numeric", "demo": True},
    )


def seed(days: int = 14, seed_value: int = 7) -> dict[str, int]:
    rng = random.Random(seed_value)
    db.init_db()
    now = utcnow()
    day0 = (now - timedelta(days=days - 1)).replace(hour=12, minute=0, second=0, microsecond=0)
    sensors: list[Sensor] = []
    for sid, z, name in SENSORS:
        loc = geo.location_from_zip(z)
        s = db.upsert_sensor(Sensor(id=sid, kind=KIND_HUMAN, name=name, chat_id=sid.split(":")[1],
                                    location=loc or geo.Location()))
        sensors.append(s)
    # reference sensors
    spi = geo.location_from_point(39.844, -89.678, online=False)
    db.ensure_reference_sensor("station:spi", KIND_STATION, "Springfield (SPI)", spi, 0.9)
    db.ensure_reference_sensor("lsr:ilx", KIND_OFFICIAL, "NWS ILX storm reports", geo.Location(), 0.95)
    db.ensure_reference_sensor("nws:nws_lincoln_il", KIND_AUTHORITY, "NWS LINCOLN IL", geo.Location(), 1.0)
    st_spi = db.get_sensor("station:spi")
    lsr = db.get_sensor("lsr:ilx")

    n = 0
    storm_day = days - 4
    for d in range(days):
        day = day0 + timedelta(days=d)
        for s in sensors:
            z = s.location.zip5
            active = rng.random() < (0.55 if z in STORM_ZIPS else 0.3)
            if not active:
                continue
            t = day + timedelta(minutes=rng.randint(-300, 420))
            # everyday weather
            temp_f = 68 + 14 * rng.random() + (6 if d > days - 6 else 0)
            for sig in (
                _sig(s, "temp_c", (temp_f - 32) * 5 / 9, t),
                _sig(s, "rain_mm", round(rng.choice([0, 0, 0.1, 0.3, 0.6]) * 25.4, 1), t + timedelta(minutes=5)),
            ):
                if network.process(sig):
                    n += 1
            # occasional other-topic reports
            r = rng.random()
            if r < 0.15:
                sig = _sig(s, "soil_moisture_pct", 14 + 20 * rng.random(), t + timedelta(minutes=10))
            elif r < 0.25:
                sig = _sig(s, "corn_stage", float(rng.choice([18, 19, 20, 20, 21])), t + timedelta(minutes=10))
            elif r < 0.32:
                sig = _sig(s, "stage_m", (3 + 4 * rng.random()) * 0.3048, t + timedelta(minutes=10))
            elif r < 0.37:
                sig = _sig(s, "pm25_ugm3", 6 + 18 * rng.random(), t + timedelta(minutes=10))
            elif r < 0.40:
                sig = _sig(s, "crop_condition", float(rng.choice([3, 4, 4, 5])), t + timedelta(minutes=10))
            else:
                sig = None
            if sig and network.process(sig):
                n += 1
        if d == storm_day:
            # the storm: hail + gusts across central IL, in a tight window
            t0 = day + timedelta(hours=5)
            for s in sensors:
                if s.location.zip5 not in STORM_ZIPS:
                    continue
                t = t0 + timedelta(minutes=rng.randint(0, 40))
                hail = rng.choice([25, 25, 32, 44, 44, 51])
                gust = rng.choice([24, 27, 29, 31, 34])
                for sig in (
                    _sig(s, "hail_mm", float(hail), t, "hail pounding the deck"),
                    _sig(s, "wind_gust_ms", float(gust), t + timedelta(minutes=3)),
                    _sig(s, "rain_mm", 30 + 25 * rng.random(), t + timedelta(minutes=40)),
                ):
                    if network.process(sig):
                        n += 1
                if rng.random() < 0.4:
                    if network.process(_sig(s, "wind_damage", 1.0, t + timedelta(minutes=6), "limbs down")):
                        n += 1
                if rng.random() < 0.25:
                    if network.process(_sig(s, "crop_damage", 1.0, t + timedelta(minutes=50), "corn flattened")):
                        n += 1
            # official support: a warning over Sangamon + Christian, LSR hail, a station gust
            for fips in ("17167", "17021", "17019"):
                c = geo.county(fips)
                db.insert_signal(Signal(
                    source="nws_alerts", source_id=f"demo-alert-1|{fips}", sensor_id="nws:nws_lincoln_il",
                    sensor_kind=KIND_AUTHORITY, topic="weather", metric="alert.severe_thunderstorm_warning",
                    value=3, text="Severe Thunderstorm Warning", observed_at=t0 - timedelta(minutes=10),
                    received_at=t0, expires_at=t0 + timedelta(hours=1),
                    location=geo.location_from_county(c), quality="reference", group_key="demo-alert-1",
                    evidence={"kind": "alert", "event": "Severe Thunderstorm Warning", "severity": "Severe",
                              "sender": "NWS Lincoln IL", "demo": True},
                ))
            if lsr:
                lsr_loc = geo.location_from_point(39.80, -89.64, online=False)
                lsr.location = lsr_loc
                if network.process(_sig(lsr, "hail_mm", 44.0, t0 + timedelta(minutes=25), "Golf ball hail — 2 N Springfield", source="iem_lsr")):
                    n += 1
            if st_spi and network.process(_sig(st_spi, "wind_gust_ms", 29.8, t0 + timedelta(minutes=12), source="iem_asos")):
                n += 1
        if d == days - 2:
            # a water event in the south + an air event in Chicago
            for s in sensors:
                if s.location.zip5 in ("62901", "62864") and network.process(
                    _sig(s, "dissolved_oxygen_mgl", 2.6, day + timedelta(hours=2), "fish gasping at the boat ramp")
                ):
                    n += 1
                if s.location.zip5 in ("60601", "60302") and network.process(
                    _sig(s, "aqi", 158.0, day + timedelta(hours=3), "smoke haze")
                ):
                    n += 1
        # station reference temps every day (Springfield)
        if st_spi and network.process(_sig(st_spi, "temp_c", 23 + 4 * rng.random(), day + timedelta(hours=1), source="iem_asos")):
            n += 1
    # subscriptions + a digest row so the site has links to show
    for s in sensors[:20]:
        db.add_subscription(s.chat_id or s.id, "weather.warnings", f"il.{geo.county(s.location.county_fips).slug}")
    for s in sensors[:12]:
        db.add_subscription(s.chat_id or s.id, "weather.digest", "il")
    for s in sensors[5:9]:
        db.add_subscription(s.chat_id or s.id, "soil.events", f"il.{geo.county(s.location.county_fips).slug}")
    network.close_stale_events()
    return {"sensors": len(sensors), "signals": n, "events": len(db.events_since(days * 24, limit=500))}
