"""Authenticated local QQ bridge status and identity configuration."""

from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cutefish.api.router import require_admin

from .service import QqError, QqService


router = APIRouter(prefix="/api")


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QqAccountCreate(RequestModel):
    account_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    service_url: str | None = Field(default=None, max_length=512)


class QqIdentityBind(RequestModel):
    sender_id: str = Field(min_length=1, max_length=128)


class QqContactBind(QqIdentityBind):
    person_id: str = Field(min_length=1, max_length=128)


class QqCredentials(RequestModel):
    napcat_access_token: str = Field(min_length=1, max_length=4096)
    webhook_secret: str = Field(min_length=32, max_length=4096)


class QqEnabled(RequestModel):
    enabled: bool


def _service(request: Request) -> QqService:
    context = request.app.state.context
    service = getattr(context, "qq_service", None)
    if service is None:
        service = QqService(context.session_factory, context.secrets, getattr(context, "memory_service", None))
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


def _error(request: Request, exc: QqError) -> JSONResponse:
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


@router.get("/qq/status")
async def qq_status(request: Request, _admin=Depends(require_admin)):
    try:
        return _success(request, await _service(request).status())
    except QqError as exc:
        return _error(request, exc)


@router.get("/qq/accounts")
async def qq_accounts(request: Request, _admin=Depends(require_admin)):
    try:
        return _success(request, {"items": await _service(request).list_accounts()})
    except QqError as exc:
        return _error(request, exc)


@router.post("/qq/accounts", status_code=201)
async def create_qq_account(request: Request, payload: QqAccountCreate, _admin=Depends(require_admin)):
    try:
        item = await _service(request).create_account(
            account_id=payload.account_id,
            display_name=payload.display_name,
            service_url=payload.service_url,
        )
    except QqError as exc:
        return _error(request, exc)
    return _success(request, item, status_code=201)


@router.get("/qq/accounts/{account_id}/identities")
async def qq_identities(request: Request, account_id: str, _admin=Depends(require_admin)):
    try:
        items = await _service(request).list_identities(account_id)
    except QqError as exc:
        return _error(request, exc)
    return _success(request, {"items": items, "account_id": account_id})


@router.put("/qq/accounts/{account_id}/owner-identity")
async def bind_qq_owner(
    request: Request,
    account_id: str,
    payload: QqIdentityBind,
    _admin=Depends(require_admin),
):
    try:
        item = await _service(request).bind_owner(account_id, payload.sender_id)
    except QqError as exc:
        return _error(request, exc)
    return _success(request, item)


@router.put("/qq/accounts/{account_id}/contact-identities")
async def bind_qq_contact(
    request: Request,
    account_id: str,
    payload: QqContactBind,
    _admin=Depends(require_admin),
):
    try:
        item = await _service(request).bind_contact(
            account_id,
            person_id=payload.person_id,
            sender_id=payload.sender_id,
        )
    except QqError as exc:
        return _error(request, exc)
    return _success(request, item)


@router.put("/qq/accounts/{account_id}/credentials")
async def configure_qq_credentials(
    request: Request,
    account_id: str,
    payload: QqCredentials,
    _admin=Depends(require_admin),
):
    try:
        item = await _service(request).configure_credentials(
            account_id,
            napcat_access_token=payload.napcat_access_token,
            webhook_secret=payload.webhook_secret,
        )
    except QqError as exc:
        return _error(request, exc)
    return _success(request, item)


@router.put("/qq/accounts/{account_id}/enabled")
async def set_qq_enabled(
    request: Request,
    account_id: str,
    payload: QqEnabled,
    _admin=Depends(require_admin),
):
    try:
        item = await _service(request).set_enabled(account_id, enabled=payload.enabled)
    except QqError as exc:
        return _error(request, exc)
    return _success(request, item)


@router.post("/qq/events", status_code=202)
async def receive_qq_event(request: Request):
    """NapCat OneBot v11 reverse HTTP callback; authenticated by its dedicated header secret."""
    declared_length = request.headers.get("content-length")
    if declared_length and (not declared_length.isdigit() or int(declared_length) > 65536):
        return _error(request, QqError("event_too_large", "QQ事件超过本机接收上限。", 413))
    raw = await request.body()
    if len(raw) > 65536:
        return _error(request, QqError("event_too_large", "QQ事件超过本机接收上限。", 413))
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error(request, QqError("invalid_qq_event", "QQ事件必须是有效JSON。", 422))
    if not isinstance(payload, dict):
        return _error(request, QqError("invalid_qq_event", "QQ事件必须是JSON对象。", 422))
    try:
        result = await _service(request).accept_event(
            payload,
            webhook_secret=request.headers.get("x-cutefish-webhook-secret"),
        )
    except QqError as exc:
        return _error(request, exc)
    return _success(request, result, status_code=202)
