from __future__ import annotations

import asyncio
import base64
import hashlib

import httpx
import pytest
from fastapi import FastAPI

from cutefish.core.auth import OwnerPrincipal
from cutefish.integrations import (
    ActionScope,
    BoundedReadOnlyConnector,
    ConnectorPolicy,
    EvidenceStore,
    HandlerRegistration,
    HandlerRegistry,
    HttpResponse,
    PRODUCTION_READ_REGISTRY,
    ReadConnectorRegistry,
    ReadRegistration,
)
from cutefish.integrations.router import require_admin, router
from cutefish.integrations.service import IntegrationError, IntegrationService


class FakeTransport:
    def __init__(self, response: HttpResponse):
        self.response = response
        self.calls: list[tuple[str, float, int]] = []

    def get(self, url: str, *, timeout_seconds: float, max_response_bytes: int) -> HttpResponse:
        self.calls.append((url, timeout_seconds, max_response_bytes))
        return self.response


class FakeOutbox:
    def __init__(self):
        self.preview = None
        self.actor_id = None

    async def record_action_preview(self, preview, *, actor_id: str):
        self.preview = preview
        self.actor_id = actor_id
        return {"preview_id": preview.preview_id, "state": "preview_only"}


def test_read_uses_only_registered_path_and_issues_server_evidence_for_core_preview() -> None:
    body = b'{"title":"clip"}'
    policy = ConnectorPolicy(
        site_id="bilibili",
        capability_id="bilibili.details",
        origin="https://video.example",
        allowed_paths=frozenset({"/api/item"}),
        allowed_query_keys=frozenset({"id", "lang"}),
        allowed_content_types=frozenset({"application/json"}),
    )
    transport = FakeTransport(
        HttpResponse(
            200,
            {"Content-Type": "application/json; charset=utf-8"},
            body,
            "https://video.example/api/item?id=BV123&lang=zh",
        )
    )
    reads = ReadConnectorRegistry(
        (ReadRegistration("bilibili", "bilibili.details", "/api/item", BoundedReadOnlyConnector(policy, transport), "metadata"),)
    )
    actions = HandlerRegistry(
        (
            HandlerRegistration(
                site_id="hexi",
                capability_id="hexi.post.create_draft",
                version="test-v1",
                verification_ref="fixture-only",
                side_effect="external_write",
                handler=lambda _payload: None,
            ),
        )
    )
    store = EvidenceStore()
    outbox = FakeOutbox()
    service = IntegrationService(evidence_store=store, read_registry=reads, action_registry=actions, outbox=outbox)

    result = service.read(
        site_id="bilibili",
        capability_id="bilibili.details",
        object_id="BV123",
        query={"id": "BV123", "lang": "zh"},
    )

    assert transport.calls == [
        ("https://video.example/api/item?id=BV123&lang=zh", 5.0, 2_000_000)
    ]
    assert result["content_base64"] == base64.b64encode(body).decode("ascii")
    assert result["evidence"]["sha256"] == hashlib.sha256(body).hexdigest()
    evidence = store.resolve([result["source_ref_id"]])
    assert len(evidence) == 1
    assert evidence[0].object_id == "BV123"
    assert evidence[0].read_scope == "metadata"

    preview = asyncio.run(
        service.preview(
            site_id="hexi",
            account_ref="publisher",
            object_id="draft-1",
            action="hexi.post.create_draft",
            payload={"title": "clip summary"},
            source_ref_ids=[result["source_ref_id"]],
            scope=ActionScope(site_id="hexi", object_id="draft-1", action="hexi.post.create_draft"),
            actor_id="admin:1",
            expected_revision=3,
        )
    )

    assert preview["preview_hash"]
    assert preview["source_refs"][0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert preview["outbox_preview"]["preview_id"] == preview["preview_id"]
    assert outbox.actor_id == "admin:1"
    assert outbox.preview.expected_revision == 3
    assert outbox.preview.source_refs[0]["object_id"] == "BV123"


def test_production_read_registry_is_empty_and_never_calls_transport() -> None:
    service = IntegrationService(evidence_store=EvidenceStore())

    with pytest.raises(IntegrationError) as error:
        service.read(
            site_id="bilibili",
            capability_id="bilibili.search",
            object_id="query-1",
            query={"q": "fish"},
        )

    assert PRODUCTION_READ_REGISTRY.has("bilibili", "bilibili.search") is False
    assert error.value.code == "needs_configuration"


def test_capabilities_report_explicit_read_registration_separately_from_target_and_handler() -> None:
    policy = ConnectorPolicy(
        site_id="bilibili",
        capability_id="bilibili.details",
        origin="https://video.example",
        allowed_paths=frozenset({"/api/item"}),
        allowed_query_keys=frozenset({"id"}),
        allowed_content_types=frozenset({"application/json"}),
    )
    reads = ReadConnectorRegistry(
        (
            ReadRegistration(
                "bilibili",
                "bilibili.details",
                "/api/item",
                BoundedReadOnlyConnector(policy, FakeTransport(HttpResponse(200, {}, b"{}", "https://video.example/api/item"))),
            ),
        )
    )
    service = IntegrationService(evidence_store=EvidenceStore(), read_registry=reads)
    capabilities = {item["capability_id"]: item for item in service.capabilities()}

    details = capabilities["bilibili.details"]
    search = capabilities["bilibili.search"]
    assert details["read_registered"] is True
    assert details["handler_registered"] is False
    assert details["target_configured"] is False
    assert details["status"] == "needs_configuration"
    assert search["read_registered"] is False


@pytest.mark.asyncio
async def test_integrations_routes_are_admin_only_and_read_rejects_arbitrary_url_fields() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.context = type(
        "ContextStub",
        (),
        {"evidence_store": EvidenceStore(), "integration_handlers": HandlerRegistry(), "outbox": None},
    )()
    app.dependency_overrides[require_admin] = lambda: OwnerPrincipal(admin_id=1, session_id="test")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        caps = await client.get("/api/integrations/capabilities")
        assert caps.status_code == 200
        statuses = {item["capability_id"]: item["status"] for item in caps.json()["data"]["items"]}
        assert statuses["bilibili.search"] == "needs_configuration"

        failed = await client.post(
            "/api/integrations/read",
            json={
                "site_id": "bilibili",
                "capability_id": "bilibili.search",
                "object_id": "query-1",
                "query": {"q": "fish"},
                "url": "https://attacker.example/",
                "method": "GET",
                "headers": {"Cookie": "secret"},
            },
        )
        assert failed.status_code == 422

        unconfigured = await client.post(
            "/api/integrations/read",
            json={
                "site_id": "bilibili",
                "capability_id": "bilibili.search",
                "object_id": "query-1",
                "query": {"q": "fish"},
            },
        )
        assert unconfigured.status_code == 409
        assert unconfigured.json()["error"]["code"] == "needs_configuration"
