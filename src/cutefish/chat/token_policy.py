"""Local token estimates backed by the pinned DeepSeek tokenizer data."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cutefish.core.paths import HostPathResolver, PathPolicyError
from cutefish.core.settings import load_settings, project_root


TOKENIZER_RESOURCE_ID = "deepseek-v4-tokenizer"
TOKENIZER_LIBRARY = "tokenizers"
TOKENIZER_LIBRARY_VERSION = "0.23.3"
TOKENIZER_FILE_PATH = "deepseek-v4-tokenizer/tokenizer.json"
TOKENIZER_FILE_BYTES = 6_367_096
TOKENIZER_FILE_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"
TOKENIZER_MANIFEST_RELATIVE_PATH = Path("config/tokenizers/deepseek-v4/manifest.json")
_UNSET = object()


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    tokens: int
    status: str


class TokenPolicy:
    """Estimate message tokens; the count is policy guidance, never a hard model cap."""

    def __init__(self, *, manifest_path: Path | None = None, model_root: Path | None | object = _UNSET):
        self.manifest_path = manifest_path or (project_root() / TOKENIZER_MANIFEST_RELATIVE_PATH)
        if model_root is _UNSET:
            try:
                model_root = load_settings().model_root
            except Exception:
                model_root = None
        self.model_root = Path(model_root).expanduser() if model_root is not None else None
        self._tokenizer = None
        self._load_attempted = False
        self._load_error = "tokenizer_unavailable"

    def fingerprint(self) -> dict[str, str | int]:
        return {
            "resource_id": TOKENIZER_RESOURCE_ID,
            "manifest_path": TOKENIZER_MANIFEST_RELATIVE_PATH.as_posix(),
            "file_sha256": TOKENIZER_FILE_SHA256,
            "library": TOKENIZER_LIBRARY,
            "library_version": TOKENIZER_LIBRARY_VERSION,
            "fallback": "utf8_byte_upper_estimate_marked_partial",
            "target_semantics": "estimated_tokens_not_provider_hard_cap",
        }

    def status(self) -> dict[str, Any]:
        self._ensure_loaded()
        return {
            "state": "estimated" if self._tokenizer is not None else "partial",
            "resource_id": TOKENIZER_RESOURCE_ID,
            "library": TOKENIZER_LIBRARY,
            "library_version": TOKENIZER_LIBRARY_VERSION,
            "error_code": None if self._tokenizer is not None else self._load_error,
            "hard_cap": False,
        }

    def estimate_messages(self, messages: list[dict[str, str]]) -> TokenEstimate:
        text = self._serialized_messages(messages)
        self._ensure_loaded()
        if self._tokenizer is None:
            # UTF-8 bytes are retained only as a conservative fallback estimate;
            # callers receive partial status and must not describe it as token truth.
            return TokenEstimate(len(text.encode("utf-8")), "partial")
        encoded = self._tokenizer.encode(text, add_special_tokens=False)
        return TokenEstimate(len(encoded.ids), "estimated")

    @staticmethod
    def _serialized_messages(messages: list[dict[str, str]]) -> str:
        parts: list[str] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if not isinstance(role, str) or not isinstance(content, str):
                raise ValueError("prompt messages must contain text roles and content")
            parts.append(f"<{role}>\n{content}\n</{role}>")
        return "\n".join(parts)

    def _ensure_loaded(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True
        try:
            self._tokenizer = self._load_tokenizer()
            self._load_error = ""
        except Exception as exc:
            # A missing or changed local resource degrades to the explicit partial
            # fallback. It never triggers a download, AutoTokenizer, or remote code.
            self._load_error = getattr(exc, "code", None) or "tokenizer_resource_unavailable"

    def _load_tokenizer(self):
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self._validate_manifest(payload)
        if self.model_root is None:
            raise _TokenizerResourceError("model_root_unconfigured")
        resolver = HostPathResolver({"model_root": self.model_root})
        resource_path = resolver.resolve("model_root", TOKENIZER_FILE_PATH, must_exist=True)
        if not resource_path.is_file():
            raise _TokenizerResourceError("tokenizer_resource_unavailable")
        with resource_path.open("rb") as stream:
            resource_bytes = stream.read(TOKENIZER_FILE_BYTES + 1)
        if len(resource_bytes) != TOKENIZER_FILE_BYTES:
            raise _TokenizerResourceError("tokenizer_size_mismatch")
        digest = hashlib.sha256(resource_bytes).hexdigest()
        if digest != TOKENIZER_FILE_SHA256:
            raise _TokenizerResourceError("tokenizer_hash_mismatch")
        try:
            library_version = importlib.metadata.version(TOKENIZER_LIBRARY)
        except importlib.metadata.PackageNotFoundError as exc:
            raise _TokenizerResourceError("tokenizer_library_missing") from exc
        if library_version != TOKENIZER_LIBRARY_VERSION:
            raise _TokenizerResourceError("tokenizer_library_version_mismatch")

        resource_text = resource_bytes.decode("utf-8")
        resource_json = json.loads(resource_text)
        self._validate_tokenizer_json(resource_json)
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_str(resource_text)
        if tokenizer.get_vocab_size(with_added_tokens=False) != 128_000:
            raise _TokenizerResourceError("tokenizer_vocab_mismatch")
        return tokenizer

    @staticmethod
    def _validate_manifest(payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise _TokenizerResourceError("tokenizer_manifest_invalid")
        if payload.get("resource_id") != TOKENIZER_RESOURCE_ID:
            raise _TokenizerResourceError("tokenizer_manifest_invalid")
        file_entry = payload.get("file")
        if not isinstance(file_entry, dict) or any(
            file_entry.get(name) != expected
            for name, expected in (
                ("path", TOKENIZER_FILE_PATH),
                ("format", "tokenizer.json"),
                ("bytes", TOKENIZER_FILE_BYTES),
                ("sha256", TOKENIZER_FILE_SHA256),
            )
        ):
            raise _TokenizerResourceError("tokenizer_manifest_invalid")
        source = payload.get("source")
        if not isinstance(source, dict) or any(
            source.get(name) != expected
            for name, expected in (
                ("url", "https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip"),
                ("archive_bytes", 1_911_504),
                ("archive_sha256", "e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495"),
                ("version_status", "unversioned"),
                ("official_checksum", None),
            )
        ):
            raise _TokenizerResourceError("tokenizer_manifest_invalid")
        tokenizer = payload.get("tokenizer")
        if not isinstance(tokenizer, dict) or any(
            tokenizer.get(name) != expected
            for name, expected in (
                ("library", TOKENIZER_LIBRARY),
                ("locked_version", TOKENIZER_LIBRARY_VERSION),
                ("json_version", "1.0"),
                ("model_type", "BPE"),
                ("vocab_size", 128_000),
                ("merges_count", 127_741),
            )
        ):
            raise _TokenizerResourceError("tokenizer_manifest_invalid")

    @staticmethod
    def _validate_tokenizer_json(payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("version") != "1.0":
            raise _TokenizerResourceError("tokenizer_data_invalid")
        model = payload.get("model")
        if (
            not isinstance(model, dict)
            or model.get("type") != "BPE"
            or not isinstance(model.get("vocab"), dict)
            or len(model["vocab"]) != 128_000
            or not isinstance(model.get("merges"), list)
            or len(model["merges"]) != 127_741
        ):
            raise _TokenizerResourceError("tokenizer_data_invalid")


class _TokenizerResourceError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
