from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

import av
import pytest
from starlette.requests import Request

from cutefish.core.paths import HostPathResolver
from cutefish.extensions import asr, asr_worker
from cutefish.extensions.asr import AsrService
from cutefish.extensions.router import transcribe_uploaded_audio
from cutefish.extensions.tts import SpeechError
from cutefish.local_audio import LocalAudioBusy, LocalAudioLane, LocalAudioProcessResult


def _wav_bytes(*, seconds: float = 0.2, sample_rate: int = 16_000) -> bytes:
    output = io.BytesIO()
    samples = int(seconds * sample_rate)
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * samples)
    return output.getvalue()


class _TemporaryLane:
    def __init__(self, parent: Path):
        self.parent = parent
        self.workspace: Path | None = None
        self.cleaned = False
        self.calls = 0

    @contextlib.asynccontextmanager
    async def job(self, operation: str):
        assert operation == "asr"
        self.calls += 1
        temporary = tempfile.TemporaryDirectory(dir=self.parent)
        self.workspace = Path(temporary.name)
        try:
            yield SimpleNamespace(workspace=self.workspace)
        finally:
            temporary.cleanup()
            self.cleaned = not self.workspace.exists()


def _ready_service(lane) -> AsrService:
    service = AsrService(None, None, None, local_audio_lane=lane)
    service.capabilities = lambda: {"runtime_available": True, "selected_model_id": "small"}
    service._model_path = lambda _model_id: Path.cwd()
    service._run_worker = _fake_worker
    return service


async def _fake_worker(_job, _audio_path, _model_path):
    return {
        "text": "测试文本",
        "language": "zh",
        "duration_seconds": 0.2,
        "media_type": "audio/wav",
        "segments": [{"start": 0.0, "end": 0.2, "text": "测试文本"}],
    }


async def _chunks(*items: bytes):
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_upload_busy_fails_before_reading_audio(tmp_path):
    class BusyLane:
        @contextlib.asynccontextmanager
        async def job(self, _operation):
            raise LocalAudioBusy("asr")
            yield  # pragma: no cover - makes this an async context manager

    consumed = False

    async def body():
        nonlocal consumed
        consumed = True
        yield b"audio"

    service = _ready_service(BusyLane())
    with pytest.raises(SpeechError) as error:
        await service.transcribe_upload(body(), content_type="audio/wav")

    assert error.value.status_code == 409
    assert error.value.code == "asr_busy"
    assert not consumed


@pytest.mark.asyncio
async def test_upload_limit_cleans_partial_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(asr, "MAX_ASR_BYTES", 3)
    lane = _TemporaryLane(tmp_path)
    service = _ready_service(lane)

    with pytest.raises(SpeechError) as error:
        await service.transcribe_upload(_chunks(b"12", b"34"), content_type="audio/wav")

    assert error.value.status_code == 413
    assert lane.calls == 1
    assert lane.cleaned


@pytest.mark.asyncio
async def test_declared_upload_limit_rejects_before_acquiring_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(asr, "MAX_ASR_BYTES", 3)
    lane = _TemporaryLane(tmp_path)
    service = _ready_service(lane)

    with pytest.raises(SpeechError) as error:
        await service.transcribe_upload(
            _chunks(b"never read"),
            content_type="audio/wav",
            content_length="4",
        )

    assert error.value.status_code == 413
    assert lane.calls == 0


def test_worker_normalizes_real_wav_and_rejects_duration_and_playlists(tmp_path):
    source = tmp_path / "input.audio"
    normalized = tmp_path / "normalized.wav"
    source.write_bytes(_wav_bytes(seconds=0.2, sample_rate=8_000))

    media_type, duration = asr_worker._write_normalized_audio(source, normalized, max_seconds=1)

    assert media_type == "audio/wav"
    assert duration == pytest.approx(0.2, abs=0.002)
    with wave.open(str(normalized), "rb") as audio:
        assert audio.getframerate() == 16_000
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2

    with pytest.raises(asr_worker.WorkerFailure, match="audio_too_long"):
        asr_worker._write_normalized_audio(source, tmp_path / "too-long.wav", max_seconds=0)

    source.write_text("#EXTM3U\nhttps://example.invalid/live\n", encoding="utf-8")
    with pytest.raises(asr_worker.WorkerFailure, match="invalid_audio_container"):
        asr_worker._write_normalized_audio(source, tmp_path / "playlist.wav", max_seconds=1)


