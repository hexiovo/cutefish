"""Authenticated owner controls for manual proactivity policy and batches."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cutefish.api.router import require_admin

from .service import ProactivityError, ProactivityService


router = APIRouter(prefix="/api")


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProactivityPolicyUpdate(RequestModel):
    expected_revision: int = Field(ge=1)
    enabled: bool
    explicit_pause: bool
    dnd_enabled: bool
    manual_takeover: bool


class ProactivityBatchCreate(RequestModel):
    label: str = Field(min_length=1, max_length=120)
    conversation_id: str = Field(min_length=1, max_length=36)
    source_event_ids: list[str] = Field(min_length=1, max_length=20)
    expires_at: datetime
    max_candidates: Literal[1, 2, 3]
    task_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def source_ids_unique(self):
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("source_event_ids 不能重复")
        return self


def _service(request: Request) -> ProactivityService:
    context = request.app.state.context
    service = getattr(context, "proactivity_service", None)
    if service is None:
        service = ProactivityService(
            context.session_factory,
            context.budget,
            context.chat_service,
            get_deployment_profile=lambda: getattr(context, "deployment_profile", "target_staging"),
            is_runtime_policy_valid=lambda: getattr(context, "runtime_policy_valid", False),
        )
        context.proactivity_service = service
    return service


def _success(request: Request, data, *, status_code: int = 200) -> JSONResponse:
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
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _failure(request: Request, exc: ProactivityError) -> JSONResponse:
    return JSONResponse(
        {
            "status": "failed",
            "data": None,
            "evidence": [],
            "error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.status_code >= 500,
                "details": None,
            },
            "usage": None,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "time": datetime.now(timezone.utc).isoformat(),
        },
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/proactivity/policy")
async def get_policy(request: Request, _admin=Depends(require_admin)):
    try:
        return _success(request, await _service(request).get_policy())
    except ProactivityError as exc:
        return _failure(request, exc)


@router.put("/proactivity/policy")
async def update_policy(
    request: Request,
    payload: ProactivityPolicyUpdate,
    _admin=Depends(require_admin),
):
    try:
        data = await _service(request).update_policy(
            expected_revision=payload.expected_revision,
            enabled=payload.enabled,
            explicit_pause=payload.explicit_pause,
            dnd_enabled=payload.dnd_enabled,
            manual_takeover=payload.manual_takeover,
            updated_by=str(getattr(_admin, "admin_id", "owner")),
        )
        return _success(request, data)
    except ProactivityError as exc:
        return _failure(request, exc)


@router.post("/proactivity/batches", status_code=201)
async def create_batch(
    request: Request,
    payload: ProactivityBatchCreate,
    _admin=Depends(require_admin),
):
    try:
        data = await _service(request).create_batch(
            label=payload.label,
            conversation_id=payload.conversation_id,
            source_event_ids=payload.source_event_ids,
            expires_at=payload.expires_at,
            max_candidates=payload.max_candidates,
            task_id=payload.task_id,
        )
        return _success(request, data, status_code=201)
    except ProactivityError as exc:
        return _failure(request, exc)


@router.post("/proactivity/batches/{batch_id}/generate")
async def generate_batch(
    request: Request,
    batch_id: str,
    _admin=Depends(require_admin),
):
    try:
        data = await _service(request).generate_batch(batch_id)
        return _success(request, data)
    except ProactivityError as exc:
        return _failure(request, exc)

