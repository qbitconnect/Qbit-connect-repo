"""QBIT CONNECT — Orchestration & Scraping Intelligence Tests (Brief §38-§43)."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from app.services.orchestration.completion import CompletionEngine, CompletionStatus
from app.services.orchestration.diagnostics import DiagnosticsEngine, FailureCategory
from app.services.orchestration.interpreter import TaskInterpreter
from app.services.orchestration.planner import ExecutionPlanner
from app.services.orchestration.tool_registry import ToolRegistry
from app.services.orchestration.validation import ConfidenceScore, RecordQuality, RecordValidator
from app.services.scraping.registry import ActorRegistry


@pytest.fixture
def mock_registry():
    from app.scrapers.bootstrap import register_builtin_actors

    reg = ActorRegistry()
    register_builtin_actors(reg)
    return reg


def test_tool_registry_discovers_all_twelve_actors(mock_registry):
    registry = ToolRegistry(mock_registry)
    tools = registry.list_tools()
    actor_ids = {t.actor_id for t in tools}

    expected = {
        "indiamart",
        "justdial",
        "google-maps",
        "instagram",
        "linkedin-public",
        "meta-ads-library",
        "email-finder",
        "business-directory",
        "public-data",
        "sitemap-intelligence",
        "website",
        "universal-web",
    }
    assert expected.issubset(actor_ids), f"Missing actors: {expected - actor_ids}"

    # Verify IndiaMART definition
    im = registry.get_tool("indiamart")
    assert im is not None
    assert im.source_name == "IndiaMART"
    assert "supplier" in im.target_entities
    assert len(im.capabilities) > 0

    # Verify JustDial definition
    jd = registry.get_tool("justdial")
    assert jd is not None
    assert jd.source_name == "JustDial"
    assert "local_business" in jd.target_entities


def test_task_interpreter_intent_and_entity_parsing():
    interpreter = TaskInterpreter()

    # 1. B2B / Supplier query with location and target
    t1 = interpreter.interpret("Find 5,000 shoe wholesalers in Modinagar")
    assert t1.intent == "supplier_search"
    assert t1.target_entity == "supplier"
    assert t1.location == "Modinagar"
    assert t1.target_count == 5000
    assert "shoe" in t1.keywords.lower()

    # 2. Source lock via keyword
    t2 = interpreter.interpret("Extract 500 gyms in Delhi using JustDial")
    assert t2.location == "Delhi"
    assert t2.target_count == 500
    assert t2.requested_source == "justdial"
    assert t2.source_lock is True

    # 3. Ad intelligence query
    t3 = interpreter.interpret("Find active shoe ads on Meta Ads")
    assert t3.intent == "ad_intelligence"
    assert t3.target_entity == "ad_creative"
    assert t3.requested_source == "meta-ads-library"
    assert t3.source_lock is True

    # 4. Domain email crawl
    t4 = interpreter.interpret("Find 50 contact emails from acme-corp.com")
    assert t4.intent == "email_discovery"
    assert t4.target_count == 50


def test_auto_mode_source_selection(mock_registry):
    planner = ExecutionPlanner(ToolRegistry(mock_registry))
    interpreter = TaskInterpreter()

    task = interpreter.interpret("Find shoe wholesalers in Modinagar")
    plan = planner.create_plan(task)

    assert plan.mode == "AUTO"
    assert plan.source_locked is False
    assert plan.primary_tool in ("indiamart", "justdial")
    assert plan.fallback_tool is not None
    assert "AUTO MODE" in plan.rationale
    assert len(plan.steps) >= 1


def test_source_lock_enforcement_prevents_silent_switching(mock_registry):
    planner = ExecutionPlanner(ToolRegistry(mock_registry))
    interpreter = TaskInterpreter()

    # Operator explicitly locks to IndiaMART
    task = interpreter.interpret(
        "Find shoe wholesalers in Modinagar using IndiaMART",
        forced_source="indiamart",
        forced_source_lock=True,
    )
    plan = planner.create_plan(task)

    assert plan.mode == "SOURCE_LOCKED"
    assert plan.source_locked is True
    assert plan.primary_tool == "indiamart"
    assert plan.primary_source_name == "IndiaMART"
    assert plan.fallback_tool is None  # CRITICAL: NO SILENT FALLBACK
    assert "SOURCE LOCKED" in plan.rationale


def test_large_scale_collection_decomposition(mock_registry):
    planner = ExecutionPlanner(ToolRegistry(mock_registry))
    interpreter = TaskInterpreter()

    # Large target of 2,000 records
    task = interpreter.interpret("Find 2,000 shoe wholesalers in Modinagar using IndiaMART")
    plan = planner.create_plan(task)

    assert plan.target_count == 2000
    assert plan.primary_tool == "indiamart"
    assert len(plan.steps) > 1  # Decomposed into query partitions


def test_completion_engine_zero_agent_authority():
    """Agent cannot declare completion if factual unique records have not reached target."""
    # 1. Target 10,000, collected 100 -> must be IN_PROGRESS
    snap1 = CompletionEngine.evaluate(
        target=10000,
        collected=100,
        unique=97,
        duplicates=3,
        invalid=0,
        job_status="RUNNING",
    )
    assert snap1.status == CompletionStatus.IN_PROGRESS
    assert snap1.is_terminal is False
    assert snap1.remaining == 9903

    # 2. Target 10,000, unique 10,000 -> TARGET_REACHED
    snap2 = CompletionEngine.evaluate(
        target=10000,
        collected=12000,
        unique=10000,
        duplicates=2000,
        invalid=0,
        job_status="COMPLETED",
    )
    assert snap2.status == CompletionStatus.TARGET_REACHED
    assert snap2.is_terminal is True
    assert snap2.completion_percentage == 100.0

    # 3. Target 10,000, unique 7,426, all queries finished -> SOURCE_EXHAUSTED
    snap3 = CompletionEngine.evaluate(
        target=10000,
        collected=8000,
        unique=7426,
        duplicates=574,
        invalid=0,
        queries_completed=5,
        queries_total=5,
        sources_exhausted=True,
    )
    assert snap3.status == CompletionStatus.SOURCE_EXHAUSTED
    assert snap3.is_terminal is True
    assert snap3.unique == 7426

    # 4. Job failed -> FAILED
    snap4 = CompletionEngine.evaluate(
        target=1000,
        collected=50,
        unique=40,
        duplicates=10,
        invalid=0,
        has_fatal_error=True,
    )
    assert snap4.status == CompletionStatus.FAILED
    assert snap4.is_terminal is True


def test_record_validator():
    # 1. Valid rich record
    rec1 = {
        "name": "Apex Footwear Co",
        "phone": "+91 98765 43210",
        "email": "contact@apexfootwear.com",
        "website": "https://www.apexfootwear.com",
        "address": "Railway Road, Modinagar",
    }
    v1 = RecordValidator.validate_record(rec1)
    assert v1.is_valid is True
    assert v1.quality == RecordQuality.VALID
    assert v1.confidence == ConfidenceScore.HIGH
    assert v1.has_valid_phone is True
    assert v1.has_valid_email is True

    # 2. Missing name -> INVALID
    rec2 = {"phone": "9876543210", "email": "info@test.com"}
    v2 = RecordValidator.validate_record(rec2)
    assert v2.is_valid is False
    assert v2.quality == RecordQuality.INVALID
    assert "Missing business identity / name" in v2.errors

    # 3. Invalid dummy email
    rec3 = {"name": "Local Store", "phone": "9876543210", "email": "user@example.com"}
    v3 = RecordValidator.validate_record(rec3)
    assert v3.has_valid_email is False


def test_diagnostics_engine_error_categorization():
    # Access blocked / login wall
    diag1 = DiagnosticsEngine.diagnose_error(
        actor_id="instagram",
        actor_name="Instagram Intelligence",
        error="Target blocked: Instagram served a login challenge",
        error_code="TARGET_BLOCKED",
    )
    assert diag1.category == FailureCategory.ACCESS_BLOCKED
    assert diag1.is_retryable is False
    assert len(diag1.recommended_actions) > 0

    # Markup changed
    diag2 = DiagnosticsEngine.diagnose_error(
        actor_id="indiamart",
        actor_name="IndiaMART",
        error="Extraction error: Expected product listing selector not detected",
    )
    assert diag2.category == FailureCategory.MARKUP_CHANGED
    assert diag2.is_retryable is False

    # Timeout (transient)
    diag3 = DiagnosticsEngine.diagnose_error(
        actor_id="justdial",
        actor_name="JustDial",
        error="ReadTimeout: Connection timed out after 20s",
    )
    assert diag3.category == FailureCategory.TRANSIENT_NETWORK
    assert diag3.is_retryable is True


@pytest.mark.asyncio
async def test_orchestration_api_endpoints(client, admin_headers):
    headers = admin_headers

    # 1. GET /api/v1/orchestration/tools
    r_tools = await client.get("/api/v1/orchestration/tools", headers=headers)
    assert r_tools.status_code == 200
    data_tools = r_tools.json()
    assert data_tools["success"] is True
    assert data_tools["count"] >= 12

    # 2. POST /api/v1/orchestration/interpret
    r_interp = await client.post(
        "/api/v1/orchestration/interpret",
        headers=headers,
        json={"query": "Find 500 shoe suppliers in Modinagar using IndiaMART"},
    )
    assert r_interp.status_code == 200
    d_interp = r_interp.json()["data"]
    assert d_interp["target_count"] == 500
    assert d_interp["requested_source"] == "indiamart"
    assert d_interp["source_lock"] is True

    # 3. POST /api/v1/orchestration/plan (Auto mode)
    r_plan_auto = await client.post(
        "/api/v1/orchestration/plan",
        headers=headers,
        json={"query": "Find dentists in Delhi"},
    )
    assert r_plan_auto.status_code == 200
    d_plan = r_plan_auto.json()["data"]
    assert d_plan["mode"] == "AUTO"
    assert d_plan["primary_tool"] in ("justdial", "google-maps")
    assert d_plan["source_locked"] is False

    # 4. POST /api/v1/orchestration/plan (Source Lock mode)
    r_plan_lock = await client.post(
        "/api/v1/orchestration/plan",
        headers=headers,
        json={"query": "Find suppliers in Modinagar", "source": "indiamart", "source_lock": True},
    )
    assert r_plan_lock.status_code == 200
    d_lock = r_plan_lock.json()["data"]
    assert d_lock["mode"] == "SOURCE_LOCKED"
    assert d_lock["source_locked"] is True
    assert d_lock["primary_tool"] == "indiamart"
    assert d_lock["fallback_tool"] is None
