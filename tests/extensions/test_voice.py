from __future__ import annotations

import hashlib
import io
import wave
from datetime import timedelta

import pytest
from cutefish.chat.models import ChatMessage, Conversation
from cutefish.core.db import Database
from cutefish.core.models import TrialBatch, utc_now
from cutefish.core.paths import HostPathResolver
from cutefish.core.registry import ModuleRegistry
from cutefish.extensions import register_module
from cutefish.extensions.contracts import VoiceReplyCreate
from cutefish.extensions.models import ManagedMediaRecord
from cutefish.extensions.voice import VoiceError, VoiceService
from cutefish.memory.service import OWNER_PERSON_ID
from cutefish.qq.sender import ControlledRecordMedia


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x00\x00" * 1_600)
    return output.getvalue()


class FakeTts:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []
        self.audio = _wav_bytes()

    def inspect(self):
        return {
            "runtime_available": True,
            "voices": [{"voice_id": "fake-voice", "name": "Fixture Voice", "culture": "zh-CN"}],
        }

    def synthesize_preview(self, text: str, voice_id: str, *, max_text_chars: int = 240):
        self.calls.append((text, voice_id, max_text_chars))
        return self.audio, {"voice_id": voice_id, "name": "Fixture Voice", "culture": "zh-CN"}


class FakeQqService:
    def __init__(self):
        self.guards = []

    async def validate_delivery_target(self, guard):
        self.guards.append(dict(guard))


def test_extension_registry_declares_only_manual_local_voice_preview_and_dto_is_strict():
    registry = ModuleRegistry()
    module = register_module(registry)

    assert module.id == "extensions"
    assert module.enabled is False
    assert module.action_handler is None
    assert registry.capability("extensions", "extensions.voice.preview") is not None
    assert registry.capability("extensions", "qq.voice.send") is None

    valid = VoiceReplyCreate(
        conversation_id="conversation-fixture",
        expected_conversation_revision=2,
        source_message_id="message-fixture",
        batch_id="voice-batch-fixture",
        expected_batch_version=3,
    )
    assert valid.batch_id == "voice-batch-fixture"
    with pytest.raises(ValueError):
        VoiceReplyCreate(
            conversation_id="conversation-fixture",
            expected_conversation_revision=2,
            source_message_id="message-fixture",
            batch_id="voice-batch-fixture",
            expected_batch_version=3,
            approval_ref="client-supplied-approval",
        )


