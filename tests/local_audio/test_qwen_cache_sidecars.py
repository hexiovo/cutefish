from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cutefish.core.paths import HostPathResolver
from cutefish.local_audio import QwenRuntimeError, QwenTtsRuntime
from cutefish.local_audio import qwen_worker
from cutefish.local_audio import qwen_runtime as runtime_module


def _fixture_runtime(tmp_path: Path, monkeypatch):
    contents = {
        "config.json": b"{}",
        "model.safetensors": b"weight",
        "speech_tokenizer/config.json": b"{}",
    }
    hashes = {relative: hashlib.sha256(data).hexdigest() for relative, data in contents.items()}
    total = sum(map(len, contents.values()))
    runtime_info = {
        "directory": "base",
        "model_id": "test/qwen-base",
        "revision": "test-revision",
        "expected_snapshot_bytes": total,
        "file_sizes": {relative: len(data) for relative, data in contents.items()},
        "known_hashes": {"model.safetensors": hashes["model.safetensors"]},
    }
    worker_info = {
        "directory": "base",
        "model_id": runtime_info["model_id"],
        "revision": runtime_info["revision"],
        "total": total,
        "sizes": runtime_info["file_sizes"],
        "known_hashes": runtime_info["known_hashes"],
    }
    monkeypatch.setitem(runtime_module._MODEL_INFO, "base", runtime_info)
    monkeypatch.setitem(qwen_worker.MODEL_INFO, "base", worker_info)

    model_root = tmp_path / "models"
    model_path = model_root / "qwen3-tts" / "base"
    model_path.mkdir(parents=True)
    manifest_files = []
    for relative, data in contents.items():
        output = model_path / Path(relative)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        manifest_files.append(
            {
                "relative_path": relative,
                "size_bytes": len(data),
                "sha256": hashes[relative],
            }
        )
    manifest = {
        "schema_version": 1,
        "model_id": runtime_info["model_id"],
        "revision": runtime_info["revision"],
        "license": "Apache-2.0",
        "expected_snapshot_bytes": total,
        "status": "downloaded",
        "files": manifest_files,
    }
    (model_path / "model.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    runtime = QwenTtsRuntime(
        paths=HostPathResolver({"model_root": model_root}),
        model_root=model_root,
        interpreter_path=None,
    )
    return runtime, model_path


def _worker_request(model_path: Path, snapshot) -> dict[str, str]:
    return {
        "operation": "synthesize_clone",
        "model_path": str(model_path),
        "manifest_path": str(model_path / "model.manifest.json"),
        "model_id": snapshot.model_id,
        "model_revision": snapshot.revision,
        "resource_fingerprint": snapshot.resource_fingerprint,
    }


def test_hf_sidecars_are_ignored_by_runtime_and_worker_fingerprint(tmp_path, monkeypatch):
    runtime, model_path = _fixture_runtime(tmp_path, monkeypatch)
    initial = runtime.resource_snapshot("base", verify_files=True)

    download_dir = model_path / ".cache" / "huggingface" / "download" / "repo"
    download_dir.mkdir(parents=True)
    (model_path / ".cache" / "huggingface" / ".gitignore").write_text("# cache\n", encoding="utf-8")
    for suffix in (".metadata", ".lock", ".incomplete"):
        (download_dir / f"partial{suffix}").write_bytes(b"inert download state")

    snapshot = runtime.resource_snapshot("base", verify_files=True)
    assert snapshot.resource_fingerprint == initial.resource_fingerprint
    worker_result = qwen_worker._validate_resource(
        _worker_request(model_path, snapshot), tmp_path
    )
    assert worker_result[2] == initial.resource_fingerprint


def test_incomplete_sidecar_does_not_satisfy_a_missing_required_weight(tmp_path, monkeypatch):
    runtime, model_path = _fixture_runtime(tmp_path, monkeypatch)
    required_weight = model_path / "model.safetensors"
    expected_size = required_weight.stat().st_size
    required_weight.unlink()
    download_dir = model_path / ".cache" / "huggingface" / "download"
    download_dir.mkdir(parents=True)
    (download_dir / "model.safetensors.incomplete").write_bytes(b"x" * expected_size)

    with pytest.raises(QwenRuntimeError) as exc_info:
        runtime.resource_snapshot("base", verify_files=True)
    assert exc_info.value.code == "qwen_resource_unavailable"
    assert not required_weight.exists()


@pytest.mark.parametrize(
    "relative_path",
    [
        ".cache/huggingface/download/repo/model.py",
        ".cache/other.txt",
        "unregistered.bin",
    ],
)
def test_unknown_cache_or_model_files_are_rejected(tmp_path, monkeypatch, relative_path):
    runtime, model_path = _fixture_runtime(tmp_path, monkeypatch)
    snapshot = runtime.resource_snapshot("base", verify_files=True)
    extra = model_path / Path(relative_path)
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"unexpected")

    with pytest.raises(QwenRuntimeError) as exc_info:
        runtime.resource_snapshot("base", verify_files=False)
    assert exc_info.value.code == "qwen_resource_mismatch"
    with pytest.raises(qwen_worker.WorkerFailure):
        qwen_worker._validate_resource(_worker_request(model_path, snapshot), tmp_path)


def test_cache_links_are_rejected(tmp_path, monkeypatch):
    runtime, model_path = _fixture_runtime(tmp_path, monkeypatch)
    snapshot = runtime.resource_snapshot("base", verify_files=True)
    download_dir = model_path / ".cache" / "huggingface" / "download"
    download_dir.mkdir(parents=True)
    external_file = tmp_path / "external.lock"
    external_file.write_bytes(b"outside")
    link = download_dir / "state.lock"
    try:
        link.symlink_to(external_file)
    except OSError:
        # Some Windows hosts deny symlink creation; mark the same path as a
        # reparse point so both validators' fail-closed branch remains covered.
        link.write_bytes(b"link marker")
        runtime_is_link = runtime_module._is_link_or_junction
        worker_is_link = qwen_worker._is_link
        monkeypatch.setattr(
            runtime_module,
            "_is_link_or_junction",
            lambda path: path == link or runtime_is_link(path),
        )
        monkeypatch.setattr(
            qwen_worker,
            "_is_link",
            lambda path: path == link or worker_is_link(path),
        )

    with pytest.raises(QwenRuntimeError) as exc_info:
        runtime.resource_snapshot("base", verify_files=False)
    assert exc_info.value.code == "qwen_resource_mismatch"
    with pytest.raises(qwen_worker.WorkerFailure):
        qwen_worker._validate_resource(_worker_request(model_path, snapshot), tmp_path)


def test_corrupted_required_weight_hash_is_rejected_by_runtime_and_worker(tmp_path, monkeypatch):
    runtime, model_path = _fixture_runtime(tmp_path, monkeypatch)
    snapshot = runtime.resource_snapshot("base", verify_files=True)
    weight = model_path / "model.safetensors"
    weight.write_bytes(b"broken")

    with pytest.raises(QwenRuntimeError) as exc_info:
        runtime.resource_snapshot("base", verify_files=True)
    assert exc_info.value.code == "qwen_resource_mismatch"
    with pytest.raises(qwen_worker.WorkerFailure):
        qwen_worker._validate_resource(_worker_request(model_path, snapshot), tmp_path)
