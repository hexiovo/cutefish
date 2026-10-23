from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO

import pytest
from PIL import Image

from cutefish.integrations import (
    ActionScope,
    BoundedReadOnlyConnector,
    ConnectorPolicy,
    EvidenceStore,
    EvidenceStoreError,
    FetchError,
    FetchedDocument,
    HandlerRegistration,
    HandlerRegistry,
    HandlerUnavailable,
    HttpResponse,
    ImageMetadata,
    PRODUCTION_HANDLER_REGISTRY,
    SourceEvidence,
    build_action_preview,
    capability_snapshot,
    validate_image,
)


class FakeTransport:
    def __init__(self, response: HttpResponse):
        self.response = response
        self.calls: list[tuple[str, float, int]] = []

    def get(self, url: str, *, timeout_seconds: float, max_response_bytes: int) -> HttpResponse:
        self.calls.append((url, timeout_seconds, max_response_bytes))
        return self.response


def policy(**overrides) -> ConnectorPolicy:
    values = {
        "site_id": "source",
        "capability_id": "source.read",
        "origin": "https://source.example",
        "allowed_paths": frozenset({"/item"}),
        "allowed_query_keys": frozenset({"q"}),
        "allowed_content_types": frozenset({"application/json", "image/png"}),
        "max_response_bytes": 128,
        "timeout_seconds": 2,
    }
    values.update(overrides)
    return ConnectorPolicy(**values)


def test_fetch_returns_actual_bytes_and_provenance_with_bounded_get() -> None:
    body = b'{"title":"item"}'
    response = HttpResponse(
        status=200,
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=body,
        final_url="https://source.example/item?q=fish",
    )
    transport = FakeTransport(response)
    connector = BoundedReadOnlyConnector(policy(), transport)

    document = connector.fetch("https://source.example/item?q=fish", object_id="item-1")

    assert document.content == body
    assert document.evidence.sha256 == hashlib.sha256(body).hexdigest()
    assert document.evidence.byte_length == len(body)
    assert document.evidence.source_url == "https://source.example/item?q=fish"
    assert document.evidence.object_id == "item-1"
    assert document.evidence.fetched_at.tzinfo == timezone.utc
    assert transport.calls == [("https://source.example/item?q=fish", 2, 128)]


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/item?q=fish",
        "https://source.example/other?q=fish",
        "https://source.example/item?token=secret",
        "https://source.example/item?q=fish&q=chips",
        "http://source.example/item?q=fish",
        "https://user:pass@source.example/item?q=fish",
    ],
)
def test_fetch_rejects_unapproved_urls_before_transport(url: str) -> None:
    transport = FakeTransport(
        HttpResponse(200, {"Content-Type": "application/json"}, b"{}", "https://source.example/item")
    )
    connector = BoundedReadOnlyConnector(policy(), transport)

    with pytest.raises(FetchError):
        connector.fetch(url, object_id="item-1")

    assert transport.calls == []


def test_fetch_rejects_redirect_and_oversized_response() -> None:
    moved = FakeTransport(
        HttpResponse(200, {"Content-Type": "application/json"}, b"{}", "https://source.example/other")
    )
    with pytest.raises(FetchError, match="地址发生变化"):
        BoundedReadOnlyConnector(policy(), moved).fetch("https://source.example/item", object_id="item-1")

    large = FakeTransport(
        HttpResponse(200, {"Content-Type": "application/json"}, b"x" * 129, "https://source.example/item")
    )
    with pytest.raises(FetchError, match="大小上限"):
        BoundedReadOnlyConnector(policy(), large).fetch("https://source.example/item", object_id="item-1")


def _evidence(body: bytes = b"evidence") -> SourceEvidence:
    return SourceEvidence(
        site_id="source",
        capability_id="source.read",
        source_url="https://source.example/item",
        object_id="item-1",
        content_type="application/json",
        fetched_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        sha256=hashlib.sha256(body).hexdigest(),
        byte_length=len(body),
        read_scope="content",
    )


