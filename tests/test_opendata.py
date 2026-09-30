"""Open data out: GeoJSON and CAP feeds, the researcher's database, nightly backups."""
from __future__ import annotations

import gzip
import json
import sqlite3
import xml.etree.ElementTree as ET

from intelnet import backup, contrib, db, export, opendata
from intelnet.config import settings

CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"


def _verified(make_sensor):
    """Two people agree on golf-ball hail in Sangamon: a verified event."""
    ann = make_sensor("tg:1", zip_code="62704")
    ann.location = __import__("intelnet.geo", fromlist=["x"]).location_from_point(39.78123, -89.65432, online=False)
    db.upsert_sensor(ann)
    bob = make_sensor("tg:2", name="Bob", zip_code="62711")
    for s, text, base in ((ann, "hail golf ball -- on my deck at 12 Elm St", "m1"), (bob, "hail 1.75in", "m2")):
        contrib.contribute(s, text, source_id_base=base, online=False, use_llm=False)


def test_the_event_feeds_publish_zip_centres_never_a_persons_point(fresh_db, make_sensor, monkeypatch):
    monkeypatch.setattr(settings, "site_url", "https://example.test/net/")
    _verified(make_sensor)
    [f] = opendata.events_geojson()["features"]
    lon, lat = f["geometry"]["coordinates"]
    assert (lat, lon) != (39.78123, -89.65432) and f["properties"]["zip5"] and f["properties"]["sensors"] == 2
    assert f["properties"]["url"] == "https://example.test/net/county/sangamon.html"
    [st] = opendata.storms_geojson()["features"]
    assert st["properties"]["brief"] and st["geometry"]["coordinates"] == [lon, lat]


def test_the_cap_feed_is_well_formed_cap_1_2(fresh_db, make_sensor):
    _verified(make_sensor)
    root = ET.fromstring(opendata.cap_atom())
    [alert] = root.iter(f"{CAP}alert")
    assert [c.tag.replace(CAP, "") for c in alert][:6] == ["identifier", "sender", "sent", "status", "msgType", "scope"]
    assert alert.findtext(f"{CAP}sent").endswith("-00:00") and alert.findtext(f"{CAP}status") == "Actual"
    info = alert.find(f"{CAP}info")
    assert info.findtext(f"{CAP}category") == "Met" and info.findtext(f"{CAP}certainty") == "Observed"
    assert "2 people" in info.findtext(f"{CAP}description")
    area = info.find(f"{CAP}area")
    assert area.findtext(f"{CAP}areaDesc") == "Sangamon County, IL"
    assert [g.findtext(f"{CAP}value") for g in area.iter(f"{CAP}geocode")] == ["017167", "017167"]
    assert "39.78123" not in ET.tostring(alert, encoding="unicode")


def test_the_research_database_keeps_people_anonymous(fresh_db, make_sensor, tmp_path):
    _verified(make_sensor)
    path = opendata.write_sqlite(tmp_path / "network.sqlite")
    con = sqlite3.connect(path)
    rows = con.execute("SELECT sensor, sensor_kind, zip5, lat, lon, quality FROM readings ORDER BY id").fetchall()
    assert [r[0][:2] for r in rows] == ["s-", "s-"] and all(r[3] is None and r[4] is None for r in rows)
    everything = "\n".join(str(r) for t in ("readings", "events", "storms", "sensors")
                           for r in con.execute(f"SELECT * FROM {t}"))
    for private in ("tg:1", "Ann", "Bob", "Elm St", "39.78123"):
        assert private not in everything
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM counties").fetchone()[0] == 102


def test_the_export_writes_every_open_data_file(fresh_db, make_sensor, tmp_path):
    _verified(make_sensor)
    res = export.export_all(tmp_path, days=1)
    assert res["opendata"]["sqlite"]
    for name in ("feeds/events.geojson", "feeds/storms.geojson", "feeds/cap.atom", "data/network.sqlite",
                 "data/metadata.json"):
        assert (tmp_path / name).exists(), name
    meta = json.loads((tmp_path / "data" / "metadata.json").read_text())
    assert set(meta["databases"]["network"]["tables"]) >= {"readings", "events", "storms"}


def test_nightly_backups_are_gzipped_snapshots_and_rotate(fresh_db, tmp_path, make_sensor):
    make_sensor("tg:1", zip_code="62704")
    for day in range(9):
        (tmp_path / f"network-2026-09-{day + 10:02d}.db.gz").write_bytes(b"old")
    out = backup.run(tmp_path, keep=7)
    assert len(list(tmp_path.glob("network-*.db.gz"))) == 7 and out.exists()
    (tmp_path / "restored.db").write_bytes(gzip.decompress(out.read_bytes()))
    con = sqlite3.connect(tmp_path / "restored.db")
    assert con.execute("SELECT id FROM sensors").fetchone()[0] == "tg:1"
