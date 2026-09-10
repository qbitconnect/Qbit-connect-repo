"""Actor Platform spec §7 + §39 — parser unit tests for the five new actors.

Pure-function tests over deterministic fixture HTML (spec §39: 'Create
deterministic fixtures where live websites are unstable'). No network, no
DB — layer-1 parser verification only.
"""

from __future__ import annotations

import json

import pytest
from bs4 import BeautifulSoup

from app.scrapers.actors.indiamart.parser import parse_search_page, parse_supplier_page
from app.scrapers.actors.instagram.parser import (
    parse_embedded_posts,
    parse_follow_counters,
    parse_hashtag_page,
)
from app.scrapers.actors.instagram.parser import parse_profile as ig_parse_profile
from app.scrapers.actors.justdial.parser import (
    build_search_url,
    decode_mobilesv,
    parse_business_page,
    parse_listing_page,
)
from app.scrapers.actors.linkedin.parser import parse_company
from app.scrapers.actors.linkedin.parser import parse_profile as li_parse_profile
from app.scrapers.actors.meta_ads_library.parser import (
    ad_content_hash,
    build_search_url as build_ads_url,
    parse_ads,
)


# ===================================================================== Instagram
IG_PROFILE = """
<html><head>
<title>Fixture Brand (@fixturebrand) • Instagram photos and videos</title>
<meta property="og:title" content="Fixture Brand (@fixturebrand) • Instagram photos and videos">
<meta property="og:description" content="123K Followers, 45 Following, 678 Posts - Retail shop in Pune. Contact us at shop@fixturebrand.test">
<meta property="og:url" content="https://www.instagram.com/fixturebrand/">
</head><body><span class="bio">Retail shop in Pune</span></body></html>
"""


def test_instagram_follow_counters():
    counters = parse_follow_counters("123K Followers, 45 Following, 678 Posts")
    assert counters == {"followers": 123000, "following": 45, "posts": 678}


def test_instagram_profile_parse_public_surface():
    record = ig_parse_profile(IG_PROFILE, username="fixturebrand", source_url="https://www.instagram.com/fixturebrand/")
    assert record is not None
    assert record["business_name"] == "Fixture Brand"
    meta = record["metadata"]
    assert meta["followers"] == 123000
    assert meta["posts_count"] == 678
    assert "Pune" in meta["biography"]
    # public contact pattern inside og:description
    assert record["email"] == "shop@fixturebrand.test"


def test_instagram_embedded_posts():
    blob = {
        "edge_owner_to_timeline_media": {
            "edges": [
                {"node": {
                    "shortcode": "ABC123", "id": "991",
                    "edge_media_to_caption": {"edges": [{"node": {"text": "New stock #fixtures"}}]},
                    "edge_media_preview_like": {"count": 34}, "edge_media_to_comment": {"count": 2},
                    "__typename": "GraphImage",
                }}
            ]
        }
    }
    posts = parse_embedded_posts([blob], username="fixturebrand", cap=10)
    assert len(posts) == 1
    assert posts[0]["metadata"]["post_id"] == "991"
    assert "#fixtures" in posts[0]["metadata"]["caption"]
    assert posts[0]["metadata"]["likes"] == 34


def test_instagram_hashtag_page():
    html = """
    <html><head>
    <meta property="og:title" content="#fixturetag on Instagram">
    <script type="application/ld+json">{"@type":"CollectionPage","interactionStatistic":{"userInteractionCount":4321}}</script>
    </head><body><h1>#fixturetag</h1></body></html>
    """
    record = parse_hashtag_page(html, tag="fixturetag", source_url="https://www.instagram.com/explore/tags/fixturetag/")
    assert record["business_name"] == "#fixturetag"
    assert record["metadata"]["media_count"] == 4321