@pytest.mark.asyncio
async def test_voice_artifact_binds_independent_batch_and_reader_rechecks_exact_lineage(tmp_path):
    db = Database(tmp_path / "voice-test.sqlite3")
    await db.initialize()
    try:
        now = utc_now()
        async with db.session_factory() as session:
            async with session.begin():
                from cutefish.memory.models import MemoryPerson

                session.add(
                    MemoryPerson(
                        person_id=OWNER_PERSON_ID,
                        display_name="Owner fixture",
                        role="owner",
                        state="active",
                        chat_only=False,
                    )
                )
                await session.flush()
                session.add(
                    Conversation(
                        id="conversation-fixture",
                        person_id=OWNER_PERSON_ID,
                        scope="production",
                        channel="qq",
                        purpose="production",
                        delivery_mode="qq",
                        revision=4,
                        state="active",
                        stop_requested=False,
                        human_takeover=False,
                        account_id="account-fixture",
                        recipient_id="owner-recipient",
                        batch_id="source-text-batch",
                    )
                )
                await session.flush()
                session.add(
                    ChatMessage(
                        id="assistant-message-fixture",
                        conversation_id="conversation-fixture",
                        person_id=OWNER_PERSON_ID,
                        scope="production",
                        role="assistant",
                        content="Fixture response for isolated voice validation.",
                        channel="qq",
                        created_at=now,
                    )
                )
                session.add(
                    TrialBatch(
                        id="voice-batch-fixture",
                        label="single voice fixture",
                        module_id="qq",
                        capability_id="qq.voice.send",
                        object_ids=["owner-recipient"],
                        stop_condition={"kind": "after_n", "max_actions": 1},
                        expires_at=now + timedelta(minutes=30),
                        state="running",
                        action_count=0,
                        version=2,
                    )
                )

        data_root = tmp_path / "data"
        temp_root = tmp_path / "temp"
        data_root.mkdir()
        temp_root.mkdir()
        tts = FakeTts()
        qq = FakeQqService()
        service = VoiceService(
            db.session_factory,
            HostPathResolver({"data_root": data_root, "temp_root": temp_root}),
            tts,
            qq_service=qq,
        )

        await service.set_profile(expected_revision=1, voice_id="fake-voice", actor_id="owner-fixture")
        audition = await service.create_profile_preview(
            expected_profile_version=1, text="fixture audition", actor_id="owner-fixture"
        )
        approval = await service.approve_profile_preview(
            expected_profile_version=1,
            preview_artifact_id=audition["artifact_id"],
            actor_id="owner-fixture",
        )
        current = await service.status()
        await service.set_settings(
            expected_revision=current["revision"],
            runtime_enabled=True,
            voice_outbox_enabled=True,
            actor_id="owner-fixture",
        )

        artifact = await service.prepare_voice_reply(
            conversation_id="conversation-fixture",
            expected_conversation_revision=4,
            source_message_id="assistant-message-fixture",
            batch_id="voice-batch-fixture",
            expected_batch_version=2,
            actor_id="owner-fixture",
        )
        assert artifact["purpose"] == "reply"
        assert artifact["artifact_state"] == "ready"
        assert approval["approval_id"]
        assert len(tts.calls) == 2
        assert tts.calls[-1][2] == 2_000
        assert [guard["batch_id"] for guard in qq.guards] == ["source-text-batch"] * 2

        async with db.session_factory() as session:
            record = await session.get(ManagedMediaRecord, artifact["artifact_id"])
            assert record is not None
            assert record.batch_id == "voice-batch-fixture"
            assert record.source_batch_id == "source-text-batch"
            assert record.batch_version == 2
            assert record.approval_ref == approval["approval_id"]

        lineage = {
            "conversation_id": "conversation-fixture",
            "expected_revision": 4,
            "batch_id": "voice-batch-fixture",
            "account_id": "account-fixture",
            "recipient_id": "owner-recipient",
            "person_id": OWNER_PERSON_ID,
        }
        binding = await service.resolve_voice_outbox_binding(artifact["artifact_id"], **lineage)
        assert binding.media_format == "audio/wav"
        assert binding.sha256 == hashlib.sha256(tts.audio).hexdigest()
        await service.voice_validator({**binding.model_dump(), **lineage})

        with pytest.raises(VoiceError, match="QQ 目标不匹配"):
            await service.resolve_voice_outbox_binding(
                artifact["artifact_id"], **{**lineage, "recipient_id": "other-recipient"}
            )
        async with db.session_factory() as session:
            record = await session.get(ManagedMediaRecord, artifact["artifact_id"])
            assert record.artifact_state == "ready"

        # Core claims the sole action before the sender's controlled reader runs.
        async with db.session_factory() as session:
            async with session.begin():
                batch = await session.get(TrialBatch, "voice-batch-fixture")
                batch.action_count = 1
        media = await service.read_controlled_record(
            {
                "artifact_id": binding.artifact_id,
                "sha256": binding.sha256,
                "voice_version": f"{binding.profile_id}@{binding.profile_version}",
                "approval_ref": binding.approval_id,
            },
            **lineage,
        )
        assert isinstance(media, ControlledRecordMedia)
        assert media.content == tts.audio
        assert media.media_type == "audio/wav"
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
async def test_voice_artifact_staleness_is_audited_and_target_staging_is_gated(tmp_path):
    db = Database(tmp_path / "voice-stale-test.sqlite3")
    await db.initialize()
    try:
        now = utc_now()
        async with db.session_factory() as session:
            async with session.begin():
                from cutefish.memory.models import MemoryPerson

                session.add(MemoryPerson(person_id=OWNER_PERSON_ID, display_name="Owner", role="owner", chat_only=False))
                await session.flush()
                session.add(
                    Conversation(
                        id="conversation-stale",
                        person_id=OWNER_PERSON_ID,
                        scope="production",
                        channel="qq",
                        purpose="production",
                        delivery_mode="qq",
                        revision=1,
                        state="active",
                        stop_requested=False,
                        human_takeover=False,
                        account_id="account-fixture",
                        recipient_id="owner-recipient",
                        batch_id="source-batch",
                    )
                )
                await session.flush()
                session.add(
                    ChatMessage(
                        id="assistant-stale",
                        conversation_id="conversation-stale",
                        person_id=OWNER_PERSON_ID,
                        scope="production",
                        role="assistant",
                        content="Fixture reply.",
                        channel="qq",
                        created_at=now,
                    )
                )
                session.add(
                    TrialBatch(
                        id="voice-batch-stale",
                        label="stale fixture",
                        module_id="qq",
                        capability_id="qq.voice.send",
                        object_ids=["owner-recipient"],
                        stop_condition={"kind": "after_n", "max_actions": 1},
                        expires_at=now + timedelta(minutes=30),
                        state="running",
                        action_count=0,
                        version=2,
                    )
                )
        data_root = tmp_path / "data"
        data_root.mkdir()
        paths = HostPathResolver({"data_root": data_root, "temp_root": tmp_path / "temp"})
        tts = FakeTts()
        qq = FakeQqService()
        service = VoiceService(db.session_factory, paths, tts, qq_service=qq)
        await service.set_profile(expected_revision=1, voice_id="fake-voice", actor_id="owner-fixture")
        audition = await service.create_profile_preview(
            expected_profile_version=1, text="fixture audition", actor_id="owner-fixture"
        )
        await service.approve_profile_preview(
            expected_profile_version=1,
            preview_artifact_id=audition["artifact_id"],
            actor_id="owner-fixture",
        )
        current = await service.status()
        await service.set_settings(
            expected_revision=current["revision"], runtime_enabled=True, voice_outbox_enabled=True, actor_id="owner-fixture"
        )
        artifact = await service.prepare_voice_reply(
            conversation_id="conversation-stale",
            expected_conversation_revision=1,
            source_message_id="assistant-stale",
            batch_id="voice-batch-stale",
            expected_batch_version=2,
            actor_id="owner-fixture",
        )

        async with db.session_factory() as session:
            async with session.begin():
                conversation = await session.get(Conversation, "conversation-stale")
                conversation.revision += 1

        lineage = {
            "conversation_id": "conversation-stale",
            "expected_revision": 1,
            "batch_id": "voice-batch-stale",
            "account_id": "account-fixture",
            "recipient_id": "owner-recipient",
            "person_id": OWNER_PERSON_ID,
        }
        with pytest.raises(VoiceError, match="语音来源会话"):
            await service.resolve_voice_outbox_binding(artifact["artifact_id"], **lineage)
        async with db.session_factory() as session:
            record = await session.get(ManagedMediaRecord, artifact["artifact_id"])
            assert record.artifact_state == "cancelled"
        assert not (data_root / "voice-artifacts" / f"{artifact['artifact_id']}.wav").exists()

        staging = VoiceService(db.session_factory, paths, tts, deployment_profile=lambda: "target_staging", qq_service=qq)
        with pytest.raises(VoiceError) as exc_info:
            await staging.resolve_voice_outbox_binding(artifact["artifact_id"], **lineage)
        assert exc_info.value.code == "target_staging"
    finally:
        await db.engine.dispose()
