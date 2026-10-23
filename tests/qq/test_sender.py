import base64
import hashlib
import json

import pytest

from cutefish.core.outbox import OutboxService
from cutefish.qq.sender import ControlledRecordMedia, NapCatHttpSender
from cutefish.qq.service import QqError


class FakeQqService:
    async def sender_credentials_for_delivery(self, guard):
        self.guard = guard
        return {"service_url": "http://127.0.0.1:5700", "access_token": "test-token"}


def _payload(message_type="text", **values):
    return {
        "account_id": "account-1",
        "recipient_id": "123456",
        "person_id": "person-1",
        "conversation_id": "conversation-1",
        "batch_id": "batch-1",
        "expected_revision": 2,
        "attempt_id": "attempt-1",
        "message_type": message_type,
        "payload": "hello" if message_type == "text" else "",
        **values,
    }


def _outbox_record_payload(record_ref):
    return OutboxService._sender_payload(
        {
            "id": "outbox-1",
            "attempt_id": "attempt-1",
            "account_id": "account-1",
            "recipient_id": "123456",
            "person_id": "person-1",
            "conversation_id": "conversation-1",
            "expected_revision": 2,
            "batch_id": "batch-1",
            "message_type": "record",
            "payload": "",
            "segment_index": 0,
            "segment_count": 1,
            "record_ref": record_ref,
        }
    )


@pytest.mark.asyncio
async def test_record_is_rejected_before_media_read_or_transport_when_unverified():
    media_reader_calls = []
    transport_calls = []
    record_ref = {
        "artifact_id": "artifact-1",
        "sha256": "a" * 64,
        "voice_version": "default@1",
        "approval_ref": "approval-1",
    }
    sender = NapCatHttpSender(
        FakeQqService(),
        record_media_reader=lambda ref, **lineage: media_reader_calls.append((ref, lineage)),
        transport=lambda *args: transport_calls.append(args),
    )

    receipt = await sender.send(_outbox_record_payload(record_ref))

    assert receipt.outcome == "rejected"
    assert receipt.result_code == "needs_transport_format_verification"
    assert receipt.no_side_effect_proven is True
    assert sender.record_transport_verified is False
    assert sender.record_transport_status == "disabled_unverified"
    assert media_reader_calls == []
    assert transport_calls == []


@pytest.mark.asyncio
async def test_record_rejects_direct_media_locators():
    sender = NapCatHttpSender(FakeQqService(), record_transport_verified=True)
    record_ref = {
        "artifact_id": "artifact-1",
        "sha256": "a" * 64,
        "voice_version": "default@1",
        "approval_ref": "approval-1",
    }
    payload = _payload("record", record_ref=record_ref)
    payload["file"] = "https://example.invalid/audio.wav"

    with pytest.raises(QqError, match="URL"):
        await sender.send(payload)


@pytest.mark.asyncio
async def test_verified_record_uses_reader_bytes_and_record_receipt_code():
    content = b"RIFF\x04\x00\x00\x00WAVE"
    record_ref = {
        "artifact_id": "artifact-1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "voice_version": "default@1",
        "approval_ref": "approval-1",
    }
    transport_calls = []

    def transport(url, headers, body, timeout):
        transport_calls.append((url, headers, json.loads(body), timeout))
        return 200, b'{"retcode":0,"status":"ok","data":{"message_id":42}}'

    sender = NapCatHttpSender(
        FakeQqService(),
        record_media_reader=lambda ref, **lineage: ControlledRecordMedia(content, "audio/wav"),
        record_transport_verified=True,
        transport=transport,
    )

    receipt = await sender.send(_outbox_record_payload(record_ref))

    assert receipt.outcome == "sent"
    assert receipt.remote_receipt_id == "42"
    assert receipt.result_code == "onebot_record_sent"
    message = transport_calls[0][2]["message"]
    encoded = base64.b64encode(content).decode("ascii")
    assert message == [{"type": "record", "data": {"file": f"base64://{encoded}"}}]


