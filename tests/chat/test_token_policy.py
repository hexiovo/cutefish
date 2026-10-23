from __future__ import annotations

import json
import hashlib
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from cutefish.chat.prompt import PromptBuilder
import cutefish.chat.token_policy as token_policy_module
from cutefish.chat.token_policy import TokenEstimate, TokenPolicy


class CharacterTokenPolicy:
    def estimate_messages(self, messages: list[dict[str, str]]) -> TokenEstimate:
        total = sum(len(item["role"]) + len(item["content"]) for item in messages)
        return TokenEstimate(total, "estimated")

    def fingerprint(self):
        return {"test": "character-count"}


def test_prompt_builder_uses_token_targets_and_records_separate_body_size():
    builder = PromptBuilder(token_policy=CharacterTokenPolicy())
    result = builder.build(
        person_role="contact",
        purpose="production",
        history=[
            {"role": "user", "content": "A" * 20_000},
            {"role": "assistant", "content": "B" * 20_000},
            {"role": "user", "content": "latest"},
        ],
        persona_content=None,
        memories=[],
    )

    assert result.token_target == 32_768
    assert result.estimated_input_tokens <= result.token_target
    assert result.token_estimate_status == "estimated"
    assert result.dropped_history_count == 1
    assert result.utf8_bytes == PromptBuilder._utf8_size(result.messages)
    assert result.utf8_bytes < PromptBuilder.MAX_REQUEST_BODY_BYTES


def test_prompt_builder_uses_owner_token_target():
    result = PromptBuilder(token_policy=CharacterTokenPolicy()).build(
        person_role="owner",
        purpose="production",
        history=[{"role": "user", "content": "hello"}],
        persona_content=None,
        memories=[],
    )

    assert result.token_target == 131_072
    assert result.token_estimate_status == "estimated"


def test_tokenizer_failure_is_explicitly_partial_and_uses_byte_fallback(tmp_path):
    policy = TokenPolicy(manifest_path=tmp_path / "missing-manifest.json", model_root=None)

    estimate = policy.estimate_messages([{"role": "user", "content": "你好"}])

    assert estimate.status == "partial"
    assert estimate.tokens >= len("你好".encode("utf-8"))
    assert policy.status()["state"] == "partial"
    assert policy.status()["hard_cap"] is False


def test_tokenizer_manifest_rejects_unapproved_resource_hash():
    project_root = Path(__file__).resolve().parents[2]
    manifest_path = project_root / "config" / "tokenizers" / "deepseek-v4" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["file"]["sha256"] = "0" * 64

    with pytest.raises(RuntimeError, match="tokenizer_manifest_invalid"):
        TokenPolicy._validate_manifest(payload)


def test_tokenizer_parses_the_same_verified_bytes_without_reopening_file(tmp_path, monkeypatch):
    raw = b'{"version":"1.0","model":{"type":"BPE"}}'
    resource_path = tmp_path / "tokenizer.json"
    resource_path.write_bytes(raw)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    captured: list[str] = []

    class RecordingTokenizer:
        @classmethod
        def from_str(cls, source: str):
            captured.append(source)
            return cls()

        @classmethod
        def from_file(cls, _path: str):
            raise AssertionError("tokenizer loader must not reopen the resource path")

        def get_vocab_size(self, *, with_added_tokens: bool):
            assert with_added_tokens is False
            return 128_000

    original_read_text = Path.read_text
    original_open = Path.open
    read_limits: list[int] = []

    class RecordingStream:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def read(self, size: int = -1):
            read_limits.append(size)
            return self.wrapped.read(size)

    def read_text(path: Path, *args, **kwargs):
        if path.resolve() == resource_path.resolve():
            raise AssertionError("verified tokenizer bytes must be parsed in memory")
        return original_read_text(path, *args, **kwargs)

    def open_path(path: Path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path.resolve() == resource_path.resolve() and mode == "rb":
            return RecordingStream(stream)
        return stream

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(token_policy_module, "TOKENIZER_FILE_PATH", "tokenizer.json")
    monkeypatch.setattr(token_policy_module, "TOKENIZER_FILE_BYTES", len(raw))
    monkeypatch.setattr(token_policy_module, "TOKENIZER_FILE_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(TokenPolicy, "_validate_manifest", staticmethod(lambda _payload: None))
    monkeypatch.setattr(TokenPolicy, "_validate_tokenizer_json", staticmethod(lambda _payload: None))
    monkeypatch.setattr(token_policy_module.importlib.metadata, "version", lambda _name: "0.23.3")
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=RecordingTokenizer))

    resource_path.write_bytes(raw + b"x")
    policy = TokenPolicy(manifest_path=manifest_path, model_root=tmp_path)
    with pytest.raises(RuntimeError, match="tokenizer_size_mismatch"):
        policy._load_tokenizer()
    assert read_limits == [len(raw) + 1]
    assert captured == []

    resource_path.write_bytes(raw)
    tokenizer = policy._load_tokenizer()

    assert isinstance(tokenizer, RecordingTokenizer)
    assert read_limits == [len(raw) + 1, len(raw) + 1]
    assert captured == [raw.decode("utf-8")]
