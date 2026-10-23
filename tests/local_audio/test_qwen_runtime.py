from __future__ import annotations

import sys
from pathlib import Path

from cutefish.core.paths import HostPathResolver
from cutefish.local_audio import QwenTtsRuntime


def test_inspect_fails_closed_without_creating_missing_model_resources(tmp_path: Path) -> None:
    model_root = tmp_path / "models"
    runtime = QwenTtsRuntime(
        paths=HostPathResolver({"model_root": model_root}),
        model_root=model_root,
        interpreter_path=Path(sys.executable),
    )

    status = runtime.inspect()

    assert status["status"] == "needs_resource"
    assert status["runtime_available"] is True
    assert status["downloads_enabled"] is False
    assert status["device"] == "cuda:0"
    assert status["generation"]["initial_max_new_tokens"] == 512
    assert status["generation"]["oom_retry_max_new_tokens"] == 256
    assert set(status["models"]) == {"voice_design", "base"}
    assert all(model["available"] is False for model in status["models"].values())
    assert all(model["resource_fingerprint"] is None for model in status["models"].values())
    assert not model_root.exists()