@pytest.mark.asyncio
async def test_record_reader_receives_exact_outbox_lineage():
    content = b"RIFF\x04\x00\x00\x00WAVE"
    record_ref = {
        "artifact_id": "artifact-1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "voice_version": "default@1",
        "approval_ref": "approval-1",
    }
    reader_calls = []

    async def reader(ref, **lineage):
        reader_calls.append((ref, lineage))
        return ControlledRecordMedia(content, "audio/wav")

    sender = NapCatHttpSender(
        FakeQqService(),
        record_media_reader=reader,
        record_transport_verified=True,
        transport=lambda *args: (200, b'{"retcode":0,"status":"ok","data":{"message_id":42}}'),
    )

    receipt = await sender.send(_outbox_record_payload(record_ref))

    assert receipt.outcome == "sent"
    assert reader_calls == [
        (
            record_ref,
            {
                "conversation_id": "conversation-1",
                "expected_revision": 2,
                "batch_id": "batch-1",
                "account_id": "account-1",
                "recipient_id": "123456",
                "person_id": "person-1",
            },
        )
    ]


@pytest.mark.asyncio
async def test_record_requires_exact_revision_and_batch_before_reader_or_transport():
    reader_calls = []
    transport_calls = []
    sender = NapCatHttpSender(
        FakeQqService(),
        record_media_reader=lambda ref, **lineage: reader_calls.append((ref, lineage)),
        record_transport_verified=True,
        transport=lambda *args: transport_calls.append(args),
    )
    record_ref = {
        "artifact_id": "artifact-1",
        "sha256": "a" * 64,
        "voice_version": "default@1",
        "approval_ref": "approval-1",
    }
    payload = _payload("record", record_ref=record_ref)
    payload.pop("expected_revision")

    with pytest.raises(QqError, match="会话版本或试用批次"):
        await sender.send(payload)

    assert reader_calls == []
    assert transport_calls == []


@pytest.mark.asyncio
async def test_record_hash_mismatch_is_rejected_before_transport():
    transport_calls = []
    sender = NapCatHttpSender(
        FakeQqService(),
        record_media_reader=lambda ref, **lineage: ControlledRecordMedia(b"unmatched", "audio/wav"),
        record_transport_verified=True,
        transport=lambda *args: transport_calls.append(args),
    )
    record_ref = {
        "artifact_id": "artifact-1",
        "sha256": "a" * 64,
        "voice_version": "default@1",
        "approval_ref": "approval-1",
    }

    with pytest.raises(QqError, match="SHA-256"):
        await sender.send(_payload("record", record_ref=record_ref))

    assert transport_calls == []


@pytest.mark.asyncio
async def test_record_rejection_receipt_has_record_specific_code():
    content = b"RIFF\x04\x00\x00\x00WAVE"
    record_ref = {
        "artifact_id": "artifact-1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "voice_version": "default@1",
        "approval_ref": "approval-1",
    }
    sender = NapCatHttpSender(
        FakeQqService(),
        record_media_reader=lambda ref, **lineage: ControlledRecordMedia(content, "audio/wav"),
        record_transport_verified=True,
        transport=lambda *args: (200, b'{"retcode":1200,"status":"failed","data":{}}'),
    )

    receipt = await sender.send(_payload("record", record_ref=record_ref))

    assert receipt.outcome == "rejected"
    assert receipt.result_code == "onebot_record_rejected_1200"
    assert receipt.no_side_effect_proven is True


@pytest.mark.asyncio
async def test_text_send_keeps_existing_wire_shape_and_receipt_code():
    transport_calls = []

    def transport(url, headers, body, timeout):
        transport_calls.append(json.loads(body))
        return 200, b'{"retcode":0,"status":"ok","data":{"message_id":17}}'

    sender = NapCatHttpSender(FakeQqService(), transport=transport)

    receipt = await sender.send(_payload())

    assert receipt.outcome == "sent"
    assert receipt.result_code == "onebot_sent"
    assert transport_calls == [{"user_id": 123456, "message": "hello"}]
