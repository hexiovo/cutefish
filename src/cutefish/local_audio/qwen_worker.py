"""Short-lived, offline worker for one pinned Qwen3-TTS checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
import wave
from pathlib import Path, PurePosixPath
from typing import Any


MAX_REQUEST_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_SECONDS = 45
MAX_NEW_TOKENS = 512
OOM_RETRY_MAX_NEW_TOKENS = 256
OOM_RETRY_POLICY = "single_cuda_oom_retry_half_tokens"
MAX_SYNTHESIS_CHARACTERS = 2_000
HF_DOWNLOAD_DIR = ".cache/huggingface/download"
HF_GITIGNORE = ".cache/huggingface/.gitignore"
HF_SIDECAR_SUFFIXES = (".metadata", ".lock", ".incomplete")
MODEL_INFO = {
    "voice_design": {
        "directory": "voice-design",
        "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        "revision": "5ecdb67327fd37bb2e042aab12ff7391903235d3",
        "total": 4_520_163_832,
        "sizes": {
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
        "total": 4_544_229_700,
        "sizes": {
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


class WorkerFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_link(path: Path) -> bool:
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


def _is_hf_download_sidecar(relative_path: str) -> bool:
    prefix = f"{HF_DOWNLOAD_DIR}/"
    if not relative_path.startswith(prefix):
        return False
    name = PurePosixPath(relative_path).name
    return any(len(name) > len(suffix) and name.endswith(suffix) for suffix in HF_SIDECAR_SUFFIXES)


def _resolve(path: Any, *, workspace: Path | None = None, exists: bool = True) -> Path:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise WorkerFailure("qwen_resource_mismatch")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise WorkerFailure("qwen_resource_mismatch")
    current = candidate
    while current != current.parent:
        if _is_link(current):
            raise WorkerFailure("qwen_resource_mismatch")
        current = current.parent
    try:
        resolved = candidate.resolve(strict=exists)
    except (OSError, ValueError) as exc:
        raise WorkerFailure("qwen_resource_unavailable") from exc
    if workspace is not None:
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise WorkerFailure("qwen_reference_mismatch") from exc
    return resolved


def _configure_offline_workspace(workspace: Path) -> None:
    cache_root = workspace / "cache"
    cache_paths = {
        "HF_HOME": cache_root,
        "HF_HUB_CACHE": cache_root / "hub",
        "HUGGINGFACE_HUB_CACHE": cache_root / "hub",
        "TRANSFORMERS_CACHE": cache_root / "transformers",
        "HF_DATASETS_CACHE": cache_root / "datasets",
        "TORCH_HOME": cache_root / "torch",
        "XDG_CACHE_HOME": cache_root,
    }
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
            "TMP": str(workspace),
            "TEMP": str(workspace),
            "TMPDIR": str(workspace),
            **{key: str(path) for key, path in cache_paths.items()},
        }
    )
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN"):
        os.environ.pop(name, None)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_resource(request: dict[str, Any], workspace: Path) -> tuple[str, str, str, Path]:
    slot = request.get("operation")
    slot = "voice_design" if slot == "voice_design" else "base" if slot == "synthesize_clone" else None
    if slot is None:
        raise WorkerFailure("qwen_runtime_unavailable")
    info = MODEL_INFO[slot]
    model_path = _resolve(request.get("model_path"), exists=True)
    manifest_path = _resolve(request.get("manifest_path"), exists=True)
    if not model_path.is_dir() or manifest_path != model_path / "model.manifest.json":
        raise WorkerFailure("qwen_resource_mismatch")
    try:
        if manifest_path.stat().st_size > 256 * 1024:
            raise WorkerFailure("qwen_resource_mismatch")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except WorkerFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure("qwen_resource_mismatch") from exc
    if not isinstance(manifest, dict) or (
        manifest.get("schema_version") != 1
        or manifest.get("model_id") != info["model_id"]
        or manifest.get("revision") != info["revision"]
        or manifest.get("license") != "Apache-2.0"
        or manifest.get("expected_snapshot_bytes") != info["total"]
        or manifest.get("status") == "manifested_not_downloaded"
    ):
        raise WorkerFailure("qwen_resource_mismatch")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != len(info["sizes"]):
        raise WorkerFailure("qwen_resource_mismatch")
    entries: dict[str, dict[str, Any]] = {}
    for item in raw_files:
        if not isinstance(item, dict):
            raise WorkerFailure("qwen_resource_mismatch")
        relative = item.get("relative_path")
        if not isinstance(relative, str) or relative not in info["sizes"] or relative in entries:
            raise WorkerFailure("qwen_resource_mismatch")
        posix_relative = PurePosixPath(relative)
        if posix_relative.is_absolute() or ".." in posix_relative.parts or "\\" in relative:
            raise WorkerFailure("qwen_resource_mismatch")
        size = item.get("size_bytes")
        digest = item.get("sha256") or item.get("hub_lfs_sha256")
        known_digest = info["known_hashes"].get(relative)
        if type(size) is not int or size != info["sizes"][relative]:
            raise WorkerFailure("qwen_resource_mismatch")
        if not isinstance(digest, str) or len(digest) != 64:
            raise WorkerFailure("qwen_resource_unavailable")
        if known_digest is not None and digest != known_digest:
            raise WorkerFailure("qwen_resource_mismatch")
        entries[relative] = {"relative_path": relative, "size_bytes": size, "sha256": digest}
    if set(entries) != set(info["sizes"]):
        raise WorkerFailure("qwen_resource_mismatch")
    ordered = [entries[key] for key in sorted(entries)]
    if sum(item["size_bytes"] for item in ordered) != info["total"]:
        raise WorkerFailure("qwen_resource_mismatch")

    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    allowed_directories = {"speech_tokenizer", ".cache", ".cache/huggingface", HF_DOWNLOAD_DIR}
    required_files = set(info["sizes"]) | {"model.manifest.json"}
    for current_root, directories, filenames in os.walk(model_path, followlinks=False):
        current = Path(current_root)
        for name in list(directories):
            directory = current / name
            if _is_link(directory):
                raise WorkerFailure("qwen_resource_mismatch")
            relative_directory = directory.relative_to(model_path).as_posix()
            if (
                relative_directory not in allowed_directories
                and not relative_directory.startswith(f"{HF_DOWNLOAD_DIR}/")
            ):
                raise WorkerFailure("qwen_resource_mismatch")
            try:
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    raise WorkerFailure("qwen_resource_mismatch")
            except OSError as exc:
                raise WorkerFailure("qwen_resource_mismatch") from exc
            actual_directories.add(relative_directory)
        for name in filenames:
            path = current / name
            if _is_link(path):
                raise WorkerFailure("qwen_resource_mismatch")
            relative_file = path.relative_to(model_path).as_posix()
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise WorkerFailure("qwen_resource_mismatch")
            except OSError as exc:
                raise WorkerFailure("qwen_resource_mismatch") from exc
            if relative_file in required_files:
                actual_files.add(relative_file)
            elif relative_file == HF_GITIGNORE or _is_hf_download_sidecar(relative_file):
                continue
            else:
                raise WorkerFailure("qwen_resource_mismatch")
    if actual_files != required_files or "speech_tokenizer" not in actual_directories:
        raise WorkerFailure("qwen_resource_mismatch")

    canonical = {
        "schema_version": 1,
        "model_id": info["model_id"],
        "revision": info["revision"],
        "license": "Apache-2.0",
        "expected_snapshot_bytes": info["total"],
        "files": ordered,
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        request.get("model_id") != info["model_id"]
        or request.get("model_revision") != info["revision"]
        or request.get("resource_fingerprint") != fingerprint
    ):
        raise WorkerFailure("qwen_resource_mismatch")
    for item in ordered:
        file_path = model_path.joinpath(*PurePosixPath(item["relative_path"]).parts)
        try:
            metadata = file_path.stat()
            digest = _hash_file(file_path)
        except OSError as exc:
            raise WorkerFailure("qwen_resource_unavailable") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != item["size_bytes"]:
            raise WorkerFailure("qwen_resource_mismatch")
        if digest != item["sha256"]:
            raise WorkerFailure("qwen_resource_mismatch")
    return info["model_id"], info["revision"], fingerprint, model_path


def _validate_reference(path: Path, expected_sha256: str, reference_text: str) -> None:
    if not isinstance(reference_text, str) or not reference_text.strip() or len(reference_text) > 160:
        raise WorkerFailure("qwen_reference_mismatch")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise WorkerFailure("qwen_reference_mismatch")
    try:
        if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > MAX_OUTPUT_BYTES:
            raise WorkerFailure("qwen_reference_mismatch")
        if _hash_file(path) != expected_sha256:
            raise WorkerFailure("qwen_reference_mismatch")
        with wave.open(str(path), "rb") as audio:
            sample_rate = audio.getframerate()
            duration = audio.getnframes() / sample_rate if sample_rate else math.inf
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getcomptype() != "NONE"
                or not 8_000 <= sample_rate <= 48_000
                or not math.isfinite(duration)
                or duration <= 0
                or duration > MAX_OUTPUT_SECONDS
            ):
                raise WorkerFailure("qwen_reference_mismatch")
    except WorkerFailure:
        raise
    except (OSError, wave.Error, EOFError) as exc:
        raise WorkerFailure("qwen_reference_mismatch") from exc


def _load_model(model_path: Path):
    torch = None
    try:
        import torch
        from qwen_tts import Qwen3TTSModel

        model = Qwen3TTSModel.from_pretrained(
            str(model_path),
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
            low_cpu_mem_usage=True,
            use_safetensors=True,
            local_files_only=True,
            trust_remote_code=False,
        )
        return torch, model
    except Exception as exc:
        if torch is not None and _is_cuda_oom(exc, torch):
            # A smaller generation cap cannot make checkpoint loading fit, so
            # do not spend the one inference retry on a second doomed load.
            raise WorkerFailure("qwen_cuda_oom") from exc
        raise WorkerFailure("qwen_runtime_unavailable") from exc


def _to_mono_float32(torch, waveform: Any):
    try:
        import numpy as np

        if isinstance(waveform, torch.Tensor):
            values = waveform.detach().to(device="cpu", dtype=torch.float32).numpy()
        else:
            values = np.asarray(waveform, dtype=np.float32)
        values = np.asarray(values, dtype=np.float32).squeeze()
        if values.ndim != 1 or values.size <= 0 or not np.isfinite(values).all():
            raise WorkerFailure("qwen_output_invalid")
        return values
    except WorkerFailure:
        raise
    except Exception as exc:
        raise WorkerFailure("qwen_output_invalid") from exc


def _is_cuda_oom(exc: BaseException, torch) -> bool:
    oom_type = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
    if isinstance(oom_type, type) and isinstance(exc, oom_type):
        return True
    return isinstance(exc, RuntimeError) and "out of memory" in str(exc).casefold()


def _write_wave(output_path: Path, waveform: Any, sample_rate: Any, torch) -> tuple[str, int, float, int]:
    try:
        import numpy as np

        rate = int(sample_rate)
        if not 8_000 <= rate <= 48_000:
            raise WorkerFailure("qwen_output_invalid")
        samples = _to_mono_float32(torch, waveform)
        duration = samples.size / rate
        if not math.isfinite(duration) or duration <= 0 or duration > MAX_OUTPUT_SECONDS:
            raise WorkerFailure("qwen_output_invalid")
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2", copy=False).tobytes()
        # 44-byte header plus bounded 16-bit mono PCM data.
        if not pcm or len(pcm) + 44 > MAX_OUTPUT_BYTES:
            raise WorkerFailure("qwen_output_invalid")
        with wave.open(str(output_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(pcm)
        byte_length = output_path.stat().st_size
        digest = _hash_file(output_path)
        return digest, byte_length, duration, rate
    except WorkerFailure:
        raise
    except Exception as exc:
        raise WorkerFailure("qwen_output_invalid") from exc


def _generate(request: dict[str, Any], model_path: Path, workspace: Path, output_path: Path, model_id: str, revision: str, fingerprint: str) -> dict[str, Any]:
    operation = request["operation"]
    text = request.get("text")
    seed = request.get("seed")
    text_limit = 160 if operation == "voice_design" else MAX_SYNTHESIS_CHARACTERS
    if not isinstance(text, str) or not text.strip() or len(text) > text_limit or "\x00" in text:
        raise WorkerFailure("qwen_output_invalid")
    if type(seed) is not int or not 0 <= seed <= 4_294_967_295:
        raise WorkerFailure("qwen_output_invalid")
    if not output_path.parent == workspace or output_path.exists():
        raise WorkerFailure("qwen_output_invalid")

    torch, model = _load_model(model_path)
    try:
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        if operation == "voice_design":
            style_prompt = request.get("style_prompt")
            if not isinstance(style_prompt, str) or not style_prompt.strip() or len(style_prompt) > 500:
                raise WorkerFailure("qwen_output_invalid")
            max_new_tokens = MAX_NEW_TOKENS
            retried_after_oom = False
            try:
                with torch.inference_mode():
                    waveforms, sample_rate = model.generate_voice_design(
                        text=text,
                        language="Chinese",
                        instruct=style_prompt,
                        max_new_tokens=max_new_tokens,
                    )
            except Exception as exc:
                if not _is_cuda_oom(exc, torch):
                    raise
                torch.cuda.empty_cache()
                max_new_tokens = OOM_RETRY_MAX_NEW_TOKENS
                retried_after_oom = True
                try:
                    with torch.inference_mode():
                        waveforms, sample_rate = model.generate_voice_design(
                            text=text,
                            language="Chinese",
                            instruct=style_prompt,
                            max_new_tokens=max_new_tokens,
                        )
                except Exception as retry_exc:
                    if _is_cuda_oom(retry_exc, torch):
                        raise WorkerFailure("qwen_oom_retry_exhausted") from retry_exc
                    raise
            settings = {
                "seed": seed,
                "max_new_tokens": max_new_tokens,
                "initial_max_new_tokens": MAX_NEW_TOKENS,
                "attempt_count": 2 if retried_after_oom else 1,
                "retry_policy": OOM_RETRY_POLICY,
                "language": "Chinese",
                "retry_after_cuda_oom": retried_after_oom,
            }
        elif operation == "synthesize_clone":
            max_new_tokens = request.get("max_new_tokens")
            if type(max_new_tokens) is not int or not 1 <= max_new_tokens <= MAX_NEW_TOKENS:
                raise WorkerFailure("qwen_output_invalid")
            reference_path = _resolve(request.get("reference_path"), workspace=workspace)
            _validate_reference(reference_path, request.get("reference_sha256"), request.get("reference_text"))
            retried_after_oom = False
            while True:
                try:
                    with torch.inference_mode():
                        # Rebuild this only in-memory prompt for each attempt.
                        # No pickle or prompt cache is written to disk.
                        voice_clone_prompt = model.create_voice_clone_prompt(
                            ref_audio=str(reference_path),
                            ref_text=request["reference_text"],
                            x_vector_only_mode=False,
                        )
                        waveforms, sample_rate = model.generate_voice_clone(
                            text=text,
                            language="Chinese",
                            voice_clone_prompt=voice_clone_prompt,
                            max_new_tokens=max_new_tokens,
                        )
                    break
                except Exception as exc:
                    is_oom = _is_cuda_oom(exc, torch)
                    if retried_after_oom and is_oom:
                        raise WorkerFailure("qwen_oom_retry_exhausted") from exc
                    if not is_oom:
                        raise
                    if max_new_tokens <= 1:
                        raise WorkerFailure("qwen_cuda_oom") from exc
                    torch.cuda.empty_cache()
                    max_new_tokens = max(1, max_new_tokens // 2)
                    retried_after_oom = True
            settings = {
                "seed": seed,
                "max_new_tokens": max_new_tokens,
                "initial_max_new_tokens": request["max_new_tokens"],
                "attempt_count": 2 if retried_after_oom else 1,
                "retry_policy": OOM_RETRY_POLICY,
                "language": "Chinese",
                "retry_after_cuda_oom": retried_after_oom,
            }
        else:
            raise WorkerFailure("qwen_runtime_unavailable")
        if not isinstance(waveforms, (list, tuple)) or len(waveforms) != 1:
            raise WorkerFailure("qwen_output_invalid")
        digest, byte_length, duration, sample_rate = _write_wave(output_path, waveforms[0], sample_rate, torch)
        return {
            "wav_path": str(output_path),
            "byte_length": byte_length,
            "sha256": digest,
            "duration_seconds": round(duration, 4),
            "sample_rate": sample_rate,
            "model_id": model_id,
            "model_revision": revision,
            "resource_fingerprint": fingerprint,
            "settings": settings,
        }
    except WorkerFailure:
        raise
    except Exception as exc:
        # Never return exception text or host paths from model/library errors.
        raise WorkerFailure("qwen_worker_failed") from exc
    finally:
        del model
        if "torch" in locals() and torch.cuda.is_available():
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass


def _emit_error(code: str) -> int:
    print(json.dumps({"error": code}, ensure_ascii=False, allow_nan=False))
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    try:
        request_path = _resolve(args.request, exists=True)
        if not request_path.is_file() or request_path.stat().st_size > MAX_REQUEST_BYTES:
            raise WorkerFailure("qwen_output_invalid")
        request_bytes = request_path.read_bytes()
        request = json.loads(request_bytes.decode("utf-8"))
        if not isinstance(request, dict) or request.get("schema_version") != 1:
            raise WorkerFailure("qwen_output_invalid")
        workspace = _resolve(request.get("workspace"), exists=True)
        if not workspace.is_dir() or request_path.parent != workspace:
            raise WorkerFailure("qwen_output_invalid")
        _configure_offline_workspace(workspace)
        output_path = _resolve(request.get("output_path"), workspace=workspace, exists=False)
        model_id, revision, fingerprint, model_path = _validate_resource(request, workspace)
        # Redirect model/library progress to stderr; stdout remains a bounded
        # single-JSON protocol consumed by the API process.
        from contextlib import redirect_stdout

        with redirect_stdout(sys.stderr):
            result = _generate(request, model_path, workspace, output_path, model_id, revision, fingerprint)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return 0
    except WorkerFailure as exc:
        return _emit_error(exc.code)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return _emit_error("qwen_output_invalid")
    except Exception:
        return _emit_error("qwen_worker_failed")


if __name__ == "__main__":
    raise SystemExit(main())
