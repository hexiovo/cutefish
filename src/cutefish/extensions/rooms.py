"""DB-indexed room records and explicitly authorized managed media."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from sqlalchemy import func, select

from cutefish.core.paths import HostPathResolver, PathPolicyError

from .contracts import SourceReference
from .media_manifest import hash_stream, validate_relative_path
from .models import ManagedMediaRecord, RoomAuthorization, RoomDiskState, RoomItem, utc_now


ROOM_KINDS = frozenset({"collection", "note", "interest", "plan", "work", "album", "share_candidate"})
ROOM_STATES = frozenset({"pending", "ready", "trashed", "unavailable"})
ROOM_ROOTS = frozenset({"room_root", "gallery_root"})
ROOM_ACTIONS = frozenset({"read", "create", "update", "rename", "soft_delete", "restore", "export", "register_media"})
MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_MEDIA_BYTES = 64 * 1024 * 1024


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _dedupe_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str | None, str | None]] = set()
    result = []
    for ref in refs:
        key = (ref["kind"], ref["id"], ref.get("person_id"), ref.get("scope"))
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return result


def _source_person(person_ids: set[str]) -> str | None:
    if len(person_ids) > 1:
        raise RoomError("source_person_mismatch", "来源同时属于多个人，不能放入同一房间条目。", 403)
    return next(iter(person_ids), None)


def _mime_for_path(relative_path: str) -> str:
    mime_type, _encoding = mimetypes.guess_type(PurePosixPath(relative_path).name, strict=False)
    return mime_type or "application/octet-stream"


def _validate_mime(mime_type: str, relative_path: str) -> None:
    expected = _mime_for_path(relative_path)
    if mime_type != expected:
        raise RoomError("media_mime_mismatch", "登记的 MIME 类型与逻辑路径扩展名不匹配。", 422)
    if mime_type == "application/octet-stream":
        raise RoomError("unsupported_media_type", "无法从路径识别媒体类型。", 422)


def _validate_mime_bytes(header: bytes, mime_type: str) -> None:
    """Check common media signatures without loading an optional parser."""
    checks: dict[str, Callable[[bytes], bool]] = {
        "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
        "image/gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
        "image/bmp": lambda data: data.startswith(b"BM"),
        "application/pdf": lambda data: data.startswith(b"%PDF-"),
        "audio/x-wav": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE",
        "audio/wav": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE",
        "audio/flac": lambda data: data.startswith(b"fLaC"),
        "audio/ogg": lambda data: data.startswith(b"OggS"),
        "audio/mpeg": lambda data: data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0),
        "video/mp4": lambda data: len(data) >= 12 and data[4:8] == b"ftyp",
        "video/webm": lambda data: data.startswith(b"\x1aE\xdf\xa3"),
        "video/x-msvideo": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI ",
        "application/zip": lambda data: data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")),
        "text/plain": lambda data: b"\x00" not in data and _is_utf8(data),
    }
    check = checks.get(mime_type)
    if check is not None and not check(header):
        raise RoomError("media_mime_mismatch", "媒体内容签名与登记 MIME 类型不一致。", 422)


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


class RoomError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class RoomSourceAuthorizer:
    """Resolve each persisted source reference against its authoritative row."""

    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    async def normalize(
        self,
        session,
        refs: list[dict[str, Any] | SourceReference],
        source_scope: str,
        action: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if source_scope not in {"production", "calibration"} or not refs:
            raise RoomError("invalid_source_scope", "来源引用与有效用途范围均为必需项。")
        normalized: list[dict[str, Any]] = []
        person_ids: set[str] = set()

        for raw_ref in refs:
            try:
                ref = SourceReference.model_validate(raw_ref)
            except (TypeError, ValueError) as exc:
                raise RoomError("invalid_source_reference", "来源引用格式无效或来源类型未知。") from exc
            ref_id = ref.id.strip()
            if not ref_id:
                raise RoomError("invalid_source_reference", "来源引用 ID 不能为空。")
            if ref.scope is not None and ref.scope != source_scope:
                raise RoomError("source_scope_mismatch", "来源用途范围与房间条目不一致。", 403)

            if ref.kind == "owner_supplied":
                # This declaration is accepted only on authenticated owner routes.
                # It establishes origin, never public-release permission.
                if ref.person_id is not None:
                    raise RoomError("invalid_source_reference", "owner_supplied 来源不能声明 person_id。")
                normalized.append({"kind": ref.kind, "id": ref_id, "scope": source_scope})
                continue

            if ref.kind == "room_item":
                target = await session.get(RoomItem, ref_id)
                if target is None or target.state != "ready":
                    raise RoomError("unknown_source_reference", "房间来源不存在或当前不可用。", 403)
                if target.source_scope != source_scope:
                    raise RoomError("source_scope_mismatch", "房间来源用途范围不一致。", 403)
                person_id = target.source_person_id
            elif ref.kind == "managed_media":
                target = await session.get(ManagedMediaRecord, ref_id)
                if target is None or not target.role_use_approved:
                    raise RoomError("unknown_source_reference", "媒体来源不存在或未获角色使用批准。", 403)
                if target.source_scope != source_scope:
                    raise RoomError("source_scope_mismatch", "媒体来源用途范围不一致。", 403)
                person_id = target.source_person_id
            elif ref.kind == "chat_event":
                from cutefish.chat.models import ChatMessage, Conversation

                target = await session.get(ChatMessage, ref_id)
                conversation = await session.get(Conversation, target.conversation_id) if target is not None else None
                if (
                    target is None
                    or conversation is None
                    or target.person_id != conversation.person_id
                    or target.scope != conversation.scope
                    or target.scope != source_scope
                ):
                    raise RoomError("unknown_source_reference", "聊天来源不存在，或无法核验其 person/scope。", 403)
                person_id = target.person_id
            else:  # The Pydantic DTO is intentionally closed, retain a safe guard.
                raise RoomError("invalid_source_reference", "来源引用类型未知。")

            if ref.person_id is not None and ref.person_id != person_id:
                raise RoomError("source_person_mismatch", "来源 person_id 与已登记数据不一致。", 403)
            if person_id is not None:
                person_ids.add(person_id)
            canonical = {"kind": ref.kind, "id": ref_id, "scope": source_scope}
            if person_id is not None:
                canonical["person_id"] = person_id
            normalized.append(canonical)

        return _dedupe_refs(normalized), _source_person(person_ids)

    async def shareable(self, session, refs: list[dict[str, Any] | SourceReference]) -> bool:
        """Check upstream grants before an item is marked shareable.

        owner_supplied is accepted only alongside the separate explicit
        share_allowed flag on the room item itself. Chat events carry no public
        release grant in the current schema, so they cannot be made shareable.
        """
        for raw_ref in refs:
            try:
                ref = SourceReference.model_validate(raw_ref)
            except (TypeError, ValueError):
                return False
            if ref.kind == "owner_supplied":
                continue
            if ref.kind == "room_item":
                target = await session.get(RoomItem, ref.id)
                if target is None or not target.share_allowed:
                    return False
            elif ref.kind == "managed_media":
                target = await session.get(ManagedMediaRecord, ref.id)
                if target is None or not target.share_allowed:
                    return False
            else:
                return False
        return bool(refs)

    async def __call__(self, refs: list[dict[str, Any]], source_scope: str, action: str) -> bool:
        if self.session_factory is None:
            return False
        try:
            async with self.session_factory() as session:
                await self.normalize(session, refs, source_scope, action)
            return True
        except RoomError:
            return False


class ManagedMediaReader:
    """Resolve only a registered logical path and verify its stored manifest."""

    def __init__(self, paths: HostPathResolver):
        self.paths = paths

    def __call__(self, asset: ManagedMediaRecord) -> bytes:
        if asset.root_key not in ROOM_ROOTS:
            raise RoomError("invalid_media_root", "媒体登记根目录不受支持。", 403)
        try:
            path = self.paths.resolve(asset.root_key, asset.relative_path, must_exist=True)
            if not path.is_file():
                raise RoomError("managed_media_unavailable", "登记媒体不是可读取文件。", 409)
            stat_before = path.stat()
            if stat_before.st_size != asset.byte_length or stat_before.st_size > MAX_MEDIA_BYTES:
                raise RoomError("media_integrity_error", "媒体文件大小与登记清单不一致。", 409)
            with path.open("rb") as stream:
                header = stream.read(8192)
                stream.seek(0)
                raw = stream.read(MAX_MEDIA_BYTES + 1)
            if len(raw) > MAX_MEDIA_BYTES or len(raw) != asset.byte_length:
                raise RoomError("media_integrity_error", "媒体文件大小与登记清单不一致。", 409)
            stat_after = path.stat()
        except RoomError:
            raise
        except (PathPolicyError, FileNotFoundError, OSError, ValueError) as exc:
            raise RoomError("managed_media_unavailable", "登记媒体当前无法通过本机路径策略读取。", 409) from exc
        if stat_before.st_mtime_ns != stat_after.st_mtime_ns or stat_before.st_size != stat_after.st_size:
            raise RoomError("media_integrity_error", "读取期间媒体文件发生变化。", 409)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != asset.sha256:
            raise RoomError("media_integrity_error", "媒体文件 hash 与登记清单不一致。", 409)
        _validate_mime(asset.mime_type, asset.relative_path)
        _validate_mime_bytes(raw[:8192], asset.mime_type)
        return raw


class RoomService:
    """Room metadata is DB-only; body and media access are separate operations."""

    def __init__(
        self,
        session_factory,
        paths: HostPathResolver,
        *,
        source_authorizer: Callable[..., Any] | RoomSourceAuthorizer | None = None,
        media_reader: Callable[..., bytes] | None = None,
    ):
        self.session_factory = session_factory
        self.paths = paths
        self.source_authorizer = source_authorizer or RoomSourceAuthorizer()
        self.media_reader = media_reader or ManagedMediaReader(paths)

    async def status(self) -> dict[str, Any]:
        """Return cached DB state and logical-root configuration without probing disk."""
        async with self.session_factory() as session:
            auth_rows = (
                await session.scalars(
                    select(RoomAuthorization)
                    .where(RoomAuthorization.root_key.in_(ROOM_ROOTS))
                    .order_by(RoomAuthorization.revision.desc(), RoomAuthorization.granted_at.desc())
                )
            ).all()
            root_disk = {
                root: await session.get(RoomDiskState, root)
                for root in ROOM_ROOTS
            }
        auth_by_root = {
            root: [row for row in auth_rows if row.root_key == root]
            for root in ROOM_ROOTS
        }
        roots = []
        for root in sorted(ROOM_ROOTS):
            disk = root_disk[root]
            rows = auth_by_root[root]
            active = next((row for row in rows if row.revoked_at is None), None)
            latest = rows[0] if rows else None
            roots.append(
                {
                    "root_key": root,
                    "configured": self.paths.configured(root),
                    "disk_state": disk.disk_state if disk else ("unknown" if self.paths.configured(root) else "unavailable"),
                    "authorization_state": "granted" if active else ("revoked" if latest else "needs_authorization"),
                    "revision": latest.revision if latest else 0,
                }
            )
        room_root = next(item for item in roots if item["root_key"] == "room_root")
        return {
            "root_key": "room_root",
            "configured": room_root["configured"],
            "disk_state": room_root["disk_state"],
            "authorization_state": room_root["authorization_state"],
            "revision": room_root["revision"],
            "roots": roots,
        }

    async def put_authorization(self, payload: dict[str, Any], *, granted_by: str) -> dict[str, Any]:
        root_key = str(payload.get("root_key", ""))
        if root_key not in ROOM_ROOTS:
            raise RoomError("invalid_room_root", "只支持 room_root 或 gallery_root 授权。")
        if not self.paths.configured(root_key):
            raise RoomError("room_root_unconfigured", f"{root_key} 尚未配置。", 409)
        try:
            prefix = self._normalize_path(payload.get("relative_prefix", ""), allow_empty=True)
            self.paths.resolve(root_key, prefix, for_write=True)
        except (ValueError, PathPolicyError) as exc:
            raise RoomError("invalid_room_path", "授权路径不符合本机房间路径策略。", 422) from exc
        actions = list(dict.fromkeys(str(action) for action in payload.get("allowed_actions", [])))
        if not actions or any(action not in ROOM_ACTIONS for action in actions):
            raise RoomError("invalid_room_actions", "授权动作列表为空或含不支持的动作。")
        expected = int(payload.get("expected_revision", -1))
        async with self.session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(RoomAuthorization)
                        .where(RoomAuthorization.root_key == root_key)
                        .order_by(RoomAuthorization.revision.desc(), RoomAuthorization.granted_at.desc())
                    )
                ).all()
                latest = rows[0] if rows else None
                current_revision = latest.revision if latest else 0
                if current_revision != expected:
                    raise RoomError("room_authorization_conflict", "房间授权已被更新，请刷新后重试。", 409)
                active = next((row for row in rows if row.revoked_at is None), None)
                now = utc_now()
                if active is not None:
                    active.relative_prefix = prefix
                    active.allowed_actions = actions
                    active.revision = current_revision + 1
                    active.granted_by = str(granted_by)
                    active.granted_at = now
                    active.revoked_at = None
                    row = active
                else:
                    row = RoomAuthorization(
                        id=str(uuid.uuid4()),
                        root_key=root_key,
                        relative_prefix=prefix,
                        allowed_actions=actions,
                        revision=current_revision + 1,
                        granted_by=str(granted_by),
                        granted_at=now,
                        revoked_at=None,
                    )
                    session.add(row)
                await session.flush()
                return self._authorization_data(row)

    async def revoke_authorization(self, *, expected_revision: int, root_key: str = "room_root") -> dict[str, Any]:
        if root_key not in ROOM_ROOTS:
            raise RoomError("invalid_room_root", "只支持 room_root 或 gallery_root 授权。")
        async with self.session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(RoomAuthorization)
                        .where(RoomAuthorization.root_key == root_key)
                        .order_by(RoomAuthorization.revision.desc(), RoomAuthorization.granted_at.desc())
                    )
                ).all()
                latest = rows[0] if rows else None
                current_revision = latest.revision if latest else 0
                if current_revision != expected_revision:
                    raise RoomError("room_authorization_conflict", "房间授权已被更新，请刷新后重试。", 409)
                active = next((row for row in rows if row.revoked_at is None), None)
                if active is None:
                    raise RoomError("room_authorization_missing", "当前没有可撤销的房间授权。", 409)
                active.revoked_at = utc_now()
                active.revision += 1
                await session.flush()
                return self._authorization_data(active)

    async def register_media(self, payload: dict[str, Any], *, registered_by: str) -> dict[str, Any]:
        root_key = str(payload.get("root_key", ""))
        if root_key not in ROOM_ROOTS:
            raise RoomError("invalid_media_root", "媒体只能登记在 room_root 或 gallery_root 下。")
        if not self.paths.configured(root_key):
            raise RoomError("room_root_unconfigured", f"{root_key} 尚未配置。", 409)
        relative_path = self._normalize_path(payload.get("relative_path"), allow_empty=False)
        try:
            root = self.paths.root_path(root_key)
            if not root.is_dir():
                raise RoomError("room_root_unavailable", f"{root_key} 目录当前不可用。", 409)
            path = self.paths.resolve(root_key, relative_path, must_exist=True)
            if not path.is_file():
                raise RoomError("managed_media_unavailable", "媒体登记目标必须是单个文件。", 422)
            stat_before = path.stat()
            if stat_before.st_size > MAX_MEDIA_BYTES:
                raise RoomError("media_too_large", "登记媒体超过 64 MiB 上限。", 413)
            with path.open("rb") as stream:
                header = stream.read(8192)
                stream.seek(0)
                sha256, byte_length = hash_stream(stream)
            stat_after = path.stat()
            if stat_before.st_size != stat_after.st_size or stat_before.st_mtime_ns != stat_after.st_mtime_ns:
                raise RoomError("media_changed_during_registration", "登记期间媒体文件发生变化。", 409)
        except RoomError:
            raise
        except (PathPolicyError, FileNotFoundError, OSError, ValueError) as exc:
            raise RoomError("managed_media_unavailable", "目标文件未通过本机路径校验。", 409) from exc
        mime_type = str(payload.get("mime_type") or _mime_for_path(relative_path)).strip().lower()
        _validate_mime(mime_type, relative_path)
        _validate_mime_bytes(header, mime_type)
        if byte_length > MAX_MEDIA_BYTES:
            raise RoomError("media_too_large", "登记媒体超过 64 MiB 上限。", 413)

        source_scope = str(payload.get("source_scope", ""))
        raw_refs = list(payload.get("source_refs") or [])
        if not raw_refs:
            raise RoomError("invalid_room_item", "媒体登记必须说明来源引用。")
        media_id = str(uuid.uuid4())
        requested_person_id = payload.get("source_person_id")
        async with self.session_factory() as session:
            async with session.begin():
                grant = await self._require_grant(session, "register_media", relative_path, root_key)
                self._validate_item_path(root_key, relative_path, grant, for_write=False)
                normalized_refs, source_person_id = await self._authorize_refs(
                    session, raw_refs, source_scope, "create"
                )
                if requested_person_id is not None and str(requested_person_id) != source_person_id:
                    raise RoomError("source_person_mismatch", "媒体来源 person_id 与引用记录不一致。", 403)
                existing = await session.scalar(
                    select(ManagedMediaRecord).where(
                        ManagedMediaRecord.root_key == root_key,
                        ManagedMediaRecord.relative_path == relative_path,
                    )
                )
                now = utc_now()
                if existing is None:
                    record = ManagedMediaRecord(
                        id=media_id,
                        root_key=root_key,
                        relative_path=relative_path,
                        sha256=sha256,
                        mime_type=mime_type,
                        byte_length=byte_length,
                        source_refs=normalized_refs,
                        source_scope=source_scope,
                        source_person_id=source_person_id,
                        role_use_approved=bool(payload.get("role_use_approved", False)),
                        share_allowed=bool(payload.get("share_allowed", False)),
                        registered_by=str(registered_by),
                        version=1,
                        created_at=now,
                        legacy_state="registered",
                        legacy_media_type=mime_type,
                        legacy_size_bytes=byte_length,
                        legacy_updated_at=now,
                    )
                    session.add(record)
                else:
                    record = existing
                    record.sha256 = sha256
                    record.mime_type = mime_type
                    record.byte_length = byte_length
                    record.source_refs = normalized_refs
                    record.source_scope = source_scope
                    record.source_person_id = source_person_id
                    record.role_use_approved = bool(payload.get("role_use_approved", False))
                    record.share_allowed = bool(payload.get("share_allowed", False))
                    record.registered_by = str(registered_by)
                    record.version = max(int(record.version or 0), 0) + 1
                    record.created_at = record.created_at or now
                    record.legacy_state = "registered"
                    record.legacy_media_type = mime_type
                    record.legacy_size_bytes = byte_length
                    record.legacy_updated_at = now
                disk = await session.get(RoomDiskState, root_key)
                if disk is None:
                    session.add(RoomDiskState(root_key=root_key, disk_state="available", updated_at=now))
                else:
                    disk.disk_state = "available"
                    disk.updated_at = now
                await session.flush()
                return self.serialize_media(record)

    async def list(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if kind is not None and kind not in ROOM_KINDS:
            raise RoomError("invalid_room_kind", "房间条目类型不受支持。")
        if state is not None and state not in ROOM_STATES:
            raise RoomError("invalid_room_state", "房间条目状态不受支持。")
        async with self.session_factory() as session:
            statement = select(RoomItem)
            count_statement = select(func.count()).select_from(RoomItem)
            if kind:
                statement = statement.where(RoomItem.kind == kind)
                count_statement = count_statement.where(RoomItem.kind == kind)
            if state:
                statement = statement.where(RoomItem.state == state)
                count_statement = count_statement.where(RoomItem.state == state)
            rows = (
                await session.scalars(
                    statement.order_by(RoomItem.updated_at.desc(), RoomItem.id).limit(limit).offset(offset)
                )
            ).all()
            total = int((await session.scalar(count_statement)) or 0)
            return {"items": [self.serialize(row) for row in rows], "total": total}

    async def get(self, item_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(RoomItem, item_id)
            if row is None:
                raise RoomError("room_item_not_found", "房间条目不存在。", 404)
            return self.serialize(row)

    async def read_content(self, item_id: str) -> dict[str, Any]:
        """Read one item's body through a separately authorized operation."""
        async with self.session_factory() as session:
            row = await session.get(RoomItem, item_id)
            if row is None:
                raise RoomError("room_item_not_found", "房间条目不存在。", 404)
            if row.state != "ready":
                raise RoomError("invalid_room_item_state", "只有可用条目可以读取正文。", 409)
            if not await self._source_allowed(session, row.source_refs, row.source_scope, "read"):
                raise RoomError("source_scope_denied", "来源范围未获正文读取授权。", 403)
            if row.content_text is not None:
                grant = await self._require_grant(session, "read", row.relative_path, row.root_key)
                self._validate_item_path(row.root_key, row.relative_path, grant, for_write=False)
                raw = row.content_text.encode("utf-8")
                digest = hashlib.sha256(raw).hexdigest()
                if row.content_hash is not None and row.content_hash != digest:
                    raise RoomError("room_content_integrity_error", "房间正文 hash 校验失败。", 409)
                return {
                    "item_id": row.id,
                    "encoding": "utf-8",
                    "media_type": "text/plain; charset=utf-8",
                    "content_hash": digest,
                    "data": row.content_text,
                }
            if row.managed_media_id:
                asset = await self._media_asset(session, row)
                if not await self._source_allowed(session, asset.source_refs, asset.source_scope, "read"):
                    raise RoomError("source_scope_denied", "媒体来源未获正文读取授权。", 403)
                grant = await self._require_grant(session, "read", asset.relative_path, asset.root_key)
                self._validate_item_path(asset.root_key, asset.relative_path, grant, for_write=False)
                raw = self._read_media_asset(asset)
                return {
                    "item_id": row.id,
                    "encoding": "base64",
                    "media_type": asset.mime_type,
                    "content_hash": hashlib.sha256(raw).hexdigest(),
                    "data": base64.b64encode(raw).decode("ascii"),
                }
            raise RoomError("room_content_unavailable", "条目正文或已登记媒体目前不可读取。", 409)

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = payload.get("kind")
        if kind not in ROOM_KINDS:
            raise RoomError("invalid_room_kind", "房间条目类型不受支持。")
        title = str(payload.get("title", "")).strip()
        source_scope = str(payload.get("source_scope", ""))
        raw_refs = list(payload.get("source_refs") or [])
        text_content = payload.get("content_text")
        media_id = str(payload.get("managed_media_id") or "").strip() or None
        if not title or source_scope not in {"production", "calibration"} or not raw_refs:
            raise RoomError("invalid_room_item", "标题、来源引用与有效来源范围均为必需项。")
        if (text_content is None) == (media_id is None):
            raise RoomError("invalid_room_content", "文字内容与媒体 ID 必须且只能提供一个。")
        item_id = str(uuid.uuid4())
        requested_path = self._normalize_path(payload.get("relative_path"), allow_empty=False, nullable=True)

        async with self.session_factory() as session:
            async with session.begin():
                media = await session.get(ManagedMediaRecord, media_id) if media_id is not None else None
                if media_id is not None and media is None:
                    raise RoomError("managed_media_not_found", "指定的媒体 ID 尚未登记。", 404)
                if media is not None:
                    self._validate_media_record(media)
                    if media.source_scope != source_scope:
                        raise RoomError("source_scope_mismatch", "媒体与房间条目的用途范围不一致。", 403)
                    if requested_path is not None and requested_path != media.relative_path:
                        raise RoomError("media_path_mismatch", "房间条目路径必须匹配登记媒体路径。", 403)
                    raw_refs.extend(media.source_refs)
                    raw_refs.append(
                        {
                            "kind": "managed_media",
                            "id": media.id,
                            "scope": media.source_scope,
                            **({"person_id": media.source_person_id} if media.source_person_id else {}),
                        }
                    )
                normalized_refs, source_person_id = await self._authorize_refs(
                    session, raw_refs, source_scope, "create"
                )
                if media is not None and source_person_id != media.source_person_id:
                    raise RoomError("source_person_mismatch", "媒体与房间条目的来源 person 不一致。", 403)
                root_key = media.root_key if media is not None else "room_root"
                relative_path = media.relative_path if media is not None else requested_path
                if media is None and not self.paths.configured("room_root"):
                    raise RoomError("room_root_unconfigured", "room_root 尚未配置。", 409)
                grant = await self._require_grant(session, "create", relative_path, root_key)
                self._validate_item_path(root_key, relative_path, grant, for_write=True)

                body = str(text_content) if text_content is not None else None
                digest = hashlib.sha256(body.encode("utf-8")).hexdigest() if body is not None else media.sha256
                persisted_refs = _dedupe_refs(
                    normalized_refs
                    + [
                        {
                            "kind": "room_item",
                            "id": item_id,
                            "scope": source_scope,
                            **({"person_id": source_person_id} if source_person_id else {}),
                        }
                    ]
                )
                share_allowed = bool(payload.get("share_allowed", False))
                if share_allowed and not await self._sources_shareable(session, normalized_refs):
                    raise RoomError("source_share_denied", "一个或多个来源未获分享批准。", 403)
                row = RoomItem(
                    id=item_id,
                    kind=kind,
                    title=title,
                    root_key=root_key,
                    relative_path=relative_path,
                    content_text=body,
                    source_refs=persisted_refs,
                    source_scope=source_scope,
                    source_person_id=source_person_id,
                    role_owned=body is not None and all(ref["kind"] == "owner_supplied" for ref in normalized_refs),
                    managed_media_id=media.id if media is not None else None,
                    version=1,
                    content_hash=digest,
                    share_allowed=share_allowed,
                    tags=list(dict.fromkeys(str(tag).strip() for tag in payload.get("tags", []) if str(tag).strip())),
                    state="ready",
                    deleted_at=None,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                session.add(row)
                await session.flush()
                return self.serialize(row)

    async def patch(self, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        expected_version = int(payload["expected_version"])
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(RoomItem, item_id)
                if row is None:
                    raise RoomError("room_item_not_found", "房间条目不存在。", 404)
                if row.version != expected_version:
                    raise RoomError("room_item_version_conflict", "房间条目已被更新，请刷新后重试。", 409)
                if row.state == "trashed":
                    raise RoomError("invalid_room_item_state", "回收站中的条目不能直接编辑。", 409)
                target_path = row.relative_path
                new_path_value = payload.get("new_relative_path")
                path_changed = False
                if new_path_value is not None:
                    target_path = self._normalize_path(new_path_value, allow_empty=False)
                    path_changed = target_path != row.relative_path
                if path_changed and row.managed_media_id:
                    raise RoomError("managed_media_path_locked", "已登记媒体路径需通过重新登记更新。", 409)
                changed_text = "content_text" in payload and payload.get("content_text") is not None
                changed_fields = any(payload.get(key) is not None for key in ("title", "tags", "share_allowed"))
                if path_changed:
                    grant = await self._require_grant(session, "rename", row.relative_path, row.root_key)
                    self._validate_item_path(row.root_key, row.relative_path, grant, for_write=True)
                    self._validate_item_path(row.root_key, target_path, grant, for_write=True)
                else:
                    action = "update" if changed_text or changed_fields else "update"
                    grant = await self._require_grant(session, action, row.relative_path, row.root_key)
                    self._validate_item_path(row.root_key, row.relative_path, grant, for_write=True)
                if changed_text and not await self._source_allowed(session, row.source_refs, row.source_scope, "update"):
                    raise RoomError("source_scope_denied", "来源范围未获内容修改授权。", 403)
                if payload.get("title") is not None:
                    value = str(payload["title"]).strip()
                    if not value:
                        raise RoomError("invalid_room_item", "标题不能为空。")
                    row.title = value
                if payload.get("tags") is not None:
                    row.tags = list(dict.fromkeys(str(tag).strip() for tag in payload["tags"] if str(tag).strip()))
                if payload.get("share_allowed") is not None:
                    new_share = bool(payload["share_allowed"])
                    source_refs = [
                        ref
                        for ref in row.source_refs
                        if not (ref.get("kind") == "room_item" and ref.get("id") == row.id)
                    ]
                    if new_share and not await self._sources_shareable(session, source_refs):
                        raise RoomError("source_share_denied", "一个或多个来源未获分享批准。", 403)
                    row.share_allowed = new_share
                if changed_text:
                    row.content_text = str(payload["content_text"])
                    row.managed_media_id = None
                    row.content_hash = hashlib.sha256(row.content_text.encode("utf-8")).hexdigest()
                    row.root_key = "room_root"
                    row.relative_path = None
                    existing_sources = [
                        ref
                        for ref in row.source_refs
                        if not (ref.get("kind") == "room_item" and ref.get("id") == row.id)
                    ]
                    row.role_owned = bool(
                        row.role_owned and all(ref.get("kind") == "owner_supplied" for ref in existing_sources)
                    )
                    row.state = "ready"
                if path_changed:
                    row.relative_path = target_path
                row.version += 1
                row.updated_at = utc_now()
                await session.flush()
                return self.serialize(row)

    async def soft_delete(self, item_id: str, *, expected_version: int) -> dict[str, Any]:
        return await self._change_state(item_id, expected_version, "soft_delete")

    async def restore(self, item_id: str, *, expected_version: int) -> dict[str, Any]:
        return await self._change_state(item_id, expected_version, "restore")

    async def _change_state(self, item_id: str, expected_version: int, action: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(RoomItem, item_id)
                if row is None:
                    raise RoomError("room_item_not_found", "房间条目不存在。", 404)
                if row.version != expected_version:
                    raise RoomError("room_item_version_conflict", "房间条目已被更新，请刷新后重试。", 409)
                if action == "soft_delete":
                    if row.state == "trashed":
                        raise RoomError("invalid_room_item_state", "条目已经在回收站中。", 409)
                    grant = await self._require_grant(session, "soft_delete", row.relative_path, row.root_key)
                    self._validate_item_path(row.root_key, row.relative_path, grant, for_write=True)
                    row.state = "trashed"
                    row.deleted_at = utc_now()
                else:
                    if row.state != "trashed":
                        raise RoomError("invalid_room_item_state", "只有回收站中的条目可以恢复。", 409)
                    grant = await self._require_grant(session, "restore", row.relative_path, row.root_key)
                    self._validate_item_path(row.root_key, row.relative_path, grant, for_write=True)
                    row.state = "ready" if row.content_text is not None or row.managed_media_id else "pending"
                    row.deleted_at = None
                row.version += 1
                row.updated_at = utc_now()
                await session.flush()
                return self.serialize(row)

    async def export(self, item_id: str, *, expected_version: int) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(RoomItem, item_id)
            if row is None:
                raise RoomError("room_item_not_found", "房间条目不存在。", 404)
            if row.version != expected_version:
                raise RoomError("room_item_version_conflict", "房间条目已被更新，请刷新后重试。", 409)
            if row.state != "ready":
                raise RoomError("invalid_room_item_state", "只有可用条目可以导出。", 409)
            if not await self._source_allowed(session, row.source_refs, row.source_scope, "export"):
                raise RoomError("source_scope_denied", "来源或用途范围未获单项导出授权。", 403)
            if row.content_text is not None:
                grant = await self._require_grant(session, "export", row.relative_path, row.root_key)
                self._validate_item_path(row.root_key, row.relative_path, grant, for_write=False)
                raw = row.content_text.encode("utf-8")
                if row.content_hash and hashlib.sha256(raw).hexdigest() != row.content_hash:
                    raise RoomError("room_content_integrity_error", "房间正文 hash 校验失败。", 409)
                encoding = "utf-8"
                media_type = "text/plain; charset=utf-8"
                data = row.content_text
                filename = f"{row.id}.txt"
            elif row.managed_media_id:
                asset = await self._media_asset(session, row)
                if not await self._source_allowed(session, asset.source_refs, asset.source_scope, "export"):
                    raise RoomError("source_scope_denied", "媒体来源未获单项导出授权。", 403)
                grant = await self._require_grant(session, "export", asset.relative_path, asset.root_key)
                self._validate_item_path(asset.root_key, asset.relative_path, grant, for_write=False)
                raw = self._read_media_asset(asset)
                if len(raw) > MAX_EXPORT_BYTES:
                    raise RoomError("room_export_too_large", "单项导出超过 8 MiB 上限。", 413)
                encoding = "base64"
                data = base64.b64encode(raw).decode("ascii")
                media_type = asset.mime_type
                filename = f"{row.id}{self._suffix_for_media_type(asset.mime_type)}"
            else:
                raise RoomError("room_content_unavailable", "条目正文或已登记媒体目前不可读取。", 409)
            if len(raw) > MAX_EXPORT_BYTES:
                raise RoomError("room_export_too_large", "单项导出超过 8 MiB 上限。", 413)
            digest = hashlib.sha256(raw).hexdigest()
            exported_at = utc_now().isoformat().replace("+00:00", "Z")
            return {
                "item": self.serialize(row),
                "manifest": {
                    "format_version": 1,
                    "exported_at": exported_at,
                    "filename": filename,
                    "sha256": digest,
                    "byte_length": len(raw),
                    "source_scope": row.source_scope,
                },
                "content": {"encoding": encoding, "media_type": str(media_type), "data": data},
            }

    async def _authorize_refs(
        self, session, refs: list[dict[str, Any]], source_scope: str, action: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        normalize = getattr(self.source_authorizer, "normalize", None)
        if normalize is not None:
            return await normalize(session, refs, source_scope, action)
        result = self.source_authorizer(refs, source_scope, action)
        if hasattr(result, "__await__"):
            result = await result
        if not result:
            raise RoomError("source_scope_denied", "来源或用途范围未获房间操作授权。", 403)
        normalized = [SourceReference.model_validate(ref).model_dump(exclude_none=True) for ref in refs]
        person_ids = {ref["person_id"] for ref in normalized if ref.get("person_id")}
        return _dedupe_refs(normalized), _source_person(person_ids)

    async def _source_allowed(self, session, refs: list[dict[str, Any]], source_scope: str, action: str) -> bool:
        try:
            await self._authorize_refs(session, refs, source_scope, action)
            return True
        except RoomError:
            return False

    async def _sources_shareable(self, session, refs: list[dict[str, Any]]) -> bool:
        checker = getattr(self.source_authorizer, "shareable", None)
        if checker is not None:
            return bool(await checker(session, refs))
        return False

    async def _media_asset(self, session, row: RoomItem) -> ManagedMediaRecord:
        asset = await session.get(ManagedMediaRecord, row.managed_media_id)
        if asset is None:
            raise RoomError("managed_media_not_found", "条目引用的媒体登记不存在。", 404)
        self._validate_media_record(asset)
        if (
            row.root_key != asset.root_key
            or row.relative_path != asset.relative_path
            or row.source_scope != asset.source_scope
            or row.source_person_id != asset.source_person_id
        ):
            raise RoomError("source_scope_mismatch", "媒体与房间条目的根目录、路径或来源身份不一致。", 403)
        _refs, person_id = await self._authorize_refs(session, asset.source_refs, asset.source_scope, "read")
        if person_id != asset.source_person_id:
            raise RoomError("source_person_mismatch", "媒体登记 person 与来源引用不一致。", 403)
        return asset

    @staticmethod
    def _validate_media_record(asset: ManagedMediaRecord) -> None:
        if asset.root_key not in ROOM_ROOTS:
            raise RoomError("invalid_media_root", "媒体登记根目录不受支持。", 403)
        if not asset.role_use_approved:
            raise RoomError("managed_media_not_approved", "媒体尚未获角色使用批准。", 403)
        if not asset.relative_path or not asset.sha256 or not asset.mime_type or asset.byte_length is None:
            raise RoomError("managed_media_unavailable", "媒体登记缺少有效清单字段。", 409)

    def _read_media_asset(self, asset: ManagedMediaRecord) -> bytes:
        result = self.media_reader(asset)
        if hasattr(result, "__await__"):
            raise RoomError("invalid_media_reader", "媒体读取器必须是同步的受限本机读取器。", 500)
        return result

    def _validate_item_path(
        self,
        root_key: str,
        relative_path: str | None,
        grant: RoomAuthorization,
        *,
        for_write: bool,
    ) -> None:
        if not self.paths.configured(root_key):
            raise RoomError("room_root_unconfigured", f"{root_key} 尚未配置。", 409)
        path = relative_path or ""
        prefix = grant.relative_prefix
        if grant.root_key != root_key:
            raise RoomError("room_path_out_of_scope", "操作授权的逻辑根目录不匹配。", 403)
        if prefix and (not path or (path != prefix and not path.startswith(prefix + "/"))):
            raise RoomError("room_path_out_of_scope", "条目路径超出已授权的房间子目录。", 403)
        try:
            self.paths.resolve(root_key, path, for_write=for_write)
        except (PathPolicyError, OSError, ValueError) as exc:
            raise RoomError("invalid_room_path", "条目相对路径未通过本机路径校验。", 403) from exc

    async def _find_grant(
        self, session, action: str, relative_path: str | None, root_key: str
    ) -> RoomAuthorization | None:
        rows = (
            await session.scalars(
                select(RoomAuthorization)
                .where(RoomAuthorization.root_key == root_key, RoomAuthorization.revoked_at.is_(None))
                .order_by(RoomAuthorization.revision.desc(), RoomAuthorization.granted_at.desc())
            )
        ).all()
        if not rows:
            return None
        grant = rows[0]
        if action not in grant.allowed_actions:
            return None
        prefix = grant.relative_prefix
        path = relative_path or ""
        if prefix and (not path or (path != prefix and not path.startswith(prefix + "/"))):
            return None
        return grant

    async def _require_grant(
        self, session, action: str, relative_path: str | None, root_key: str = "room_root"
    ) -> RoomAuthorization:
        grant = await self._find_grant(session, action, relative_path, root_key)
        if grant is None:
            raise RoomError("room_authorization_required", "此房间操作需要匹配逻辑根与路径范围的显式授权。", 403)
        return grant

    @staticmethod
    def _normalize_path(value: Any, *, allow_empty: bool, nullable: bool = False) -> str | None:
        if value is None and nullable:
            return None
        raw = str(value or "").strip()
        if not raw and allow_empty:
            return ""
        try:
            return validate_relative_path(raw)
        except ValueError as exc:
            raise RoomError("invalid_room_path", "只接受逻辑根目录下的安全相对路径。") from exc

    @staticmethod
    def _authorization_data(row: RoomAuthorization) -> dict[str, Any]:
        return {
            "root_key": row.root_key,
            "relative_prefix": row.relative_prefix,
            "allowed_actions": list(row.allowed_actions),
            "revision": row.revision,
            "granted_by": row.granted_by,
            "granted_at": _utc(row.granted_at).isoformat(),
            "revoked_at": _utc(row.revoked_at).isoformat() if row.revoked_at else None,
        }

    @staticmethod
    def serialize_media(row: ManagedMediaRecord) -> dict[str, Any]:
        return {
            "id": row.id,
            "root_key": row.root_key,
            "relative_path": row.relative_path,
            "sha256": row.sha256,
            "mime_type": row.mime_type,
            "byte_length": row.byte_length,
            "source_refs": list(row.source_refs),
            "source_scope": row.source_scope,
            "source_person_id": row.source_person_id,
            "role_use_approved": row.role_use_approved,
            "share_allowed": row.share_allowed,
            "registered_by": row.registered_by,
            "version": row.version,
            "created_at": _utc(row.created_at).isoformat(),
        }

    @staticmethod
    def serialize(row: RoomItem) -> dict[str, Any]:
        return {
            "id": row.id,
            "kind": row.kind,
            "title": row.title,
            "root_key": row.root_key,
            "relative_path": row.relative_path,
            "source_refs": list(row.source_refs),
            "source_scope": row.source_scope,
            "source_person_id": row.source_person_id,
            "role_owned": row.role_owned,
            "managed_media_id": row.managed_media_id,
            "content_hash": row.content_hash,
            "share_allowed": row.share_allowed,
            "tags": list(row.tags),
            "state": row.state,
            "version": row.version,
            "deleted_at": _utc(row.deleted_at).isoformat() if row.deleted_at else None,
            "created_at": _utc(row.created_at).isoformat(),
            "updated_at": _utc(row.updated_at).isoformat(),
        }

    @staticmethod
    def _suffix_for_media_type(media_type: str) -> str:
        suffix = mimetypes.guess_extension(media_type, strict=False)
        return suffix or ".bin"
