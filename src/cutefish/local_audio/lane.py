"""Nonblocking shared lane for explicitly requested local audio workers."""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Literal, Sequence

from cutefish.core.paths import HostPathResolver, PathPolicyError

from .errors import (
    LocalAudioBusy,
    LocalAudioTimeout,
    LocalAudioUnavailable,
    LocalAudioWorkerStartError,
)


AudioOperation = Literal["asr", "tts"]
MAX_WORKER_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class LocalAudioProcessResult:
    returncode: int
    stdout: bytes


class LocalAudioLane:
    """One nonwaiting high-memory slot shared by ASR and TTS in a Context."""

    def __init__(self, paths: HostPathResolver, *, model_root: Path | None = None):
        self._paths = paths
        self._model_root = model_root
        self._lock = asyncio.Lock()
        self._active_operation: AudioOperation | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def active_operation(self) -> AudioOperation | None:
        return self._active_operation

    @asynccontextmanager
    async def job(self, operation: AudioOperation) -> AsyncIterator["LocalAudioJob"]:
        """Acquire the shared slot without queueing and yield a disposable job."""
        if operation not in {"asr", "tts"}:
            raise ValueError("operation must be 'asr' or 'tts'")
        if self._lock.locked():
            raise LocalAudioBusy(operation)
        # asyncio.Lock's unlocked acquire completes synchronously on the owning
        # event loop, so a concurrent request cannot slip between these lines.
        await self._lock.acquire()
        self._active_operation = operation
        try:
            workspace_root = self._workspace_root()
            try:
                workspace_root.mkdir(parents=True, exist_ok=True)
                temporary_workspace = tempfile.TemporaryDirectory(
                    prefix=f"{operation}-",
                    dir=workspace_root,
                )
            except OSError as exc:
                raise LocalAudioUnavailable(
                    "local_audio_workspace_unavailable",
                    "本机语音临时目录当前不可用。",
                ) from exc
            try:
                job = LocalAudioJob(operation, Path(temporary_workspace.name))
                try:
                    yield job
                finally:
                    await job._terminate_active_process()
            finally:
                temporary_workspace.cleanup()
        finally:
            self._active_operation = None
            self._lock.release()

    def _workspace_root(self) -> Path:
        if self._model_root is None or not self._paths.configured("model_root"):
            raise LocalAudioUnavailable(
                "model_root_unconfigured",
                "本机语音模型目录尚未配置。",
            )
        try:
            root = self._paths.root_path("model_root")
            if not root.is_dir():
                raise LocalAudioUnavailable(
                    "model_root_unavailable",
                    "本机语音模型目录当前不可用。",
                )
            return self._paths.resolve(
                "model_root",
                ".local-audio-tmp",
                for_write=True,
            )
        except LocalAudioUnavailable:
            raise
        except (PathPolicyError, OSError, ValueError) as exc:
            raise LocalAudioUnavailable(
                "model_root_unavailable",
                "本机语音模型目录未通过路径安全检查。",
            ) from exc


class LocalAudioJob:
    """Request-scoped workspace and process launcher yielded by LocalAudioLane."""

    def __init__(self, operation: AudioOperation, workspace: Path):
        self.operation = operation
        self.workspace = workspace
        self._active_process: asyncio.subprocess.Process | None = None
        self._communication: asyncio.Task[tuple[bytes, bytes | None]] | None = None

    async def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout_seconds: float,
    ) -> LocalAudioProcessResult:
        """Run one argv-only worker with isolated offline/cache/temp env settings."""
        args = tuple(os.fspath(item) for item in argv)
        if not args or any(not isinstance(item, str) or not item for item in args):
            raise ValueError("argv must contain non-empty string arguments")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > MAX_WORKER_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be between 0 and 600 seconds")
        if self._active_process is not None:
            raise RuntimeError("a local audio job cannot run multiple workers concurrently")

        environment = self._child_environment()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(self.workspace),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                creationflags=creationflags,
            )
        except (OSError, ValueError) as exc:
            raise LocalAudioWorkerStartError(self.operation) from exc

        self._active_process = process
        communication = asyncio.create_task(process.communicate())
        self._communication = communication
        try:
            try:
                stdout, _stderr = await asyncio.wait_for(
                    asyncio.shield(communication),
                    timeout=float(timeout_seconds),
                )
            except asyncio.TimeoutError as exc:
                await self._terminate_active_process()
                raise LocalAudioTimeout(self.operation) from exc
            except asyncio.CancelledError:
                await self._terminate_active_process()
                raise
            return LocalAudioProcessResult(
                returncode=int(process.returncode or 0),
                stdout=stdout or b"",
            )
        finally:
            if self._active_process is process:
                self._active_process = None
                self._communication = None

    def _child_environment(self) -> dict[str, str]:
        cache_root = self.workspace / "cache"
        cache_paths = {
            "HF_HOME": cache_root,
            "HF_HUB_CACHE": cache_root / "hub",
            "HUGGINGFACE_HUB_CACHE": cache_root / "hub",
            "TRANSFORMERS_CACHE": cache_root / "transformers",
            "HF_DATASETS_CACHE": cache_root / "datasets",
            "TORCH_HOME": cache_root / "torch",
            "XDG_CACHE_HOME": cache_root,
        }
        environment = os.environ.copy()
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "TMP": str(self.workspace),
                "TEMP": str(self.workspace),
                "TMPDIR": str(self.workspace),
                **{key: str(path) for key, path in cache_paths.items()},
            }
        )
        for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN"):
            environment.pop(name, None)
        return environment

    async def _terminate_active_process(self) -> None:
        process = self._active_process
        if process is None:
            return
        communication = self._communication
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await process.wait()
        finally:
            if communication is not None and not communication.done():
                try:
                    await communication
                except (asyncio.CancelledError, Exception):
                    pass
