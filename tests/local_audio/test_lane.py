from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from cutefish.core.context import Context
from cutefish.core.paths import HostPathResolver
from cutefish.core.settings import load_settings
from cutefish.local_audio import LocalAudioBusy, LocalAudioLane, LocalAudioTimeout


def _settings(tmp_path: Path, model_root: str | None):
    host_config = tmp_path / "host.local.json"
    payload = {"schema_version": 1, "room_root": str(tmp_path / "rooms")}
    if model_root is not None:
        payload["model_root"] = model_root
    host_config.write_text(json.dumps(payload), encoding="utf-8")
    return load_settings(
        data_dir=tmp_path / "data",
        host_config_path=host_config,
        feature_status_path=tmp_path / "feature-status.json",
        frontend_dist=tmp_path / "frontend",
    )


def test_model_root_is_optional_and_load_does_not_create_it(tmp_path: Path) -> None:
    model_root = tmp_path / "models"
    settings = _settings(tmp_path, str(model_root))

    assert settings.model_root == model_root
    assert settings.roots["model_root"] == str(model_root)
    assert not model_root.exists()

    relative = _settings(tmp_path, "relative-models")
    assert relative.model_root is None
    assert "model_root" not in relative.roots


def test_context_shares_lane_with_asr_without_loading_models(tmp_path: Path) -> None:
    model_root = tmp_path / "models"
    settings = _settings(tmp_path, str(model_root))

    context = Context(settings)

    assert context.model_root == model_root
    assert context.local_audio_lane is not None
    assert context.extension_services["asr"].local_audio_lane is context.local_audio_lane
    assert not model_root.exists()


@pytest.mark.asyncio
async def test_lane_rejects_concurrent_job_and_cleans_workspace(tmp_path: Path) -> None:
    model_root = tmp_path / "models"
    model_root.mkdir()
    lane = LocalAudioLane(
        HostPathResolver({"model_root": model_root}),
        model_root=model_root,
    )

    async with lane.job("asr") as job:
        workspace = job.workspace
        assert workspace.is_dir()
        assert lane.busy
        assert lane.active_operation == "asr"
        with pytest.raises(LocalAudioBusy) as exc_info:
            async with lane.job("tts"):
                pytest.fail("a busy lane must not queue another operation")
        assert exc_info.value.code == "local_audio_busy"
        assert exc_info.value.operation == "tts"

    assert not workspace.exists()
    assert not lane.busy
    assert lane.active_operation is None


@pytest.mark.asyncio
async def test_lane_scopes_child_cache_and_kills_worker_on_timeout(tmp_path: Path) -> None:
    model_root = tmp_path / "models"
    model_root.mkdir()
    lane = LocalAudioLane(
        HostPathResolver({"model_root": model_root}),
        model_root=model_root,
    )
    inherited_hf_home = os.environ.get("HF_HOME")

    async with lane.job("asr") as job:
        workspace = job.workspace
        result = await job.run(
            [
                sys.executable,
                "-c",
                "import json, os; print(json.dumps({k: os.environ.get(k) for k in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HOME', 'TMP')}))",
            ],
            timeout_seconds=10,
        )
        child_env = json.loads(result.stdout.decode("utf-8"))
        assert result.returncode == 0
        assert child_env["HF_HUB_OFFLINE"] == "1"
        assert child_env["TRANSFORMERS_OFFLINE"] == "1"
        assert Path(child_env["HF_HOME"]).is_relative_to(workspace)
        assert Path(child_env["TMP"]) == workspace
        assert os.environ.get("HF_HOME") == inherited_hf_home

        with pytest.raises(LocalAudioTimeout):
            await job.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout_seconds=0.05,
            )

    assert not workspace.exists()
    assert not lane.busy


@pytest.mark.asyncio
async def test_lane_kills_worker_and_cleans_workspace_on_cancellation(tmp_path: Path) -> None:
    model_root = tmp_path / "models"
    model_root.mkdir()
    lane = LocalAudioLane(
        HostPathResolver({"model_root": model_root}),
        model_root=model_root,
    )
    started = tmp_path / "worker-started"

    async with lane.job("tts") as job:
        workspace = job.workspace
        worker = asyncio.create_task(
            job.run(
                [
                    sys.executable,
                    "-c",
                    "import pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text('started'); time.sleep(60)",
                    str(started),
                ],
                timeout_seconds=30,
            )
        )
        for _ in range(500):
            if started.exists():
                break
            await asyncio.sleep(0.01)
        assert started.exists(), "worker did not start within five seconds"

        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, timeout=3)
        assert workspace.exists()

    assert not workspace.exists()
    assert not lane.busy
