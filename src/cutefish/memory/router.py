"""Authenticated person and memory endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cutefish.api.router import require_admin
from cutefish.memory.service import MemoryError, MemoryService


router = APIRouter(prefix="/api")


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonCreate(RequestModel):
    display_name: str = Field(min_length=1, max_length=128)


class MemoryCorrection(RequestModel):
    corrected_content: str = Field(min_length=1, max_length=12000)
    expected_version: int = Field(ge=1)
    source_event_id: str = Field(min_length=1, max_length=256)
    source_message_id: str | None = Field(default=None, max_length=36)


class MemoryCreate(RequestModel):
    scope: str = Field(default="production", min_length=1, max_length=24)
    kind: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=12000)
    source_message_id: str | None = Field(default=None, max_length=36)
    source_event_id: str | None = Field(default=None, max_length=256)
    source_reference: str | None = Field(default=None, max_length=512)
    source_locator: str | None = Field(default=None, max_length=512)


class MemoryDelete(RequestModel):
    expected_version: int = Field(ge=1)


def _service(request: Request) -> MemoryService:
    service = getattr(request.app.state.context, "memory_service", None)
    return service or MemoryService(request.app.state.context.session_factory)


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


def _error(request: Request, exc: MemoryError) -> JSONResponse:
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


@router.get("/people")
async def list_people(request: Request, _admin=Depends(require_admin)):
    return _success(request, {"items": await _service(request).list_people()})


@router.post("/people", status_code=201)
async def create_person(request: Request, payload: PersonCreate, _admin=Depends(require_admin)):
    try:
        item = await _service(request).add_contact(payload.display_name)
    except MemoryError as exc:
        return _error(request, exc)
    return _success(request, item, status_code=201)


@router.get("/memory/people/{person_id}/items")
async def list_memory(
    request: Request,
    person_id: str,
    scope: str = "production",
    include_history: bool = True,
    limit: int = 100,
    _admin=Depends(require_admin),
):
    try:
        items = await _service(request).list_items(
            person_id=person_id,
            scope=scope,
            include_history=include_history,
            limit=limit,
        )
    except MemoryError as exc:
        return _error(request, exc)
    return _success(request, {"items": items, "person_id": person_id, "scope": scope})


@router.post("/memory/people/{person_id}/items", status_code=201)
async def create_memory(
    request: Request,
    person_id: str,
    payload: MemoryCreate,
    _admin=Depends(require_admin),
):
    source_reference = payload.source_reference
    if not any((payload.source_message_id, payload.source_event_id, source_reference)):
        source_reference = "owner_manual_entry"
    try:
        item = await _service(request).create_item(
            person_id=person_id,
            scope=payload.scope,
            kind=payload.kind,
            content=payload.content,
            source_message_id=payload.source_message_id,
            source_event_id=payload.source_event_id,
            source_reference=source_reference,
            source_locator=payload.source_locator,
        )
    except MemoryError as exc:
        return _error(request, exc)
    return _success(request, item, status_code=201)


@router.post("/memory/items/{item_id}/correct")
async def correct_memory(
    request: Request,
    item_id: str,
    payload: MemoryCorrection,
    _admin=Depends(require_admin),
):
    try:
        item = await _service(request).correct_item(
            item_id,
            expected_version=payload.expected_version,
            corrected_content=payload.corrected_content,
            source_event_id=payload.source_event_id,
            source_message_id=payload.source_message_id,
        )
    except MemoryError as exc:
        return _error(request, exc)
    return _success(request, item)


@router.post("/memory/items/{item_id}/delete")
async def delete_memory(
    request: Request,
    item_id: str,
    payload: MemoryDelete,
    _admin=Depends(require_admin),
):
    try:
        item = await _service(request).delete_item(item_id, expected_version=payload.expected_version)
    except MemoryError as exc:
        return _error(request, exc)
    return _success(request, item)
