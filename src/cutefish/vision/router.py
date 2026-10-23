"""Authenticated local endpoints for explicit, already-authorized image observations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cutefish.api.router import require_admin
from cutefish.extensions.rooms import RoomError

from .service import VisionError, VisionService


router = APIRouter(prefix="/api")


class ObserveImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_item_id: str = Field(min_length=1, max_length=128)
    expected_room_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    detail_level: str = Field(default="low", pattern="^(low|high|original|auto)$")


def _success(request: Request, data) -> JSONResponse:
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


def _failure(request: Request, exc: VisionError | RoomError) -> JSONResponse:
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


def _service(request: Request) -> VisionService:
    context = request.app.state.context
    service = getattr(context, "vision_service", None)
    if service is None:
        services = getattr(context, "extension_services", {})
        rooms = services.get("rooms") if isinstance(services, dict) else None
        if rooms is None:
            raise VisionError("needs_configuration", "本地房间媒体服务尚未接入。", 409)
        chat_service = getattr(context, "chat_service", None)
        service = VisionService(
            context.session_factory,
            rooms,
            budget=getattr(context, "budget", None),
            secrets=getattr(context, "secrets", None),
            provider_adapter=getattr(chat_service, "provider", None),
        )
        context.vision_service = service
    return service


@router.get("/vision/capability")
async def get_vision_capability(request: Request, _admin=Depends(require_admin)):
    return _success(request, _service(request).capability())


@router.post("/vision/observe")
async def observe_image(
    request: Request,
    payload: ObserveImageRequest,
    _admin=Depends(require_admin),
):
    try:
        data = await _service(request).observe_room_item(
            payload.room_item_id,
            expected_room_version=payload.expected_room_version,
            idempotency_key=payload.idempotency_key,
            detail_level=payload.detail_level,
        )
    except (VisionError, RoomError) as exc:
        return _failure(request, exc)
    return _success(request, data)
