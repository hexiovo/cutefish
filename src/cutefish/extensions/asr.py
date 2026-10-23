"""On-demand, local-only faster-whisper transcription for approved Room WAVs."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

from cutefish.chat.service import ChatError
from cutefish.core.paths import HostPathResolver, PathPolicyError

from .rooms import RoomError, RoomService
from .tasks import TaskError, TaskService
from .tts import SpeechError, _validate_wave


MAX_ASR_BYTES = 64 * 1024 * 1024
MAX_ASR_SECONDS = 120
MAX_ASR_WORKER_SECONDS = 300
ASR_MODEL_IDS = ("base", "small")


class AsrService:
    def __init__(self, paths: HostPathResolver, rooms: RoomService, tasks: TaskService, chat_service=None):
        self.paths = paths
        self.rooms = rooms
        self.tasks = tasks
        self.chat_service = chat_service

    def capabilities(self) -> dict[str, Any]:
        try:
            from importlib.metadata import PackageNotFoundError, version

            try:
                installed_version = version("faster-whisper")
            except PackageNotFoundError:
                installed_version = None
        except Exception:
            installed_version = None

        models = [
            {"model_id": model_id, "installed": self._model_path(model_id) is not None}
            for model_id in ASR_MODEL_IDS
        ]
        available_model = next((item["model_id"] for item in models if item["installed"]), None)
        runtime_available = installed_version is not None and available_model is not None
        if runtime_available:
            status = "partial"
            detail = "本机存在可用的 faster-whisper CPU int8 模型；试听、ChatService 与 QQ 语音回复仍需分别核验和批准。"
        elif installed_version is None:
            status = "needs_resource"
            detail = "faster-whisper 未安装；未导入、下载、安装或联网。"
        else:
            status = "needs_resource"
            detail = "未发现 model_root/faster-whisper/base 或 small 本地模型；未下载模型。"
        return {
            "status": status,
            "engine_id": "faster-whisper",
            "installed_version": installed_version,
            "runtime_available": runtime_available,
            "model_candidates": models,
            "selected_model_id": available_model,
            "device": "cpu",
            "compute_type": "int8",
            "max_duration_seconds": MAX_ASR_SECONDS,
            "max_bytes": MAX_ASR_BYTES,
            "accepted_formats": ["audio/wav"],
            "chat_service": "not_started",
            "voice_reply": "not_approved",
            "qq_outbox": "not_started",
            "downloads_enabled": False,
            "detail": detail,
        }

    async def transcribe_room_item(
        self,
        *,
        room_item_id: str,
        expected_room_version: int,
        task_id: str | None = None,
        expected_task_revision: int | None = None,
        submit_to_chat: bool = False,
        conversation_id: str | None = None,
        expected_conversation_revision: int | None = None,
        chat_idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if (task_id is None) != (expected_task_revision is None):
            raise SpeechError("invalid_task_revision", "任务 ID 与版本号必须同时提供。")
        chat_fields = (conversation_id, expected_conversation_revision, chat_idempotency_key)
        if submit_to_chat and any(value is None for value in chat_fields):
            raise SpeechError("invalid_chat_submission", "明确提交到聊天时，会话 ID、预期版本和幂等键均为必需项。")
        if not submit_to_chat and any(value is not None for value in chat_fields):
            raise SpeechError("invalid_chat_submission", "只有明确提交到 ChatService 时才能提供聊天字段。")
        if submit_to_chat and task_id is not None:
            raise SpeechError("invalid_chat_submission", "带任务锁的 ASR 不能在同一请求中派发模型聊天。")
        if task_id is not None:
            await self._assert_task_current(task_id, expected_task_revision)

        item = await self.rooms.get(room_item_id)
        if item.get("version") != expected_room_version:
            raise RoomError("room_version_conflict", "房间条目已更新，请刷新后重试。", 409)
        if item.get("state") != "ready" or not item.get("managed_media_id"):
            raise SpeechError("audio_room_media_required", "ASR 只读取已就绪、已登记的 Room 媒体条目。", 409)

        content = await self.rooms.read_content(room_item_id)
        if content.get("encoding") != "base64" or content.get("media_type") not in {"audio/wav", "audio/x-wav"}:
            raise SpeechError("unsupported_audio_format", "本地 ASR 当前只接受已批准的 PCM WAV 媒体。", 422)
        try:
            encoded = content.get("data")
            if not isinstance(encoded, str) or len(encoded) > ((MAX_ASR_BYTES + 2) // 3) * 4:
                raise SpeechError("audio_too_large", "音频超过64 MiB上限。", 413)
            audio = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise SpeechError("invalid_audio_payload", "已登记的 WAV 媒体数据无效。", 422) from exc
        if len(audio) > MAX_ASR_BYTES:
            raise SpeechError("audio_too_large", "音频超过64 MiB上限。", 413)
        try:
            duration = _validate_wave(audio, max_seconds=MAX_ASR_SECONDS)
        except (wave.Error, EOFError, OSError) as exc:
            raise SpeechError("invalid_audio_wave", "音频必须是可读取、时长不超过120秒的 PCM WAV。", 422) from exc

        # Recheck after the separately authorized Room read to avoid using a
        # stale item version or task state as the transcription input.
        current_item = await self.rooms.get(room_item_id)
        if current_item.get("version") != expected_room_version or current_item.get("state") != "ready":
            raise RoomError("room_version_conflict", "音频条目在读取期间发生变化，请刷新后重试。", 409)
        if task_id is not None:
            await self._assert_task_current(task_id, expected_task_revision)

        capabilities = self.capabilities()
        if not capabilities["runtime_available"]:
            return {
                "status": "needs_resource",
                "transcript": None,
                "source": {
                    "room_item_id": room_item_id,
                    "room_version": expected_room_version,
                    "media_type": content["media_type"],
                    "duration_seconds": round(duration, 3),
                    "byte_length": len(audio),
                    "content_hash": content["content_hash"],
                },
                "downstream": {
                    "chat_service": "not_started",
                    "voice_reply": "not_approved",
                    "qq_outbox": "not_started",
                },
                "detail": capabilities["detail"],
            }

        model_id = str(capabilities["selected_model_id"])
        model_path = self._model_path(model_id)
        if model_path is None:
            raise SpeechError("asr_model_unavailable", "ASR 本地模型在处理前已不可用。", 409)
        transcript, language = await self._run_worker(audio, model_path)
        if task_id is not None:
            await self._assert_task_current(task_id, expected_task_revision)
        latest_item = await self.rooms.get(room_item_id)
        if latest_item.get("version") != expected_room_version or latest_item.get("state") != "ready":
            raise RoomError("room_version_conflict", "音频条目在识别期间发生变化；本次结果已丢弃。", 409)
        result = {
            "status": "transcribed",
            "transcript": transcript,
            "language": language,
            "source": {
                "room_item_id": room_item_id,
                "room_version": expected_room_version,
                "media_type": content["media_type"],
                "duration_seconds": round(duration, 3),
                "byte_length": len(audio),
                "content_hash": content["content_hash"],
            },
            "downstream": {
                "chat_service": "ready_for_explicit_user_submission",
                "voice_reply": "not_approved",
                "qq_outbox": "not_started",
            },
            "detail": "识别结果尚未发送给 ChatService、合成语音或加入 QQ Outbox。",
        }
        if submit_to_chat:
            chat_request = await self._submit_to_chat(
                conversation_id=conversation_id,
                expected_revision=expected_conversation_revision,
                idempotency_key=chat_idempotency_key,
                transcript=transcript,
            )
            result["downstream"]["chat_service"] = "submitted"
            result["chat_request"] = chat_request
            result["detail"] = "识别文本已按明确请求提交到 ChatService；本操作没有合成或发送语音，也没有启动 QQ Outbox。"
        return result

    async def _assert_conversation_current(self, conversation_id: str | None, expected_revision: int | None) -> None:
        if self.chat_service is None:
            raise SpeechError("chat_service_unavailable", "ChatService 未接入；没有提交识别文本。", 503)
        if conversation_id is None or expected_revision is None:
            raise SpeechError("invalid_chat_submission", "提交到 ChatService 时必须提供会话 ID 和预期版本。")
        matcher = getattr(self.chat_service, "conversation_revision_matches", None)
        if matcher is None or not await matcher(conversation_id, expected_revision):
            raise SpeechError("conversation_revision_conflict", "会话版本已变化，请刷新后重新提交；识别文本未发送。", 409)

    async def _submit_to_chat(
        self,
        *,
        conversation_id: str | None,
        expected_revision: int | None,
        idempotency_key: str | None,
        transcript: str,
    ) -> dict[str, Any]:
        if self.chat_service is None:
            raise SpeechError("chat_service_unavailable", "ChatService 未接入；没有提交识别文本。", 503)
        if conversation_id is None or expected_revision is None or idempotency_key is None:
            raise SpeechError("invalid_chat_submission", "提交到 ChatService 时会话参数不完整。")

        try:
            # ChatService's idempotency lookup runs before its own state checks.
            # Keep that behavior for retries whose successful prior submission
            # has already advanced the conversation revision.
            find_existing = getattr(self.chat_service, "_find_request_by_key", None)
            existing = await find_existing(idempotency_key) if find_existing is not None else None
            if existing is None:
                await self._assert_conversation_current(conversation_id, expected_revision)
            return await self.chat_service.send_turn(
                conversation_id,
                content=transcript,
                idempotency_key=idempotency_key,
            )
        except ChatError as exc:
            raise SpeechError(exc.code, exc.message, exc.status_code) from exc

    async def _assert_task_current(self, task_id: str, expected_revision: int | None) -> None:
        if expected_revision is None:
            raise SpeechError("invalid_task_revision", "任务版本号必需。")
        task = await self.tasks.get(task_id)
        if task["revision"] != expected_revision:
            raise TaskError("task_revision_conflict", "任务已变化；语音操作结果已丢弃，请刷新后重试。", 409)
        if task["status"] in {"paused", "done", "abandoned"} or task["explicit_pause"] or task["manual_takeover"]:
            raise TaskError("task_unavailable", "任务已暂停、接管或结束，语音操作未启动。", 409)
        if self.tasks.hard_lock_check is not None:
            reason = await self.tasks.hard_lock_check(task_id)
            if reason:
                raise TaskError("task_hard_lock", f"任务当前受控：{reason}。", 409)

    def _model_path(self, model_id: str) -> Path | None:
        if model_id not in ASR_MODEL_IDS:
            return None
        try:
            path = self.paths.resolve("model_root", f"faster-whisper/{model_id}", must_exist=True)
        except (PathPolicyError, FileNotFoundError, OSError):
            return None
        required = (path / "model.bin", path / "config.json")
        return path if path.is_dir() and all(item.is_file() for item in required) else None

    async def _run_worker(self, audio: bytes, model_path: Path) -> tuple[str, str | None]:
        try:
            worker_root = self.paths.resolve("temp_root", "extensions/asr-worker", for_write=True)
            worker_root.mkdir(parents=True, exist_ok=True)
        except (PathPolicyError, OSError) as exc:
            raise SpeechError("temp_root_unavailable", "本机 ASR 临时目录当前不可写。", 409) from exc

        try:
            with tempfile.TemporaryDirectory(prefix="transcribe-", dir=worker_root) as temp_dir:
                audio_path = Path(temp_dir) / "input.wav"
                audio_path.write_bytes(audio)
                env = os.environ.copy()
                env.update(
                    {
                        "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1",
                        "HF_DATASETS_OFFLINE": "1",
                    }
                )
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-X",
                    "utf8",
                    "-B",
                    "-m",
                    "cutefish.extensions.asr_worker",
                    "--model-path",
                    str(model_path),
                    "--audio-path",
                    str(audio_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    creationflags=creationflags,
                )
                try:
                    stdout, _stderr = await asyncio.wait_for(
                        process.communicate(), timeout=MAX_ASR_WORKER_SECONDS
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    process.kill()
                    await process.wait()
                    raise
                if process.returncode != 0:
                    raise SpeechError("asr_worker_failed", "本地 ASR worker 失败；没有启用网络或下载回退。", 503)
                try:
                    result = json.loads(stdout.decode("utf-8"))
                    transcript = result["text"]
                    language = result.get("language")
                except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise SpeechError("asr_worker_invalid_result", "本地 ASR worker 返回了无效结果。", 503) from exc
                if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 12_000:
                    raise SpeechError("asr_result_invalid", "识别文本为空或超出长度限制。", 503)
                return transcript, language if isinstance(language, str) else None
        except asyncio.TimeoutError as exc:
            raise SpeechError("asr_worker_timeout", "本地 ASR 超过五分钟时限，操作已停止。", 504) from exc
        except asyncio.CancelledError:
            raise
        except SpeechError:
            raise
        except OSError as exc:
            raise SpeechError("asr_worker_unavailable", "本地 ASR worker 当前不可用。", 503) from exc
