from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import uuid
import wave
from pathlib import Path

import pytest

from cutefish.core.context import Context
from cutefish.core.models import LocalVoiceCandidate, utc_now
from cutefish.core.settings import load_settings
from cutefish.extensions.models import ManagedMediaRecord
from cutefish.migration.contracts import MigrationManifest
from cutefish.migration.service import (
    MigrationError,
    MigrationPolicy,
    export_bundle,
    import_staging,
    validate_bundle,
)


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x00\x00" * 1_600)
    return output.getvalue()


async def _context_with_candidate(tmp_path: Path, *, write_file: bool = True):
    host_config = tmp_path / "host.local.json"
    host_config.write_text(
        json.dumps({"schema_version": 1, "room_root": str(tmp_path / "rooms")}),
        encoding="utf-8",
    )
    settings = load_settings(
        data_dir=tmp_path / "data",
        host_config_path=host_config,
        feature_status_path=tmp_path / "feature-status.json",
        frontend_dist=tmp_path / "frontend",
    )
    context = Context(settings)
    await context.db.initialize()
    data_root = settings.data_dir
    audio = _wav_bytes()
    candidate_id = str(uuid.uuid4())
    media_id = str(uuid.uuid4())
    relative_path = f"voice-candidates/{candidate_id}.wav"
    output_path = data_root / relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if write_file:
        output_path.write_bytes(audio)
    now = utc_now()
    digest = hashlib.sha256(audio).hexdigest()
    async with context.session_factory() as session:
        async with session.begin():
            session.add(
                ManagedMediaRecord(
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
                    duration_ms=100,
                    created_at=now,
                    legacy_state="registered",
                    legacy_media_type="audio/wav",
                    legacy_size_bytes=len(audio),
                    legacy_updated_at=now,
                )
            )
            await session.flush()
            session.add(
                LocalVoiceCandidate(
                    id=candidate_id,
                    origin="qwen_voice_design",
                    status="unapproved",
                    managed_media_id=media_id,
                    reference_relative_path=relative_path,
                    reference_sha256=digest,
                    ref_text="你好，欢迎来到小鱼的房间。",
                    style_prompt="温和、清晰、自然的中文女声。",
                    seed=7,
                    design_model_id="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                    design_revision="5ecdb67327fd37bb2e042aab12ff7391903235d3",
                    design_resource_fingerprint="a" * 64,
                    base_model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                    base_revision="fd4b254389122332181a7c3db7f27e918eec64e3",
                    base_resource_fingerprint="b" * 64,
                    created_at=now,
                )
            )
    return context, candidate_id, audio, output_path


@pytest.mark.asyncio
async def test_unapproved_candidate_reference_is_included_and_round_trips(tmp_path):
    context, candidate_id, audio, _source_path = await _context_with_candidate(tmp_path)
    try:
        policy = MigrationPolicy(paths=context.paths)
        bundle_path = context.paths.root_path("backup_root") / "voice-candidate-bundle"
        result = await export_bundle(context, bundle_path, (), policy)

        raw_manifest = json.loads((bundle_path / "manifest.json").read_text(encoding="utf-8"))
        manifest = MigrationManifest.model_validate(raw_manifest)
        assert result.media_count == 1
        assert len(manifest.files) == 1
        entry = manifest.files[0]
        assert entry.reason == "voice_candidate_reference"
        assert entry.candidate_id == candidate_id
        assert entry.relative_path == f"voice-candidates/{candidate_id}.wav"
        assert entry.sha256 == hashlib.sha256(audio).hexdigest()
        validation = await validate_bundle(bundle_path, policy)
        assert validation.valid, validation.errors

        target = context.paths.root_path("data_root") / "restored"
        await import_staging(bundle_path, target, policy)
        restored_audio = target / "media" / "data_root" / "voice-candidates" / f"{candidate_id}.wav"
        assert restored_audio.read_bytes() == audio
        materialized_audio = target / "voice-candidates" / f"{candidate_id}.wav"
        assert materialized_audio.read_bytes() == audio
        restored_db = sqlite3.connect(target / "cutefish.sqlite3")
        try:
            candidate = restored_db.execute(
                "SELECT reference_relative_path, reference_sha256, status FROM local_voice_candidate WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        finally:
            restored_db.close()
        assert candidate == (
            f"voice-candidates/{candidate_id}.wav",
            hashlib.sha256(audio).hexdigest(),
            "unapproved",
        )

        target_host_config = tmp_path / "target-host.local.json"
        target_host_config.write_text(
            json.dumps({"schema_version": 1, "room_root": str(tmp_path / "target-rooms")}),
            encoding="utf-8",
        )
        target_settings = load_settings(
            data_dir=target,
            host_config_path=target_host_config,
            feature_status_path=tmp_path / "target-feature-status.json",
            frontend_dist=tmp_path / "target-frontend",
        )
        target_context = Context(target_settings)
        try:
            await target_context.startup()
            assert target_context.deployment_profile == "target_staging"
            candidate_reader = target_context.extension_services["voice_candidates"]
            assert await candidate_reader.audio(candidate_id) == audio
        finally:
            await target_context.shutdown()
    finally:
        await context.db.close()


@pytest.mark.asyncio
async def test_candidate_media_limit_failure_does_not_publish_a_bundle(tmp_path):
    context, _candidate_id, _audio, _source_path = await _context_with_candidate(tmp_path)
    try:
        bundle_path = context.paths.root_path("backup_root") / "over-limit-bundle"
        policy = MigrationPolicy(paths=context.paths, max_media_files=0)
        with pytest.raises(MigrationError) as exc_info:
            await export_bundle(context, bundle_path, (), policy)
        assert exc_info.value.code == "media_count_limit"
        assert not bundle_path.exists()
    finally:
        await context.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("missing", "voice_candidate_reference_missing"),
        ("hash", "voice_candidate_reference_mismatch"),
        ("total_size", "media_total_size_limit"),
    ],
)
async def test_candidate_reference_failure_rejects_the_whole_export(tmp_path, change, expected_code):
    context, _candidate_id, audio, source_path = await _context_with_candidate(
        tmp_path,
        write_file=change != "missing",
    )
    try:
        if change == "missing":
            assert not source_path.exists()
        elif change == "hash":
            changed = bytearray(audio)
            changed[-1] ^= 0x01
            source_path.write_bytes(changed)
        bundle_path = context.paths.root_path("backup_root") / f"{change}-bundle"
        policy = MigrationPolicy(
            paths=context.paths,
            max_media_total_bytes=(len(audio) - 1 if change == "total_size" else 8 * 1024 * 1024 * 1024),
        )
        with pytest.raises(MigrationError) as exc_info:
            await export_bundle(context, bundle_path, (), policy)
        assert exc_info.value.code == expected_code
        assert not bundle_path.exists()
    finally:
        await context.db.close()
