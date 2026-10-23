"""Authorized local image decoding with an explicit visual-model verification gate."""

from __future__ import annotations

import base64
import hashlib
import io
import importlib.util
import warnings
from typing import Any

from sqlalchemy import select

from cutefish.extensions.models import ManagedMediaRecord
from cutefish.extensions.rooms import MAX_MEDIA_BYTES, RoomError, RoomService


MAX_IMAGE_BYTES = MAX_MEDIA_BYTES
MAX_IMAGE_PIXELS = 12_000_000
SUPPORTED_MIME_TYPES = ("image/png", "image/jpeg")


class VisionError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class VisionService:
    """Decode one already registered image; never claims model observation without evidence."""

    def __init__(self, session_factory, room_service: RoomService):
        self.session_factory = session_factory
        self.room_service = room_service

    @staticmethod
    def capability() -> dict[str, Any]:
        decoder_available = importlib.util.find_spec("PIL") is not None
        return {
            "state": "needs_model_verification" if decoder_available else "needs_resource",
            "visual_model_status": "unverified",
            "pricing_status": "unverified",
            "decoder_available": decoder_available,
            "supported_mime_types": list(SUPPORTED_MIME_TYPES),
            "max_bytes": MAX_IMAGE_BYTES,
            "max_pixels": MAX_IMAGE_PIXELS,
        }

    async def observe_room_item(
        self,
        room_item_id: str,
        *,
        expected_room_version: int,
        detail_level: str = "low",
    ) -> dict[str, Any]:
        if not room_item_id or len(room_item_id) > 128:
            raise VisionError("invalid_room_item", "必须指定已登记的房间媒体条目。")
        if type(expected_room_version) is not int or expected_room_version < 1:
            raise VisionError("invalid_room_version", "房间条目版本无效。")
        if detail_level not in {"low", "high"}:
            raise VisionError("invalid_detail_level", "图像细节档位只支持 low 或 high。")

        try:
            item = await self.room_service.get(room_item_id)
        except RoomError as exc:
            raise VisionError(exc.code, exc.message, exc.status_code) from exc
        if item["version"] != expected_room_version:
            raise VisionError("room_item_stale", "房间条目已更新，请刷新后重新选择。", 409)
        if item["state"] != "ready":
            raise VisionError("room_item_unavailable", "只有当前可用的房间条目可以读取。", 409)
        if not item.get("managed_media_id"):
            raise VisionError("managed_media_required", "图像处理只接受明确登记的媒体条目。", 422)

        async with self.session_factory() as session:
            asset = await session.get(ManagedMediaRecord, item["managed_media_id"])
        if asset is None:
            raise VisionError("managed_media_not_found", "登记媒体不存在。", 404)
        if (
            asset.source_scope != item["source_scope"]
            or asset.source_person_id != item["source_person_id"]
            or asset.root_key != item["root_key"]
            or asset.relative_path != item["relative_path"]
        ):
            raise VisionError("media_scope_mismatch", "房间条目与媒体登记的人物、范围或路径不一致。", 403)
        if not asset.role_use_approved:
            raise VisionError("media_not_approved", "登记媒体尚未获角色使用批准。", 403)
        if asset.mime_type not in SUPPORTED_MIME_TYPES:
            raise VisionError("unsupported_media_type", "当前仅处理静态 PNG 和 JPEG 图像。", 415)
        if type(asset.byte_length) is not int or not 1 <= asset.byte_length <= MAX_IMAGE_BYTES:
            raise VisionError("image_size_limit", "图像为空或超过64 MiB大小上限。", 413)

        try:
            content = await self.room_service.read_content(room_item_id)
        except RoomError as exc:
            raise VisionError(exc.code, exc.message, exc.status_code) from exc
        await self._assert_media_snapshot_current(item, asset)
        if (
            not isinstance(content, dict)
            or content.get("item_id") != item["id"]
            or content.get("media_type") != asset.mime_type
            or content.get("encoding") != "base64"
        ):
            raise VisionError("media_content_mismatch", "已授权媒体内容与登记 MIME 类型不一致。", 409)
        encoded = content.get("data")
        max_encoded_chars = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
        if not isinstance(encoded, str) or len(encoded) > max_encoded_chars:
            raise VisionError("image_size_limit", "图像为空或超过64 MiB大小上限。", 413)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise VisionError("media_content_invalid", "已登记媒体内容无法安全读取。", 409) from exc
        if not raw or len(raw) > MAX_IMAGE_BYTES or len(raw) != asset.byte_length:
            raise VisionError("image_size_limit", "图像为空或超过 64 MiB 大小上限。", 413)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != asset.sha256 or digest != content.get("content_hash"):
            raise VisionError("media_integrity_error", "图像 hash 与登记来源不一致。", 409)

        dimensions = _decode_static_image(raw, asset.mime_type)
        duplicate_id = await self._find_duplicate(asset)
        source_refs = [
            {
                "kind": "room_item",
                "id": item["id"],
                "person_id": item["source_person_id"],
                "scope": item["source_scope"],
            },
            {
                "kind": "managed_media",
                "id": asset.id,
                "person_id": asset.source_person_id,
                "scope": asset.source_scope,
            },
        ]
        media = {
            "room_item_id": item["id"],
            "room_item_version": item["version"],
            "managed_media_id": asset.id,
            "sha256": digest,
            "mime_type": asset.mime_type,
            "width": dimensions["width"],
            "height": dimensions["height"],
            "pixel_count": dimensions["pixel_count"],
            "byte_length": len(raw),
            "root_key": asset.root_key,
            "relative_path": asset.relative_path,
            "media_version": asset.version,
            "pixel_digest": dimensions["pixel_digest"],
        }
        if duplicate_id is not None and duplicate_id != asset.id:
            return {
                "state": "duplicate",
                "media": media,
                "observation": None,
                "source_refs": source_refs,
                "duplicate_of_media_id": duplicate_id,
            }

        # The currently configured DeepSeek adapter is text-only, and the
        # provider capability/price evidence for image input is not verified.
        # Keep local pixel decoding useful, but never turn it into a pretend
        # observation or dispatch image bytes to an unverified endpoint.
        return {
            "state": "needs_model_verification",
            "media": media,
            "observation": None,
            "source_refs": source_refs,
            "duplicate_of_media_id": None,
        }

    async def _assert_media_snapshot_current(self, item: dict[str, Any], asset: ManagedMediaRecord) -> None:
        try:
            current_item = await self.room_service.get(item["id"])
        except RoomError as exc:
            raise VisionError(exc.code, exc.message, exc.status_code) from exc
        if (
            current_item["version"] != item["version"]
            or current_item["state"] != "ready"
            or current_item.get("managed_media_id") != asset.id
            or current_item.get("root_key") != item.get("root_key")
            or current_item.get("relative_path") != item.get("relative_path")
            or current_item.get("source_scope") != item.get("source_scope")
            or current_item.get("source_person_id") != item.get("source_person_id")
            or current_item.get("source_refs") != item.get("source_refs")
        ):
            raise VisionError("room_item_stale", "房间条目或媒体来源在读取期间发生变化。", 409)

        async with self.session_factory() as session:
            current_asset = await session.get(ManagedMediaRecord, asset.id)
        if current_asset is None or any(
            getattr(current_asset, field) != getattr(asset, field)
            for field in (
                "version",
                "root_key",
                "relative_path",
                "sha256",
                "mime_type",
                "byte_length",
                "source_refs",
                "source_scope",
                "source_person_id",
                "role_use_approved",
            )
        ):
            raise VisionError("media_source_stale", "登记媒体或其来源授权在读取期间发生变化。", 409)

    async def _find_duplicate(self, asset: ManagedMediaRecord) -> str | None:
        statement = select(ManagedMediaRecord.id).where(
            ManagedMediaRecord.sha256 == asset.sha256,
            ManagedMediaRecord.source_scope == asset.source_scope,
            ManagedMediaRecord.id != asset.id,
            ManagedMediaRecord.role_use_approved.is_(True),
        )
        if asset.source_person_id is None:
            statement = statement.where(ManagedMediaRecord.source_person_id.is_(None))
        else:
            statement = statement.where(ManagedMediaRecord.source_person_id == asset.source_person_id)
        async with self.session_factory() as session:
            return await session.scalar(statement.order_by(ManagedMediaRecord.created_at, ManagedMediaRecord.id).limit(1))


