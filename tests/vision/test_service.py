from __future__ import annotations

import asyncio
import base64
import hashlib
from types import SimpleNamespace

import pytest

import cutefish.vision.service as vision_module
from cutefish.chat.provider import (
    DEEPSEEK_MODEL,
    DeepSeekConfig,
    DeepSeekVisionResult,
    ProviderError,
    ProviderUsage,
)
from cutefish.core.budget import BudgetService
from cutefish.core.db import Database
from cutefish.core.settings import load_settings
from cutefish.extensions.rooms import RoomError
from cutefish.vision.service import VisionError, VisionService


IMAGE_BYTES = b"\x89PNG\r\n\x1a\nfixture-image-bytes"
IMAGE_SHA256 = hashlib.sha256(IMAGE_BYTES).hexdigest()
SOURCE_REFS = [{"kind": "owner_supplied", "id": "owner-image", "scope": "production"}]


def _asset():
    return SimpleNamespace(
        id="media-1",
        version=3,
        root_key="room_root",
        relative_path="images/one.png",
        sha256=IMAGE_SHA256,
        mime_type="image/png",
        byte_length=len(IMAGE_BYTES),
        source_refs=SOURCE_REFS,
        source_scope="production",
        source_person_id=None,
        role_use_approved=True,
    )


def _item():
    return {
        "id": "room-item-1",
        "version": 5,
        "state": "ready",
        "managed_media_id": "media-1",
        "source_scope": "production",
        "source_person_id": None,
        "root_key": "room_root",
        "relative_path": "images/one.png",
        "source_refs": SOURCE_REFS,
    }


class FakeSession:
    def __init__(self, asset):
        self.asset = asset

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def get(self, _model, _record_id):
        return self.asset

    async def scalar(self, _statement):
        return None


class FakeRoomService:
    def __init__(
        self,
        *,
        fail_on_read: int | None = None,
        wrong_hash: bool = False,
        wrong_hash_on_read: int | None = None,
        stale_on_get: int | None = None,
    ):
        self.item = _item()
        self.read_count = 0
        self.get_count = 0
        self.fail_on_read = fail_on_read
        self.wrong_hash = wrong_hash
        self.wrong_hash_on_read = wrong_hash_on_read
        self.stale_on_get = stale_on_get

    async def get(self, _item_id):
        self.get_count += 1
        if self.stale_on_get == self.get_count:
            return {**self.item, "version": self.item["version"] + 1}
        return dict(self.item)

    async def read_content(self, item_id):
        self.read_count += 1
        if self.fail_on_read == self.read_count:
            raise RoomError("source_scope_denied", "房间来源读取授权已撤销。", 403)
        content_hash = IMAGE_SHA256
        if self.wrong_hash or self.wrong_hash_on_read == self.read_count:
            content_hash = "0" * 64
        return {
            "item_id": item_id,
            "encoding": "base64",
            "media_type": "image/png",
            "content_hash": content_hash,
            "data": base64.b64encode(IMAGE_BYTES).decode("ascii"),
        }


class FakeSecrets:
    async def get(self, _name):
        return "fake-key"


class FakeProvider:
    def __init__(self, *, error: Exception | None = None):
        self.config = DeepSeekConfig()
        self.error = error
        self.calls = []

    async def observe_image(self, *, api_key, request):
        self.calls.append((api_key, request))
        if self.error is not None:
            raise self.error
        return DeepSeekVisionResult(
            provider_response_id="provider-response-1",
            configured_model=DEEPSEEK_MODEL,
            returned_model=DEEPSEEK_MODEL,
            observation="图中可见一只猫。",
            finish_reason="stop",
            usage=ProviderUsage(input_tokens=100, output_tokens=20, cache_hit_tokens=None, cache_miss_tokens=None),
        )


async def _budget(tmp_path):
    database = Database(tmp_path / "vision-budget.sqlite3")
    await database.initialize()
    budget = BudgetService(
        database,
        load_settings(data_dir=tmp_path / "state"),
        get_deployment_profile=lambda: "local_trial",
    )
    await budget.initialize()
    return database, budget


def _vision_service(asset, room, budget, provider):
    return VisionService(
        lambda: FakeSession(asset),
        room,
        budget=budget,
        secrets=FakeSecrets(),
        provider_adapter=provider,
    )