# ================================================================= Meta Ads
# The embedded JSON is built in Python so the fixture is guaranteed-valid JSON
# (exactly the shape Meta's public Ad Library scripts serve).
_ADS_PAYLOAD = {
    "__bbox": {"require": [["RelayPrefetchedStreamCache", [], {"ad_library_root_query": {"result": {"data": {"ads": [
        {"ad_archive_id": "1001", "snapshot": {
            "body": "Fixture Diwali sale", "cta_text": "Shop now",
            "link_url": "https://fixture.example/landing",
            "original_image_url": "https://fixture.example/a.jpg",
            "page": {"name": "Fixture Advertiser"}, "startDate": "2026-01-01"},
            "platforms": ["facebook", "instagram"]},
        {"ad_archive_id": "1002", "snapshot": {
            "body": "Fixtures restock", "cta_text": "Learn more",
            "page": {"name": "Fixture Advertiser"}},
            "platforms": ["instagram"]},
    ]}}}}]]}}
ADS_HTML = (
    "<html><head><title>Ad Library | Meta</title></head><body>"
    "<script>window.__initialData = " + json.dumps(_ADS_PAYLOAD) + ";</script>"
    "</body></html>"
)


def test_meta_ads_parse():
    ads, block = parse_ads(ADS_HTML, cap=10)
    assert block is None
    assert len(ads) == 2
    first = ads[0]
    assert first["ad_id"] == "1001"
    assert first["page_name"] == "Fixture Advertiser"
    assert "Diwali" in first["body"]
    assert first["cta"] == "Shop now"
    assert first["landing_url"] == "https://fixture.example/landing"
    assert "facebook" in first["platforms"]
    assert "fixture.example/a.jpg" in first["image_urls"][0]
    assert first["archive_url"].endswith("?id=1001")
    assert len(first["content_hash"]) == 16


def test_meta_ads_content_hash_stability():
    a = {"body": "x", "cta": "y", "landing_url": "z", "image_urls": [], "platforms": ["facebook"]}
    b = {"landing_url": "z", "body": "x", "platforms": ["facebook"], "cta": "y", "image_urls": []}
    assert ad_content_hash(a) == ad_content_hash(b)


def test_meta_ads_login_wall_detected():
    ads, block = parse_ads("<html><body>Please verify you are human - captcha</body></html>", cap=5)
    assert ads == [] and block in ("captcha", "please verify")


def test_meta_ads_search_url_builder():
    url = build_ads_url(keyword="fixtures", country="IN", active_status="active")
    assert url.startswith("https://www.facebook.com/ads/library/?")
    assert "active_status=active" in url and "country=IN" in url and "q=fixtures" in url


# ================================================================= LinkedIn
LD_COMPANY = """
<html><head>
<title>Fixture Labs on LinkedIn</title>
<meta property="og:title" content='Fixture Labs on LinkedIn: "Building test widgets since 2019"'>
<meta property="og:description" content="Fixture Labs manufactures test widgets. 1,234 employees on LinkedIn.">
</head><body></body></html>
"""

LD_PROFILE = """
<html><head>
<meta property="og:title" content='Ada Fixture on LinkedIn: "Head of Procurement at Fixture Labs"'>
<meta property="og:description" content="Head of Procurement at Fixture Labs. Bengaluru.">
</head><body></body></html>
"""


def test_linkedin_company_parse():
    record = parse_company(LD_COMPANY, source_url="https://www.linkedin.com/company/fixture-labs/")
    assert record["business_name"] == "Fixture Labs"
    assert "test widgets" in record["metadata"]["tagline"]
    assert record["metadata"]["employees_hint"] == 1234


def test_linkedin_profile_parse():
    record = li_parse_profile(LD_PROFILE, source_url="https://www.linkedin.com/in/ada-fixture/")
    assert record["business_name"] == "Ada Fixture"
    assert "Head of Procurement" in record["metadata"]["headline"]


# ================================================================= JustDial
JD_LISTING = """
<html><body>
<div class="cntanr">
  <h2 class="business-name"><span class="lng_cont_name">Fixture Traders</span></h2>
  <span class="cont_sw_addr">12 MG Road, Bengaluru 560001</span>
  <span class="total_rate">4.5</span>
  <span class="rating_count">123 Votes</span>
  <a class="business-name" href="/Bengaluru/Fixture-Traders-BBBB123"></a>
  <a href="tel:+919876543210"></a>
</div>
<div class="cntanr">
  <h2 class="business-name"><span class="lng_cont_name">Second Traders</span></h2>
</div>
</body></html>
"""

