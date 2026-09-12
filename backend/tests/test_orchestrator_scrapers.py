"""QBIT CONNECT — Scraper-by-Scraper Orchestration Matrix Test (Brief §39).

Verifies the tool registry, input validation, execution plan generation, and
output capability definitions across all 12 specialized scrapers:
1. Email Finder
2. Google Maps
3. IndiaMART
4. Instagram Intelligence
5. JustDial
6. LinkedIn Public
7. Meta Ads Library
8. Public Data
9. Sitemap Intelligence
10. Universal Web
11. Website Scraper
12. Business Directory
"""

import pytest

from app.services.orchestration.interpreter import TaskInterpreter
from app.services.orchestration.planner import ExecutionPlanner
from app.services.orchestration.tool_registry import ToolRegistry
from app.services.scraping.registry import ActorRegistry


@pytest.fixture
def registry():
    from app.scrapers.bootstrap import register_builtin_actors

    reg = ActorRegistry()
    register_builtin_actors(reg)
    return ToolRegistry(reg)


SCRAPER_CASES = [
    (
        "indiamart",
        "Find wholesale leather shoe suppliers in Modinagar using IndiaMART",
        {"keyword": "leather shoes", "city": "Modinagar", "mode": "supplier_search"},
        "supplier",
    ),
    (
        "justdial",
        "Find gyms in Delhi using JustDial",
        {"category": "gyms", "city": "Delhi", "mode": "search"},
        "local_business",
    ),
    (
        "google-maps",
        "Find dental clinics in London using Google Maps",
        {"query": "dental clinics in London"},
        "local_business",
    ),
    (
        "instagram",
        "Extract public profile details for nike on Instagram",
        {"query": "nike", "mode": "search"},
        "social_profile",
    ),
    (
        "linkedin-public",
        "Extract public company info for acme on LinkedIn",
        {"company_slug": "acme-corp"},
        "company",
    ),
    (
        "meta-ads-library",
        "Search active ads for sneakers on Meta Ads",
        {"keyword": "sneakers"},
        "ad_creative",
    ),
    (
        "email-finder",
        "Find contact emails from example.com using Email Finder",
        {"domain": "example.com"},
        "business_email",
    ),
    (
        "public-data",
        "Ingest public dataset from https://data.gov/businesses.csv using Public Data",
        {"url": "https://data.gov/businesses.csv", "format": "csv"},
        "open_dataset",
    ),
    (
        "sitemap-intelligence",
        "Audit sitemap for https://example.com using Sitemap",
        {"url": "https://example.com"},
        "sitemap_entry",
    ),
    (
        "website",
        "Crawl public website https://example.com using Website Scraper",
        {"url": "https://example.com"},
        "webpage",
    ),
    (
        "universal-web",
        "Scrape public page https://example.com using Universal Web",
        {"url": "https://example.com", "strategy": "auto"},
        "generic_webpage",
    ),
    (
        "business-directory",
        "Scrape custom directory using Business Directory",
        {
            "adapter": "generic",
            "config": {
                "list_url": "https://directory.example.com/businesses",
                "item_selector": ".card",
                "fields": {"business_name": "h2"},
            },
        },
        "business_lead",
    ),
]


@pytest.mark.parametrize("actor_id,query,valid_input,target_entity", SCRAPER_CASES)
def test_specialized_scraper_orchestration_contract(
    registry, actor_id, query, valid_input, target_entity
):
    # 1. Tool discovery
    tool = registry.get_tool(actor_id)
    assert tool is not None, f"Actor {actor_id} not found in ToolRegistry"
    assert tool.actor_id == actor_id
    assert len(tool.capabilities) > 0
    assert len(tool.output_fields) > 0

    # 2. Planning with Source Lock
    interpreter = TaskInterpreter()
    task = interpreter.interpret(query, forced_source=actor_id, forced_source_lock=True)
    planner = ExecutionPlanner(registry)
    plan = planner.create_plan(task)

    assert plan.primary_tool == actor_id
    assert plan.source_locked is True
    assert plan.fallback_tool is None
    assert "SOURCE LOCKED" in plan.rationale

    # 3. Actor input validation contract
    actor = registry.get_actor_instance(actor_id)
    report = actor.validate_input(valid_input)
    assert report.valid is True, f"Actor {actor_id} rejected valid sample input: {report.errors}"
    assert isinstance(report.normalized_input, dict)
