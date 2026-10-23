"""Authenticated API routes for integrations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from cutefish.api.router import require_admin

from .contracts import ActionScope
from .evidence import EvidenceStore
from .registry import PRODUCTION_HANDLER_REGISTRY
from .service import (
    PRODUCTION_READ_REGISTRY,
    IntegrationError,
    IntegrationService,
    ReadConnectorRegistry,
)


router = APIRouter(prefix="/api/integrations", tags=["integrations"])


class IntegrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class IntegrationReadRequest(IntegrationRequest):
    site_id: StrictStr = Field(min_length=1, max_length=64)
    capability_id: StrictStr = Field(min_length=1, max_length=128)
    object_id: StrictStr = Field(min_length=1, max_length=512)
    query: dict[StrictStr, StrictStr] = Field(default_factory=dict, max_length=16)

    @field_validator("object_id")
    @classmethod
    def _printable_object_id(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("object_id must be printable")
        return value

    @field_validator("query")
    @classmethod
    def _bounded_query(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not key or len(key) > 64 or any(ord(char) < 33 for char in key):
                raise ValueError("query key is invalid")
            if len(item) > 512 or any(ord(char) < 32 for char in item):
                raise ValueError("query value is invalid")
            folded = key.casefold().replace("-", "_")
            if set(folded.split("_")) & {
                "auth", "authorization", "cookie", "credential", "csrf", "key", "password", "secret", "session", "signature", "token"
            }:
                raise ValueError("credential-like query keys are forbidden")
        return value


class IntegrationPreviewRequest(IntegrationRequest):
    site_id: StrictStr = Field(min_length=1, max_length=64)
    account_ref: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    object_id: StrictStr = Field(min_length=1, max_length=256)
    action: StrictStr = Field(min_length=1, max_length=128)
    payload: dict[str, Any]
    source_ref_ids: list[StrictStr] = Field(max_length=32)
    scope: ActionScope
    expected_revision: StrictInt = Field(default=0, ge=0)

    @field_validator("object_id")
    @classmethod
    def _printable_object_id(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("object_id must be printable")
        return value


def _context(request: Request):
    return request.app.state.context


def _service(request: Request) -> IntegrationService:
    context = _context(request)
    return IntegrationService(
        evidence_store=getattr(context, "evidence_store", EvidenceStore()),
        read_registry=getattr(request.app.state, "integration_read_registry", PRODUCTION_READ_REGISTRY),
        action_registry=getattr(context, "integration_handlers", PRODUCTION_HANDLER_REGISTRY),
        outbox=getattr(context, "outbox", None),
    )


def _success(request: Request, data: Any) -> JSONResponse:
    return JSONResponse(
        {
            "status": "success",
            "data": data,
            "evidence": [],
            "error": None,
            "usage": None,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "time": datetime.now(timezone.utc).isoformat(),
        },
        headers={"Cache-Control": "no-store"},
    )


def _failure(request: Request, error: IntegrationError) -> JSONResponse:
    return JSONResponse(
        {
            "status": "failed",
            "data": None,
            "evidence": [],
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.status_code >= 500,
                "details": None,
            },
            "usage": None,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "time": datetime.now(timezone.utc).isoformat(),
        },
        status_code=error.status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/capabilities")
async def integration_capabilities(request: Request, _admin=Depends(require_admin)):
    return _success(request, {"items": list(_service(request).capabilities())})


@router.post("/read")
async def read_source(
    request: Request,
    payload: IntegrationReadRequest,
    _admin=Depends(require_admin),
):
    try:
        result = _service(request).read(
            site_id=payload.site_id,
            capability_id=payload.capability_id,
            object_id=payload.object_id,
            query=payload.query,
        )
    except IntegrationError as exc:
        return _failure(request, exc)
    return _success(request, result)


@router.post("/preview")
async def preview_action(
    request: Request,
    payload: IntegrationPreviewRequest,
    principal=Depends(require_admin),
):
    try:
        result = await _service(request).preview(
            site_id=payload.site_id,
            account_ref=payload.account_ref,
            object_id=payload.object_id,
            action=payload.action,
            payload=payload.payload,
            source_ref_ids=payload.source_ref_ids,
            scope=payload.scope,
            actor_id=f"admin:{principal.admin_id}",
            expected_revision=payload.expected_revision,
        )
    except IntegrationError as exc:
        return _failure(request, exc)
    return _success(request, result)

