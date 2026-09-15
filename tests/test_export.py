"""Public snapshot + site build: anonymised, complete, and the page inlines it."""
from __future__ import annotations

import json
from pathlib import Path

from intelnet import db, demo, export


def test_snapshot_is_public_by_construction(fresh_db):
    out = demo.seed(days=5)
    assert out["sensors"] == 36 and out["signals"] > 100
    snap = export.snapshot(days=5, sample=True)
    assert snap["sample"] and snap["state"] == "IL" and snap["vitals"]["sensors_total"] == 36
    assert len(snap["counties"]) == 102 and sum(c["human"] for c in snap["counties"]) > 0
    assert len(snap["activity"]) == 5 and set(snap["activity"][0]) >= {"date", "weather", "soil", "reference", "alerts"}
    assert {t["name"] for t in snap["topics"]} == {"weather", "soil", "water", "agriculture", "air"}
    assert snap["sources"] and all("url" in s for s in snap["sources"])
    # no identity leaks: handles only, no names / ZIP+4 / coordinates for humans
    text = json.dumps(snap)
    for leak in ('"Ann"', '"Bo"', '"Cy"', "tg:1001", "62704-", '"lat": 39.77'):
        assert leak not in text, leak
    sensors = [n for n in snap["graph"]["nodes"] if n["kind"] == "sensor"]
    assert sensors and all(n["label"].startswith("s-") and "lat" not in n for n in sensors)
    assert any(link["kind"] == "corroborated" for link in snap["graph"]["links"])
    assert snap["events"] and any(e["verified"] for e in snap["events"])
    assert snap["leaderboard"] and snap["leaderboard"][0]["handle"].startswith("s-")


def test_write_json_and_build_site(fresh_db, tmp_path: Path):
    demo.seed(days=3)
    snap = export.snapshot(days=3, sample=True)
    files = export.write_json(snap, tmp_path / "data")
    assert {p.name for p in files} >= {"network.json", "graph.json", "counties.json", "events.json", "sources.json"}
    frag = tmp_path / "frag.html"
    frag.write_text("<title>T</title>\n<link rel=\"stylesheet\" href=\"x\">\n<style>b{}</style>\n"
                    "<main>hi</main>\n<script>window.NETWORK_DATA = /*__NETWORK_DATA__*/null;</script>\n",
                    encoding="utf-8")
    out = export.build_site(snap, fragment=frag, out=tmp_path / "index.html")
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "<title>T</title>" in html.split("</head>")[0]
    assert "window.NETWORK_DATA = {" in html and '"sample": true' in html.replace('"sample":true', '"sample": true')
    assert "<\\/" in html or "</" not in json.dumps(snap)      # script-safe escaping
    body = export.artifact_fragment(snap, fragment=frag)
    assert body.startswith("<title>") and "window.NETWORK_DATA = {" in body


def test_export_all_without_fragment(fresh_db, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(export, "FRAGMENT", tmp_path / "missing.html")
    res = export.export_all(tmp_path / "docs", days=2, site=True)
    assert len(res["json"]) == 10 and "site" not in res
    assert db.vitals()["sensors_total"] == 0


def test_policy_pages_are_filled_in(fresh_db, tmp_path: Path, monkeypatch):
    from intelnet.config import settings

    monkeypatch.setattr(settings, "network_contact_email", "network@example.org")
    snap = export.snapshot(days=1)
    written = export.render_static_pages(snap, tmp_path, site_dir=export.SITE_DIR)
    assert {p.name for p in written} == {"privacy.html", "terms.html"}
    for p in written:
        html = p.read_text(encoding="utf-8")
        assert "{{" not in html and "network@example.org" in html and "@intelligence_network_bot" in html
    privacy = (tmp_path / "privacy.html").read_text(encoding="utf-8")
    assert "Limited Use" in privacy and "drive.file" in privacy and "/forget confirm" in privacy
    assert "Not an official warning service" in (tmp_path / "terms.html").read_text(encoding="utf-8")
    assert snap["site_url"] == "https://dram-dev.github.io/intelligence-network/"


def test_drive_links_hidden_while_drive_is_off(fresh_db, monkeypatch):
    from intelnet.config import settings

    db.record_digest("2026-09-15", drive_file_id="d", drive_url="https://docs.google.com/document/d/d/edit",
                     latest_url="https://docs.google.com/document/d/l/edit",
                     folder_url="https://drive.google.com/drive/folders/f", n_events=0, n_signals=0, n_sensors=0)
    off = export.snapshot(days=1)
    assert off["links"] == {"folder": None, "latest": None, "digest": None}
    assert off["digests"][0]["date"] == "2026-09-15" and off["digests"][0]["drive_url"] is None
    monkeypatch.setattr(settings, "gdrive_enabled", True)
    on = export.snapshot(days=1)
    assert on["links"]["latest"].endswith("/l/edit") and on["digests"][0]["drive_url"].endswith("/d/edit")