JD_DETAIL = """
<html><head>
<script type="application/ld+json">{"@type":"LocalBusiness","name":"Fixture Traders",
 "telephone":"080 1234 5678","address":{"streetAddress":"12 MG Road","addressLocality":"Bengaluru","postalCode":"560001"}}</script>
<h1>Fixture Traders</h1>
<span class="mobilesv icon-ak"></span><span class="mobilesv icon-ih"></span><span class="mobilesv icon-ji"></span>
<span class="mobilesv icon-ih"></span><span class="mobilesv icon-hg"></span><span class="mobilesv icon-gf"></span>
<span class="mobilesv icon-fe"></span><span class="mobilesv icon-ed"></span><span class="mobilesv icon-dc"></span>
<span class="mobilesv icon-cb"></span><span class="mobilesv icon-ba"></span><span class="mobilesv icon-ji"></span>
<span class="mobilesv icon-aj"></span>
</body></html>
"""


def test_justdial_search_url_builder():
    assert build_search_url("new delhi", "electricians") == "https://www.justdial.com/New-Delhi/Electricians"
    assert (
        build_search_url("delhi", "electricians", "emergency")
        == "https://www.justdial.com/Delhi/Electricians-Emergency"
    )


def test_justdial_mobilesv_decode():
    soup = BeautifulSoup(JD_DETAIL, "html.parser")
    # ak=+, ih=9, ji=1, ih=9, hg=8, gf=7, fe=6, ed=5, dc=4, cb=3, ba=2, ji=1, aj=0
    assert decode_mobilesv(soup) == "+919876543210"


def test_justdial_mobilesv_unknown_glyph_never_guessed():
    html = '<span class="mobilesv icon-zz"></span><span class="mobilesv icon-ji"></span>'
    assert decode_mobilesv(BeautifulSoup(html, "html.parser")) is None


def test_justdial_listing_parse():
    records = parse_listing_page(JD_LISTING, source_url="https://www.justdial.com/Bengaluru/Traders", city_hint="Bengaluru", cap=10)
    assert len(records) == 2
    first = records[0]
    assert first["business_name"] == "Fixture Traders"
    assert first["phone"] == "+919876543210"
    assert first["rating"] == 4.5
    assert first["review_count"] == 123
    assert "MG Road" in first["address"]
    assert first["metadata"]["detail_url"].endswith("/Bengaluru/Fixture-Traders-BBBB123")


def test_justdial_detail_parse():
    record = parse_business_page(JD_DETAIL, source_url="https://www.justdial.com/Bengaluru/Fixture-Traders-BBBB123")
    assert record["business_name"] == "Fixture Traders"
    assert record["phone"] == "+919876543210"  # glyph decode wins (public render)
    assert record["city"] == "Bengaluru"
    assert record["postal_code"] == "560001"


# ================================================================= IndiaMART
IM_SEARCH = """
<html><body>
<div class="card">
  <a class="cardlinks" href="https://www.indiamart.com/fixture-exports/leather-shoes.html">Fixture Leather Shoes</a>
  <p class="company name"><a href="https://www.indiamart.com/fixture-exports/">Fixture Exports</a></p>
  <span class="price">Rs 1,250 / Piece</span>
  <span class="newLocationUi">Delhi</span>
  <a href="tel:+919812345678"></a>
</div>
<div class="card"><p class="company name"><a href="https://www.indiamart.com/second-exports/">Second Exports</a></p></div>
</body></html>
"""

IM_SUPPLIER = """
<html><body>
<h1>Fixture Exports</h1>
<a href="tel:+919812345678"></a>
<p class="add">Plot 5, Okhla Industrial Area, New Delhi 110020</p>
<script type="application/ld+json">{"@type":"Organization","name":"Fixture Exports","address":"Okhla Phase 2"}</script>
</body></html>
"""


