"""Explicit OneBot v11 private text/record sender; absent from default Context."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cutefish.core.outbox import SenderReceipt

from .service import QqError, _safe_external_id


@dataclass(frozen=True, slots=True)
class ControlledRecordMedia:
    """Verified media bytes returned by the extension's registered-asset reader."""

    content: bytes
    media_type: str


_RECORD_REF_FIELDS = frozenset({"artifact_id", "sha256", "voice_version", "approval_ref"})
_RECORD_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_MAX_RECORD_BYTES = 8 * 1024 * 1024
_RECORD_MEDIA_TYPE = "audio/wav"


class NapCatHttpSender:
    """Send one approved private text segment through NapCat's OneBot HTTP API.

    Construction alone does not perform network I/O. Each call revalidates its exact
    account/person/recipient target and performs a single request without redirects,
    proxies, or transport retries. The application Context intentionally uses
    ``sender=None`` unless a later, explicit local Trial configuration changes that.
    A verified record reader receives the typed record reference and exact outbox
    lineage as keyword arguments so it can re-resolve the approved artifact.
    """

    def __init__(
        self,
        qq_service,
        *,
        timeout_seconds: float = 5.0,
        transport: Callable | None = None,
        record_media_reader: Callable[..., Any] | None = None,
        record_transport_verified: bool = False,
    ):
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0.1 and 30")
        if type(record_transport_verified) is not bool:
            raise ValueError("record_transport_verified must be a bool")
        self.qq_service = qq_service
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.record_media_reader = record_media_reader
        # This remains false unless the local NapCat audio format/codec is explicitly
        # verified. No application context enables it by default.
        self._record_transport_verified = record_transport_verified

    @property
    def record_transport_verified(self) -> bool:
        return self._record_transport_verified

    @property
    def record_transport_status(self) -> Literal["verified", "disabled_unverified"]:
        return "verified" if self._record_transport_verified else "disabled_unverified"

    async def send(self, payload: dict[str, Any]) -> SenderReceipt:
        if not isinstance(payload, dict):
            raise QqError("invalid_sender_payload", "QQ外发数据格式无效。", 422)
        account_id = _safe_external_id(payload.get("account_id"))
        recipient_id = _safe_external_id(payload.get("recipient_id"))
        person_id = payload.get("person_id")
        conversation_id = payload.get("conversation_id")
        attempt_id = payload.get("attempt_id")
        message_type = payload.get("message_type", "text")
        message = payload.get("payload")
        if (
            not account_id
            or not recipient_id
            or not isinstance(person_id, str)
            or not person_id
            or not isinstance(conversation_id, str)
            or not conversation_id
            or not isinstance(attempt_id, str)
            or not attempt_id
        ):
            raise QqError("invalid_sender_payload", "QQ外发数据不完整或超出文本上限。", 422)
        if not recipient_id.isascii() or not recipient_id.isdecimal():
            raise QqError("invalid_sender_payload", "OneBot私聊目标必须是数字QQ身份。", 422)
        try:
            onebot_user_id = int(recipient_id)
        except ValueError as exc:
            raise QqError("invalid_sender_payload", "OneBot私聊目标必须是数字QQ身份。", 422) from exc
        if onebot_user_id < 0:
            raise QqError("invalid_sender_payload", "OneBot私聊目标必须是有效QQ身份。", 422)

        if message_type == "text":
            if (
                not isinstance(message, str)
                or not message
                or len(message) > 4000
                or "[CQ:" in message.upper()
                or "record_ref" in payload
            ):
                raise QqError("invalid_sender_payload", "QQ外发文本不完整或包含不支持的消息段。", 422)
            onebot_message: str | list[dict[str, Any]] = message
        elif message_type == "record":
            forbidden_media_keys = {
                "url",
                "path",
                "file",
                "base64",
                "media",
                "media_url",
                "media_path",
                "media_base64",
                "record",
                "audio",
                "audio_url",
                "audio_path",
                "bytes",
                "content",
                "data",
            }
            if any(
                isinstance(key, str) and key.casefold() in forbidden_media_keys
                for key in payload
            ):
                raise QqError("invalid_sender_payload", "语音外发不能包含URL、路径或原始媒体数据。", 422)
            record_ref = _validate_record_ref(payload.get("record_ref"))
            # Never treat the legacy text field as a media locator or payload. Core
            # stores voice metadata in the typed record_ref and emits an empty display
            # payload for this message kind.
            if message not in (None, ""):
                raise QqError("invalid_sender_payload", "语音外发必须使用受控的record_ref，不能借用普通payload。", 422)
            if not self.record_transport_verified:
                return _record_preflight_rejection(account_id, attempt_id)
            expected_revision = payload.get("expected_revision")
            batch_id = payload.get("batch_id")
            if (
                type(expected_revision) is not int
                or expected_revision < 0
                or not isinstance(batch_id, str)
                or not batch_id
                or len(batch_id) > 128
            ):
                raise QqError("invalid_sender_payload", "语音外发缺少精确的会话版本或试用批次绑定。", 422)
            record_lineage = {
                "conversation_id": conversation_id,
                "expected_revision": expected_revision,
                "batch_id": batch_id,
                "account_id": account_id,
                "recipient_id": recipient_id,
                "person_id": person_id,
            }
            onebot_message = None
        else:
            raise QqError("invalid_sender_payload", "QQ外发消息类型不受支持。", 422)

        guard = {
            "account_id": account_id,
            "recipient_id": recipient_id,
            "person_id": person_id,
            "conversation_id": conversation_id,
            "delivery_mode": "qq",
        }
        credentials = await self.qq_service.sender_credentials_for_delivery(guard)
        if message_type == "record":
            # Read media only after rechecking the exact target and credentials.
            onebot_message = await self._record_message(record_ref, lineage=record_lineage)
        url = _private_message_url(credentials["service_url"])
        headers = {
            "Authorization": f"Bearer {credentials['access_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps(
            {"user_id": onebot_user_id, "message": onebot_message},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        if self.transport is None:
            status, response_body = await asyncio.to_thread(
                _request_once,
                url,
                headers,
                body,
                self.timeout_seconds,
            )
        else:
            result = self.transport(url, headers, body, self.timeout_seconds)
            if hasattr(result, "__await__"):
                result = await result
            status, response_body = result
        return _receipt_from_response(
            account_id=account_id,
            attempt_id=attempt_id,
            status=status,
            response_body=response_body,
            message_type=message_type,
        )

    async def _record_message(
        self,
        record_ref: dict[str, str],
        *,
        lineage: dict[str, Any],
    ) -> list[dict[str, Any]]:
        reader = self.record_media_reader
        if not callable(reader):
            raise QqError("record_media_unavailable", "语音素材读取器未配置。", 409)
        media = reader(record_ref, **lineage)
        if hasattr(media, "__await__"):
            media = await media
        if not isinstance(media, ControlledRecordMedia):
            raise QqError("invalid_record_media", "语音素材必须由登记素材读取器以受控类型返回。", 422)
        content = media.content
        if (
            not isinstance(content, bytes)
            or not content
            or len(content) > _MAX_RECORD_BYTES
            or media.media_type != _RECORD_MEDIA_TYPE
            or hashlib.sha256(content).hexdigest() != record_ref["sha256"]
        ):
            raise QqError("invalid_record_media", "语音素材格式、长度或SHA-256校验无效。", 422)
        encoded = base64.b64encode(content).decode("ascii")
        return [{"type": "record", "data": {"file": f"base64://{encoded}"}}]


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_once(
    url: str, headers: dict[str, str], body: bytes, timeout_seconds: float
) -> tuple[int, bytes]:
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return int(response.status), response.read(65537)
    except HTTPError as exc:
        # An HTTP error is still a response, but its side-effect semantics are not
        # specified by OneBot. Keep it ambiguous unless the JSON body proves reject.
        return int(exc.code), exc.read(65537)
    except (URLError, TimeoutError, OSError) as exc:
        # The connection may have failed after NapCat accepted the message.
        raise RuntimeError("NapCat transport outcome is unknown") from exc


def _private_message_url(service_url: str) -> str:
    parsed = urlsplit(service_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise QqError("service_url_must_be_local", "NapCat发送器只允许本机HTTP服务。", 403)
    path = parsed.path.rstrip("/")
    if not path.endswith("/send_private_msg"):
        path += "/send_private_msg"
    return urlunsplit((parsed.scheme, parsed.netloc, path or "/send_private_msg", "", ""))


def _validate_record_ref(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _RECORD_REF_FIELDS:
        raise QqError("invalid_record_ref", "OneBot语音外发只接受登记素材及批准版本引用。", 422)
    artifact_id = _safe_external_id(value.get("artifact_id"))
    voice_version = _safe_external_id(value.get("voice_version"))
    approval_ref = _safe_external_id(value.get("approval_ref"))
    sha256 = value.get("sha256")
    identifiers = (artifact_id, voice_version, approval_ref)
    if (
        any(item is None or _RECORD_ID_RE.fullmatch(item) is None for item in identifiers)
        or not isinstance(sha256, str)
        or _RECORD_SHA256_RE.fullmatch(sha256) is None
    ):
        raise QqError("invalid_record_ref", "OneBot语音外发引用格式无效。", 422)
    return {
        "artifact_id": artifact_id,
        "sha256": sha256,
        "voice_version": voice_version,
        "approval_ref": approval_ref,
    }


def _record_preflight_rejection(account_id: str, attempt_id: str) -> SenderReceipt:
    return SenderReceipt(
        outcome="rejected",
        evidence_ref=f"napcat://{account_id}/attempts/{attempt_id}",
        result_code="needs_transport_format_verification",
        no_side_effect_proven=True,
    )


def _receipt_from_response(
    *,
    account_id: str,
    attempt_id: str,
    status: int,
    response_body: bytes,
    message_type: str = "text",
) -> SenderReceipt:
    if status < 200 or status >= 300 or len(response_body) > 65536:
        raise RuntimeError("NapCat transport outcome is unknown")
    try:
        data = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("NapCat transport outcome is unknown") from exc
    if not isinstance(data, dict):
        raise RuntimeError("NapCat transport outcome is unknown")

    retcode = data.get("retcode")
    api_status = data.get("status")
    response_data = data.get("data")
    evidence_ref = f"napcat://{account_id}/attempts/{attempt_id}"
    receipt_kind = "onebot_record" if message_type == "record" else "onebot"
    if (
        type(retcode) is int
        and retcode == 0
        and api_status == "ok"
        and isinstance(response_data, dict)
    ):
        message_id = _safe_external_id(response_data.get("message_id"))
        if message_id:
            return SenderReceipt(
                outcome="sent",
                evidence_ref=evidence_ref,
                remote_receipt_id=message_id,
                result_code=f"{receipt_kind}_sent",
            )
    if type(retcode) is int and retcode > 0 and api_status == "failed" and not (
        isinstance(response_data, dict) and response_data.get("message_id") is not None
    ):
        return SenderReceipt(
            outcome="rejected",
            evidence_ref=evidence_ref,
            result_code=f"{receipt_kind}_rejected_{retcode}",
            no_side_effect_proven=True,
        )
    raise RuntimeError("NapCat transport outcome is unknown")
