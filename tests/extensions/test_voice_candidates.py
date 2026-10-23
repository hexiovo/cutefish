from __future__ import annotations

import hashlib
import io
import json
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from cutefish.core.db import Database
from cutefish.core.models import LocalVoiceCandidate
from cutefish.core.paths import HostPathResolver
from cutefish.extensions.models import ManagedMediaRecord, VoiceApproval, VoiceProfileVersion
from cutefish.extensions.tts import SpeechError
from cutefish.extensions.voice import VoiceError, VoiceService, _profile_settings_hash
from cutefish.extensions.voice_candidates import VoiceCandidateService


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x00\x00" * 1_600)
    return output.getvalue()


class FakeLane:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def job(self, operation: str):
        assert operation == "tts"
        import tempfile

        with tempfile.TemporaryDirectory(dir=self.root) as workspace:
            yield SimpleNamespace(operation=operation, workspace=Path(workspace))


class FakeQwenRuntime:
    def __init__(self):
        self.audio = _wav_bytes()
        self.base_calls = []
        self.design_calls = 0
        self.device = "cuda:0"
        self.bad_base_settings = False
        self.bad_design_seed = False
        self.models_available = True
        self.snapshots = {
            "voice_design": SimpleNamespace(
                model_id="Qwen/VoiceDesign", revision="design-rev", resource_fingerprint="a" * 64
            ),
            "base": SimpleNamespace(
                model_id="Qwen/Base", revision="base-rev", resource_fingerprint="b" * 64
            ),
        }

    def inspect(self):
        return {
            "status": "ready" if self.models_available else "needs_resource",
            "runtime_available": True,
            "interpreter_available": True,
            "device": self.device,
            "dtype": "bfloat16",
            "attn_implementation": "sdpa",
            "downloads_enabled": False,
            "trust_remote_code": False,
            "generation": {
                "initial_max_new_tokens": 512,
                "oom_retry_max_new_tokens": 256,
                "oom_retry_policy": "single_cuda_oom_retry_half_tokens",
                "max_output_seconds": 45,
                "max_output_bytes": 16 * 1024 * 1024,
            },
            "models": {
                "voice_design": {
                    "model_id": "Qwen/VoiceDesign",
                    "revision": "design-rev",
                    "available": self.models_available,
                    "resource_fingerprint": "a" * 64,
                },
                "base": {
                    "model_id": "Qwen/Base",
                    "revision": "base-rev",
                    "available": self.models_available,
                    "resource_fingerprint": "b" * 64,
                },
            },
            "detail": "fixture ready",
        }

    def resource_snapshot(self, slot: str, *, verify_files: bool = True):
        assert verify_files is True
        return self.snapshots[slot]

    async def design_reference(self, job, *, text: str, style_prompt: str, seed: int):
        self.design_calls += 1
        path = Path(job.workspace) / "design.wav"
        path.write_bytes(self.audio)
        return self._result(path, "voice_design")

    async def synthesize_clone(
        self,
        job,
        *,
        text: str,
        reference_wav: bytes,
        reference_sha256: str,
        reference_text: str,
        expected_model_id: str,
        expected_model_revision: str,
        expected_resource_fingerprint: str,
        max_new_tokens: int,
        seed: int,
    ):
        self.base_calls.append(
            {
                "text": text,
                "reference_wav": reference_wav,
                "reference_sha256": reference_sha256,
                "reference_text": reference_text,
                "expected_model_id": expected_model_id,
                "expected_model_revision": expected_model_revision,
                "expected_resource_fingerprint": expected_resource_fingerprint,
                "max_new_tokens": max_new_tokens,
                "seed": seed,
            }
        )
        path = Path(job.workspace) / "base-output.wav"
        path.write_bytes(self.audio)
        return self._result(path, "base")

    def _result(self, path: Path, slot: str):
        snapshot = self.snapshots[slot]
        settings = {
            "device": "cuda:0",
            "dtype": "bfloat16",
            "attn_implementation": "sdpa",
            "trust_remote_code": False,
            "local_files_only": True,
            "seed": 23,
            "max_new_tokens": 512,
            "initial_max_new_tokens": 512,
            "attempt_count": 1,
            "retry_policy": "single_cuda_oom_retry_half_tokens",
            "language": "Chinese",
            "retry_after_cuda_oom": False,
        }
        if slot == "base" and self.bad_base_settings:
            settings["dtype"] = "float32"
        if slot == "voice_design" and self.bad_design_seed:
            settings["seed"] = 99
        return {
            "wav_path": path,
            "sample_rate": 16_000,
            "duration_seconds": 0.1,
            "byte_length": len(self.audio),
            "sha256": hashlib.sha256(self.audio).hexdigest(),
            "model_id": snapshot.model_id,
            "model_revision": snapshot.revision,
            "resource_fingerprint": snapshot.resource_fingerprint,
            "settings": settings,
        }


