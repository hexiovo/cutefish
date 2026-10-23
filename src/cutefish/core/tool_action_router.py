"""Authenticated Outbox controls for verified, typed non-QQ actions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from cutefish.api.router import require_admin


router = APIRouter(prefix="/api/outbox/tool-actions", tags=["tool-actions"])


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolActionGrantRequest(_Request):
    preview_id: StrictStr = Field(min_length=1, max_length=36)
    batch_id: StrictStr = Field(min_length=1, max_length=36)
    expires_in_seconds: int = Field(default=600, ge=1, le=1800)


class ToolActionPrepareRequest(_Request):
    preview_id: StrictStr = Field(min_length=1, max_length=36)
    grant_id: StrictStr = Field(min_length=1, max_length=36)
    batch_id: StrictStr = Field(min_length=1, max_length=36)
    idempotency_key: StrictStr = Field(min_length=1, max_length=256)


def _success(request: Request, data, *, status_code: int = 200):
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


def _outbox(request: Request):
    return request.app.state.context.outbox


@router.get("")
async def list_tool_actions(
    request: Request,
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    _admin=Depends(require_admin),
):
    return _success(request, await _outbox(request).list_actions(state=state, limit=limit, offset=offset))


@router.get("/{item_id}")
async def get_tool_action(request: Request, item_id: str, _admin=Depends(require_admin)):
    return _success(request, await _outbox(request).get_action(item_id))


@router.post("/grants", status_code=201)
async def issue_tool_action_grant(
    request: Request,
    payload: ToolActionGrantRequest,
    principal=Depends(require_admin),
):
    result = await _outbox(request).issue_action_grant(
        payload.preview_id,
        actor_id=f"admin:{principal.admin_id}",
        batch_id=payload.batch_id,
        expires_in_seconds=payload.expires_in_seconds,
    )
    return _success(request, result, status_code=201)


@router.post("/prepare", status_code=201)
async def prepare_tool_action(
    request: Request,
    payload: ToolActionPrepareRequest,
    principal=Depends(require_admin),
):
    result = await _outbox(request).prepare_action(
        payload.preview_id,
        payload.grant_id,
        payload.batch_id,
        payload.idempotency_key,
        actor_id=f"admin:{principal.admin_id}",
    )
    return _success(request, result, status_code=201)


@router.post("/{item_id}/dispatch")
async def dispatch_tool_action(request: Request, item_id: str, _admin=Depends(require_admin)):
    return _success(request, await _outbox(request).dispatch_action(item_id))


@router.post("/{item_id}/cancel")
async def cancel_tool_action(request: Request, item_id: str, _admin=Depends(require_admin)):
    return _success(request, await _outbox(request).cancel_action(item_id))
