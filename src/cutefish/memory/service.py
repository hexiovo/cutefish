"""Bounded memory operations with person-first isolation and non-destructive correction."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from cutefish.chat.models import ChatDraft, Conversation

from .models import MemoryItem, MemoryPerson


OWNER_PERSON_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://cutefish.local/person/owner/v1"))
SCOPES = frozenset({"production", "calibration"})
MEMORY_KINDS = frozenset({"fact", "preference", "event", "shared_context", "fiction_frame", "correction"})


class MemoryError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    async def _invalidate_person_context(self, session, *, person_id: str, scope: str, now: datetime) -> None:
        conversations = (
            await session.scalars(
                select(Conversation).where(
                    Conversation.person_id == person_id,
                    Conversation.scope == scope,
                )
            )
        ).all()
        for conversation in conversations:
            conversation.revision += 1
            conversation.updated_at = now

        drafts = (
            await session.scalars(
                select(ChatDraft).where(
                    ChatDraft.person_id == person_id,
                    ChatDraft.scope == scope,
                    ChatDraft.state == "review",
                )
            )
        ).all()
        for draft in drafts:
            draft.state = "stale"
            draft.stale_reason = "memory_changed"
            draft.revision += 1
            draft.updated_at = now

    async def ensure_owner(self) -> MemoryPerson:
        async with self.session_factory() as session:
            async with session.begin():
                owner = await session.get(MemoryPerson, OWNER_PERSON_ID)
                if owner is None:
                    owner = MemoryPerson(
                        person_id=OWNER_PERSON_ID,
                        display_name="管理员",
                        role="owner",
                        state="active",
                        chat_only=False,
                    )
                    session.add(owner)
                    await session.flush()
                return owner

    async def list_people(self) -> list[dict]:
        await self.ensure_owner()
        async with self.session_factory() as session:
            rows = (await session.scalars(select(MemoryPerson).order_by(MemoryPerson.created_at))).all()
            return [self.serialize_person(row) for row in rows]

    async def add_contact(self, display_name: str) -> dict:
        name = display_name.strip()
        if not name or len(name) > 128:
            raise MemoryError("invalid_person_name", "联系人名称不能为空且最多128个字符。")
        person = MemoryPerson(
            person_id=str(uuid.uuid4()),
            display_name=name,
            role="contact",
            state="active",
            chat_only=True,
        )
        async with self.session_factory() as session:
            async with session.begin():
                session.add(person)
                await session.flush()
                return self.serialize_person(person)

    async def get_person(self, person_id: str) -> MemoryPerson:
        async with self.session_factory() as session:
            row = await session.get(MemoryPerson, person_id)
            if row is None:
                raise MemoryError("person_not_found", "人物资料不存在。", 404)
            return row

    async def create_item(
        self,
        *,
        person_id: str,
        scope: str,
        kind: str,
        content: str,
        source_message_id: str | None = None,
        source_event_id: str | None = None,
        source_reference: str | None = None,
        source_locator: str | None = None,
        effective_from: datetime | None = None,
        effective_until: datetime | None = None,
        metadata: dict | None = None,
    ) -> dict:
        self._validate_scope(scope)
        self._validate_kind(kind)
        self._validate_content(content)
        if not any((source_message_id, source_event_id, source_reference)):
            raise MemoryError("memory_source_required", "记忆必须保留至少一个原文来源。")
        if effective_from is not None:
            effective_from = self._utc(effective_from)
        if effective_until is not None:
            effective_until = self._utc(effective_until)
        if effective_until is not None and effective_from is not None and effective_until <= effective_from:
            raise MemoryError("invalid_memory_time", "记忆失效时间必须晚于生效时间。")
        async with self.session_factory() as session:
            async with session.begin():
                person = await session.get(MemoryPerson, person_id)
                if person is None or person.state != "active":
                    raise MemoryError("person_not_found", "人物资料不存在或已停用。", 404)
                item = MemoryItem(
                    id=str(uuid.uuid4()),
                    person_id=person_id,
                    scope=scope,
                    kind=kind,
                    content=content.strip(),
                    source_message_id=source_message_id,
                    source_event_id=source_event_id,
                    source_reference=source_reference,
                    source_locator=source_locator,
                    effective_from=effective_from or utc_now(),
                    effective_until=effective_until,
                    metadata_json=metadata or {},
                    version=1,
                )
                session.add(item)
                await session.flush()
                await self._invalidate_person_context(
                    session,
                    person_id=person_id,
                    scope=scope,
                    now=utc_now(),
                )
                return self.serialize_item(item)

    async def list_items(
        self,
        *,
        person_id: str,
        scope: str,
        include_history: bool = True,
        limit: int = 100,
    ) -> list[dict]:
        self._validate_scope(scope)
        if not 1 <= limit <= 200:
            raise MemoryError("invalid_limit", "limit必须在1到200之间。")
        async with self.session_factory() as session:
            person = await session.get(MemoryPerson, person_id)
            if person is None or person.state != "active":
                raise MemoryError("person_not_found", "人物资料不存在或已停用。", 404)
            statement = select(MemoryItem).where(
                MemoryItem.person_id == person_id,
                MemoryItem.scope == scope,
            )
            if not include_history:
                now = utc_now()
                statement = statement.where(
                    MemoryItem.corrected_by_id.is_(None),
                    MemoryItem.deleted_at.is_(None),
                    MemoryItem.effective_from <= now,
                    (MemoryItem.effective_until.is_(None) | (MemoryItem.effective_until > now)),
                )
            rows = (
                await session.scalars(statement.order_by(MemoryItem.effective_from.desc()).limit(limit))
            ).all()
            return [self.serialize_item(row) for row in rows]

    async def correct_item(
        self,
        item_id: str,
        *,
        expected_version: int,
        corrected_content: str,
        source_event_id: str,
        source_message_id: str | None = None,
    ) -> dict:
        self._validate_content(corrected_content)
        if not source_event_id or len(source_event_id) > 256:
            raise MemoryError("correction_source_required", "纠正必须带有本次来源事件ID。")
        async with self.session_factory() as session:
            async with session.begin():
                old = await session.get(MemoryItem, item_id)
                if old is None:
                    raise MemoryError("memory_not_found", "记忆不存在。", 404)
                if old.version != expected_version:
                    raise MemoryError("version_conflict", "记忆已更新，请刷新后再纠正。", 409)
                if old.deleted_at is not None:
                    raise MemoryError("memory_deleted", "已删除的记忆不能直接纠正。", 409)
                if old.corrected_by_id is not None:
                    raise MemoryError("memory_already_corrected", "该版本已被纠正；请纠正当前有效版本。", 409)
                now = utc_now()
                new_item_id = str(uuid.uuid4())
                replacement = MemoryItem(
                    id=new_item_id,
                    person_id=old.person_id,
                    scope=old.scope,
                    kind=old.kind,
                    content=corrected_content.strip(),
                    source_message_id=source_message_id,
                    source_event_id=source_event_id,
                    source_reference="owner_ui_correction",
                    effective_from=now,
                    supersedes_id=old.id,
                    metadata_json={},
                    version=old.version + 1,
                    created_at=now,
                )
                session.add(replacement)
                # The replacement references the old item, and the old item then
                # points back to the replacement. Insert first because SQLite
                # checks these foreign keys immediately during each statement.
                await session.flush()
                old.corrected_by_id = new_item_id
                old.effective_until = now
                old.invalidation_reason = "corrected"
                old.version += 1
                await session.flush()
                await self._invalidate_person_context(
                    session,
                    person_id=old.person_id,
                    scope=old.scope,
                    now=now,
                )
                return self.serialize_item(replacement)

    async def delete_item(self, item_id: str, *, expected_version: int) -> dict:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(MemoryItem, item_id)
                if row is None:
                    raise MemoryError("memory_not_found", "记忆不存在。", 404)
                if row.version != expected_version:
                    raise MemoryError("version_conflict", "记忆已更新，请刷新后重试。", 409)
                row.deleted_at = utc_now()
                row.invalidation_reason = "owner_deleted"
                row.version += 1
                await session.flush()
                await self._invalidate_person_context(
                    session,
                    person_id=row.person_id,
                    scope=row.scope,
                    now=self._utc(row.deleted_at),
                )
                return self.serialize_item(row)

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if scope not in SCOPES:
            raise MemoryError("invalid_scope", "scope只能是production或calibration。")

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in MEMORY_KINDS:
            raise MemoryError("invalid_memory_kind", "记忆类型不受支持。")

    @staticmethod
    def _validate_content(content: str) -> None:
        if not content or not content.strip() or len(content) > 12000:
            raise MemoryError("invalid_memory_content", "记忆内容不能为空且最多12000个字符。")

    @staticmethod
    def serialize_person(row: MemoryPerson) -> dict:
        return {
            "person_id": row.person_id,
            "display_name": row.display_name,
            "role": row.role,
            "state": row.state,
            "chat_only": bool(row.chat_only),
        }

    @staticmethod
    def serialize_item(row: MemoryItem) -> dict:
        now = utc_now()
        effective_from = MemoryService._utc(row.effective_from)
        effective_until = MemoryService._utc(row.effective_until) if row.effective_until is not None else None
        created_at = MemoryService._utc(row.created_at)
        if row.deleted_at is not None:
            status = "deleted"
        elif row.corrected_by_id is not None:
            status = "corrected"
        elif effective_until is not None and effective_until <= now:
            status = "expired"
        elif effective_from > now:
            status = "scheduled"
        else:
            status = "active"
        return {
            "id": row.id,
            "person_id": row.person_id,
            "scope": row.scope,
            "kind": row.kind,
            "content": row.content,
            "status": status,
            "version": row.version,
            "source_message_id": row.source_message_id,
            "source_event_id": row.source_event_id,
            "source_reference": row.source_reference,
            "source_locator": row.source_locator,
            "valid_from": effective_from.isoformat(),
            "valid_until": effective_until.isoformat() if effective_until else None,
            "supersedes_id": row.supersedes_id,
            "corrected_by_id": row.corrected_by_id,
            "invalidation_reason": row.invalidation_reason,
            "created_at": created_at.isoformat(),
        }
