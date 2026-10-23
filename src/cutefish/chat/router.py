"""Authenticated local chat and draft endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cutefish.api.router import require_admin
from cutefish.memory.service import OWNER_PERSON_ID

from .service import ChatError, ChatService


router = APIRouter(prefix="/api")


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreate(RequestModel):
    person_id: str | None = Field(default=None, min_length=1, max_length=128)
    scope: Literal["production", "calibration"] = "production"
    purpose: Literal["production", "persona_interview", "persona_trial"] = "production"
    title: str | None = Field(default=None, min_length=1, max_length=256)
    persona_version_id: str | None = Field(default=None, min_length=1, max_length=36)


class TurnCreate(RequestModel):
    content: str = Field(min_length=1, max_length=12000)
    idempotency_key: str = Field(min_length=1, max_length=256)


def _service(request: Request) -> ChatService:
    service = getattr(request.app.state.context, "chat_service", None)
    if service is None:
        raise ChatError("chat_not_ready", "聊天服务正在初始化，请稍后重试。", 503)
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


def _error(request: Request, exc: ChatError) -> JSONResponse:
    return JSONResponse(
        {
            "status": "failed",
            "data": None,
            "evidence": [],
            "error": {"code": exc.code, "message": exc.message, "retryable": exc.status_code >= 500, "details": exc.details},
            "usage": None,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "time": datetime.now(timezone.utc).isoformat(),
        },
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/chat/conversations")
async def list_conversations(
    request: Request,
    person_id: str | None = Query(default=None, min_length=1, max_length=128),
    scope: Literal["production", "calibration"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    _admin=Depends(require_admin),
):
    if person_id is not None and person_id != OWNER_PERSON_ID:
        return _error(request, ChatError("ui_actor_mismatch", "本机界面只读取已认证管理员的聊天记录。", 403))
    try:
        return _success(request, {"items": await _service(request).list_conversations(scope=scope, limit=limit)})
    except ChatError as exc:
        return _error(request, exc)


@router.post("/chat/conversations", status_code=201)
async def create_conversation(
    request: Request,
    payload: ConversationCreate,
    _admin=Depends(require_admin),
):
    if payload.person_id is not None and payload.person_id != OWNER_PERSON_ID:
        return _error(request, ChatError("ui_actor_mismatch", "本机界面聊天固定使用已认证管理员身份。", 403))
    try:
        conversation = await _service(request).create_conversation(
            person_id=OWNER_PERSON_ID,
            scope=payload.scope,
            purpose=payload.purpose,
            channel="ui",
            delivery_mode="ui",
            title=payload.title,
            persona_version_id=payload.persona_version_id,
        )
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, conversation, status_code=201)


@router.get("/chat/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: str, _admin=Depends(require_admin)):
    try:
        conversation = await _service(request).get_conversation(conversation_id)
        if conversation["person_id"] != OWNER_PERSON_ID:
            return _error(request, ChatError("conversation_not_found", "会话不存在。", 404))
        messages = await _service(request).list_messages(conversation_id)
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, {"conversation": conversation, "messages": messages})


@router.get("/chat/conversations/{conversation_id}/messages")
async def list_messages(
    request: Request,
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    _admin=Depends(require_admin),
):
    try:
        conversation = await _service(request).get_conversation(conversation_id)
        if conversation["person_id"] != OWNER_PERSON_ID:
            return _error(request, ChatError("conversation_not_found", "会话不存在。", 404))
        items = await _service(request).list_messages(conversation_id, limit=limit)
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, {"items": items, "conversation_id": conversation_id})


@router.post("/chat/conversations/{conversation_id}/turns")
async def create_turn(
    request: Request,
    conversation_id: str,
    payload: TurnCreate,
    _admin=Depends(require_admin),
):
    try:
        conversation = await _service(request).get_conversation(conversation_id)
        if conversation["person_id"] != OWNER_PERSON_ID:
            return _error(request, ChatError("conversation_not_found", "会话不存在。", 404))
        data = await _service(request).send_turn(
            conversation_id,
            content=payload.content,
            idempotency_key=payload.idempotency_key,
            request_id=getattr(request.state, "request_id", None),
        )
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data)


@router.post("/chat/conversations/{conversation_id}/stop")
async def stop_conversation(request: Request, conversation_id: str, _admin=Depends(require_admin)):
    try:
        conversation = await _service(request).get_conversation(conversation_id)
        if conversation["person_id"] != OWNER_PERSON_ID:
            return _error(request, ChatError("conversation_not_found", "会话不存在。", 404))
        data = await _service(request).stop_conversation(conversation_id)
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data)


@router.post("/chat/conversations/{conversation_id}/takeover")
async def takeover_conversation(request: Request, conversation_id: str, _admin=Depends(require_admin)):
    try:
        conversation = await _service(request).get_conversation(conversation_id)
        if conversation["person_id"] != OWNER_PERSON_ID:
            return _error(request, ChatError("conversation_not_found", "会话不存在。", 404))
        data = await _service(request).takeover_conversation(conversation_id)
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data)


@router.get("/chat/requests/{local_request_id}")
async def get_request(request: Request, local_request_id: str, _admin=Depends(require_admin)):
    try:
        data = await _service(request).get_request(local_request_id)
        if data["conversation"]["person_id"] != OWNER_PERSON_ID:
            return _error(request, ChatError("request_not_found", "聊天请求不存在。", 404))
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, data)


@router.get("/chat/drafts")
async def list_drafts(
    request: Request,
    state: Literal["review", "stale", "approved", "sent", "cancelled"] = "review",
    limit: int = Query(default=100, ge=1, le=200),
    _admin=Depends(require_admin),
):
    try:
        items = await _service(request).list_qq_drafts(state=state, limit=limit)
    except ChatError as exc:
        return _error(request, exc)
    return _success(request, {"items": items})
