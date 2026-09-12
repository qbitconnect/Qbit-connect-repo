"""QBIT CONNECT — Scraping Execution Planner & Source Selector (Brief §9, §10, §13, §27, §28).

Builds deterministic, inspection-ready execution plans from interpreted tasks.
Enforces Mode A (Auto Selection with explicit rationale) and Mode B (Source Lock
with ZERO silent fallback).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.orchestration.interpreter import InterpretedTask
from app.services.orchestration.tool_registry import ToolCapabilityDefinition, ToolRegistry


@dataclass
class PlanStep:
    step_id: int
    actor_id: str
    action: str
    parameters: dict[str, Any]
    target_records: int
    batch_size: int = 100
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionPlan:
    task_id: str
    user_query: str
    primary_tool: str
    primary_source_name: str
    source_locked: bool
    fallback_tool: str | None
    target_count: int
    fields: list[str]
    mode: str  # "AUTO" | "SOURCE_LOCKED"
    rationale: str
    steps: list[PlanStep]
    input_payload: dict[str, Any]
    estimated_pages: int
    max_runtime_seconds: int = 3600

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user_query": self.user_query,
            "primary_tool": self.primary_tool,
            "primary_source_name": self.primary_source_name,
            "source_locked": self.source_locked,
            "fallback_tool": self.fallback_tool,
            "target_count": self.target_count,
            "fields": self.fields,
            "mode": self.mode,
            "rationale": self.rationale,
            "steps": [s.to_dict() for s in self.steps],
            "input_payload": self.input_payload,
            "estimated_pages": self.estimated_pages,
            "max_runtime_seconds": self.max_runtime_seconds,
        }


class ExecutionPlanner:
    def __init__(self, tool_registry: ToolRegistry) -> None:
        self.tool_registry = tool_registry

    def select_source(
        self, task: InterpretedTask
    ) -> tuple[str, str | None, bool, str]:
        """Returns (primary_actor_id, fallback_actor_id, source_locked, rationale)."""
        # --- MODE B: SOURCE LOCK ---
        if task.source_lock and task.requested_source:
            canonical = self.tool_registry.resolve_source(task.requested_source)
            if canonical and self.tool_registry.get_tool(canonical):
                tool = self.tool_registry.get_tool(canonical)
                return (
                    canonical,
                    None,
                    True,
                    f"SOURCE LOCKED by operator request to '{tool.source_name}' ({canonical}). "
                    f"Silent fallback is disabled. Unrecoverable failures will halt with diagnostics.",
                )

        # --- MODE A: AUTO SELECTION ---
        scored_tools: list[tuple[float, str, str]] = []
        tools = self.tool_registry.list_tools()

        is_indian_geo = False
        if task.location:
            ind_locs = ("modinagar", "delhi", "mumbai", "noida", "ghaziabad", "bangalore", "india", "pune", "gurgaon", "chennai")
            is_indian_geo = any(loc in task.location.lower() for loc in ind_locs)

        for t in tools:
            score = 0.0
            reasons = []

            # Entity affinity
            if task.target_entity in t.target_entities:
                score += 50.0
                reasons.append(f"entity match ({task.target_entity})")

            # Intent alignment
            if task.intent == "supplier_search":
                if t.actor_id == "indiamart":
                    score += 40.0
                    reasons.append("IndiaMART top affinity for supplier & B2B product search")
                elif t.actor_id == "justdial":
                    score += 20.0
                elif t.actor_id == "google-maps":
                    score += 15.0
            elif task.intent == "business_discovery":
                if is_indian_geo:
                    if t.actor_id == "justdial":
                        score += 35.0
                        reasons.append("JustDial high affinity for Indian local business directories")
                    elif t.actor_id == "indiamart":
                        score += 30.0
                        reasons.append("IndiaMART strong local business/wholesaler coverage")
                    elif t.actor_id == "google-maps":
                        score += 25.0
                else:
                    if t.actor_id == "google-maps":
                        score += 35.0
                    elif t.actor_id == "justdial":
                        score += 20.0
            elif task.intent == "ad_intelligence" and t.actor_id == "meta-ads-library":
                score += 60.0
            elif task.intent == "social_intelligence" and t.actor_id in ("instagram", "linkedin-public"):
                score += 50.0
            elif task.intent == "email_discovery" and t.actor_id == "email-finder":
                score += 50.0
            elif task.intent == "sitemap_audit" and t.actor_id == "sitemap-intelligence":
                score += 50.0

            # General fallback capability
            if t.actor_id == "universal-web":
                score += 10.0

            scored_tools.append((score, t.actor_id, "; ".join(reasons)))

        scored_tools.sort(key=lambda x: x[0], reverse=True)
        best_actor = scored_tools[0][1]
        best_reason = scored_tools[0][2]
        fallback_actor = scored_tools[1][1] if len(scored_tools) > 1 and scored_tools[1][0] > 10.0 else None

        tool = self.tool_registry.get_tool(best_actor)
        rationale = f"AUTO MODE selected '{tool.source_name}' ({best_actor}): {best_reason}."
        if fallback_actor:
            fb_tool = self.tool_registry.get_tool(fallback_actor)
            rationale += f" Fallback candidate: '{fb_tool.source_name}' ({fallback_actor})."

        return best_actor, fallback_actor, False, rationale

    def create_plan(
        self,
        task: InterpretedTask,
        task_id: str | None = None,
    ) -> ExecutionPlan:
        primary_tool, fallback_tool, source_locked, rationale = self.select_source(task)
        tool_defn = self.tool_registry.get_tool(primary_tool)
        source_name = tool_defn.source_name if tool_defn else primary_tool

        target_count = max(1, task.target_count)
        estimated_pages = max(1, math.ceil(target_count / 20))

        # Build actor-specific input payload
        input_payload: dict[str, Any] = {}
        steps: list[PlanStep] = []

        if primary_tool == "indiamart":
            input_payload = {
                "mode": "supplier_search" if task.intent == "supplier_search" else "product_search",
                "keyword": task.keywords,
                "city": task.location or "",
                "max_results": target_count,
            }
            # Decompose into query variations if large scale
            if target_count > 500 and task.location:
                sub_queries = [
                    f"{task.keywords}",
                    f"{task.keywords} wholesalers",
                    f"{task.keywords} manufacturers",
                    f"{task.keywords} suppliers",
                ]
                records_per_sub = math.ceil(target_count / len(sub_queries))
                for idx, q in enumerate(sub_queries, start=1):
                    steps.append(
                        PlanStep(
                            step_id=idx,
                            actor_id="indiamart",
                            action="supplier_search",
                            parameters={"keyword": q, "city": task.location, "max_results": records_per_sub},
                            target_records=records_per_sub,
                            description=f"Query partition '{q}' in {task.location}",
                        )
                    )
            else:
                steps.append(
                    PlanStep(
                        step_id=1,
                        actor_id="indiamart",
                        action="search",
                        parameters=dict(input_payload),
                        target_records=target_count,
                        description=f"IndiaMART search for '{task.keywords}' in {task.location or 'all regions'}",
                    )
                )

        elif primary_tool == "justdial":
            input_payload = {
                "mode": "search",
                "category": task.keywords,
                "city": task.location or "Delhi",
                "max_results": min(1000, target_count),
                "include_details": True,
            }
            steps.append(
                PlanStep(
                    step_id=1,
                    actor_id="justdial",
                    action="category_city_crawl",
                    parameters=dict(input_payload),
                    target_records=target_count,
                    description=f"JustDial business listing crawl for '{task.keywords}' in {task.location or 'Delhi'}",
                )
            )

        elif primary_tool == "google-maps":
            query = f"{task.keywords} in {task.location}" if task.location else task.keywords
            input_payload = {
                "query": query,
                "max_records": target_count,
            }
            steps.append(
                PlanStep(
                    step_id=1,
                    actor_id="google-maps",
                    action="places_search",
                    parameters=dict(input_payload),
                    target_records=target_count,
                    description=f"Google Maps provider query '{query}'",
                )
            )

        elif primary_tool == "instagram":
            input_payload = {
                "mode": "search",
                "query": task.keywords,
                "max_items": target_count,
            }
            steps.append(
                PlanStep(
                    step_id=1,
                    actor_id="instagram",
                    action="instagram_search",
                    parameters=dict(input_payload),
                    target_records=target_count,
                    description=f"Public Instagram discovery for '{task.keywords}'",
                )
            )

        elif primary_tool == "meta-ads-library":
            input_payload = {
                "keyword": task.keywords,
                "max_ads": min(500, target_count),
            }
            steps.append(
                PlanStep(
                    step_id=1,
                    actor_id="meta-ads-library",
                    action="ads_search",
                    parameters=dict(input_payload),
                    target_records=target_count,
                    description=f"Meta Ads search for '{task.keywords}'",
                )
            )

        elif primary_tool == "email-finder":
            input_payload = {
                "domain": task.keywords if "." in task.keywords else f"{task.keywords.replace(' ', '').lower()}.com",
                "max_pages": min(50, max(5, math.ceil(target_count / 2))),
            }
            steps.append(
                PlanStep(
                    step_id=1,
                    actor_id="email-finder",
                    action="domain_crawl",
                    parameters=dict(input_payload),
                    target_records=target_count,
                    description=f"Email Finder domain crawl on '{input_payload['domain']}'",
                )
            )

        else:
            # Default / Universal Web
            input_payload = {
                "url": task.keywords if task.keywords.startswith("http") else f"https://{task.keywords}",
                "strategy": "auto",
                "max_records": target_count,
            }
            steps.append(
                PlanStep(
                    step_id=1,
                    actor_id=primary_tool,
                    action="execute_scrape",
                    parameters=dict(input_payload),
                    target_records=target_count,
                    description=f"Execute {source_name} with target {target_count}",
                )
            )

        # Max runtime calculation based on target count
        runtime = min(86400, max(300, target_count * 5))

        import uuid
        plan_id = task_id or f"plan_{uuid.uuid4().hex[:12]}"

        return ExecutionPlan(
            task_id=plan_id,
            user_query=task.raw_query,
            primary_tool=primary_tool,
            primary_source_name=source_name,
            source_locked=source_locked,
            fallback_tool=fallback_tool,
            target_count=target_count,
            fields=task.requested_fields,
            mode="SOURCE_LOCKED" if source_locked else "AUTO",
            rationale=rationale,
            steps=steps,
            input_payload=input_payload,
            estimated_pages=estimated_pages,
            max_runtime_seconds=runtime,
        )
