"""Authenticated interview, immutable draft, feedback, and approval routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cutefish.api.router import require_admin

from .persona import PersonaService
from .service import ChatError


router = APIRouter(prefix="/api/persona")


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InterviewCreate(RequestModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)


class DraftCreate(RequestModel):
    version_label: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=24000)
    based_on_version_id: str | None = Field(default=None, min_length=1, max_length=36)


class TrialFeedback(RequestModel):
    inbound_message_id: str = Field(min_length=1, max_length=36)
    reply_message_id: str = Field(min_length=1, max_length=36)
    action: str = Field(min_length=1, max_length=24)
    change_points: str | None = Field(default=None, max_length=4000)


class PersonaApprovalRequest(RequestModel):
    interview_session_id: str = Field(min_length=1, max_length=36)
    inbound_message_id: str = Field(min_length=1, max_length=36)
    reply_message_id: str = Field(min_length=1, max_length=36)
    expected_state_revision: int = Field(ge=1)
    version_hash: str = Field(min_length=64, max_length=64)


class ConfirmedDesignImportRequest(RequestModel):
    bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _service(request: Request) -> PersonaService:
    context = request.app.state.context
    existing = getattr(context, "persona_service", None)
    if existing is not None:
        return existing
    chat_service = getattr(context, "chat_service", None)
    if chat_service is None:
        raise ChatError("chat_not_ready", "人格访谈服务正在初始化，请稍后重试。", 503)
    settings = getattr(context, "settings", None)
    return PersonaService(
        context.session_factory,
        chat_service,
        confirmed_design_import_root=getattr(settings, "confirmed_design_import_root", None),
    )


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


def _error(request: Request, exc: ChatError) -> JSONResponse:
    return JSONResponse(
        {
            "status": "failed",
            "data": None,
            "evidence": [],
            "error": {"code": exc.code, "message": exc.message, "retryable": exc.status_code >= 500, "details": None},
            "usage": None,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "time": datetime.now(timezone.utc).isoformat(),
        },
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/current")
async def current_persona(request: Request, _admin=Depends(require_admin)):
    try:
        versions = await _service(request).list_versions(limit=100)
    except ChatError as exc:
        return _error(request, exc)
    current = next((version for version in versions if version["state"] == "approved"), None)
    return _success(request, {"version": current})


@router.get("/confirmed-design-import")
async def confirmed_design_import_status(request: Request, _admin=Depends(require_admin)):
    try:
        data = await _service(request).confirmed_design_import_status()
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data)


@router.post("/confirmed-design-import")
async def import_confirmed_design(
    request: Request,
    payload: ConfirmedDesignImportRequest,
    _admin=Depends(require_admin),
):
    try:
        data = await _service(request).import_confirmed_design(bundle_hash=payload.bundle_hash)
    except ChatError as exc:
        return _error(request, exc)
    status_code = 201 if data["import_status"] == "created" else 200
    return _success(request, data, status_code=status_code)


@router.get("/interviews")
async def list_interviews(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    _admin=Depends(require_admin),
):
    try:
        items = await _service(request).list_interviews(limit=limit)
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, {"items": items})


@router.post("/interviews", status_code=201)
async def create_interview(request: Request, payload: InterviewCreate, _admin=Depends(require_admin)):
    try:
        data = await _service(request).create_interview(title=payload.title)
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data, status_code=201)


@router.get("/interviews/{interview_id}")
async def get_interview(request: Request, interview_id: str, _admin=Depends(require_admin)):
    try:
        data = await _service(request).get_interview(interview_id)
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data)


@router.post("/interviews/{interview_id}/drafts", status_code=201)
async def create_draft(
    request: Request,
    interview_id: str,
    payload: DraftCreate,
    _admin=Depends(require_admin),
):
    try:
        data = await _service(request).create_draft(
            interview_id,
            version_label=payload.version_label,
            content=payload.content,
            based_on_version_id=payload.based_on_version_id,
        )
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data, status_code=201)


@router.post("/interviews/{interview_id}/feedback", status_code=201)
async def add_feedback(
    request: Request,
    interview_id: str,
    payload: TrialFeedback,
    _admin=Depends(require_admin),
):
    try:
        data = await _service(request).add_feedback(
            interview_id,
            inbound_message_id=payload.inbound_message_id,
            reply_message_id=payload.reply_message_id,
            action=payload.action,
            change_points=payload.change_points,
        )
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data, status_code=201)


@router.get("/versions")
async def list_versions(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    _admin=Depends(require_admin),
):
    try:
        items = await _service(request).list_versions(limit=limit)
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, {"items": items})


@router.get("/versions/{version_id}")
async def get_version(request: Request, version_id: str, _admin=Depends(require_admin)):
    try:
        data = await _service(request).get_version(version_id)
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data)


@router.post("/versions/{version_id}/approve")
async def approve_version(
    request: Request,
    version_id: str,
    payload: PersonaApprovalRequest,
    principal=Depends(require_admin),
):
    try:
        data = await _service(request).approve(
            version_id,
            principal=principal,
            interview_session_id=payload.interview_session_id,
            inbound_message_id=payload.inbound_message_id,
            reply_message_id=payload.reply_message_id,
            expected_state_revision=payload.expected_state_revision,
            version_hash=payload.version_hash,
        )
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data)
