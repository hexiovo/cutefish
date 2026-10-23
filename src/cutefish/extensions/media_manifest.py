"""Content-hash manifest helpers for explicit, already-authorized media items."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import BinaryIO, Iterable


MEDIA_MANIFEST_VERSION = 1
MEDIA_EXCLUSION_REASONS = frozenset(
    {
        "unknown_permission",
        "private_source",
        "out_of_scope",
        "unsupported_format",
        "size_limit",
        "temporary_material",
        "user_excluded",
        "missing_source",
    }
)


def validate_relative_path(relative_path: str) -> str:
    raw = str(relative_path)
    windows_path = PureWindowsPath(raw)
    posix_path = PurePosixPath(raw)
    if (
        not raw
        or "\x00" in raw
        or windows_path.is_absolute()
        or windows_path.drive
        or posix_path.is_absolute()
        or raw.startswith(("\\\\", "//"))
    ):
        raise ValueError("manifest path must be a non-empty relative path")
    parts = [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or ":" in part for part in parts):
        raise ValueError("manifest path cannot escape its logical root")
    return "/".join(parts)


def hash_stream(stream: BinaryIO, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    if chunk_size < 4096:
        raise ValueError("chunk_size must be at least 4096 bytes")
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("media stream must be binary")
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True, slots=True)
class MediaManifestEntry:
    item_id: str
    kind: str
    root_key: str
    relative_path: str
    sha256: str
    size_bytes: int
    source_refs: tuple[dict[str, str], ...] = ()
    source_scope: str = "unknown"
    share_allowed: bool = False
    excluded: bool = False
    exclusion_reason: str | None = None

    def __post_init__(self):
        if not self.item_id or not self.kind:
            raise ValueError("manifest item_id and kind are required")
        if self.root_key not in {"room_root", "gallery_root", "data_root"}:
            raise ValueError("unsupported logical root for media manifest")
        object.__setattr__(self, "relative_path", validate_relative_path(self.relative_path))
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise ValueError("sha256 must contain 64 lowercase hexadecimal characters")
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        if self.excluded and self.exclusion_reason not in MEDIA_EXCLUSION_REASONS:
            raise ValueError("excluded items require a recognized exclusion reason")
        if not self.excluded and self.exclusion_reason is not None:
            raise ValueError("included items cannot carry an exclusion reason")


@dataclass(frozen=True, slots=True)
class MediaManifest:
    items: tuple[MediaManifestEntry, ...]
    manifest_version: int = MEDIA_MANIFEST_VERSION

    def __post_init__(self):
        if self.manifest_version != MEDIA_MANIFEST_VERSION:
            raise ValueError("unsupported media manifest version")
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("manifest item ids must be unique")

    def to_json(self) -> str:
        payload = {
            "manifest_version": self.manifest_version,
            "items": [asdict(item) for item in self.items],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_manifest(items: Iterable[MediaManifestEntry]) -> MediaManifest:
    return MediaManifest(items=tuple(items))
