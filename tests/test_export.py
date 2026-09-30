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
    assert {t["name"] for t in snap["topics"]} == {"weather", "soil", "water", "agriculture",
                                                   "air", "quake", "nature", "markets"}
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
    # the page draws its map from inlined geography: 102 county shapes and every ZIP
    data = json.loads(html.split("window.NETWORK_DATA = ", 1)[1].split(";</script>", 1)[0].replace("<\\/", "</"))
    assert len(data["boundaries"]["features"]) == 102 and data["zips"]["62704"][0] == "17167"
    assert "boundaries" not in {p.stem for p in files}                   # page furniture, not public data


def test_snapshot_says_when_each_feed_last_worked(fresh_db):
    db.log_run(run_type="watch", source="nws_alerts", items_fetched=3, items_new=1, duration_ms=5, status="ok")
    db.log_run(run_type="watch", source="iem_lsr", items_fetched=0, items_new=0, duration_ms=5,
               status="error", error="timeout")
    snap = export.snapshot(days=1)
    assert set(snap["feeds"]) == {"nws_alerts"} and snap["feeds"]["nws_alerts"].endswith("+00:00")


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


def test_link_preview_tags_get_absolute_urls_and_assets_ship(fresh_db, tmp_path: Path, monkeypatch):
    """Messaging apps need absolute og: URLs and the card sitting next to the page."""
    from intelnet.config import settings

    monkeypatch.setattr(settings, "site_url", "https://example.test/net/")
    snap = export.snapshot(days=1)
    out = export.build_site(snap, out=tmp_path / "index.html")
    html = out.read_text(encoding="utf-8")
    assert "{{SITE_URL}}" not in html
    assert '<meta property="og:image" content="https://example.test/net/assets/og.png">' in html
    assert '<meta property="og:url" content="https://example.test/net/">' in html
    assert '<link rel="apple-touch-icon" href="assets/apple-touch-icon.png">' in html
    assert 'name="theme-color" media="(prefers-color-scheme: dark)"' in html
    # every head tag has to be on its own line, or build_site can't hoist it
    assert '<meta name="twitter:card" content="summary_large_image">' in html.split("</head>")[0]

    copied = {p.name for p in export.copy_assets(tmp_path)}
    assert {"icon.svg", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "og.png",
            "site.webmanifest"} <= copied
    assert (tmp_path / "assets" / "og.png").stat().st_size > 10_000


def test_copy_assets_is_a_no_op_without_an_assets_dir(tmp_path: Path):
    assert export.copy_assets(tmp_path, site_dir=tmp_path / "empty-site") == []


def test_every_county_gets_a_page_and_the_sitemap_lists_them(fresh_db, tmp_path: Path):
    snap = export.snapshot(days=1)
    pages = export.render_county_pages(snap, tmp_path)
    assert len(pages) == 102
    html = (tmp_path / "county" / "sangamon.html").read_text(encoding="utf-8")
    assert "<title>Sangamon County, Illinois" in html and "{{" not in html
    assert "start=sub_weather_warnings_sangamon" in html and "start=sub_weather_digest_il" in html
    assert "--accent:" in html                                         # the front page's tokens
    assert 'href="menard.html"' in html                               # a neighbor
    data = json.loads(html.split("window.COUNTY_DATA = ", 1)[1].split(";</script>", 1)[0].replace("<\\/", "</"))
    assert data["fips"] == "17167" and data["shapes"]["features"][0]["properties"]["fips"] == "17167"
    sitemap = export.write_sitemap(snap, tmp_path, pages).read_text(encoding="utf-8")
    assert sitemap.count("<url>") == 1 + len(export.STATIC_PAGES) + 102 and "county/sangamon.html" in sitemap
    assert "Sitemap:" in (tmp_path / "robots.txt").read_text(encoding="utf-8")
