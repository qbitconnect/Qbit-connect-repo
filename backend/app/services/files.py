"""FileService — metadata (PostgreSQL) + content (StorageService) (Brief §7, §22, §24).

Security model: clients reference files by id only. Storage keys are generated
server-side; downloads resolve id → metadata → validated path → streamed file.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import BinaryIO

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.logging import get_logger
from app.models.file import FileCategory, FileRecord
from app.services.audit import AuditService
from app.services.storage import StorageService

logger = get_logger("qbit.files")

ALLOWED_DELETE_CATEGORIES = {c.value for c in FileCategory}


class FileService:
    def __init__(self, storage: StorageService, audit: AuditService) -> None:
        self.storage = storage
        self.audit = audit

    async def store(
        self,
        session: AsyncSession,
        *,
        content: BinaryIO,
        filename: str,
        mime_type: str | None,
        category: str = FileCategory.OTHER.value,
        created_by: uuid.UUID | None = None,
        metadata: dict | None = None,
        max_bytes: int | None = None,
    ) -> FileRecord:
        """Stream `content` into storage, then persist metadata in one flow."""
        if category not in ALLOWED_DELETE_CATEGORIES:
            raise NotFoundError(f"Unknown file category: {category}")

        from app.core.path_safety import safe_filename

        key = self.storage.generate_key(category, filename)
        meta = self.storage.save(key, content, category=category)

        if max_bytes is not None and meta.size > max_bytes:
            self.storage.delete(key, category=category)
            from fastapi import HTTPException

            raise HTTPException(status_code=413, detail="Uploaded file exceeds the size limit")

        record = FileRecord(
            name=safe_filename(filename),  # sanitized — raw client names are never persisted
            path=key,
            mime_type=mime_type,
            size=meta.size,
            storage_backend=self.storage.backend_name,
            category=category,
            created_by=created_by,
            checksum_sha256=meta.checksum_sha256,
            metadata_json={**(metadata or {}), "original_name": filename},
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)

        await self.audit.log(
            session,
            action="file.created",
            resource_type="file",
            resource_id=str(record.id),
            actor_user_id=created_by,
            metadata={"category": category, "size": record.size, "name": record.name},
        )
        return record

    async def get(self, session: AsyncSession, file_id: uuid.UUID) -> FileRecord:
        record = await session.get(FileRecord, file_id)
        if record is None or record.is_deleted:
            raise NotFoundError("File not found")
        return record

    async def list(
        self,
        session: AsyncSession,
        *,
        category: str | None = None,
        page: int = 1,
        page_size: int = 25,
        include_deleted: bool = False,
    ) -> tuple[list[FileRecord], int]:
        query = select(FileRecord)
        if not include_deleted:
            query = query.where(FileRecord.deleted_at.is_(None))
        if category:
            query = query.where(FileRecord.category == category)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await session.execute(
            query.order_by(FileRecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def open_download(self, session: AsyncSession, file_id: uuid.UUID):
        """id → metadata → validated path → file handle (Brief §22). Never trust client paths."""
        record = await self.get(session, file_id)
        stream = self.storage.open(record.path, category=record.category)
        return record, stream

    def filesystem_path(self, record: FileRecord):
        """Validated absolute path for streaming responses (server-side only)."""
        from app.core.path_safety import validate_storage_key

        root = self.storage.root_for(record.category)
        return validate_storage_key(record.path, root)

    async def delete(
        self,
        session: AsyncSession,
        file_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID | None,
    ) -> FileRecord:
        record = await self.get(session, file_id)
        if record.category == FileCategory.BACKUP.value:
            # Extra guard: backups are never deleted through the file API.
            raise PermissionDeniedError("Backup files are managed by the backup service")

        self.storage.delete(record.path, category=record.category)
        record.deleted_at = datetime.now(timezone.utc)
        await session.execute(
            update(FileRecord).where(FileRecord.id == record.id).values(deleted_at=record.deleted_at)
        )
        await session.commit()

        await self.audit.log(
            session,
            action="file.deleted",
            resource_type="file",
            resource_id=str(record.id),
            actor_user_id=actor_user_id,
            metadata={"name": record.name, "category": record.category},
        )
        return record

    async def purge_rows_for_missing_objects(self, session: AsyncSession) -> int:
        """Operational helper (CLI only): mark rows whose physical object vanished.

        Never deletes data on its own — only reconciles metadata with reality.
        """
        missing: list[FileRecord] = []
        rows = await session.execute(
            select(FileRecord).where(FileRecord.deleted_at.is_(None))
        )
        for record in rows.scalars():
            if not self.storage.exists(record.path, category=record.category):
                missing.append(record)
        if missing:
            await session.execute(
                sa_delete(FileRecord).where(
                    FileRecord.id.in_([m.id for m in missing])
                )
            )
            await session.commit()
        return len(missing)
