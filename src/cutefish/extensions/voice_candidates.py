"""Local Qwen VoiceDesign candidate generation and immutable media storage."""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import os
import uuid
import wave
from datetime import timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from cutefish.core.models import LocalVoiceCandidate, utc_now
from cutefish.core.paths import HostPathResolver, PathPolicyError
from cutefish.local_audio.errors import LocalAudioError

from .models import ManagedMediaRecord
from .tts import SpeechError, _validate_wave


VOICE_ID_PREFIX = "qwen3:"
VOICE_REFERENCE_ROOT = "voice-candidates"
VOICE_REFERENCE_PREFIX = f"{VOICE_REFERENCE_ROOT}/"
MAX_REFERENCE_BYTES = 16 * 1024 * 1024
MAX_REFERENCE_SECONDS = 45
MAX_CANDIDATE_TEXT = 160
MAX_STYLE_PROMPT = 500
MAX_SEED = 4_294_967_295
_BASE_RUNTIME_BINDING = {
    "device": "cuda:0",
    "dtype": "bfloat16",
    "attn_implementation": "sdpa",
    "trust_remote_code": False,
    "downloads_enabled": False,
    "generation": {
        "initial_max_new_tokens": 512,
        "oom_retry_max_new_tokens": 256,
        "oom_retry_policy": "single_cuda_oom_retry_half_tokens",
        "max_output_seconds": 45,
        "max_output_bytes": MAX_REFERENCE_BYTES,
    },
}