def _decode_static_image(raw: bytes, mime_type: str) -> dict[str, int | str]:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise VisionError("needs_resource", "当前运行环境没有可用的本地图片解码器。", 409) from exc

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as opened:
                expected_format = {"image/png": "PNG", "image/jpeg": "JPEG"}[mime_type]
                if opened.format != expected_format:
                    raise VisionError("media_mime_mismatch", "图片内容签名与登记 MIME 类型不一致。", 422)
                if getattr(opened, "n_frames", 1) != 1:
                    raise VisionError("animated_image_unsupported", "当前不将动画图片误读为单帧完整观察。", 415)
                width, height = opened.size
                if width < 1 or height < 1:
                    raise VisionError("invalid_image", "图像尺寸无效。", 422)
                pixel_count = width * height
                if pixel_count > MAX_IMAGE_PIXELS:
                    raise VisionError("image_pixel_limit", "图像超过 1200 万像素上限。", 413)
                opened.load()
                oriented = ImageOps.exif_transpose(opened)
                width, height = oriented.size
                pixel_count = width * height
                if pixel_count > MAX_IMAGE_PIXELS:
                    raise VisionError("image_pixel_limit", "图像超过 1200 万像素上限。", 413)
                # Loading and hashing normalized RGB bytes proves the decoder
                # consumed pixel data without exposing pixels in the response.
                rgb = oriented.convert("RGB")
                pixel_digest = hashlib.sha256(rgb.tobytes()).hexdigest()
    except VisionError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise VisionError("invalid_image", "图像文件损坏或无法安全解码。", 422) from exc

    return {"width": width, "height": height, "pixel_count": pixel_count, "pixel_digest": pixel_digest}
