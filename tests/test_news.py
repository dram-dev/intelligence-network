"""Reading list: per-feed filters, the shipped feed config, NWS statements."""
from __future__ import annotations

import re

from intelnet import db
from intelnet.ingest import news, nws_statements
from intelnet.ingest.base import IngestedItem

PNS_TEXT = """
000
NOUS43 KILX 111613
PNSILX
ILZ027>031-036>038-111715-

Public Information Statement
National Weather Service Lincoln IL
1113 AM CDT Fri Sep 11 2026

...NWS Damage Survey for 09/10/2026 Toluca Tornado...

.Toluca Tornado...

Rating:                 EFU
Path Length /statute/:  0.57 miles
Start Location:         2 SE Toluca / Marshall County / IL
"""

LISTING = {
    "id": "0d424a86-c57c-4998-be9a-94438ca7778e",
    "issuanceTime": "2026-09-11T16:13:00+00:00",
    "wmoCollectiveId": "NOUS43",
    "issuingOffice": "KILX",
    "productName": "Public Information Statement",
}


def _item(feed: str, title: str, content: str = "") -> IngestedItem:
    return IngestedItem(source="news", source_id=f"{feed}:{title}", title=title,
                        content=content, metadata={"feed": feed})


def test_include_and_exclude_gate_only_their_own_feed():
    feeds = [{"name": "Agency", "url": "u1", "include": "pesticide|drought"},
             {"name": "Action Day", "url": "u2", "exclude": "currently no Air Pollution Action Day"},
             {"name": "Open", "url": "u3"}]
    placeholder = _item("Action Day", "Chicago, IL - Action Day",
                        "There are currently no Air Pollution Action Day alerts for Chicago, IL")
    real = _item("Action Day", "Chicago, IL - Action Day", "Ozone Action Day in effect Tuesday")
    items = [_item("Agency", "State fair track schedules"),
             _item("Agency", "Pesticide misuse case closed"),
             _item("Agency", "County designations", "drought disaster in 12 counties"),
             placeholder, real,
             _item("Open", "Anything at all")]
    kept = news.apply_filters(items, feeds)
    assert [i.title for i in kept] == ["Pesticide misuse case closed", "County designations",
                                       "Chicago, IL - Action Day", "Anything at all"]
    assert kept[2] is real                      # the placeholder copy was the one dropped


def test_items_from_an_unknown_feed_pass_through():
    assert len(news.apply_filters([_item("Not in config", "x")], [])) == 1


def test_shipped_feed_config_is_wellformed():
    feeds = news.feed_list()
    assert len(feeds) >= 40
    names = [f["name"] for f in feeds]
    assert len(names) == len(set(names))
    for f in feeds:
        assert f["url"].startswith("https://"), f
        assert f.get("topic_hint") in {"weather", "water", "soil", "agriculture", "air"}, f
        for key in ("include", "exclude"):
            if f.get(key):
                re.compile(f[key])
    # the weather.gov office pages parse but carry no items — the nws_statements
    # ingestor covers those offices instead
    assert not [f for f in feeds if "rss_page.php" in f["url"]]


def test_statement_becomes_an_item_with_its_headline_and_a_readable_link():
    item = nws_statements.to_item("ILX", LISTING, PNS_TEXT)
    assert item.title == "NWS Damage Survey for 09/10/2026 Toluca Tornado (NWS Lincoln)"
    assert (item.source, item.source_id) == ("nws_statements", LISTING["id"])
    assert item.url.endswith("p.php?pid=202609111613-KILX-NOUS43-PNSILX")
    assert item.published_at is not None and item.published_at.day == 11
    assert item.metadata["topic_hint"] == "weather" and item.metadata["office"] == "ILX"
    assert "Marshall County" in item.content


def test_statement_without_a_headline_falls_back_to_product_name_and_api_url():
    item = nws_statements.to_item("LOT", {"id": "x", "productName": "Public Information Statement"},
                                  "no dotted headline here")
    assert item.title == "Public Information Statement (NWS Chicago)"
    assert item.url == "https://api.weather.gov/products/x"


def test_fetch_skips_stored_products_and_survives_one_office_failing(fresh_db, monkeypatch):
    def listing(office: str) -> list[dict]:
        if office == "LOT":
            raise RuntimeError("503 from the API")
        return [dict(LISTING, id=f"{office}-1"), dict(LISTING, id=f"{office}-2")]

    monkeypatch.setattr(nws_statements, "fetch_listing", listing)
    monkeypatch.setattr(nws_statements, "fetch_text", lambda pid: f"...Storm total report {pid}...")
    db.upsert_items([nws_statements.to_item("ILX", dict(LISTING, id="ILX-1"), PNS_TEXT)])

    items = nws_statements.NWSStatementsIngestor().fetch()
    ids = [i.source_id for i in items]
    assert "ILX-1" not in ids and "ILX-2" in ids          # already stored is skipped
    assert not [i for i in ids if i.startswith("LOT")]    # failed office skipped
    assert len(ids) == 7                                  # 4 offices × 2, less the stored one


def test_shipped_action_day_filter_drops_every_placeholder_wording():
    """The "no alerts" boilerplate is worded differently per forecast area."""
    feeds = news.feed_list()
    feed = next(f["name"] for f in feeds if f["name"].startswith("Air quality"))
    items = [
        _item(feed, "Chicago, IL - Air Pollution Action Day Notification",
              "There are currently no Air Pollution Action Day alerts for Chicago, IL"),
        _item(feed, "Peoria, IL - Action Day Notification",
              "There are currently no Action Day alerts for Peoria, IL"),
        _item(feed, "Champaign, IL -  Notification", "There are currently no alerts for Champaign, IL"),
        _item(feed, "Chicago, IL - Air Pollution Action Day Notification",
              "Ozone Action Day in effect for Tuesday"),
    ]
    kept = news.apply_filters(items, feeds)
    assert [i.content for i in kept] == ["Ozone Action Day in effect for Tuesday"]


def test_headline_spans_wrapped_lines_and_ignores_later_sections():
    text = ("...Highest Observed Rainfall from September 8th into the Morning of\n"
            "September 9th...\n\nVolunteer observer reports are through 7 A.M.\n\n...Illinois...\n")
    assert nws_statements.headline(text) == (
        "Highest Observed Rainfall from September 8th into the Morning of September 9th")