def test_indiamart_search_parse():
    records = parse_search_page(IM_SEARCH, source_url="https://dir.indiamart.com/search.mp?ss=shoes", city_hint=None, cap=10)
    assert len(records) == 2
    first = records[0]
    assert first["business_name"] == "Fixture Exports"
    assert first["metadata"]["product"] == "Fixture Leather Shoes"
    assert first["metadata"]["price_inr"] == 1250.0
    assert first["city"] == "Delhi"
    assert first["phone"] == "+919812345678"
    assert "leather-shoes" in first["metadata"]["detail_url"]


def test_indiamart_supplier_parse():
    record = parse_supplier_page(IM_SUPPLIER, source_url="https://www.indiamart.com/fixture-exports/")
    assert record["business_name"] == "Fixture Exports"
    assert record["phone"] == "+919812345678"
    assert "Okhla" in record["address"]


# ================================================================= Universal auto
AUTO_PAGE = """
<html><head>
<title>Fixture Industries</title>
<meta property="og:title" content="Fixture Industries">
<link rel="canonical" href="https://fixture.test/canonical">
<script type="application/ld+json">{"@type":"Organization","name":"Fixture Industries",
 "telephone":"+91 11 4000 5000","address":"Connaught Place, New Delhi","url":"https://fixture.test"}</script>
</head><body>
<main>Contact us at sales@fixture.industries or +91-11-4000-5000</main>
<table><tr><th>Product</th></tr><tr><td>Widget</td></tr></table>
<img src="/x.png" alt="">
<script>__NEXT_DATA__ = {"props":{"pageProps":{"items":[1,2,3]}}}</script>
</body></html>
"""


def test_universal_auto_extracts_layers():
    from app.scrapers.actors.universal.actor import _auto_record

    soup = BeautifulSoup(AUTO_PAGE, "html.parser")
    record = _auto_record(soup, AUTO_PAGE, url="https://fixture.test/", source="universal-web")
    assert record["business_name"] == "Fixture Industries"
    assert record["email"] == "sales@fixture.industries"
    assert record["phone"]
    org = record["metadata"]["json_ld_org"]
    assert org["name"] == "Fixture Industries"
    assert "Connaught Place" in org["address"]
    assert record["metadata"]["canonical"] == "https://fixture.test/canonical"
    assert record["metadata"]["tables"] == [{"rows": 2, "columns": 1}]
    assert record["metadata"]["images"] == 1
    assert record["metadata"]["embedded_json_found"] is True
    assert record["metadata"]["extraction_strategy"] == "auto"


def test_universal_jsonld_graph_extraction():
    """@graph containers must yield their inner nodes (spec §27 layer 3)."""
    from app.scrapers.core.extraction import extract_jsonld

    html = """
    <script type="application/ld+json">{"@graph":[
      {"@type":"Organization","name":"Graph Corp"},
      {"@type":"Product","name":"Graph Widget"}]}</script>
    """
    nodes = extract_jsonld(BeautifulSoup(html, "html.parser"))
    assert {n.get("name") for n in nodes} == {"Graph Corp", "Graph Widget"}


# ================================================================= schemas
def test_instagram_input_requires_mode_fields():
    """Mode-specific requirements are enforced by the actor's run() policy —
    the schema itself stays permissive (runner validates pre-enqueue)."""
    from app.scrapers.actors.instagram.schemas import InstagramInput

    inp = InstagramInput.model_validate({"mode": "hashtag"})
    assert inp.mode.value == "hashtag"


def test_justdial_input_policy():
    from app.scrapers.actors.justdial.schemas import JustDialInput

    inp = JustDialInput.model_validate({"mode": "search", "city": "Delhi", "category": "Electricians"})
    assert inp.validate_policy() == {}
    inp2 = JustDialInput.model_validate({"mode": "search"})
    assert "search" in inp2.validate_policy()


def test_linkedin_rejects_foreign_urls():
    from pydantic import ValidationError as PydValidationError

    from app.scrapers.actors.linkedin.schemas import LinkedInInput

    with pytest.raises(PydValidationError):
        LinkedInInput.model_validate({"urls": ["https://example.com/company/x/"]})