class FakeTts:
    def inspect(self):
        return {"runtime_available": True, "voices": []}

    def synthesize_preview(self, *args, **kwargs):  # pragma: no cover - Qwen route must bypass SAPI.
        raise AssertionError("Qwen profiles must use the local Base adapter")


@pytest.mark.asyncio
async def test_candidate_generation_registers_hash_bound_managed_media_and_reads_it(tmp_path):
    db = Database(tmp_path / "candidate.sqlite3")
    await db.initialize()
    try:
        data_root = tmp_path / "data"
        data_root.mkdir()
        paths = HostPathResolver({"data_root": data_root})
        runtime = FakeQwenRuntime()
        service = VoiceCandidateService(
            db.session_factory,
            paths,
            runtime,
            FakeLane(tmp_path / "jobs"),
        )

        capability = service.capabilities()
        assert capability["candidate_generation_available"] is True
        candidate = await service.create(
            text="你好，今天也慢慢来。",
            style_prompt="温柔、自然，语速稍慢",
            seed=23,
        )
        assert candidate["voice_id"] == f"qwen3:{candidate['candidate_id']}"
        assert candidate["status"] == "unapproved"
        assert candidate["reference_text"] == "你好，今天也慢慢来。"
        assert "reference_relative_path" not in candidate
        assert candidate["design"]["resource_fingerprint"] == "a" * 64
        assert candidate["base"]["resource_fingerprint"] == "b" * 64
        assert await service.audio(candidate["candidate_id"]) == runtime.audio
        assert (await service.list())["items"] == [candidate]

        async with db.session_factory() as session:
            row = await session.get(LocalVoiceCandidate, candidate["candidate_id"])
            media = await session.get(ManagedMediaRecord, row.managed_media_id)
        assert row is not None and row.origin == "qwen_voice_design" and row.status == "unapproved"
        assert row.reference_sha256 == hashlib.sha256(runtime.audio).hexdigest()
        assert media is not None and media.root_key == "data_root"
        assert media.asset_class == "voice_candidate"
        assert media.role_use_approved is False and media.share_allowed is False

        path = data_root / row.reference_relative_path
        path.write_bytes(b"changed")
        with pytest.raises(SpeechError) as error:
            await service.audio(candidate["candidate_id"])
        assert error.value.code == "qwen_reference_mismatch"
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
async def test_missing_qwen_resources_fail_before_candidate_or_approval_rows_are_created(tmp_path):
    db = Database(tmp_path / "missing-qwen.sqlite3")
    await db.initialize()
    try:
        data_root = tmp_path / "data"
        data_root.mkdir()
        paths = HostPathResolver({"data_root": data_root})
        runtime = FakeQwenRuntime()
        runtime.models_available = False
        service = VoiceCandidateService(
            db.session_factory,
            paths,
            runtime,
            FakeLane(tmp_path / "jobs"),
        )

        capability = service.capabilities()
        assert capability["status"] == "needs_resource"
        assert capability["candidate_generation_available"] is False
        with pytest.raises(SpeechError) as error:
            await service.create(
                text="你好，今天也慢慢来。",
                style_prompt="温柔、自然，语速稍慢",
                seed=23,
            )
        assert error.value.code == "qwen_resource_unavailable"
        assert runtime.design_calls == 0

        async with db.session_factory() as session:
            assert (await session.scalars(select(LocalVoiceCandidate))).all() == []
            assert (await session.scalars(select(ManagedMediaRecord))).all() == []
            assert (await session.scalars(select(VoiceProfileVersion))).all() == []
            assert (await session.scalars(select(VoiceApproval))).all() == []
        assert not (data_root / "voice-candidates").exists()
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
async def test_mismatched_voice_design_settings_fail_before_candidate_or_media_persist(tmp_path):
    db = Database(tmp_path / "bad-design-settings.sqlite3")
    await db.initialize()
    try:
        data_root = tmp_path / "data"
        data_root.mkdir()
        paths = HostPathResolver({"data_root": data_root})
        runtime = FakeQwenRuntime()
        runtime.bad_design_seed = True
        lane = FakeLane(tmp_path / "jobs")
        service = VoiceCandidateService(db.session_factory, paths, runtime, lane)

        with pytest.raises(SpeechError) as error:
            await service.create(
                text="你好，今天也慢慢来。",
                style_prompt="温柔、自然，语速稍慢",
                seed=23,
            )
        assert error.value.code == "qwen_worker_failed"
        assert runtime.design_calls == 1

        async with db.session_factory() as session:
            assert (await session.scalars(select(LocalVoiceCandidate))).all() == []
            assert (await session.scalars(select(ManagedMediaRecord))).all() == []
        assert not (data_root / "voice-candidates").exists()
        assert list(lane.root.iterdir()) == []
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
async def test_qwen_profile_preview_and_approval_are_bound_to_candidate_resource_fingerprint(tmp_path):
    db = Database(tmp_path / "profile.sqlite3")
    await db.initialize()
    try:
        data_root = tmp_path / "data"
        data_root.mkdir()
        paths = HostPathResolver({"data_root": data_root})
        runtime = FakeQwenRuntime()
        lane = FakeLane(tmp_path / "jobs")
        candidates = VoiceCandidateService(db.session_factory, paths, runtime, lane)
        candidate = await candidates.create(
            text="你好，今天也慢慢来。",
            style_prompt="温柔、自然，语速稍慢",
            seed=23,
        )
        service = VoiceService(
            db.session_factory,
            paths,
            FakeTts(),
            voice_candidates=candidates,
            qwen_tts_runtime=runtime,
            local_audio_lane=lane,
        )
        status = await service.set_profile(
            expected_revision=1,
            voice_id=candidate["voice_id"],
            actor_id="owner-fixture",
        )
        profile = status["profile"]
        assert profile["engine_id"] == "qwen3.base"
        assert profile["voice_id"] == candidate["voice_id"]
        assert profile["engine_config"]["reference_sha256"] == candidate["reference_sha256"]
        assert profile["engine_config"]["base"]["resource_fingerprint"] == "b" * 64
        assert profile["engine_config"]["runtime"]["device"] == "cuda:0"
        assert profile["engine_config"]["runtime"]["generation"]["initial_max_new_tokens"] == 512

        runtime.device = "cuda:1"
        with pytest.raises(VoiceError) as error:
            await service.create_profile_preview(
                expected_profile_version=1,
                text="设置不一致时不得调用 Base。",
                actor_id="owner-fixture",
            )
        assert error.value.code == "qwen_runtime_mismatch"
        assert runtime.base_calls == []
        runtime.device = "cuda:0"

        audition = await service.create_profile_preview(
            expected_profile_version=1,
            text="这是另一句本地试听。",
            actor_id="owner-fixture",
        )
        assert audition["purpose"] == "approval_preview"
        assert runtime.base_calls[-1]["text"] == "这是另一句本地试听。"
        assert runtime.base_calls[-1]["reference_wav"] == runtime.audio
        assert runtime.base_calls[-1]["expected_model_id"] == "Qwen/Base"
        assert runtime.base_calls[-1]["expected_resource_fingerprint"] == "b" * 64
        assert runtime.base_calls[-1]["max_new_tokens"] == 512
        assert runtime.base_calls[-1]["seed"] == 23

        runtime.bad_base_settings = True
        with pytest.raises(VoiceError) as error:
            await service.create_profile_preview(
                expected_profile_version=1,
                text="错误的 Base 设置必须被拒绝。",
                actor_id="owner-fixture",
            )
        assert error.value.code == "qwen_worker_failed"
        runtime.bad_base_settings = False

        approved = await service.approve_profile_preview(
            expected_profile_version=1,
            preview_artifact_id=audition["artifact_id"],
            actor_id="owner-fixture",
        )
        assert approved["approval_id"]
        async with db.session_factory() as session:
            async with session.begin():
                profile_row = await session.get(VoiceProfileVersion, ("local-sapi", 1))
                assert profile_row is not None
                changed_config = json.loads(profile_row.engine_config_json)
                changed_config["base"]["resource_fingerprint"] = "c" * 64
                profile_row.engine_config_json = json.dumps(
                    changed_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )

        assert (await service.status())["readiness"] == "needs_approval"
        with pytest.raises(VoiceError, match="不匹配") as error:
            await service.approve_profile_preview(
                expected_profile_version=1,
                preview_artifact_id=audition["artifact_id"],
                actor_id="owner-fixture",
            )
        assert error.value.code == "voice_preview_unavailable"
    finally:
        await db.engine.dispose()


def test_legacy_profile_hash_stays_identical_while_engine_config_is_hash_bound():
    engine_id, voice_id, voice_name, culture = (
        "windows.system_speech",
        "fixture-id",
        "Fixture Voice",
        "zh-CN",
    )
    legacy_data = {
        "engine_id": engine_id,
        "voice_id": voice_id,
        "voice_name": voice_name,
        "culture": culture,
    }
    old_hash = hashlib.sha256(
        json.dumps(legacy_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert _profile_settings_hash(engine_id, voice_id, voice_name, culture, None) == old_hash

    first = '{"base":{"resource_fingerprint":"b"},"candidate_id":"candidate"}'
    second = '{"base":{"resource_fingerprint":"c"},"candidate_id":"candidate"}'
    assert _profile_settings_hash(engine_id, voice_id, voice_name, culture, first) != _profile_settings_hash(
        engine_id, voice_id, voice_name, culture, second
    )
