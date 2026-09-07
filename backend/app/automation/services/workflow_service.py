"""WorkflowService — workflow CRUD, validation, publishing, lifecycle (§2, §3,
§21, §42, §46, §52, §60).

Publishing rules (§60 — immutable versions):
- editing a workflow with a published version creates a NEW draft version
- publishing validates the definition end-to-end; invalid workflows can
  never be activated (§21, §46)
- publishing marks the new version PUBLISHED, retires the previous one and
  flips the workflow ACTIVE; running executions keep referencing their own
  version row (TEST 3)
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.actions.registry import build_action_registry
from app.automation.core.condition import validate_node_condition
from app.automation.core.exceptions import (
    ConfigurationError,
    ValidationError,
)
from app.automation.core.schemas import ConditionGroup, WorkflowDefinition
from app.automation.core.workflow import validate_graph
from app.automation.services.analytics import workflow_stats
from app.automation.triggers.definitions import build_trigger_registry
from app.core.errors import ConflictError, NotFoundError
from app.models.automation import (
    Workflow,
    WorkflowExecution,
    WorkflowStatus,
    WorkflowVersion,
    WorkflowVersionStatus,
)

_MAX_NAME = 200


def _checksum(definition: dict) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkflowService:
    # ------------------------------------------------------------------ CRUD
    async def create(
        self, session: AsyncSession, *, name: str, trigger_type: str,
        definition: dict, description: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> Workflow:
        name = (name or "").strip()
        if not name or len(name) > _MAX_NAME:
            raise ValidationError([f"name is required (max {_MAX_NAME} chars)"])

        registry = build_trigger_registry()
        if registry.get(trigger_type) is None:
            raise ValidationError([f"Unknown trigger type: {trigger_type!r}"])

        parsed = self.parse_definition(definition)
        validate_graph(parsed, max_nodes=self._max_nodes())
        self.validate_definition(session, parsed, user_id=user_id)

        workflow = Workflow(
            name=name,
            description=description,
            trigger_type=trigger_type,
            status=WorkflowStatus.DRAFT,
            created_by=user_id,
            updated_by=user_id,
        )
        session.add(workflow)
        await session.flush()
        session.add(WorkflowVersion(
            workflow_id=workflow.id,
            version=1,
            definition=parsed.model_dump(by_alias=True, exclude_none=True),
            checksum=_checksum(parsed.model_dump(by_alias=True, exclude_none=True)),
            status=WorkflowVersionStatus.DRAFT,
            created_by=user_id,
        ))
        await session.commit()
        await session.refresh(workflow)
        return workflow

    async def get(self, session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
        workflow = await session.get(Workflow, workflow_id)
        if workflow is None:
            raise NotFoundError("Workflow not found")
        return workflow

    async def list(
        self, session: AsyncSession, *, page: int = 1, page_size: int = 25,
        status: str | None = None, trigger_type: str | None = None,
        search: str | None = None,
    ) -> tuple[list[dict], int]:
        query = select(Workflow).order_by(desc(Workflow.updated_at), Workflow.id)
        count_query = select(func.count()).select_from(Workflow)
        if status:
            query = query.where(Workflow.status == status)
            count_query = count_query.where(Workflow.status == status)
        if trigger_type:
            query = query.where(Workflow.trigger_type == trigger_type)
            count_query = count_query.where(Workflow.trigger_type == trigger_type)
        if search:
            like = f"%{search}%"
            query = query.where(Workflow.name.ilike(like))
            count_query = count_query.where(Workflow.name.ilike(like))
        total = await session.scalar(count_query)
        rows = (await session.execute(
            query.offset(max(0, page - 1) * page_size).limit(page_size)
        )).scalars().all()
        items = []
        for workflow in rows:
            item = workflow.to_public_dict()
            item.update(await workflow_stats(session, workflow.id))
            # attach the latest draft/published definition summary
            item["trigger_label"] = self.trigger_label(session, workflow.trigger_type)
            items.append(item)
        return items, int(total or 0)

    async def update(
        self, session: AsyncSession, workflow: Workflow, *,
        name: str | None = None, description: str | None = None,
        definition: dict | None = None, user_id: uuid.UUID | None = None,
    ) -> Workflow:
        if workflow.status == WorkflowStatus.ARCHIVED:
            raise ConflictError("Archived workflows cannot be edited")
        if name is not None:
            name = name.strip()
            if not name or len(name) > _MAX_NAME:
                raise ValidationError([f"name is required (max {_MAX_NAME} chars)"])
            workflow.name = name
        if description is not None:
            workflow.description = description

        if definition is not None:
            parsed = self.parse_definition(definition)
            validate_graph(parsed, max_nodes=self._max_nodes())
            self.validate_definition(session, parsed, user_id=user_id)
            await self._upsert_draft_version(session, workflow, parsed, user_id)

        workflow.updated_by = user_id
        workflow.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(workflow)
        return workflow

    async def _upsert_draft_version(
        self, session: AsyncSession, workflow: Workflow,
        parsed: WorkflowDefinition, user_id: uuid.UUID | None,
    ) -> WorkflowVersion:
        definition_json = parsed.model_dump(by_alias=True, exclude_none=True)
        checksum = _checksum(definition_json)

        draft = (await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowVersion.status == WorkflowVersionStatus.DRAFT,
            ).order_by(desc(WorkflowVersion.version)).limit(1)
        )).scalars().first()

        if draft is not None:
            draft.definition = definition_json
            draft.checksum = checksum
            await session.flush()
            return draft

        # published workflows get a NEW immutable draft version (§60)
        max_version = await session.scalar(
            select(func.max(WorkflowVersion.version)).where(
                WorkflowVersion.workflow_id == workflow.id
            )
        )
        version_no = int(max_version or 0) + 1
        draft = WorkflowVersion(
            workflow_id=workflow.id,
            version=version_no,
            definition=definition_json,
            checksum=checksum,
            status=WorkflowVersionStatus.DRAFT,
            created_by=user_id,
        )
        session.add(draft)
        await session.flush()
        return draft

    async def delete(self, session: AsyncSession, workflow: Workflow) -> None:
        """Only drafts may be deleted (§61 RBAC; published history stays)."""
        if workflow.status != WorkflowStatus.DRAFT:
            raise ConflictError(
                "Only DRAFT workflows can be deleted — archive published ones"
            )
        running = await session.scalar(
            select(func.count()).select_from(WorkflowExecution).where(
                WorkflowExecution.workflow_id == workflow.id,
                WorkflowExecution.status.in_(["QUEUED", "RUNNING", "WAITING"]),
            )
        )
        if int(running or 0) > 0:
            raise ConflictError("Workflow has active executions — cancel them first")
        await session.delete(workflow)
        await session.commit()

    # ------------------------------------------------------------- lifecycle
    async def validate(
        self, session: AsyncSession, workflow: Workflow,
    ) -> dict[str, Any]:
        """§46 visual validation — structured PASS/ERROR report."""
        checks: dict[str, dict] = {}
        definition = await self._definition_of(session, workflow)
        parsed = None
        try:
            parsed = self.parse_definition(definition)
            validate_graph(parsed, max_nodes=self._max_nodes())
            checks["structure"] = {"ok": True, "message": "Graph valid — all nodes reachable, no cycles"}
        except ValidationError as exc:
            checks["structure"] = {"ok": False, "message": "; ".join(exc.issues)}
            return {"ok": False, "checks": checks}

        try:
            self.validate_definition(session, parsed)
            checks["nodes"] = {"ok": True, "message": "All actions, conditions and triggers configured"}
        except (ValidationError, ConfigurationError) as exc:
            message = getattr(exc, "issues", None) or [str(exc)]
            checks["nodes"] = {"ok": False, "message": "; ".join(str(m) for m in message)}

        ok = all(c.get("ok") for c in checks.values())
        return {"ok": ok, "checks": checks}

    async def publish(
        self, session: AsyncSession, workflow: Workflow,
        *, user_id: uuid.UUID | None = None,
    ) -> WorkflowVersion:
        if workflow.status == WorkflowStatus.ARCHIVED:
            raise ConflictError("Archived workflows cannot be published")

        draft = (await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowVersion.status == WorkflowVersionStatus.DRAFT,
            ).order_by(desc(WorkflowVersion.version)).limit(1)
        )).scalars().first()

        parsed = self.parse_definition(await self._definition_of(session, workflow, draft))
        validate_graph(parsed, max_nodes=self._max_nodes())
        self.validate_definition(session, parsed, user_id=user_id)

        now = datetime.now(timezone.utc)
        if draft is None:
            draft = WorkflowVersion(
                workflow_id=workflow.id,
                version=1,
                definition=parsed.model_dump(by_alias=True, exclude_none=True),
                checksum=_checksum(parsed.model_dump(by_alias=True, exclude_none=True)),
                status=WorkflowVersionStatus.DRAFT,
                created_by=user_id,
            )
            session.add(draft)
            await session.flush()

        # retire the previous published version, publish this one
        previous = (await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowVersion.status == WorkflowVersionStatus.PUBLISHED,
            )
        )).scalars().all()
        for row in previous:
            row.status = WorkflowVersionStatus.RETIRED
        draft.status = WorkflowVersionStatus.PUBLISHED
        draft.published_at = now

        workflow.status = WorkflowStatus.ACTIVE
        workflow.current_version = draft.version
        workflow.published_at = now
        workflow.updated_by = user_id
        await session.commit()
        await session.refresh(workflow)
        await session.refresh(draft)
        return draft

    async def pause(self, session: AsyncSession, workflow: Workflow) -> Workflow:
        if workflow.status != WorkflowStatus.ACTIVE:
            raise ConflictError(f"Cannot pause from status {workflow.status}")
        workflow.status = WorkflowStatus.PAUSED
        await session.commit()
        await session.refresh(workflow)
        return workflow

    async def resume(self, session: AsyncSession, workflow: Workflow) -> Workflow:
        if workflow.status != WorkflowStatus.PAUSED:
            raise ConflictError(f"Cannot resume from status {workflow.status}")
        if workflow.current_version == 0:
            raise ConflictError("Workflow has no published version")
        workflow.status = WorkflowStatus.ACTIVE
        await session.commit()
        await session.refresh(workflow)
        return workflow

    async def archive(self, session: AsyncSession, workflow: Workflow) -> Workflow:
        if workflow.status == WorkflowStatus.ARCHIVED:
            raise ConflictError("Workflow is already archived")
        workflow.status = WorkflowStatus.ARCHIVED
        workflow.archived_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(workflow)
        return workflow

    async def duplicate(
        self, session: AsyncSession, workflow: Workflow,
        *, user_id: uuid.UUID | None = None,
    ) -> Workflow:
        """Duplicate → a DRAFT copy (never auto-activated, §47)."""
        copy = Workflow(
            name=f"{workflow.name} (copy)"[:_MAX_NAME],
            description=workflow.description,
            trigger_type=workflow.trigger_type,
            status=WorkflowStatus.DRAFT,
            current_version=0,
            created_by=user_id,
            updated_by=user_id,
        )
        session.add(copy)
        await session.flush()
        definition = await self._definition_of(session, workflow)
        session.add(WorkflowVersion(
            workflow_id=copy.id,
            version=1,
            definition=definition,
            checksum=_checksum(definition),
            status=WorkflowVersionStatus.DRAFT,
            created_by=user_id,
        ))
        await session.commit()
        await session.refresh(copy)
        return copy

    # --------------------------------------------------------------- versions
    async def list_versions(
        self, session: AsyncSession, workflow: Workflow,
    ) -> list[WorkflowVersion]:
        rows = (await session.execute(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow.id)
            .order_by(desc(WorkflowVersion.version))
        )).scalars().all()
        return list(rows)

    async def definition_of(self, session: AsyncSession, workflow: Workflow) -> dict:
        return await self._definition_of(session, workflow)

    async def _definition_of(
        self, session: AsyncSession, workflow: Workflow,
        draft: WorkflowVersion | None = None,
    ) -> dict:
        if draft is not None:
            return draft.definition
        row = (await session.execute(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow.id)
            .order_by(desc(WorkflowVersion.version)).limit(1)
        )).scalars().first()
        return row.definition if row is not None else {}

    # ------------------------------------------------------------- validation
    def parse_definition(self, definition: Any) -> WorkflowDefinition:
        if not isinstance(definition, dict):
            raise ValidationError(["definition must be an object"])
        try:
            return WorkflowDefinition.model_validate(definition)
        except Exception as exc:  # pydantic errors
            issues = [str(e) for e in getattr(exc, "errors", lambda: [])()]
            if not issues:
                issues = [f"Invalid definition: {exc}"]
            # cap the issue list; keep messages bounded
            raise ValidationError([i[:300] for i in issues[:20]]) from exc

    def validate_definition(
        self, session: AsyncSession | None, parsed: WorkflowDefinition,
        *, user_id: uuid.UUID | None = None,
    ) -> None:
        """Full node-level validation (§21): field/operator/value, action
        configs (DB-aware when a session is provided), trigger config."""
        issues: list[str] = []
        registry = build_trigger_registry()
        actions = build_action_registry()

        for node in parsed.nodes:
            try:
                if node.type == "TRIGGER":
                    trigger = registry.get((node.trigger_config or {}).get("type"))
                    if trigger is None:
                        raise ConfigurationError(
                            f"Unknown trigger type: {(node.trigger_config or {}).get('type')!r}"
                        )
                    trigger.validate_config(node.trigger_config or {})
                elif node.type == "CONDITION":
                    validate_node_condition("CONDITION",
                                            node.condition.model_dump(by_alias=True, exclude_none=True) if node.condition else None)
                elif node.type == "BRANCH":
                    if node.branches:
                        for branch in node.branches:
                            validate_node_condition("BRANCH",
                                                    branch.condition.model_dump(by_alias=True, exclude_none=True))
                elif node.type == "ACTION":
                    action = actions.get(node.action)
                    if action is None:
                        raise ConfigurationError(f"Unknown action: {node.action!r}")
                    action.validate_config(node.config or {}, session=session)
            except ConfigurationError as exc:
                issues.append(f"Node {node.id}: {exc}")
            except ValidationError as exc:
                issues.extend(f"Node {node.id}: {i}" for i in exc.issues)

        if issues:
            raise ValidationError(issues[:50])

    # ------------------------------------------------------------------ misc
    def trigger_label(self, session: AsyncSession, trigger_type: str) -> str:
        trigger = build_trigger_registry().get(trigger_type)
        return trigger.describe({}) if trigger else trigger_type

    def _max_nodes(self) -> int:
        from app.core.config import get_settings

        return getattr(get_settings(), "QBIT_AUTOMATION_MAX_NODES", 50)