class VoiceCandidateService:
    """Generate references, register them as managed media, and serve reads."""

    def __init__(
        self,
        session_factory,
        paths: HostPathResolver,
        runtime,
        local_audio_lane,
        *,
        deployment_profile=None,
    ):
        self.session_factory = session_factory
        self.paths = paths
        self.runtime = runtime
        self.local_audio_lane = local_audio_lane
        self.get_deployment_profile = deployment_profile or (lambda: "local_trial")

    def capabilities(self) -> dict[str, Any]:
        """Expose only local readiness metadata; this never loads a model."""
        unavailable = {
            "status": "unavailable",
            "runtime_available": False,
            "candidate_generation_available": False,
            "downloads_enabled": False,
            "trust_remote_code": False,
            "device": "cuda:0",
            "dtype": "bfloat16",
            "attn_implementation": "sdpa",
            "models": {
                "voice_design": {"available": False},
                "base": {"available": False},
            },
            "detail": "Qwen 本地语音 runtime 当前不可用。",
        }
        if self.runtime is None:
            return unavailable
        try:
            result = self.runtime.inspect()
        except Exception:
            return unavailable
        if not isinstance(result, dict):
            return unavailable
        raw_models = result.get("models")
        raw_models = raw_models if isinstance(raw_models, dict) else {}
        models = {}
        for slot in ("voice_design", "base"):
            raw = raw_models.get(slot)
            raw = raw if isinstance(raw, dict) else {}
            models[slot] = {
                key: raw[key]
                for key in ("model_id", "revision", "available", "resource_fingerprint")
                if key in raw
            }
            models[slot].setdefault("available", False)
        runtime_available = bool(result.get("runtime_available"))
        interpreter_available = bool(result.get("interpreter_available"))
        ready = runtime_available and interpreter_available and all(
            models[slot].get("available") is True for slot in ("voice_design", "base")
        )
        status = "ready" if ready else str(result.get("status") or "needs_resource")
        if status not in {"ready", "needs_resource", "unavailable"}:
            status = "needs_resource"
        return {
            "status": status,
            "runtime_available": runtime_available,
            "candidate_generation_available": (
                ready
                and self.local_audio_lane is not None
                and self.get_deployment_profile() == "local_trial"
            ),
            "downloads_enabled": False,
            "trust_remote_code": False,
            "device": str(result.get("device") or "cuda:0"),
            "dtype": str(result.get("dtype") or "bfloat16"),
            "attn_implementation": str(result.get("attn_implementation") or "sdpa"),
            "models": models,
            "detail": str(result.get("detail") or ""),
        }

    async def create(self, *, text: str, style_prompt: str, seed: int) -> dict[str, Any]:
        content = text.strip() if isinstance(text, str) else ""
        style = style_prompt.strip() if isinstance(style_prompt, str) else ""
        if not content or len(content) > MAX_CANDIDATE_TEXT:
            raise SpeechError("invalid_voice_candidate_text", "参考文本必须为1到160个字符。")
        if not style or len(style) > MAX_STYLE_PROMPT:
            raise SpeechError("invalid_voice_candidate_style", "风格提示必须为1到500个字符。")
        if type(seed) is not int or not 0 <= seed <= MAX_SEED:
            raise SpeechError("invalid_voice_candidate_seed", "seed 必须是 uint32 整数。")
        if self.get_deployment_profile() != "local_trial":
            raise SpeechError("target_staging", "target_staging 禁止生成本机声线候选。", 409)
        if self.runtime is None or self.local_audio_lane is None:
            raise SpeechError("qwen_runtime_unavailable", "本机 Qwen 语音 runtime 当前不可用。", 409)

        design_snapshot = await self._snapshot("voice_design")
        base_snapshot = await self._snapshot("base")
        capability = self.capabilities()
        if not capability["candidate_generation_available"]:
            raise SpeechError("qwen_resource_unavailable", capability["detail"] or "本机 Qwen 语音资源尚未就绪。", 409)

        candidate_id = str(uuid.uuid4())
        media_id = str(uuid.uuid4())
        relative_path = f"{VOICE_REFERENCE_ROOT}/{candidate_id}.wav"
        created_path = None
        persisted = False
        try:
            async with self.local_audio_lane.job("tts") as job:
                result = await self.runtime.design_reference(
                    job,
                    text=content,
                    style_prompt=style,
                    seed=seed,
                )
                audio, duration_ms = self._read_result_wav(result, job.workspace)
            self._validate_design_result(result, design_snapshot, seed)

            output_path = self._reference_path(relative_path, for_write=True)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path = self._reference_path(relative_path, for_write=True)
            with output_path.open("xb") as stream:
                stream.write(audio)
                stream.flush()
                os.fsync(stream.fileno())
            created_path = output_path
            digest = hashlib.sha256(audio).hexdigest()
            if output_path.stat().st_size != len(audio) or digest != _result_value(result, "sha256"):
                raise SpeechError("qwen_output_invalid", "VoiceDesign WAV 与 worker 元数据不一致。", 502)

            now = utc_now()
            media = ManagedMediaRecord(
                id=media_id,
                root_key="data_root",
                relative_path=relative_path,
                sha256=digest,
                mime_type="audio/wav",
                byte_length=len(audio),
                source_refs=[{"kind": "owner_supplied", "id": "qwen-voice-design"}],
                source_scope="calibration",
                source_person_id=None,
                role_use_approved=False,
                share_allowed=False,
                registered_by="system:qwen_voice_design",
                version=1,
                asset_class="voice_candidate",
                generation_request_id=candidate_id,
                artifact_purpose="candidate_reference",
                artifact_state="unapproved",
                duration_ms=duration_ms,
                created_at=now,
                legacy_state="registered",
                legacy_media_type="audio/wav",
                legacy_size_bytes=len(audio),
                legacy_updated_at=now,
            )
            candidate = LocalVoiceCandidate(
                id=candidate_id,
                origin="qwen_voice_design",
                status="unapproved",
                managed_media_id=media_id,
                reference_relative_path=relative_path,
                reference_sha256=digest,
                ref_text=content,
                style_prompt=style,
                seed=seed,
                design_model_id=_snapshot_value(design_snapshot, "model_id"),
                design_revision=_snapshot_value(design_snapshot, "revision"),
                design_resource_fingerprint=_snapshot_value(design_snapshot, "resource_fingerprint"),
                base_model_id=_snapshot_value(base_snapshot, "model_id"),
                base_revision=_snapshot_value(base_snapshot, "revision"),
                base_resource_fingerprint=_snapshot_value(base_snapshot, "resource_fingerprint"),
                created_at=now,
            )
            async with self.session_factory() as session:
                async with session.begin():
                    session.add(media)
                    await session.flush()
                    session.add(candidate)
                    await session.flush()
            persisted = True
            return self._candidate_dto(candidate, duration_ms=duration_ms, byte_length=len(audio))
        except BaseException as exc:
            if created_path is not None and not persisted:
                try:
                    created_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception):
                raise
            raise self._translate_error(exc) from exc

    async def list(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(LocalVoiceCandidate)
                        .order_by(LocalVoiceCandidate.created_at.desc(), LocalVoiceCandidate.id.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
            media_ids = [row.managed_media_id for row in rows]
            media_rows = list(
                (
                    await session.scalars(
                        select(ManagedMediaRecord).where(ManagedMediaRecord.id.in_(media_ids))
                    )
                ).all()
            ) if media_ids else []
        media_by_id = {row.id: row for row in media_rows}
        return {
            "items": [
                self._candidate_dto(
                    row,
                    duration_ms=media_by_id.get(row.managed_media_id).duration_ms if media_by_id.get(row.managed_media_id) else None,
                    byte_length=media_by_id.get(row.managed_media_id).byte_length if media_by_id.get(row.managed_media_id) else None,
                )
                for row in rows
            ]
        }

    async def audio(self, candidate_id: str) -> bytes:
        candidate, media = await self._get_rows(candidate_id)
        return self._read_reference(candidate, media)

    async def profile_binding(self, voice_id: str) -> dict[str, Any]:
        candidate_id = _candidate_id_from_voice_id(voice_id)
        candidate, media = await self._get_rows(candidate_id)
        self._read_reference(candidate, media)
        return {
            **self._binding(candidate),
            "runtime": self._runtime_binding(),
        }

    async def profile_reference(self, binding: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        candidate_id = str(binding.get("candidate_id") or "")
        candidate, media = await self._get_rows(candidate_id)
        current_binding = {
            **self._binding(candidate),
            "runtime": self._runtime_binding(),
        }
        if current_binding != binding:
            raise SpeechError("qwen_reference_mismatch", "声线 profile 绑定的候选、参考 hash 或 Base 设置已变化。", 409)
        return current_binding, self._read_reference(candidate, media)

    def _runtime_binding(self) -> dict[str, Any]:
        if self.runtime is None:
            raise SpeechError("qwen_runtime_unavailable", "本机 Qwen runtime 当前不可用。", 409)
        try:
            inspection = self.runtime.inspect()
        except Exception as exc:
            raise SpeechError("qwen_runtime_unavailable", "本机 Qwen runtime 当前不可用。", 409) from exc
        if not isinstance(inspection, dict):
            raise SpeechError("qwen_runtime_unavailable", "本机 Qwen runtime 配置无效。", 409)
        generation = inspection.get("generation")
        observed = {
            "device": inspection.get("device"),
            "dtype": inspection.get("dtype"),
            "attn_implementation": inspection.get("attn_implementation"),
            "trust_remote_code": inspection.get("trust_remote_code"),
            "downloads_enabled": inspection.get("downloads_enabled"),
            "generation": {
                key: generation.get(key) if isinstance(generation, dict) else None
                for key in _BASE_RUNTIME_BINDING["generation"]
            },
        }
        if observed != _BASE_RUNTIME_BINDING:
            raise SpeechError("qwen_runtime_mismatch", "本机 Qwen Base 设置与受支持的固定策略不一致。", 409)
        return observed

    async def _get_rows(self, candidate_id: str):
        if not _is_uuid(candidate_id):
            raise SpeechError("voice_candidate_not_found", "本机声线候选不存在。", 404)
        async with self.session_factory() as session:
            candidate = await session.get(LocalVoiceCandidate, candidate_id)
            if candidate is None:
                raise SpeechError("voice_candidate_not_found", "本机声线候选不存在。", 404)
            media = await session.get(ManagedMediaRecord, candidate.managed_media_id)
        if media is None:
            raise SpeechError("qwen_reference_mismatch", "声线候选缺少登记媒体。", 409)
        return candidate, media

    def _read_reference(self, candidate, media: ManagedMediaRecord) -> bytes:
        relative_path = str(candidate.reference_relative_path)
        if (
            candidate.origin != "qwen_voice_design"
            or candidate.status != "unapproved"
            or media.id != candidate.managed_media_id
            or media.root_key != "data_root"
            or media.relative_path != relative_path
            or not relative_path.startswith(VOICE_REFERENCE_PREFIX)
            or relative_path != f"{VOICE_REFERENCE_ROOT}/{candidate.id}.wav"
            or media.asset_class != "voice_candidate"
            or media.artifact_purpose != "candidate_reference"
            or media.artifact_state != "unapproved"
            or media.mime_type != "audio/wav"
            or media.sha256 != candidate.reference_sha256
            or media.byte_length is None
            or not 0 < media.byte_length <= MAX_REFERENCE_BYTES
        ):
            raise SpeechError("qwen_reference_mismatch", "声线候选媒体登记与候选记录不匹配。", 409)
        try:
            path = self._reference_path(relative_path, must_exist=True)
            before = path.stat()
            if not path.is_file() or before.st_size != media.byte_length:
                raise SpeechError("qwen_reference_mismatch", "声线候选 WAV 缺失或长度不匹配。", 409)
            with path.open("rb") as stream:
                audio = stream.read(MAX_REFERENCE_BYTES + 1)
            after = path.stat()
        except SpeechError:
            raise
        except (PathPolicyError, FileNotFoundError, OSError, ValueError) as exc:
            raise SpeechError("qwen_reference_mismatch", "声线候选 WAV 当前不可读取。", 409) from exc
        if (
            len(audio) != media.byte_length
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or hashlib.sha256(audio).hexdigest() != candidate.reference_sha256
        ):
            raise SpeechError("qwen_reference_mismatch", "声线候选 WAV hash 或文件状态不匹配。", 409)
        duration_ms = _validate_reference_audio(audio)
        if media.duration_ms != duration_ms:
            raise SpeechError("qwen_reference_mismatch", "声线候选 WAV 时长与登记记录不匹配。", 409)
        return audio

    def _reference_path(self, relative_path: str, *, must_exist: bool = False, for_write: bool = False) -> Path:
        try:
            return self.paths.resolve(
                "data_root", relative_path, must_exist=must_exist, for_write=for_write
            )
        except (PathPolicyError, FileNotFoundError, OSError, ValueError) as exc:
            raise SpeechError("qwen_reference_mismatch", "本机受控候选目录当前不可用。", 409) from exc

    async def _snapshot(self, slot: str):
        try:
            snapshot = await asyncio.to_thread(
                self.runtime.resource_snapshot, slot, verify_files=True
            )
            if not all(
                isinstance(_snapshot_value(snapshot, key), str)
                and _snapshot_value(snapshot, key)
                for key in ("model_id", "revision", "resource_fingerprint")
            ):
                raise SpeechError("qwen_resource_unavailable", "Qwen 资源清单缺少固定模型信息。", 409)
            return snapshot
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception):
                raise
            translated = self._translate_error(exc)
            if isinstance(translated, SpeechError):
                raise translated from exc
            raise

    @staticmethod
    def _metadata_matches(result, snapshot) -> bool:
        return all(
            _result_value(result, result_key) == _snapshot_value(snapshot, snapshot_key)
            for result_key, snapshot_key in (
                ("model_id", "model_id"),
                ("model_revision", "revision"),
                ("resource_fingerprint", "resource_fingerprint"),
            )
        )

    def _validate_design_result(self, result, snapshot, requested_seed: int) -> None:
        if not self._metadata_matches(result, snapshot):
            raise SpeechError("qwen_resource_mismatch", "VoiceDesign 资源指纹在生成期间发生变化。", 409)
        runtime = self._runtime_binding()
        generation = runtime["generation"]
        settings = _result_value(result, "settings")
        retried = settings.get("retry_after_cuda_oom") if isinstance(settings, dict) else None
        initial_tokens = generation["initial_max_new_tokens"]
        retry_tokens = generation["oom_retry_max_new_tokens"]
        expected_settings = {
            "device": runtime["device"],
            "dtype": runtime["dtype"],
            "attn_implementation": runtime["attn_implementation"],
            "trust_remote_code": runtime["trust_remote_code"],
            "local_files_only": runtime["downloads_enabled"] is False,
            "seed": requested_seed,
            "initial_max_new_tokens": initial_tokens,
            "max_new_tokens": retry_tokens if retried is True else initial_tokens,
            "attempt_count": 2 if retried is True else 1,
            "retry_policy": generation["oom_retry_policy"],
            "language": "Chinese",
            "retry_after_cuda_oom": retried,
        }
        if (
            not isinstance(settings, dict)
            or set(settings) != set(expected_settings)
            or type(retried) is not bool
            or any(settings.get(key) != value for key, value in expected_settings.items())
            or type(settings.get("seed")) is not int
            or type(settings.get("initial_max_new_tokens")) is not int
            or type(settings.get("max_new_tokens")) is not int
            or type(settings.get("attempt_count")) is not int
        ):
            raise SpeechError("qwen_worker_failed", "VoiceDesign 实际生成设置与候选请求或固定策略不一致。", 502)
        byte_length = _result_value(result, "byte_length")
        duration = _result_value(result, "duration_seconds")
        if (
            type(byte_length) is not int
            or not 0 < byte_length <= generation["max_output_bytes"]
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or not 0 < duration <= generation["max_output_seconds"]
        ):
            raise SpeechError("qwen_output_invalid", "VoiceDesign WAV 超出固定输出大小或时长上限。", 502)

    @staticmethod
    def _read_result_wav(result, workspace: Path) -> tuple[bytes, int]:
        raw_path = _result_value(result, "wav_path")
        if raw_path is None:
            raise SpeechError("qwen_output_invalid", "Qwen worker 没有返回 WAV 文件。", 502)
        try:
            workspace_path = Path(workspace).resolve(strict=True)
            output_path = Path(raw_path).resolve(strict=True)
            output_path.relative_to(workspace_path)
            before = output_path.stat()
            if not output_path.is_file() or not 0 < before.st_size <= MAX_REFERENCE_BYTES:
                raise SpeechError("qwen_output_invalid", "Qwen WAV 长度无效或超过16 MiB。", 502)
            with output_path.open("rb") as stream:
                audio = stream.read(MAX_REFERENCE_BYTES + 1)
            after = output_path.stat()
        except SpeechError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise SpeechError("qwen_output_invalid", "Qwen WAV 未保留在当前临时工作区。", 502) from exc
        if len(audio) != before.st_size or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise SpeechError("qwen_output_invalid", "Qwen WAV 在校验期间发生变化。", 502)
        try:
            duration = _validate_wave(audio, max_seconds=MAX_REFERENCE_SECONDS)
        except (wave.Error, EOFError, OSError, ValueError) as exc:
            raise SpeechError("qwen_output_invalid", "Qwen 结果未通过 PCM WAV 校验。", 502) from exc
        digest = hashlib.sha256(audio).hexdigest()
        reported_size = _result_value(result, "byte_length")
        reported_hash = _result_value(result, "sha256")
        reported_duration = _result_value(result, "duration_seconds")
        reported_rate = _result_value(result, "sample_rate")
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav:
                sample_rate = wav.getframerate()
        except (wave.Error, EOFError, OSError) as exc:
            raise SpeechError("qwen_output_invalid", "VoiceDesign WAV 头无效。", 502) from exc
        if (
            reported_size != len(audio)
            or reported_hash != digest
            or not isinstance(reported_duration, (int, float))
            or abs(float(reported_duration) - duration) > 0.002
            or reported_rate != sample_rate
        ):
            raise SpeechError("qwen_output_invalid", "Qwen WAV 与 worker 返回的元数据不匹配。", 502)
        return audio, int(round(duration * 1000))

    @staticmethod
    def _candidate_dto(candidate, *, duration_ms, byte_length) -> dict[str, Any]:
        return {
            "candidate_id": candidate.id,
            "voice_id": f"{VOICE_ID_PREFIX}{candidate.id}",
            "reference_text": candidate.ref_text,
            "style_prompt": candidate.style_prompt,
            "seed": candidate.seed,
            "reference_sha256": candidate.reference_sha256,
            "duration_ms": duration_ms,
            "byte_length": byte_length,
            "created_at": _iso(candidate.created_at),
            "status": candidate.status,
            "design": {
                "model_id": candidate.design_model_id,
                "revision": candidate.design_revision,
                "resource_fingerprint": candidate.design_resource_fingerprint,
            },
            "base": {
                "model_id": candidate.base_model_id,
                "revision": candidate.base_revision,
                "resource_fingerprint": candidate.base_resource_fingerprint,
            },
        }

    @staticmethod
    def _binding(candidate) -> dict[str, Any]:
        return {
            "candidate_id": candidate.id,
            "reference_sha256": candidate.reference_sha256,
            "reference_text": candidate.ref_text,
            "style_prompt": candidate.style_prompt,
            "seed": candidate.seed,
            "design": {
                "model_id": candidate.design_model_id,
                "revision": candidate.design_revision,
                "resource_fingerprint": candidate.design_resource_fingerprint,
            },
            "base": {
                "model_id": candidate.base_model_id,
                "revision": candidate.base_revision,
                "resource_fingerprint": candidate.base_resource_fingerprint,
            },
        }

    @staticmethod
    def _translate_error(exc: BaseException) -> SpeechError:
        if isinstance(exc, SpeechError):
            return exc
        if isinstance(exc, LocalAudioError):
            return SpeechError(exc.code, exc.message, exc.status_code)
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None)
        status_code = getattr(exc, "status_code", None)
        if isinstance(code, str) and code.startswith("qwen_"):
            return SpeechError(code, str(message or "本机 Qwen 语音处理失败。"), int(status_code or 409))
        if isinstance(exc, OSError):
            return SpeechError("voice_candidate_storage_unavailable", "本机声线候选目录当前不可写。", 409)
        return SpeechError("voice_candidate_store_failed", "本机声线候选未能保存。", 500)


def _result_value(result: Any, key: str):
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)


def _snapshot_value(snapshot: Any, key: str):
    if isinstance(snapshot, dict):
        return snapshot.get(key)
    return getattr(snapshot, key, None)


def _candidate_id_from_voice_id(voice_id: str) -> str:
    if not isinstance(voice_id, str) or not voice_id.startswith(VOICE_ID_PREFIX):
        raise SpeechError("voice_candidate_not_found", "必须指定本机声线候选。", 404)
    candidate_id = voice_id[len(VOICE_ID_PREFIX) :]
    if not _is_uuid(candidate_id):
        raise SpeechError("voice_candidate_not_found", "本机声线候选不存在。", 404)
    return candidate_id


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _validate_reference_audio(audio: bytes) -> int:
    try:
        duration = _validate_wave(audio, max_seconds=MAX_REFERENCE_SECONDS)
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        raise SpeechError("qwen_reference_mismatch", "声线候选没有通过 PCM WAV 校验。", 409) from exc
    return int(round(duration * 1000))


def _iso(value) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()
