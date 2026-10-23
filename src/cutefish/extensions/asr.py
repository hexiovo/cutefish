"""On-demand local ASR for approved Room media and transient owner uploads."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import sys
import wave
from collections.abc import AsyncIterable
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
ASR_MODEL_IDS = ("small",)
ASR_ACCEPTED_FORMATS = ("wav", "mp3", "m4a", "ogg", "flac")
ASR_ACCEPTED_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/x-m4a",
        "audio/ogg",
        "audio/flac",
        "audio/x-flac",
    }
)


class AsrService:
    def __init__(
        self,
        paths: HostPathResolver,
        rooms: RoomService,
        tasks: TaskService,
        chat_service=None,
        local_audio_lane=None,
    ):
        self.paths = paths
        self.rooms = rooms
        self.tasks = tasks
        self.chat_service = chat_service
        self.local_audio_lane = local_audio_lane

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
            status = "ready"
            detail = "本机离线 small ASR 运行时与模型资源可用（CPU/int8）；转写不会自动提交聊天或发送语音。"
        elif installed_version is None:
            status = "needs_resource"
            detail = "项目 venv 尚无 faster-whisper；不会自动安装或联网回退。"
        else:
            status = "needs_resource"
            detail = "model_root/faster-whisper/small 本地模型尚不可用；不会自动下载或联网回退。"
        return {
            "status": status,
            "engine_id": "faster-whisper",
            "installed_version": installed_version,
            "runtime_available": runtime_available,
            "model_candidates": models,
            "selected_model_id": available_model,
            "accepted_formats": list(ASR_ACCEPTED_FORMATS),
            "device": "cpu",
            "compute_type": "int8",
            "max_duration_seconds": MAX_ASR_SECONDS,
            "max_bytes": MAX_ASR_BYTES,
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
        from cutefish.local_audio import (
            LocalAudioBusy,
            LocalAudioTimeout,
            LocalAudioUnavailable,
            LocalAudioWorkerStartError,
        )

        if self.local_audio_lane is None:
            raise SpeechError("local_audio_unavailable", "本机语音处理通道尚未接入。", 503)
        try:
            # Room media and raw uploads share the same non-waiting lane so an
            # ASR request cannot overlap a TTS worker or another ASR worker.
            async with self.local_audio_lane.job("asr") as job:
                audio_path = job.workspace / "input.audio"
                try:
                    await asyncio.to_thread(audio_path.write_bytes, audio)
                except OSError as exc:
                    raise SpeechError("temp_root_unavailable", "本机 ASR 临时目录当前不可写。", 503) from exc
                worker_result = await self._run_worker(job, audio_path, model_path)
        except LocalAudioBusy as exc:
            raise SpeechError("asr_busy", "本机语音处理通道正忙，请稍后重试。", 409) from exc
        except LocalAudioTimeout as exc:
            raise SpeechError("asr_worker_timeout", "本地 ASR 超过五分钟时限，操作已停止。", 504) from exc
        except LocalAudioUnavailable as exc:
            raise SpeechError("local_audio_unavailable", "本机语音临时工作区当前不可用。", 503) from exc
        except LocalAudioWorkerStartError as exc:
            raise SpeechError("asr_worker_unavailable", "本地 ASR worker 当前不可用。", 503) from exc
        if task_id is not None:
            await self._assert_task_current(task_id, expected_task_revision)
        latest_item = await self.rooms.get(room_item_id)
        if latest_item.get("version") != expected_room_version or latest_item.get("state") != "ready":
            raise RoomError("room_version_conflict", "音频条目在识别期间发生变化；本次结果已丢弃。", 409)
        result = {
            "status": "transcribed",
            "transcript": worker_result["text"],
            "language": worker_result["language"],
            "duration_seconds": worker_result["duration_seconds"],
            "segments": worker_result["segments"],
            "source": {
                "kind": "room_media",
                "room_item_id": room_item_id,
                "room_version": expected_room_version,
                "media_type": worker_result["media_type"],
                "duration_seconds": worker_result["duration_seconds"],
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
            if not worker_result["text"].strip():
                raise SpeechError("empty_transcription", "识别结果为空，没有提交到 ChatService。", 422)
            chat_request = await self._submit_to_chat(
                conversation_id=conversation_id,
                expected_revision=expected_conversation_revision,
                idempotency_key=chat_idempotency_key,
                transcript=worker_result["text"],
            )
            result["downstream"]["chat_service"] = "submitted"
            result["chat_request"] = chat_request
            result["detail"] = "识别文本已按明确请求提交到 ChatService；本操作没有合成或发送语音，也没有启动 QQ Outbox。"
        return result

    async def transcribe_upload(
        self,
        chunks: AsyncIterable[bytes],
        *,
        content_type: str | None,
        content_length: str | None = None,
    ) -> dict[str, Any]:
        """Transcribe one explicitly submitted raw audio body in a private workspace."""
        normalized_content_type = (content_type or "").split(";", maxsplit=1)[0].strip().casefold()
        if normalized_content_type not in ASR_ACCEPTED_CONTENT_TYPES:
            raise SpeechError(
                "unsupported_audio_format",
                "上传必须是本机音频原始字节流；支持 WAV、MP3、M4A、OGG 或 FLAC。",
                422,
            )
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError) as exc:
                raise SpeechError("invalid_content_length", "音频上传长度标头无效。", 400) from exc
            if declared_length < 0:
                raise SpeechError("invalid_content_length", "音频上传长度标头无效。", 400)
            if declared_length > MAX_ASR_BYTES:
                raise SpeechError("audio_too_large", "音频超过64 MiB上限。", 413)

        capabilities = self.capabilities()
        if not capabilities["runtime_available"]:
            raise SpeechError("asr_model_unavailable", capabilities["detail"], 409)
        model_id = str(capabilities["selected_model_id"])
        model_path = self._model_path(model_id)
        if model_path is None:
            raise SpeechError("asr_model_unavailable", "ASR 本地模型在处理前已不可用。", 409)

        from cutefish.local_audio import (
            LocalAudioBusy,
            LocalAudioTimeout,
            LocalAudioUnavailable,
            LocalAudioWorkerStartError,
        )

        if self.local_audio_lane is None:
            raise SpeechError("local_audio_unavailable", "本机语音处理通道尚未接入。", 503)
        try:
            # Reserve the shared non-waiting lane before receiving the body. A busy
            # ASR/TTS request therefore fails without buffering private audio.
            async with self.local_audio_lane.job("asr") as job:
                input_path = job.workspace / "input.audio"
                content_hash, byte_length = await self._stream_upload(chunks, input_path)
                worker_result = await self._run_worker(job, input_path, model_path)
        except LocalAudioBusy as exc:
            raise SpeechError("asr_busy", "本机语音处理通道正忙，请稍后重试。", 409) from exc
        except LocalAudioTimeout as exc:
            raise SpeechError("asr_worker_timeout", "本地 ASR 超过五分钟时限，操作已停止。", 504) from exc
        except LocalAudioUnavailable as exc:
            raise SpeechError("local_audio_unavailable", "本机语音临时工作区当前不可用。", 503) from exc
        except LocalAudioWorkerStartError as exc:
            raise SpeechError("asr_worker_unavailable", "本地 ASR worker 当前不可用。", 503) from exc

        return {
            "status": "transcribed",
            "transcript": worker_result["text"],
            "language": worker_result["language"],
            "duration_seconds": worker_result["duration_seconds"],
            "segments": worker_result["segments"],
            "source": {
                "kind": "owner_upload",
                "media_type": worker_result["media_type"],
                "byte_length": byte_length,
                "content_hash": content_hash,
            },
            "downstream": {
                "chat_service": "not_started",
                "voice_reply": "not_approved",
                "qq_outbox": "not_started",
            },
            "detail": "上传音频已在本机离线转写；没有提交 ChatService、合成语音或启动 QQ Outbox。",
        }

    async def _stream_upload(self, chunks: AsyncIterable[bytes], path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        total_bytes = 0
        try:
            with path.open("xb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise SpeechError("invalid_audio_payload", "音频上传数据无效。", 422)
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > MAX_ASR_BYTES:
                        raise SpeechError("audio_too_large", "音频超过64 MiB上限。", 413)
                    digest.update(chunk)
                    output.write(chunk)
        except SpeechError:
            raise
        except OSError as exc:
            raise SpeechError("temp_root_unavailable", "本机 ASR 临时目录当前不可写。", 503) from exc
        if total_bytes == 0:
            raise SpeechError("invalid_audio_payload", "上传音频为空。", 422)
        return digest.hexdigest(), total_bytes

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

    async def _run_worker(self, job, audio_path: Path, model_path: Path) -> dict[str, Any]:
        try:
            completed = await job.run(
                [
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
                    "--workspace",
                    str(job.workspace),
                    "--max-seconds",
                    str(MAX_ASR_SECONDS),
                ],
                timeout_seconds=MAX_ASR_WORKER_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except SpeechError:
            raise
        except OSError as exc:
            raise SpeechError("asr_worker_unavailable", "本地 ASR worker 当前不可用。", 503) from exc

        try:
            result = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if completed.returncode != 0:
                raise SpeechError("asr_worker_failed", "本地 ASR worker 失败；没有启用网络或下载回退。", 503) from exc
            raise SpeechError("asr_worker_invalid_result", "本地 ASR worker 返回了无效结果。", 503) from exc
        if not isinstance(result, dict):
            raise SpeechError("asr_worker_invalid_result", "本地 ASR worker 返回了无效结果。", 503)
        error = result.get("error")
        if completed.returncode != 0:
            error_map = {
                "invalid_audio_container": ("invalid_audio_container", "音频容器无效或不受支持。", 422),
                "unsupported_audio_format": ("unsupported_audio_format", "音频编码不受支持。支持 WAV、MP3、M4A、OGG 或 FLAC。", 422),
                "audio_too_long": ("audio_too_long", "音频超过120秒上限。", 422),
                "invalid_audio": ("invalid_audio", "音频无法解码或没有可识别的音轨。", 422),
            }
            details = error_map.get(error)
            if details is not None:
                raise SpeechError(*details)
            raise SpeechError("asr_worker_failed", "本地 ASR worker 失败；没有启用网络或下载回退。", 503)

        transcript = result.get("text")
        language = result.get("language")
        duration = result.get("duration_seconds")
        media_type = result.get("media_type")
        segments = result.get("segments")
        if (
            not isinstance(transcript, str)
            or len(transcript) > 12_000
            or (
                language is not None
                and (not isinstance(language, str) or len(language) > 32)
            )
            or not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
            or duration <= 0
            or duration > MAX_ASR_SECONDS
            or media_type not in {"audio/wav", "audio/mpeg", "audio/mp4", "audio/ogg", "audio/flac"}
            or not isinstance(segments, list)
            or len(segments) > 2_000
        ):
            raise SpeechError("asr_worker_invalid_result", "本地 ASR worker 返回了无效结果。", 503)
        normalized_segments: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise SpeechError("asr_worker_invalid_result", "本地 ASR worker 返回了无效分段。", 503)
            start, end, text = segment.get("start"), segment.get("end"), segment.get("text")
            if (
                not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or isinstance(start, bool)
                or isinstance(end, bool)
                or not math.isfinite(float(start))
                or not math.isfinite(float(end))
                or start < 0
                or end < start
                or end > duration + 0.25
                or not isinstance(text, str)
                or len(text) > 4_000
            ):
                raise SpeechError("asr_worker_invalid_result", "本地 ASR worker 返回了无效分段。", 503)
            normalized_segments.append({"start": float(start), "end": float(end), "text": text})
        return {
            "text": transcript.strip(),
            "language": language,
            "duration_seconds": round(float(duration), 3),
            "media_type": media_type,
            "segments": normalized_segments,
        }
