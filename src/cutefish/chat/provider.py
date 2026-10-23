"""The single supported live text provider adapter: DeepSeek Chat Completions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from openai import AsyncOpenAI


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_OUTPUT_TOKEN_CAP = 4096
DEEPSEEK_CONTEXT_WINDOW = 1_048_576
DEEPSEEK_PRICE_VERSION = "deepseek-v4.1-flash-1017-cny-peak-2026-09-10-v1"
DEEPSEEK_SECRET_NAME = "deepseek_api_key"
DEEPSEEK_TEMPERATURE_MODE = "provider_default"

# Checked against the official model, completion, price, and token usage pages
# on 2026-10-23. Cost reservations use the peak, cache-miss input rate and cap
# the output at max_tokens; local text token estimates never reduce that bound.
OFFICIAL_MODEL_URL = "https://api-docs.deepseek.com/zh-cn/api/list-models/"
OFFICIAL_COMPLETION_URL = "https://api-docs.deepseek.com/api/create-chat-completion/"
OFFICIAL_PRICE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
OFFICIAL_TOKENIZER_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/token_usage/"
INPUT_MISS_CNY_PER_MILLION = Decimal("2")
INPUT_HIT_CNY_PER_MILLION = Decimal("0.04")
OUTPUT_CNY_PER_MILLION = Decimal("8")
RESERVATION_INPUT_TOKENS = DEEPSEEK_CONTEXT_WINDOW
RESERVATION_OUTPUT_TOKENS = DEEPSEEK_OUTPUT_TOKEN_CAP


class ProviderError(Exception):
    def __init__(self, code: str, message: str, *, uncertain: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.uncertain = uncertain


class NeedsCredentials(ProviderError):
    def __init__(self):
        super().__init__("needs_credentials", "请先在本机凭据设置中配置 DeepSeek API Key。", uncertain=False)


class UsageUnavailable(ProviderError):
    def __init__(self):
        super().__init__("usage_unavailable", "服务已返回内容，但没有可核验的token usage；费用预留保留为unknown。", uncertain=True)


@dataclass(frozen=True, slots=True)
class DeepSeekConfig:
    base_url: str = DEEPSEEK_BASE_URL
    model: str = DEEPSEEK_MODEL
    max_tokens: int = DEEPSEEK_OUTPUT_TOKEN_CAP
    max_retries: int = 0
    timeout_seconds: float = 60.0
    price_version: str = DEEPSEEK_PRICE_VERSION

    def fingerprint(self) -> str:
        payload = {
            "base_url": self.base_url,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "temperature_mode": DEEPSEEK_TEMPERATURE_MODE,
            "temperature": None,
            "reasoning": "disabled",
            "api_contract": "openai-compatible-chat.completions.create/v1",
            "price_version": self.price_version,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def conservative_cost(self) -> Decimal:
        hit = self.cache_hit_tokens
        miss = self.cache_miss_tokens
        if hit is None or miss is None or hit + miss != self.input_tokens:
            hit = 0
            miss = self.input_tokens
        return (
            (Decimal(miss) * INPUT_MISS_CNY_PER_MILLION)
            + (Decimal(hit) * INPUT_HIT_CNY_PER_MILLION)
            + (Decimal(self.output_tokens) * OUTPUT_CNY_PER_MILLION)
        ) / Decimal(1_000_000)

    def as_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "total_tokens": self.total_tokens,
            "cost_basis": "official_peak_rates",
        }


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider_response_id: str
    configured_model: str
    returned_model: str
    text: str
    finish_reason: str | None
    usage: ProviderUsage


class DeepSeekAdapter:
    """One-shot, non-streaming calls. Retries, tool loops, and auto-continuation are disabled."""

    def __init__(self, config: DeepSeekConfig | None = None):
        self.config = config or DeepSeekConfig()
        if self.config.model != DEEPSEEK_MODEL or self.config.price_version != DEEPSEEK_PRICE_VERSION:
            raise ProviderError("unpriced_model", "当前模型或价格版本未加入可核验allowlist。", uncertain=False)
        if self.config.max_tokens != DEEPSEEK_OUTPUT_TOKEN_CAP or self.config.max_retries != 0:
            raise ProviderError("unpriced_model_config", "当前输出上限或重试策略未加入可核验allowlist。", uncertain=False)

    @property
    def config_fingerprint(self) -> str:
        return self.config.fingerprint()

    async def complete(self, *, api_key: str | None, messages: list[dict[str, str]]) -> ProviderResult:
        if not api_key:
            raise NeedsCredentials()
        try:
            async with AsyncOpenAI(
                api_key=api_key,
                base_url=self.config.base_url,
                timeout=self.config.timeout_seconds,
                max_retries=0,
            ) as client:
                response = await client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    max_tokens=self.config.max_tokens,
                    # Temperature remains omitted until a supported explicit value
                    # is verified; the fingerprint records this provider-default policy.
                    extra_body={"thinking": {"type": "disabled"}},
                )
        except Exception as exc:
            # Once dispatched, even transport failures may have been billed.
            # Do not disclose exception text; callers retain the reservation.
            raise ProviderError("provider_outcome_unknown", "DeepSeek请求结果不确定，费用预留已保留，不能自动重发。") from exc

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ProviderError("provider_response_invalid", "DeepSeek响应缺少choices，费用状态需要人工核对。")
        choice = choices[0]
        message = getattr(choice, "message", None)
        final_text = getattr(message, "content", None)
        if not isinstance(final_text, str):
            raise ProviderError("provider_response_invalid", "DeepSeek没有返回最终content，费用状态需要人工核对。")
        usage = _usage_from_response(getattr(response, "usage", None))
        if usage is None:
            raise UsageUnavailable()
        request_id = getattr(response, "id", None)
        returned_model = getattr(response, "model", None)
        if not request_id or not returned_model:
            raise ProviderError("provider_evidence_missing", "DeepSeek响应缺少请求ID或模型证据，费用状态需要人工核对。")
        return ProviderResult(
            provider_response_id=str(request_id),
            configured_model=self.config.model,
            returned_model=str(returned_model),
            text=final_text,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=usage,
        )


def _usage_from_response(usage: Any) -> ProviderUsage | None:
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        return None
    if prompt_tokens < 0 or completion_tokens < 0:
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is None and details is not None:
        hit = getattr(details, "cached_tokens", None)
    miss = getattr(usage, "prompt_cache_miss_tokens", None)
    if not isinstance(hit, int) or not isinstance(miss, int):
        hit = None
        miss = None
    if hit is not None and (hit < 0 or miss < 0 or hit + miss != prompt_tokens):
        hit = None
        miss = None
    return ProviderUsage(prompt_tokens, completion_tokens, hit, miss)
