from __future__ import annotations

from types import SimpleNamespace

import pytest

import cutefish.chat.provider as provider_module
from cutefish.chat.provider import (
    DeepSeekAdapter,
    DeepSeekVisionRequest,
    ProviderError,
    _usage_from_response,
)


def test_inline_image_request_builds_typed_data_url_and_supported_detail():
    request = DeepSeekVisionRequest(
        prompt="描述可见内容",
        image_bytes=b"\x89PNG\r\n\x1a\nfixture",
        mime_type="image/png",
        detail="original",
    )

    message = request.to_message()

    assert message["role"] == "user"
    assert message["content"][0] == {"type": "text", "text": "描述可见内容"}
    image_part = message["content"][1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert image_part["image_url"]["detail"] == "original"
    assert request.request_bytes_upper_bound < provider_module.MAX_PROVIDER_REQUEST_BYTES


def test_provider_usage_rejects_boolean_token_counts():
    usage = SimpleNamespace(prompt_tokens=True, completion_tokens=1)

    assert _usage_from_response(usage) is None


@pytest.mark.asyncio
async def test_vision_adapter_dispatches_one_inline_message_and_rejects_unknown_usage(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                id="response-1",
                model="deepseek-flash",
                choices=[SimpleNamespace(message=SimpleNamespace(content="一只猫"), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=True, completion_tokens=2),
            )

    monkeypatch.setattr(provider_module, "AsyncOpenAI", FakeClient)
    request = DeepSeekVisionRequest(
        prompt="描述图片",
        image_bytes=b"jpeg fixture",
        mime_type="image/jpeg",
        detail="low",
    )

    with pytest.raises(ProviderError) as exc_info:
        await DeepSeekAdapter().observe_image(api_key="fake-key", request=request)

    assert exc_info.value.code == "usage_unavailable"
    assert exc_info.value.uncertain is True
    assert len(calls) == 1
    content = calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
