from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from cutefish.local_audio import qwen_worker


def _cp936_stdout(monkeypatch) -> tuple[io.TextIOWrapper, io.BytesIO]:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp936")
    monkeypatch.setattr(qwen_worker.sys, "stdout", stream)
    return stream, raw


def test_main_emits_success_json_as_utf8_with_cp936_stdout(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "语音工作区"
    workspace.mkdir()
    request_path = workspace / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace": str(workspace),
                "output_path": str(workspace / "output.wav"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    model_path = tmp_path / "模型"
    model_path.mkdir()
    result = {
        "wav_path": str(workspace / "生成的语音.wav"),
        "message": "声线生成成功",
    }
    monkeypatch.setattr(qwen_worker.sys, "argv", ["qwen_worker.py", "--request", str(request_path)])
    monkeypatch.setattr(qwen_worker, "_configure_offline_workspace", lambda _workspace: None)
    monkeypatch.setattr(
        qwen_worker,
        "_validate_resource",
        lambda _request, _workspace: ("model-id", "revision", "fingerprint", model_path),
    )
    monkeypatch.setattr(qwen_worker, "_generate", lambda *_args: result)
    stdout, raw = _cp936_stdout(monkeypatch)

    exit_code = qwen_worker.main()
    stdout.flush()
    stdout.detach()

    assert exit_code == 0
    assert json.loads(raw.getvalue().decode("utf-8")) == result


def test_json_emission_keeps_utf8_for_non_ascii_error_payload_and_error_code(monkeypatch) -> None:
    stdout, raw = _cp936_stdout(monkeypatch)
    payload: dict[str, Any] = {"error": "qwen_worker_failed", "message": "生成失败：模型路径无效"}

    qwen_worker._emit_json(payload)
    error_exit = qwen_worker._emit_error("qwen_worker_failed")
    stdout.flush()
    stdout.detach()

    lines = raw.getvalue().splitlines()
    assert error_exit == 2
    assert [json.loads(line.decode("utf-8")) for line in lines] == [
        payload,
        {"error": "qwen_worker_failed"},
    ]
