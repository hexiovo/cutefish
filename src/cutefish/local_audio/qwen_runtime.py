"""Pinned, offline Qwen3-TTS workers for local voice design and reuse.

The API process never imports torch or Qwen. It validates the controlled model
snapshot, writes a bounded request into a lane-owned workspace, and starts the
configured interpreter for one checkpoint at a time.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import wave
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from cutefish.core.paths import HostPathResolver, PathPolicyError

from .errors import LocalAudioError
from .lane import LocalAudioJob


QWEN_WORKER_TIMEOUT_SECONDS = 600
MAX_WORKER_JSON_BYTES = 64 * 1024
MAX_REFERENCE_BYTES = 16 * 1024 * 1024
MAX_REFERENCE_SECONDS = 45
MAX_TEXT_CHARACTERS = 160
MAX_SYNTHESIS_CHARACTERS = 2_000
MAX_STYLE_CHARACTERS = 500
MAX_NEW_TOKENS = 512
OOM_RETRY_MAX_NEW_TOKENS = 256
OOM_RETRY_POLICY = "single_cuda_oom_retry_half_tokens"
_HF_DOWNLOAD_DIR = ".cache/huggingface/download"
_HF_GITIGNORE = ".cache/huggingface/.gitignore"
_HF_SIDECAR_SUFFIXES = (".metadata", ".lock", ".incomplete")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

ModelSlot = Literal["voice_design", "base"]

_MODEL_INFO: dict[str, dict[str, Any]] = {
    "voice_design": {
        "directory": "voice-design",
        "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        "revision": "5ecdb67327fd37bb2e042aab12ff7391903235d3",
        "expected_snapshot_bytes": 4_520_163_832,
        "file_sizes": {
            ".gitattributes": 1519,
            "README.md": 3214,
            "config.json": 4421,
            "generation_config.json": 245,
            "merges.txt": 1_671_839,
            "model.safetensors": 3_833_402_552,
            "preprocessor_config.json": 127,
            "speech_tokenizer/config.json": 2336,
            "speech_tokenizer/configuration.json": 76,
            "speech_tokenizer/model.safetensors": 682_293_092,
            "speech_tokenizer/preprocessor_config.json": 234,
            "tokenizer_config.json": 7344,
            "vocab.json": 2_776_833,
        },
        "known_hashes": {
            "model.safetensors": "391e8db219f292c515297cdceeb43e4eae67cdde35fa57e79a6a8a532fca0522",
            "speech_tokenizer/model.safetensors": "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258",
        },
    },
    "base": {
        "directory": "base",
        "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "revision": "fd4b254389122332181a7c3db7f27e918eec64e3",
        "expected_snapshot_bytes": 4_544_229_700,
        "file_sizes": {
            ".gitattributes": 1519,
            "README.md": 57_817,
            "config.json": 4494,
            "generation_config.json": 245,
            "merges.txt": 1_671_839,
            "model.safetensors": 3_857_413_744,
            "preprocessor_config.json": 127,
            "speech_tokenizer/config.json": 2336,
            "speech_tokenizer/configuration.json": 76,
            "speech_tokenizer/model.safetensors": 682_293_092,
            "speech_tokenizer/preprocessor_config.json": 234,
            "tokenizer_config.json": 7344,
            "vocab.json": 2_776_833,
        },
        "known_hashes": {
            "model.safetensors": "38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c",
            "speech_tokenizer/model.safetensors": "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258",
        },
    },
}


class QwenRuntimeError(LocalAudioError):
    """Stable, path-redacted failure raised by the Qwen local runtime."""

    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(code, message, status_code)


@dataclass(frozen=True, slots=True)
class QwenResourceSnapshot:
    slot: ModelSlot
    model_id: str
    revision: str
    license: str
    resource_fingerprint: str
    model_path: Path
    manifest_path: Path
    total_bytes: int
    file_count: int


class QwenTtsRuntime:
    """Validate pinned local snapshots and run short-lived offline workers."""

    def __init__(
        self,
        *,
        paths: HostPathResolver,
        model_root: Path | None,
        interpreter_path: Path | None,
    ) -> None:
        self.paths = paths
        self.model_root = model_root
        self.interpreter_path = Path(interpreter_path).expanduser() if interpreter_path is not None else None

    def inspect(self) -> dict[str, Any]:
        """Return cheap readiness metadata without loading models or hashing weights."""
        interpreter_available = self._interpreter_available()
        models: dict[str, dict[str, Any]] = {}
        for slot in _MODEL_INFO:
            try:
                snapshot = self.resource_snapshot(slot, verify_files=False)
            except QwenRuntimeError:
                models[slot] = {
                    "model_id": _MODEL_INFO[slot]["model_id"],
                    "revision": _MODEL_INFO[slot]["revision"],
                    "available": False,
                    "resource_fingerprint": None,
                }
            else:
                models[slot] = {
                    "model_id": snapshot.model_id,
                    "revision": snapshot.revision,
                    "available": True,
                    "resource_fingerprint": snapshot.resource_fingerprint,
                }
        ready = interpreter_available and all(item["available"] for item in models.values())
        return {
            "status": "ready" if ready else "needs_resource",
            "runtime_available": interpreter_available,
            "interpreter_available": interpreter_available,
            "device": "cuda:0",
            "dtype": "bfloat16",
            "attn_implementation": "sdpa",
            "trust_remote_code": False,
            "downloads_enabled": False,
            "generation": {
                "initial_max_new_tokens": MAX_NEW_TOKENS,
                "oom_retry_max_new_tokens": OOM_RETRY_MAX_NEW_TOKENS,
                "oom_retry_policy": OOM_RETRY_POLICY,
                "max_output_seconds": MAX_REFERENCE_SECONDS,
                "max_output_bytes": MAX_REFERENCE_BYTES,
            },
            "models": models,
            "detail": (
                "固定 Qwen VoiceDesign/Base 本机资源与隔离解释器可用。"
                if ready
                else "固定 Qwen 本机资源或隔离解释器尚未就绪；不会自动下载或回退。"
            ),
        }

    def resource_snapshot(self, slot: str, *, verify_files: bool = True) -> QwenResourceSnapshot:
        """Validate the fixed model manifest; optionally hash every allowlisted file.

        ``verify_files=False`` performs bounded metadata checks for capabilities.
        It deliberately does not hash multi-gigabyte files.
        """
        if slot not in _MODEL_INFO:
            raise QwenRuntimeError("qwen_resource_unavailable", "指定的 Qwen 本机资源不存在。", 404)
        info = _MODEL_INFO[slot]
        try:
            model_path = self.paths.resolve(
                "model_root", f"qwen3-tts/{info['directory']}", must_exist=True
            )
            manifest_path = self.paths.resolve(
                "model_root", f"qwen3-tts/{info['directory']}/model.manifest.json", must_exist=True
            )
        except (PathPolicyError, FileNotFoundError, OSError, ValueError) as exc:
            raise QwenRuntimeError("qwen_resource_unavailable", "固定 Qwen 本机模型目录当前不可用。", 409) from exc
        if not model_path.is_dir() or _is_link_or_junction(model_path):
            raise QwenRuntimeError("qwen_resource_unavailable", "固定 Qwen 本机模型目录当前不可用。", 409)
        try:
            if manifest_path.stat().st_size > 256 * 1024 or not stat.S_ISREG(manifest_path.stat().st_mode):
                raise ValueError("manifest size or type")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型清单无效。", 409) from exc
        files, total_bytes = self._validate_manifest_payload(slot, payload)
        if payload.get("status") == "manifested_not_downloaded":
            raise QwenRuntimeError("qwen_resource_unavailable", "固定 Qwen 模型尚未完整下载。", 409)

        canonical_files: list[dict[str, Any]] = []
        for entry in files:
            relative = str(entry["relative_path"])
            try:
                path = self.paths.resolve(
                    "model_root",
                    f"qwen3-tts/{info['directory']}/{relative}",
                    must_exist=True,
                )
                metadata = path.stat()
            except (PathPolicyError, FileNotFoundError, OSError, ValueError) as exc:
                raise QwenRuntimeError("qwen_resource_unavailable", "固定 Qwen 模型文件缺失或不可读取。", 409) from exc
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != entry["size_bytes"]:
                raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型文件大小或类型与固定清单不符。", 409)
            if verify_files:
                digest = _hash_file(path)
                if digest != entry["sha256"]:
                    raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型文件 SHA256 与固定清单不符。", 409)
                known_hash = info["known_hashes"].get(relative)
                if known_hash is not None and digest != known_hash:
                    raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 官方模型文件 SHA256 不匹配。", 409)
            canonical_files.append(
                {
                    "relative_path": relative,
                    "size_bytes": entry["size_bytes"],
                    "sha256": entry["sha256"],
                }
            )

        self._validate_directory_contents(model_path, set(info["file_sizes"]))
        fingerprint_data = {
            "schema_version": 1,
            "model_id": info["model_id"],
            "revision": info["revision"],
            "license": "Apache-2.0",
            "expected_snapshot_bytes": info["expected_snapshot_bytes"],
            "files": canonical_files,
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return QwenResourceSnapshot(
            slot=slot,  # type: ignore[arg-type]
            model_id=info["model_id"],
            revision=info["revision"],
            license="Apache-2.0",
            resource_fingerprint=fingerprint,
            model_path=model_path,
            manifest_path=manifest_path,
            total_bytes=total_bytes,
            file_count=len(files),
        )

    async def design_reference(
        self,
        job: LocalAudioJob,
        *,
        text: str,
        style_prompt: str,
        seed: int,
    ) -> dict[str, Any]:
        content = _bounded_text(text, "reference text", MAX_TEXT_CHARACTERS)
        style = _bounded_text(style_prompt, "style prompt", MAX_STYLE_CHARACTERS)
        seed_value = _bounded_seed(seed)
        snapshot = await self._resource_for_worker("voice_design")
        return await self._run_worker(
            job,
            snapshot,
            {
                "operation": "voice_design",
                "text": content,
                "style_prompt": style,
                "seed": seed_value,
            },
        )

    async def synthesize_clone(
        self,
        job: LocalAudioJob,
        *,
        text: str,
        reference_wav: bytes,
        reference_sha256: str,
        reference_text: str,
        expected_model_id: str,
        expected_model_revision: str,
        expected_resource_fingerprint: str,
        max_new_tokens: int = MAX_NEW_TOKENS,
        seed: int = 0,
    ) -> dict[str, Any]:
        content = _bounded_text(text, "synthesis text", MAX_SYNTHESIS_CHARACTERS)
        ref_text = _bounded_text(reference_text, "reference text", MAX_TEXT_CHARACTERS)
        seed_value = _bounded_seed(seed)
        if type(max_new_tokens) is not int or not 1 <= max_new_tokens <= MAX_NEW_TOKENS:
            raise QwenRuntimeError("qwen_output_invalid", "Qwen 合成 token 上限无效。", 422)
        if not isinstance(reference_wav, bytes) or not 0 < len(reference_wav) <= MAX_REFERENCE_BYTES:
            raise QwenRuntimeError("qwen_reference_mismatch", "声线候选参考 WAV 无效。", 409)
        digest = hashlib.sha256(reference_wav).hexdigest()
        if not isinstance(reference_sha256, str) or not _SHA256.fullmatch(reference_sha256) or digest != reference_sha256:
            raise QwenRuntimeError("qwen_reference_mismatch", "声线候选参考 WAV SHA256 不匹配。", 409)
        _validate_pcm_wave(reference_wav)
        snapshot = await self._resource_for_worker("base")
        if (
            snapshot.model_id != expected_model_id
            or snapshot.revision != expected_model_revision
            or snapshot.resource_fingerprint != expected_resource_fingerprint
        ):
            raise QwenRuntimeError("qwen_resource_mismatch", "当前 Base 资源与候选绑定不一致。", 409)
        reference_path = job.workspace / "reference.wav"
        try:
            reference_path.write_bytes(reference_wav)
        except OSError as exc:
            raise QwenRuntimeError("qwen_worker_failed", "本机 Qwen 临时工作区当前不可写。", 503) from exc
        return await self._run_worker(
            job,
            snapshot,
            {
                "operation": "synthesize_clone",
                "text": content,
                "reference_path": str(reference_path),
                "reference_sha256": digest,
                "reference_text": ref_text,
                "max_new_tokens": max_new_tokens,
                "seed": seed_value,
            },
        )

    async def _resource_for_worker(self, slot: str) -> QwenResourceSnapshot:
        if not self._interpreter_available():
            raise QwenRuntimeError("qwen_runtime_unavailable", "隔离的 Qwen Python 解释器尚未配置。", 409)
        # Keep the multi-gigabyte integrity pass off the API event loop. This
        # entry check is authoritative even if a caller only did a cheap inspect.
        return await asyncio.to_thread(self.resource_snapshot, slot, verify_files=True)

    async def _run_worker(
        self,
        job: LocalAudioJob,
        snapshot: QwenResourceSnapshot,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        if job.operation != "tts":
            raise QwenRuntimeError("qwen_runtime_unavailable", "Qwen TTS 必须通过共享 TTS 工作通道启动。", 409)
        interpreter = self.interpreter_path
        if interpreter is None:
            raise QwenRuntimeError("qwen_runtime_unavailable", "隔离的 Qwen Python 解释器尚未配置。", 409)
        request_path = job.workspace / "qwen-request.json"
        request = {
            "schema_version": 1,
            "operation": operation["operation"],
            "model_id": snapshot.model_id,
            "model_revision": snapshot.revision,
            "resource_fingerprint": snapshot.resource_fingerprint,
            "model_path": str(snapshot.model_path),
            "manifest_path": str(snapshot.manifest_path),
            "workspace": str(job.workspace),
            "output_path": str(job.workspace / "qwen-output.wav"),
            "device": "cuda:0",
            "dtype": "bfloat16",
            "attn_implementation": "sdpa",
            "trust_remote_code": False,
            "local_files_only": True,
            **operation,
        }
        encoded = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_WORKER_JSON_BYTES:
            raise QwenRuntimeError("qwen_output_invalid", "Qwen worker 请求超过长度限制。", 422)
        try:
            request_path.write_bytes(encoded)
        except OSError as exc:
            raise QwenRuntimeError("qwen_worker_failed", "本机 Qwen 临时工作区当前不可写。", 503) from exc
        worker_path = Path(__file__).with_name("qwen_worker.py")
        try:
            result = await job.run(
                [interpreter, worker_path, "--request", request_path],
                timeout_seconds=QWEN_WORKER_TIMEOUT_SECONDS,
            )
        except LocalAudioError:
            raise
        if len(result.stdout) > MAX_WORKER_JSON_BYTES:
            raise QwenRuntimeError("qwen_worker_failed", "Qwen worker 返回内容超过长度限制。", 502)
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise QwenRuntimeError("qwen_worker_failed", "Qwen worker 返回格式无效。", 502) from exc
        if not isinstance(payload, dict):
            raise QwenRuntimeError("qwen_worker_failed", "Qwen worker 返回格式无效。", 502)
        if result.returncode != 0:
            code = payload.get("error")
            allowed = {
                "qwen_runtime_unavailable",
                "qwen_resource_unavailable",
                "qwen_resource_mismatch",
                "qwen_reference_mismatch",
                "qwen_worker_failed",
                "qwen_output_invalid",
                "qwen_cuda_oom",
                "qwen_oom_retry_exhausted",
            }
            if code not in allowed:
                code = "qwen_worker_failed"
            raise QwenRuntimeError(str(code), _error_message(str(code)), _error_status(str(code)))
        output_path = payload.get("wav_path")
        try:
            workspace = job.workspace.resolve(strict=True)
            output = Path(output_path).resolve(strict=True)
            output.relative_to(workspace)
            metadata = output.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > MAX_REFERENCE_BYTES
                or _is_link_or_junction(output)
            ):
                raise ValueError("invalid output")
            digest = _hash_file(output)
            with wave.open(str(output), "rb") as audio:
                sample_rate = audio.getframerate()
                duration = audio.getnframes() / sample_rate if sample_rate else math.inf
                if (
                    audio.getnchannels() != 1
                    or audio.getsampwidth() != 2
                    or not 8_000 <= sample_rate <= 48_000
                    or not math.isfinite(duration)
                    or duration <= 0
                    or duration > MAX_REFERENCE_SECONDS
                ):
                    raise ValueError("invalid PCM WAV")
        except (OSError, ValueError, TypeError, EOFError, wave.Error) as exc:
            raise QwenRuntimeError("qwen_output_invalid", "Qwen worker 输出未通过受控 PCM WAV 校验。", 502) from exc
        expected = {
            "model_id": snapshot.model_id,
            "model_revision": snapshot.revision,
            "resource_fingerprint": snapshot.resource_fingerprint,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise QwenRuntimeError("qwen_resource_mismatch", "Qwen worker 返回的模型元数据与固定资源不一致。", 409)
        if payload.get("sha256") != digest or payload.get("byte_length") != metadata.st_size:
            raise QwenRuntimeError("qwen_output_invalid", "Qwen worker WAV 与返回的校验信息不一致。", 502)
        reported_duration = payload.get("duration_seconds")
        if (
            isinstance(reported_duration, bool)
            or not isinstance(reported_duration, (int, float))
            or not math.isfinite(float(reported_duration))
            or abs(float(reported_duration) - duration) > 0.002
        ):
            raise QwenRuntimeError("qwen_output_invalid", "Qwen worker WAV 时长元数据无效。", 502)
        payload["wav_path"] = str(output)
        payload["sample_rate"] = sample_rate
        payload["duration_seconds"] = round(duration, 4)
        worker_settings = payload.get("settings")
        if not isinstance(worker_settings, dict):
            raise QwenRuntimeError("qwen_worker_failed", "Qwen worker 缺少生成设置元数据。", 502)
        if (
            set(worker_settings)
            - {
                "seed",
                "max_new_tokens",
                "initial_max_new_tokens",
                "attempt_count",
                "retry_policy",
                "language",
                "retry_after_cuda_oom",
            }
            or type(worker_settings.get("seed")) is not int
            or not 0 <= worker_settings["seed"] <= 4_294_967_295
            or type(worker_settings.get("max_new_tokens")) is not int
            or not 1 <= worker_settings["max_new_tokens"] <= MAX_NEW_TOKENS
            or type(worker_settings.get("initial_max_new_tokens")) is not int
            or not 1 <= worker_settings["initial_max_new_tokens"] <= MAX_NEW_TOKENS
            or type(worker_settings.get("attempt_count")) is not int
            or worker_settings["attempt_count"] not in {1, 2}
            or worker_settings.get("retry_policy") != OOM_RETRY_POLICY
            or worker_settings.get("language") != "Chinese"
            or type(worker_settings.get("retry_after_cuda_oom")) is not bool
            or worker_settings["attempt_count"] != (2 if worker_settings["retry_after_cuda_oom"] else 1)
            or worker_settings["max_new_tokens"]
            != (
                min(OOM_RETRY_MAX_NEW_TOKENS, worker_settings["initial_max_new_tokens"] // 2)
                if worker_settings["retry_after_cuda_oom"]
                else worker_settings["initial_max_new_tokens"]
            )
        ):
            raise QwenRuntimeError("qwen_worker_failed", "Qwen worker 生成设置元数据无效。", 502)
        payload["settings"] = {
            "device": "cuda:0",
            "dtype": "bfloat16",
            "attn_implementation": "sdpa",
            "trust_remote_code": False,
            "local_files_only": True,
            **worker_settings,
        }
        return payload

    def _interpreter_available(self) -> bool:
        path = self.interpreter_path
        if path is None or not path.is_absolute():
            return False
        if _is_link_or_junction(path):
            return False
        current = path
        while current != current.parent:
            if _is_link_or_junction(current):
                return False
            current = current.parent
        try:
            return path.is_file() and stat.S_ISREG(path.stat().st_mode)
        except OSError:
            return False

    def _validate_manifest_payload(self, slot: str, payload: Any) -> tuple[list[dict[str, Any]], int]:
        info = _MODEL_INFO[slot]
        if not isinstance(payload, dict):
            raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型清单格式无效。", 409)
        if (
            payload.get("schema_version") != 1
            or payload.get("model_id") != info["model_id"]
            or payload.get("revision") != info["revision"]
            or payload.get("license") != "Apache-2.0"
            or payload.get("expected_snapshot_bytes") != info["expected_snapshot_bytes"]
        ):
            raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型 ID、revision 或许可证与固定资源不一致。", 409)
        raw_files = payload.get("files")
        if not isinstance(raw_files, list) or len(raw_files) != len(info["file_sizes"]):
            raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型清单文件列表不符合固定 allowlist。", 409)
        entries: dict[str, dict[str, Any]] = {}
        for raw in raw_files:
            if not isinstance(raw, dict):
                raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型清单包含无效文件项。", 409)
            relative = raw.get("relative_path")
            if not isinstance(relative, str) or relative not in info["file_sizes"] or relative in entries:
                raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型清单包含非 allowlist 文件。", 409)
            if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts or "\\" in relative:
                raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型清单包含不安全路径。", 409)
            size = raw.get("size_bytes")
            if type(size) is not int or size != info["file_sizes"][relative]:
                raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 官方文件大小与固定资源不匹配。", 409)
            digest = raw.get("sha256")
            lfs_digest = raw.get("hub_lfs_sha256")
            if digest is not None and (not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
                raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型清单包含无效 SHA256。", 409)
            if lfs_digest is not None and (not isinstance(lfs_digest, str) or not _SHA256.fullmatch(lfs_digest)):
                raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型清单包含无效 LFS SHA256。", 409)
            known_hash = info["known_hashes"].get(relative)
            if known_hash is not None and lfs_digest is not None and lfs_digest != known_hash:
                raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型清单官方 LFS SHA256 不匹配。", 409)
            effective_hash = digest or lfs_digest
            if not isinstance(effective_hash, str):
                raise QwenRuntimeError("qwen_resource_unavailable", "Qwen 模型清单尚未记录完整文件哈希。", 409)
            if known_hash is not None and effective_hash != known_hash:
                raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 官方模型文件 SHA256 不匹配。", 409)
            entries[relative] = {
                "relative_path": relative,
                "size_bytes": size,
                "sha256": effective_hash,
            }
        if set(entries) != set(info["file_sizes"]):
            raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型清单文件列表不完整。", 409)
        ordered = [entries[key] for key in sorted(entries)]
        total = sum(item["size_bytes"] for item in ordered)
        if total != info["expected_snapshot_bytes"]:
            raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型资源总大小与固定清单不匹配。", 409)
        return ordered, total

    @staticmethod
    def _validate_directory_contents(model_path: Path, expected_files: set[str]) -> None:
        actual_files: set[str] = set()
        allowed_directories = {"speech_tokenizer", ".cache", ".cache/huggingface", _HF_DOWNLOAD_DIR}
        actual_directories: set[str] = set()
        required_files = expected_files | {"model.manifest.json"}
        for current_root, directories, filenames in os.walk(model_path, followlinks=False):
            current = Path(current_root)
            for name in list(directories):
                child = current / name
                if _is_link_or_junction(child):
                    raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型目录包含链接。", 409)
                relative_dir = child.relative_to(model_path).as_posix()
                if (
                    relative_dir not in allowed_directories
                    and not relative_dir.startswith(f"{_HF_DOWNLOAD_DIR}/")
                ):
                    raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型目录存在未登记目录。", 409)
                try:
                    if not stat.S_ISDIR(child.lstat().st_mode):
                        raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型目录包含非普通目录。", 409)
                except OSError as exc:
                    raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型目录类型无效。", 409) from exc
                actual_directories.add(relative_dir)
            for name in filenames:
                child = current / name
                if _is_link_or_junction(child):
                    raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型目录包含链接。", 409)
                relative_file = child.relative_to(model_path).as_posix()
                try:
                    metadata = child.lstat()
                except OSError as exc:
                    raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型文件类型无效。", 409) from exc
                if not stat.S_ISREG(metadata.st_mode):
                    raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型目录包含非普通文件。", 409)
                if relative_file in required_files:
                    actual_files.add(relative_file)
                elif relative_file == _HF_GITIGNORE or _is_hf_download_sidecar(relative_file):
                    continue
                else:
                    raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型目录存在未登记文件。", 409)
        if actual_files != required_files or "speech_tokenizer" not in actual_directories:
            raise QwenRuntimeError("qwen_resource_mismatch", "Qwen 模型目录存在未登记或缺失的文件。", 409)


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise QwenRuntimeError("qwen_output_invalid", f"Qwen {name} 无效。", 422)
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise QwenRuntimeError("qwen_output_invalid", f"Qwen {name} 长度无效。", 422)
    return normalized


def _bounded_seed(seed: Any) -> int:
    if type(seed) is not int or not 0 <= seed <= 4_294_967_295:
        raise QwenRuntimeError("qwen_output_invalid", "Qwen seed 必须为 uint32 整数。", 422)
    return seed


def _is_hf_download_sidecar(relative_path: str) -> bool:
    prefix = f"{_HF_DOWNLOAD_DIR}/"
    if not relative_path.startswith(prefix):
        return False
    name = PurePosixPath(relative_path).name
    return any(len(name) > len(suffix) and name.endswith(suffix) for suffix in _HF_SIDECAR_SUFFIXES)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    if os.name == "nt":
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
        except OSError:
            return False
    return False


def _validate_pcm_wave(audio_bytes: bytes) -> float:
    try:
        import io

        with wave.open(io.BytesIO(audio_bytes), "rb") as audio:
            sample_rate = audio.getframerate()
            duration = audio.getnframes() / sample_rate if sample_rate else math.inf
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getcomptype() != "NONE"
                or not 8_000 <= sample_rate <= 48_000
                or not math.isfinite(duration)
                or duration <= 0
                or duration > MAX_REFERENCE_SECONDS
            ):
                raise ValueError("invalid PCM WAV")
            return duration
    except (wave.Error, EOFError, OSError, TypeError, ValueError) as exc:
        raise QwenRuntimeError("qwen_reference_mismatch", "声线候选必须是有效的单声道 PCM WAV。", 409) from exc


def _error_message(code: str) -> str:
    return {
        "qwen_runtime_unavailable": "本机 Qwen runtime 当前不可用。",
        "qwen_resource_unavailable": "固定 Qwen 本机资源尚未就绪。",
        "qwen_resource_mismatch": "固定 Qwen 本机资源未通过完整性校验。",
        "qwen_reference_mismatch": "声线候选参考音频校验失败。",
        "qwen_worker_failed": "本机 Qwen worker 处理失败。",
        "qwen_output_invalid": "本机 Qwen worker 输出无效。",
        "qwen_cuda_oom": "本机 Qwen GPU 显存不足，当前任务已停止。",
        "qwen_oom_retry_exhausted": "本机 Qwen GPU 显存不足，单次降额重试仍未成功。",
    }.get(code, "本机 Qwen worker 处理失败。")


def _error_status(code: str) -> int:
    if code in {"qwen_runtime_unavailable", "qwen_resource_unavailable", "qwen_resource_mismatch", "qwen_reference_mismatch"}:
        return 409
    if code == "qwen_cuda_oom" or code == "qwen_oom_retry_exhausted":
        return 503
    if code == "qwen_output_invalid":
        return 502
    return 502