def test_worker_forces_allowlisted_demuxer_and_blocks_external_movie_references(tmp_path, monkeypatch):
    source = tmp_path / "input.audio"
    source.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
    observed = {}

    def reject_probe(_input, **kwargs):
        observed.update(kwargs)
        raise OSError("fixture stops before demux")

    monkeypatch.setattr(av, "open", reject_probe)
    with pytest.raises(asr_worker.WorkerFailure, match="invalid_audio"):
        asr_worker._write_normalized_audio(source, tmp_path / "normalized.wav", max_seconds=1)

    assert observed["format"] == "mov"
    assert observed["options"]["protocol_whitelist"] == "pipe"
    assert observed["options"]["enable_drefs"] == "0"
    assert observed["options"]["use_absolute_path"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_error", "expected_code"),
    [
        ("invalid_audio_container", "invalid_audio_container"),
        ("unsupported_audio_format", "unsupported_audio_format"),
        ("audio_too_long", "audio_too_long"),
        ("invalid_audio", "invalid_audio"),
    ],
)
async def test_worker_decode_and_duration_errors_map_to_unprocessable(
    worker_error, expected_code
):
    class FailedWorker:
        workspace = Path.cwd()

        async def run(self, _argv, *, timeout_seconds):
            assert timeout_seconds == asr.MAX_ASR_WORKER_SECONDS
            return LocalAudioProcessResult(
                returncode=2,
                stdout=json.dumps({"error": worker_error}).encode("utf-8"),
            )

    service = AsrService(None, None, None)
    with pytest.raises(SpeechError) as error:
        await service._run_worker(FailedWorker(), Path("input.audio"), Path("model"))

    assert error.value.status_code == 422
    assert error.value.code == expected_code


@pytest.mark.asyncio
async def test_asgi_disconnect_cancels_and_reaps_active_worker(tmp_path):
    model_root = tmp_path / "models"
    model_root.mkdir()
    model_path = model_root / "faster-whisper" / "small"
    model_path.mkdir(parents=True)
    (model_path / "model.bin").write_bytes(b"fixture")
    (model_path / "config.json").write_text("{}", encoding="utf-8")
    paths = HostPathResolver({"model_root": model_root})

    class TrackingLane(LocalAudioLane):
        last_job = None

        @contextlib.asynccontextmanager
        async def job(self, operation):
            async with super().job(operation) as job:
                self.last_job = job
                yield job

    lane = TrackingLane(paths, model_root=model_root)
    service = AsrService(paths, None, None, local_audio_lane=lane)
    service.capabilities = lambda: {"runtime_available": True, "selected_model_id": "small"}
    service._model_path = lambda _model_id: model_path
    worker_started = asyncio.Event()

    async def blocking_worker(job, _audio_path, _model_path):
        worker_started.set()
        await job.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import time; Path('worker-started').write_text('yes'); time.sleep(60)",
            ],
            timeout_seconds=300,
        )

    service._run_worker = blocking_worker
    disconnect = asyncio.Event()
    request_messages = 0

    async def receive():
        nonlocal request_messages
        if request_messages == 0:
            request_messages += 1
            return {"type": "http.request", "body": b"fixture audio", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    app = SimpleNamespace(state=SimpleNamespace(context=SimpleNamespace(extension_services={"asr": service})))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/extensions/asr/transcriptions/upload",
        "raw_path": b"/api/extensions/asr/transcriptions/upload",
        "query_string": b"",
        "headers": [(b"content-type", b"application/octet-stream")],
        "server": ("test", 80),
        "client": ("test", 123),
        "app": app,
        "state": {},
    }
    request = Request(scope, receive)
    route_task = asyncio.create_task(transcribe_uploaded_audio(request, _admin=None))

    await asyncio.wait_for(worker_started.wait(), timeout=5)
    assert lane.last_job is not None
    ready_file = lane.last_job.workspace / "worker-started"
    for _ in range(500):
        if ready_file.exists():
            break
        await asyncio.sleep(0.01)
    assert ready_file.is_file()
    process = lane.last_job._active_process
    assert process is not None and process.returncode is None

    disconnect.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(route_task, timeout=5)

    assert process.returncode is not None
    assert not lane.busy
    assert not lane.last_job.workspace.exists()
    assert list((model_root / ".local-audio-tmp").iterdir()) == []