def test_action_preview_hash_binds_target_payload_source_and_scope() -> None:
    args = {
        "site_id": "hexi",
        "account_ref": None,
        "object_id": "draft-1",
        "action": "hexi.post.edit",
        "payload": {"title": "A", "content": "B"},
        "source_refs": [_evidence()],
        "scope": ActionScope(site_id="hexi", object_id="draft-1", action="hexi.post.edit"),
        "created_at": datetime(2026, 10, 23, tzinfo=timezone.utc),
    }

    first = build_action_preview(**args)
    identical = build_action_preview(**args)
    changed_target = build_action_preview(**{**args, "object_id": "draft-2", "scope": ActionScope(site_id="hexi", object_id="draft-2", action="hexi.post.edit")})
    changed_payload = build_action_preview(**{**args, "payload": {"title": "A", "content": "C"}})
    changed_source = build_action_preview(**{**args, "source_refs": [_evidence(b"different")]})

    assert first.preview_hash == identical.preview_hash
    assert first.payload_hash == identical.payload_hash
    assert first.preview_hash != changed_target.preview_hash
    assert first.preview_hash != changed_payload.preview_hash
    assert first.preview_hash != changed_source.preview_hash
    assert first.state == "preview_only"


def test_action_preview_rejects_scope_mismatch_and_secret_fields() -> None:
    with pytest.raises(ValueError, match="scope must bind"):
        build_action_preview(
            site_id="hexi",
            account_ref=None,
            object_id="draft-1",
            action="hexi.post.edit",
            payload={"title": "A"},
            source_refs=[],
            scope=ActionScope(site_id="hexi", object_id="draft-2", action="hexi.post.edit"),
        )

    with pytest.raises(ValueError, match="credential-like"):
        build_action_preview(
            site_id="hexi",
            account_ref=None,
            object_id="draft-1",
            action="hexi.post.edit",
            payload={"api_key": "should-not-be-present"},
            source_refs=[],
            scope=ActionScope(site_id="hexi", object_id="draft-1", action="hexi.post.edit"),
        )

    with pytest.raises(ValueError, match="credential-like"):
        SourceEvidence(
            site_id="source",
            capability_id="source.read",
            source_url="https://source.example/item?token=secret",
            object_id="item-1",
            content_type="application/json",
            fetched_at=datetime.now(timezone.utc),
            sha256=hashlib.sha256(b"x").hexdigest(),
            byte_length=1,
            read_scope="content",
        )


def test_image_decode_is_in_memory_and_pixel_bounded() -> None:
    output = BytesIO()
    Image.new("RGB", (2, 1), color=(12, 34, 56)).save(output, format="PNG")
    body = output.getvalue()
    evidence = SourceEvidence(
        site_id="source",
        capability_id="source.screenshot",
        source_url="https://source.example/item",
        object_id="item-1",
        content_type="image/png",
        fetched_at=datetime.now(timezone.utc),
        sha256=hashlib.sha256(body).hexdigest(),
        byte_length=len(body),
        read_scope="frame",
    )

    assert validate_image(FetchedDocument(evidence, body)) == ImageMetadata(
        format="PNG", width=2, height=1, mode="RGB"
    )
    with pytest.raises(FetchError, match="像素解码上限"):
        validate_image(FetchedDocument(evidence, body), max_pixels=1)


def test_production_handler_registry_stays_empty_and_capabilities_fail_closed() -> None:
    assert PRODUCTION_HANDLER_REGISTRY.capability_ids() == ()
    default = {item.capability_id: item for item in capability_snapshot()}
    assert default["bilibili.search"].status == "needs_configuration"
    assert default["bilibili.search"].target_configured is False
    assert default["bilibili.search"].handler_registered is False

    configured = {
        item.capability_id: item
        for item in capability_snapshot(configured_sites=frozenset({"bilibili"}))
    }
    assert configured["bilibili.search"].status == "needs_adapter_verification"

    test_registry = HandlerRegistry(
        (
            HandlerRegistration(
                site_id="bilibili",
                capability_id="bilibili.search",
                version="test-v1",
                verification_ref="test-fixture-only",
                side_effect="none",
                handler=lambda _: None,
            ),
        )
    )
    assert capability_snapshot(
        configured_sites=frozenset({"bilibili"}), handler_registry=test_registry
    )[0].status == "ready_local"
    with pytest.raises(HandlerUnavailable):
        PRODUCTION_HANDLER_REGISTRY.resolve("bilibili.search")


def test_evidence_store_issues_only_short_lived_server_references() -> None:
    now = [0.0]
    store = EvidenceStore(max_items=1, ttl_seconds=10, clock=lambda: now[0])
    reference = store.register(_evidence())

    assert store.resolve([reference]) == (_evidence(),)
    with pytest.raises(EvidenceStoreError, match="不能重复"):
        store.resolve([reference, reference])
    with pytest.raises(EvidenceStoreError, match="上限"):
        store.register(_evidence(b"second"))

    now[0] = 11.0
    with pytest.raises(EvidenceStoreError, match="不存在或已过期"):
        store.resolve([reference])
