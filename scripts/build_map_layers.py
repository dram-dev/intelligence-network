#!/usr/bin/env python3
"""Build the Illinois reference layer for the zoomed maps (the Mini App's "Now").

Towns, major rivers and the interstate / US-highway network, from the Census
Bureau's TIGERweb services (the same public source as the county lines), so a
map around someone's home shows where it is without asking a third-party tile
server for tiles near that home. Written once and committed:

    uv run python scripts/build_map_layers.py

→ site/assets/il-reference.json   {places: [[name, lat, lon, km²]], rivers: [...], roads: [...]}
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "assets" / "il-reference.json"
TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
STATE = "17"
ENVELOPE = {"xmin": -91.52, "ymin": 36.96, "xmax": -87.01, "ymax": 42.51, "spatialReference": {"wkid": 4326}}
SIMPLIFY_DEG = 0.004          # ~400 m: plenty for a county-scale map, small enough to ship
MIN_PLACE_KM2 = 1.0

# The rivers people in Illinois would name; each is many TIGER segments, merged by name.
RIVERS = ["Mississippi River", "Illinois River", "Ohio River", "Wabash River", "Rock River", "Fox River",
          "Kankakee River", "Des Plaines River", "DuPage River", "Vermilion River", "Sangamon River",
          "Kaskaskia River", "Big Muddy River", "Embarras River", "Little Wabash River", "Spoon River",
          "Mackinaw River", "Pecatonica River", "Kishwaukee River", "Iroquois River", "La Moine River",
          "Saline River", "Cache River", "Green River", "Edwards River", "Salt Fork Sangamon River",
          "Macoupin Creek", "Shoal Creek", "Chicago River", "Calumet River", "Salt Creek", "Skillet Fork",
          "Silver Creek", "Apple River", "Plum River", "Middle Fork Vermilion River", "Sangamon River South Fork"]
WIDE_RIVERS = ["Mississippi River", "Illinois River", "Ohio River", "Wabash River", "Rock River", "Fox River",
               "Kankakee River", "Des Plaines River", "Chicago River", "Calumet River", "Kaskaskia River"]


def query(path: str, **params) -> list[dict]:
    """An ArcGIS REST query, paged, returning features."""
    out, offset = [], 0
    while True:
        r = requests.get(f"{TIGER}/{path}/query", params={
            "f": "json", "outSR": 4326, "resultOffset": offset, "resultRecordCount": 2000, **params}, timeout=120)
        r.raise_for_status()
        d = r.json()
        if d.get("error"):
            raise RuntimeError(d["error"])
        feats = d.get("features") or []
        out += feats
        if not d.get("exceededTransferLimit") or not feats:
            return out
        offset += len(feats)


def abbreviate(name: str) -> str:
    for full, short in ((" River", " Riv"), (" Creek", " Crk"), ("Middle Fork ", "Middle Fk "),
                        ("Salt Fork ", "Salt Fk "), (" South Fork", " S Fk"), ("Skillet Fork", "Skillet Fk")):
        name = name.replace(full, short)
    return name


def lines(geom: dict) -> list[list[list[float]]]:
    return [[[round(x, 4), round(y, 4)] for x, y in path] for path in (geom or {}).get("paths") or [] if len(path) > 1]


def main() -> None:
    places = query("Places_CouSub_ConCity_SubMCD/MapServer/4", where=f"STATE='{STATE}'",
                   outFields="BASENAME,INTPTLAT,INTPTLON,AREALAND", returnGeometry="false")
    rows = sorted(([p["attributes"]["BASENAME"], round(float(p["attributes"]["INTPTLAT"]), 4),
                    round(float(p["attributes"]["INTPTLON"]), 4),
                    round(float(p["attributes"]["AREALAND"]) / 1e6, 1)] for p in places),
                  key=lambda r: -r[3])
    rows = [r for r in rows if r[3] >= MIN_PLACE_KM2]

    geo = {"geometry": json.dumps(ENVELOPE), "geometryType": "esriGeometryEnvelope", "inSR": 4326,
           "spatialRel": "esriSpatialRelIntersects", "maxAllowableOffset": SIMPLIFY_DEG}
    # TIGER abbreviates: "Sangamon Riv", "Macoupin Crk", "Salt Fk Sangamon Riv"
    tiger = {abbreviate(n): n for n in RIVERS}
    names = ",".join("'" + n.replace("'", "''") + "'" for n in tiger)
    river_feats = query("Hydro/MapServer/0", where=f"NAME IN ({names})", outFields="NAME", **geo)
    rivers = defaultdict(list)
    for f in river_feats:
        rivers[tiger.get(f["attributes"]["NAME"], f["attributes"]["NAME"])] += lines(f.get("geometry"))

    # Wide rivers are drawn as water areas in TIGER, not lines
    areal = {abbreviate(n): n for n in WIDE_RIVERS}
    names = ",".join("'" + n.replace("'", "''") + "'" for n in areal)
    water = defaultdict(list)
    for f in query("Hydro/MapServer/1", where=f"NAME IN ({names})", outFields="NAME", **geo):
        rings = [[[round(x, 4), round(y, 4)] for x, y in ring] for ring in (f.get("geometry") or {}).get("rings") or []
                 if len(ring) > 3]
        water[areal.get(f["attributes"]["NAME"], f["attributes"]["NAME"])] += rings

    roads = defaultdict(list)
    for layer in (2, 3):                      # primary roads; interstates and US highways
        for f in query(f"Transportation/MapServer/{layer}", where="RTTYP IN ('I','U')",
                       outFields="NAME,RTTYP", **geo):
            a = f["attributes"]
            roads[(a.get("NAME") or "", a.get("RTTYP") or "")] += lines(f.get("geometry"))

    doc = {"source": f"U.S. Census Bureau, TIGERweb ({datetime.now(timezone.utc).date().isoformat()})",
           "places": rows,
           "rivers": [{"name": n, "lines": ls} for n, ls in sorted(rivers.items()) if ls],
           "water": [{"name": n, "rings": rs} for n, rs in sorted(water.items()) if rs],
           "roads": [{"name": n, "type": t, "lines": ls} for (n, t), ls in sorted(roads.items()) if ls and n]}
    OUT.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}: {len(rows)} places, {len(doc['rivers'])} rivers, {len(doc['water'])} wide rivers, "
          f"{len(doc['roads'])} roads, {OUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
