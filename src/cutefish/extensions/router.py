"""Authenticated local routes for tasks, candidates, room records, and probes."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from cutefish.api.router import require_admin

from .adapters import ExplicitAdapterRegistry
from .candidates import CandidateError
from .contracts import (
    CandidateAction,
    AsrTranscriptionRequest,
    ManagedMediaRegister,
    RevisionAction,
    RoomAuthorizationPut,
    RoomCreate,
    RoomExportRequest,
    RoomPatch,
    RoomVersionAction,
    TaskCreate,
    TtsPreviewRequest,
    VoiceCandidateCreate,
    VoicePreviewApproval,
    VoicePreviewCreate,
    VoiceProfilePut,
    VoiceReplyCreate,
    VoiceSettingsPut,
)
from .rooms import RoomError
from .services import create_services
from .tasks import TaskError
from .tts import SpeechError, TtsCapabilityProbe
from .voice import VoiceError


router = APIRouter(prefix="/api")


def _context(request: Request):
    return request.app.state.context


def _services(request: Request):
    context = _context(request)
    services = getattr(context, "extension_services", None)
    if services is None:
        services = create_services(context)
        context.extension_services = services
    return services


def _success(request: Request, data, *, status_code: int = 200) -> Response:
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


def _failure(request: Request, exc: TaskError | CandidateError | RoomError | SpeechError | VoiceError) -> Response:
    status_code = exc.status_code
    return JSONResponse(
        {
            "status": "failed",
            "data": None,
            "evidence": [],
            "error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": status_code >= 500,
                "details": None,
            },
            "usage": None,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "time": datetime.now(timezone.utc).isoformat(),
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


async def _respond(request: Request, awaitable, *, status_code: int = 200) -> Response:
    try:
        return _success(request, await awaitable, status_code=status_code)
    except (TaskError, CandidateError, RoomError, SpeechError, VoiceError) as exc:
        return _failure(request, exc)


async def _assert_task_ready(tasks, task_id: str, expected_revision: int) -> None:
    task = await tasks.get(task_id)
    if task["revision"] != expected_revision:
        raise TaskError("task_revision_conflict", "任务已变化；语音操作结果已丢弃，请刷新后重试。", 409)
    if task["status"] in {"paused", "done", "abandoned"} or task["explicit_pause"] or task["manual_takeover"]:
        raise TaskError("task_unavailable", "任务已暂停、接管或结束，语音操作未启动。", 409)
    if tasks.hard_lock_check is not None:
        reason = await tasks.hard_lock_check(task_id)
        if reason:
            raise TaskError("task_hard_lock", f"任务当前受控：{reason}。", 409)


@router.get("/tasks")
async def list_tasks(
    request: Request,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin=Depends(require_admin),
):
    return await _respond(request, _services(request)["tasks"].list(status=status, limit=limit, offset=offset))


@router.post("/tasks", status_code=201)
async def create_task(request: Request, payload: TaskCreate, _admin=Depends(require_admin)):
    return await _respond(request, _services(request)["tasks"].create(payload.model_dump()), status_code=201)


@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str, _admin=Depends(require_admin)):
    return await _respond(request, _services(request)["tasks"].get(task_id))


@router.post("/tasks/{task_id}/{action}")
async def control_task(
    request: Request,
    task_id: str,
    action: Literal["pause", "resume", "cancel", "handover"],
    payload: RevisionAction,
    _admin=Depends(require_admin),
):
    return await _respond(
        request,
        _services(request)["tasks"].control(
            task_id,
            action,
            expected_revision=payload.expected_revision,
            reason=payload.reason,
            release_handover=payload.release_handover,
        ),
    )


@router.get("/candidates")
async def list_candidates(
    request: Request,
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin=Depends(require_admin),
):
    return await _respond(
        request, _services(request)["candidates"].list(state=state, limit=limit, offset=offset)
    )


@router.get("/candidates/{candidate_id}")
async def get_candidate(request: Request, candidate_id: str, _admin=Depends(require_admin)):
    return await _respond(request, _services(request)["candidates"].get(candidate_id))


@router.get("/candidates/{candidate_id}/preview")
async def preview_candidate(request: Request, candidate_id: str, _admin=Depends(require_admin)):
    return await _respond(request, _services(request)["candidates"].preview(candidate_id))


@router.post("/candidates/{candidate_id}/cancel")
async def cancel_candidate(
    request: Request, candidate_id: str, payload: CandidateAction, _admin=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["candidates"].cancel(
            candidate_id, expected_revision=payload.expected_revision, reason=payload.reason
        ),
    )


@router.post("/candidates/{candidate_id}/reassess")
async def reassess_candidate(
    request: Request, candidate_id: str, payload: CandidateAction, _admin=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["candidates"].reassess(
            candidate_id, expected_revision=payload.expected_revision, reason=payload.reason
        ),
    )


@router.get("/rooms/status")
async def room_status(request: Request, _admin=Depends(require_admin)):
    return await _respond(request, _services(request)["rooms"].status())


@router.put("/rooms/authorization")
async def put_room_authorization(
    request: Request, payload: RoomAuthorizationPut, principal=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["rooms"].put_authorization(
            payload.model_dump(), granted_by=str(principal.admin_id)
        ),
    )


@router.delete("/rooms/authorization")
async def delete_room_authorization(
    request: Request,
    expected_revision: int = Query(ge=1),
    root_key: Literal["room_root", "gallery_root"] = "room_root",
    _admin=Depends(require_admin),
):
    return await _respond(
        request,
        _services(request)["rooms"].revoke_authorization(
            expected_revision=expected_revision, root_key=root_key
        ),
    )


@router.get("/rooms/items")
async def list_room_items(
    request: Request,
    kind: str | None = None,
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin=Depends(require_admin),
):
    return await _respond(
        request,
        _services(request)["rooms"].list(kind=kind, state=state, limit=limit, offset=offset),
    )


@router.post("/rooms/items", status_code=201)
async def create_room_item(request: Request, payload: RoomCreate, _admin=Depends(require_admin)):
    return await _respond(
        request, _services(request)["rooms"].create(payload.model_dump()), status_code=201
    )


@router.post("/rooms/managed-media", status_code=201)
async def register_managed_media(
    request: Request, payload: ManagedMediaRegister, principal=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["rooms"].register_media(
            payload.model_dump(), registered_by=str(principal.admin_id)
        ),
        status_code=201,
    )


@router.get("/rooms/items/{item_id}")
async def get_room_item(request: Request, item_id: str, _admin=Depends(require_admin)):
    return await _respond(request, _services(request)["rooms"].get(item_id))


@router.get("/rooms/items/{item_id}/content")
async def read_room_item_content(request: Request, item_id: str, _admin=Depends(require_admin)):
    return await _respond(request, _services(request)["rooms"].read_content(item_id))


@router.patch("/rooms/items/{item_id}")
async def patch_room_item(
    request: Request, item_id: str, payload: RoomPatch, _admin=Depends(require_admin)
):
    return await _respond(request, _services(request)["rooms"].patch(item_id, payload.model_dump(exclude_unset=True)))


@router.post("/rooms/items/{item_id}/soft-delete")
async def soft_delete_room_item(
    request: Request, item_id: str, payload: RoomVersionAction, _admin=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["rooms"].soft_delete(item_id, expected_version=payload.expected_version),
    )


@router.post("/rooms/items/{item_id}/restore")
async def restore_room_item(
    request: Request, item_id: str, payload: RoomVersionAction, _admin=Depends(require_admin)
):
    return await _respond(
        request, _services(request)["rooms"].restore(item_id, expected_version=payload.expected_version)
    )


@router.post("/rooms/items/{item_id}/export")
async def export_room_item(
    request: Request, item_id: str, payload: RoomExportRequest, _admin=Depends(require_admin)
):
    return await _respond(
        request, _services(request)["rooms"].export(item_id, expected_version=payload.expected_version)
    )


@router.get("/extensions/adapters")
async def adapter_statuses(request: Request, _admin=Depends(require_admin)):
    registry = getattr(_context(request), "extension_adapters", None)
    if registry is None:
        registry = ExplicitAdapterRegistry()
    return await _respond(request, registry.statuses())


@router.get("/extensions/tts")
async def tts_capabilities(request: Request, _admin=Depends(require_admin)):
    services = _services(request)
    result = await asyncio.to_thread(services["tts"].inspect)
    result["qwen_tts"] = await asyncio.to_thread(services["voice_candidates"].capabilities)
    return _success(request, result)


@router.post("/extensions/tts/candidates", status_code=201)
async def create_voice_candidate(
    request: Request, payload: VoiceCandidateCreate, _admin=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["voice_candidates"].create(
            text=payload.text,
            style_prompt=payload.style_prompt,
            seed=payload.seed,
        ),
        status_code=201,
    )


@router.get("/extensions/tts/candidates")
async def list_voice_candidates(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin=Depends(require_admin),
):
    return await _respond(
        request, _services(request)["voice_candidates"].list(limit=limit, offset=offset)
    )


@router.get("/extensions/tts/candidates/{candidate_id}/audio")
async def voice_candidate_audio(
    request: Request, candidate_id: str, _admin=Depends(require_admin)
):
    try:
        audio = await _services(request)["voice_candidates"].audio(candidate_id)
    except SpeechError as exc:
        return _failure(request, exc)
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="cutefish-voice-candidate.wav"',
            "X-Audio-Played": "false",
        },
    )


@router.post("/extensions/tts/preview")
async def tts_preview(request: Request, payload: TtsPreviewRequest, _admin=Depends(require_admin)):
    services = _services(request)
    try:
        if payload.task_id is not None:
            await _assert_task_ready(services["tasks"], payload.task_id, payload.expected_task_revision)
        audio, voice = await asyncio.to_thread(
            services["tts"].synthesize_preview, payload.text, payload.voice_id
        )
        if payload.task_id is not None:
            await _assert_task_ready(services["tasks"], payload.task_id, payload.expected_task_revision)
    except (TaskError, SpeechError) as exc:
        return _failure(request, exc)
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="cutefish-voice-preview.wav"',
            "X-Voice-Engine": "windows.system_speech",
            "X-Voice-Id": quote(voice["voice_id"], safe=""),
            "X-Voice-Approved": "false",
            "X-Voice-Capability": "plain_reading",
            "X-Audio-Played": "false",
            "X-Model-Downloaded": "false",
        },
    )


@router.get("/extensions/voice/profile")
async def voice_profile(request: Request, _admin=Depends(require_admin)):
    return await _respond(request, _services(request)["voice"].status())


@router.put("/extensions/voice/profile")
async def put_voice_profile(
    request: Request, payload: VoiceProfilePut, principal=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["voice"].set_profile(
            expected_revision=payload.expected_revision,
            voice_id=payload.voice_id,
            actor_id=f"admin:{principal.admin_id}",
        ),
    )


@router.put("/extensions/voice/settings")
async def put_voice_settings(
    request: Request, payload: VoiceSettingsPut, principal=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["voice"].set_settings(
            expected_revision=payload.expected_revision,
            runtime_enabled=payload.runtime_enabled,
            voice_outbox_enabled=payload.voice_outbox_enabled,
            actor_id=f"admin:{principal.admin_id}",
        ),
    )


@router.post("/extensions/voice/previews", status_code=201)
async def create_voice_preview(
    request: Request, payload: VoicePreviewCreate, principal=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["voice"].create_profile_preview(
            expected_profile_version=payload.expected_profile_version,
            text=payload.text,
            actor_id=f"admin:{principal.admin_id}",
        ),
        status_code=201,
    )


@router.post("/extensions/voice/approve")
async def approve_voice_preview(
    request: Request, payload: VoicePreviewApproval, principal=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["voice"].approve_profile_preview(
            expected_profile_version=payload.expected_profile_version,
            preview_artifact_id=payload.preview_artifact_id,
            actor_id=f"admin:{principal.admin_id}",
        ),
    )


@router.post("/extensions/voice/replies", status_code=201)
async def create_voice_reply(
    request: Request, payload: VoiceReplyCreate, principal=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["voice"].prepare_voice_reply(
            conversation_id=payload.conversation_id,
            expected_conversation_revision=payload.expected_conversation_revision,
            source_message_id=payload.source_message_id,
            batch_id=payload.batch_id,
            expected_batch_version=payload.expected_batch_version,
            actor_id=f"admin:{principal.admin_id}",
        ),
        status_code=201,
    )


@router.get("/extensions/voice/artifacts/{artifact_id}/content")
async def voice_artifact_content(
    request: Request, artifact_id: str, _admin=Depends(require_admin)
):
    try:
        audio = await _services(request)["voice"].get_artifact_content(artifact_id)
    except VoiceError as exc:
        return _failure(request, exc)
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="cutefish-voice-artifact.wav"',
            "X-Audio-Played": "false",
        },
    )


@router.get("/extensions/asr")
async def asr_capabilities(request: Request, _admin=Depends(require_admin)):
    return _success(request, _services(request)["asr"].capabilities())


@router.post("/extensions/asr/transcriptions")
async def transcribe_room_media(
    request: Request, payload: AsrTranscriptionRequest, _admin=Depends(require_admin)
):
    return await _respond(
        request,
        _services(request)["asr"].transcribe_room_item(
            room_item_id=payload.room_item_id,
            expected_room_version=payload.expected_room_version,
            task_id=payload.task_id,
            expected_task_revision=payload.expected_task_revision,
            submit_to_chat=payload.submit_to_chat,
            conversation_id=payload.conversation_id,
            expected_conversation_revision=payload.expected_conversation_revision,
            chat_idempotency_key=payload.chat_idempotency_key,
        ),
    )


@router.post("/extensions/asr/transcriptions/upload")
async def transcribe_uploaded_audio(request: Request, _admin=Depends(require_admin)):
    body_consumed = asyncio.Event()

    async def upload_chunks():
        async for chunk in request.stream():
            yield chunk
        # Only begin the disconnect watcher after request.stream() has consumed
        # the full body, so it cannot race the upload reader for ASGI messages.
        body_consumed.set()

    transcription = asyncio.create_task(
        _services(request)["asr"].transcribe_upload(
            upload_chunks(),
            content_type=request.headers.get("content-type"),
            content_length=request.headers.get("content-length"),
        )
    )

    async def cancel_when_disconnected():
        await body_consumed.wait()
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                transcription.cancel()
                return

    disconnect_watcher = asyncio.create_task(cancel_when_disconnected())
    try:
        return await _respond(request, transcription)
    finally:
        disconnect_watcher.cancel()
        try:
            await disconnect_watcher
        except asyncio.CancelledError:
            pass