@pytest.mark.asyncio
async def test_revoked_room_read_grant_after_reservation_releases_before_dispatch(tmp_path, monkeypatch):
    database, budget = await _budget(tmp_path)
    provider = FakeProvider()
    room = FakeRoomService(fail_on_read=2)
    monkeypatch.setattr(vision_module, "_decode_static_image", lambda *_args: {
        "width": 1,
        "height": 1,
        "pixel_count": 1,
        "pixel_digest": "a" * 64,
    })
    service = _vision_service(_asset(), room, budget, provider)
    try:
        with pytest.raises(VisionError) as exc_info:
            await service.observe_room_item(
                "room-item-1",
                expected_room_version=5,
                idempotency_key="invoke-1",
                detail_level="low",
            )
        assert exc_info.value.code == "source_scope_denied"
        assert provider.calls == []
        entries = await budget.list_entries(limit=10)
        assert entries["total"] == 1
        assert entries["items"][0]["state"] == "released"
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hash", "revision"])
async def test_media_hash_or_revision_change_fails_before_dispatch(tmp_path, monkeypatch, failure):
    database, budget = await _budget(tmp_path)
    provider = FakeProvider()
    room = FakeRoomService(
        wrong_hash_on_read=2 if failure == "hash" else None,
        stale_on_get=3 if failure == "revision" else None,
    )
    monkeypatch.setattr(vision_module, "_decode_static_image", lambda *_args: {
        "width": 1,
        "height": 1,
        "pixel_count": 1,
        "pixel_digest": "a" * 64,
    })
    service = _vision_service(_asset(), room, budget, provider)
    try:
        expected_error = "media_source_stale" if failure == "hash" else "room_item_stale"
        with pytest.raises(VisionError) as exc_info:
            await service.observe_room_item(
                "room-item-1",
                expected_room_version=5,
                idempotency_key="invoke-1",
                detail_level="low",
            )
        assert exc_info.value.code == expected_error
        assert provider.calls == []
        entries = await budget.list_entries(limit=10)
        assert entries["total"] == 1
        assert entries["items"][0]["state"] == "released"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_observation_uses_full_conservative_reservation_and_idempotency(tmp_path, monkeypatch):
    database, budget = await _budget(tmp_path)
    provider = FakeProvider()
    room = FakeRoomService()
    monkeypatch.setattr(vision_module, "_decode_static_image", lambda *_args: {
        "width": 1,
        "height": 1,
        "pixel_count": 1,
        "pixel_digest": "a" * 64,
    })
    service = _vision_service(_asset(), room, budget, provider)
    try:
        result = await service.observe_room_item(
            "room-item-1",
            expected_room_version=5,
            idempotency_key="invoke-1",
            detail_level="auto",
        )
        entries = await budget.list_entries(limit=10)

        assert result["state"] == "observed"
        assert result["budget_state"] == "settled"
        assert result["usage"]["input_tokens"] == 100
        assert entries["total"] == 1
        entry = entries["items"][0]
        assert entry["state"] == "settled"
        assert entry["max_input_tokens"] == 1_048_576
        assert entry["max_output_tokens"] == 4096
        assert entry["estimated_cost"] == pytest.approx(2.12992)
        assert len(provider.calls) == 1
        assert provider.calls[0][1].detail == "auto"

        with pytest.raises(VisionError) as exc_info:
            await service.observe_room_item(
                "room-item-1",
                expected_room_version=5,
                idempotency_key="invoke-1",
                detail_level="auto",
            )
        assert exc_info.value.code == "request_already_dispatched"
        assert len(provider.calls) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unknown_provider_outcome_keeps_the_full_reservation(tmp_path, monkeypatch):
    database, budget = await _budget(tmp_path)
    provider = FakeProvider(error=ProviderError("provider_outcome_unknown", "uncertain"))
    room = FakeRoomService()
    monkeypatch.setattr(vision_module, "_decode_static_image", lambda *_args: {
        "width": 1,
        "height": 1,
        "pixel_count": 1,
        "pixel_digest": "a" * 64,
    })
    service = _vision_service(_asset(), room, budget, provider)
    try:
        with pytest.raises(VisionError) as exc_info:
            await service.observe_room_item(
                "room-item-1",
                expected_room_version=5,
                idempotency_key="invoke-1",
                detail_level="low",
            )
        assert exc_info.value.code == "provider_outcome_unknown"
        entry = (await budget.list_entries(limit=10))["items"][0]
        assert entry["state"] == "unknown"
        assert entry["estimated_cost"] == pytest.approx(2.12992)
        assert len(provider.calls) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_durable_dispatch_claim_allows_only_one_service_instance(tmp_path, monkeypatch):
    database, budget = await _budget(tmp_path)
    provider = FakeProvider()
    room = FakeRoomService()
    monkeypatch.setattr(vision_module, "_decode_static_image", lambda *_args: {
        "width": 1,
        "height": 1,
        "pixel_count": 1,
        "pixel_digest": "a" * 64,
    })
    services = [
        _vision_service(_asset(), room, budget, provider),
        _vision_service(_asset(), room, budget, provider),
    ]
    original_reserve = budget.reserve
    both_reserved = asyncio.Barrier(2)

    async def synchronized_reserve(**kwargs):
        reservation = await original_reserve(**kwargs)
        await both_reserved.wait()
        return reservation

    budget.reserve = synchronized_reserve
    try:
        outcomes = await asyncio.gather(
            *(
                service.observe_room_item(
                    "room-item-1",
                    expected_room_version=5,
                    idempotency_key="same-invocation",
                    detail_level="low",
                )
                for service in services
            ),
            return_exceptions=True,
        )

        assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
        errors = [outcome for outcome in outcomes if isinstance(outcome, VisionError)]
        assert len(errors) == 1
        assert errors[0].code == "request_already_dispatched"
        assert len(provider.calls) == 1
        entries = await budget.list_entries(limit=10)
        assert entries["total"] == 1
        assert entries["items"][0]["state"] == "settled"
    finally:
        budget.reserve = original_reserve
        await database.close()
