"""Read and verify the fixed, owner-requested confirmed-design bundle."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024
MAX_SOURCE_BYTES = 128 * 1024

EXPECTED_SOURCES = (
    ("ROLE_DRAFT.md", "sourcepack:v4.9/persona/ROLE_DRAFT.md", "9173dfb41f7e529c730469cd5ca808beb95d4eca96d0cb8edc075bfe9d601345"),
    (
        "CONFIRMED_DESIGN.md",
        "sourcepack:v4.9/user/CONFIRMED_DESIGN.md",
        "9d1e5d60398a967143a6d0ec5622e36eb53b6c355fd364461bfb031891994873",
    ),
)

EXPECTED_CONFIRMED_REFS = {
    "relationship_refs": ["sourcepack:v4.9/user/CONFIRMED_DESIGN.md#relationship"],
    "permission_refs": ["sourcepack:v4.9/user/CONFIRMED_DESIGN.md#owner-tools-contact-isolation"],
    "context_target_refs": ["sourcepack:v4.9/user/CONFIRMED_DESIGN.md#context-targets"],
}
EXPECTED_CONSTRAINT_REFS = (
    "sourcepack:v4.9/user/CONFIRMED_DESIGN.md#relationship",
    "sourcepack:v4.9/user/CONFIRMED_DESIGN.md#owner-tools-contact-isolation",
    "sourcepack:v4.9/user/CONFIRMED_DESIGN.md#interaction-preferences",
)
EXPECTED_BUNDLE_HASH = "7dd629351c07e8aa89bb4d3b50ebd223f446ba41759a0758c6b95c86a0aeed52"


class ConfirmedDesignBundleError(ValueError):
    """A bundle is absent, unsafe, or does not match the approved source set."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ConfirmedDesignBundle:
    schema_version: int
    bundle_hash: str
    sources: tuple[dict[str, str], ...]
    confirmed_refs: dict[str, list[str]]
    confirmed_constraint_refs: tuple[str, ...]
    content: str

    @property
    def source_hashes(self) -> dict[str, str]:
        return {source["basename"]: source["sha256"] for source in self.sources}

    @property
    def source_refs(self) -> dict[str, str]:
        return {source["basename"]: source["source_ref"] for source in self.sources}


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None and isjunction(path):
        return True
    if os.name == "nt":
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
        except OSError:
            return False
    return False


def _assert_no_link_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            current.lstat()
        except FileNotFoundError as exc:
            raise ConfirmedDesignBundleError("bundle_unavailable") from exc
        except OSError as exc:
            raise ConfirmedDesignBundleError("bundle_unavailable") from exc
        if _is_link_or_junction(current):
            raise ConfirmedDesignBundleError("unsafe_bundle")


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConfirmedDesignBundleError("bundle_unavailable") from exc
    if _is_link_or_junction(path) or not stat.S_ISREG(metadata.st_mode):
        raise ConfirmedDesignBundleError("unsafe_bundle")
    if metadata.st_size < 0 or metadata.st_size > maximum_bytes:
        raise ConfirmedDesignBundleError("bundle_invalid")
    try:
        content = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise ConfirmedDesignBundleError("bundle_unavailable") from exc
    if _is_link_or_junction(path) or not stat.S_ISREG(after.st_mode) or len(content) > maximum_bytes:
        raise ConfirmedDesignBundleError("unsafe_bundle")
    return content


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _canonical_bundle_hash(manifest_without_hash: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            manifest_without_hash,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ConfirmedDesignBundleError("bundle_invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def load_confirmed_design_bundle(root: Path | None) -> ConfirmedDesignBundle:
    """Load only the declared schema and two fixed files; never enumerate a root."""
    if root is None:
        raise ConfirmedDesignBundleError("not_configured")
    if not isinstance(root, Path) or not root.is_absolute():
        raise ConfirmedDesignBundleError("bundle_invalid")

    _assert_no_link_components(root)
    if _is_link_or_junction(root) or not root.is_dir():
        raise ConfirmedDesignBundleError("bundle_unavailable")

    manifest_path = root / MANIFEST_NAME
    manifest_bytes = _read_regular_file(manifest_path, maximum_bytes=MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfirmedDesignBundleError("bundle_invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "bundle_hash",
        "sources",
        "confirmed_refs",
        "confirmed_constraints",
    }:
        raise ConfirmedDesignBundleError("bundle_invalid")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ConfirmedDesignBundleError("bundle_invalid")

    raw_sources = manifest.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != len(EXPECTED_SOURCES):
        raise ConfirmedDesignBundleError("bundle_invalid")
    sources: list[dict[str, str]] = []
    for raw, (expected_name, expected_ref, expected_hash) in zip(raw_sources, EXPECTED_SOURCES, strict=True):
        if not isinstance(raw, dict) or set(raw) != {"basename", "sha256", "source_ref"}:
            raise ConfirmedDesignBundleError("bundle_invalid")
        if raw.get("basename") != expected_name or raw.get("source_ref") != expected_ref:
            raise ConfirmedDesignBundleError("bundle_invalid")
        source_hash = raw.get("sha256")
        if not isinstance(source_hash, str) or len(source_hash) != 64 or any(ch not in "0123456789abcdef" for ch in source_hash):
            raise ConfirmedDesignBundleError("bundle_invalid")
        if source_hash != expected_hash:
            raise ConfirmedDesignBundleError("source_hash_mismatch")
        sources.append({"basename": expected_name, "sha256": source_hash, "source_ref": expected_ref})

    confirmed_refs = manifest.get("confirmed_refs")
    if confirmed_refs != EXPECTED_CONFIRMED_REFS:
        raise ConfirmedDesignBundleError("bundle_invalid")
    if not isinstance(confirmed_refs, dict) or set(confirmed_refs) != set(EXPECTED_CONFIRMED_REFS):
        raise ConfirmedDesignBundleError("bundle_invalid")
    if any(not isinstance(items, list) or any(not isinstance(item, str) for item in items) for items in confirmed_refs.values()):
        raise ConfirmedDesignBundleError("bundle_invalid")

    raw_constraints = manifest.get("confirmed_constraints")
    if not isinstance(raw_constraints, list) or len(raw_constraints) > len(EXPECTED_CONSTRAINT_REFS):
        raise ConfirmedDesignBundleError("bundle_invalid")
    constraints: list[dict[str, str]] = []
    for raw in raw_constraints:
        if not isinstance(raw, dict) or set(raw) != {"source_ref", "source_quote"}:
            raise ConfirmedDesignBundleError("bundle_invalid")
        source_ref = raw.get("source_ref")
        source_quote = raw.get("source_quote")
        if (
            not isinstance(source_ref, str)
            or source_ref not in EXPECTED_CONSTRAINT_REFS
            or source_ref in {item["source_ref"] for item in constraints}
            or not isinstance(source_quote, str)
            or not source_quote.strip()
            or len(source_quote) > MAX_SOURCE_BYTES
        ):
            raise ConfirmedDesignBundleError("bundle_invalid")
        constraints.append({"source_ref": source_ref, "source_quote": source_quote})
    if [item["source_ref"] for item in constraints] != [
        ref for ref in EXPECTED_CONSTRAINT_REFS if ref in {item["source_ref"] for item in constraints}
    ]:
        raise ConfirmedDesignBundleError("bundle_invalid")

    bundle_hash = manifest.get("bundle_hash")
    if not isinstance(bundle_hash, str) or len(bundle_hash) != 64 or any(ch not in "0123456789abcdef" for ch in bundle_hash):
        raise ConfirmedDesignBundleError("bundle_invalid")
    manifest_without_hash = {key: value for key, value in manifest.items() if key != "bundle_hash"}
    if _canonical_bundle_hash(manifest_without_hash) != bundle_hash or bundle_hash != EXPECTED_BUNDLE_HASH:
        raise ConfirmedDesignBundleError("bundle_hash_mismatch")

    source_texts: list[str] = []
    for source in sources:
        source_path = root / source["basename"]
        raw_content = _read_regular_file(source_path, maximum_bytes=MAX_SOURCE_BYTES)
        if hashlib.sha256(raw_content).hexdigest() != source["sha256"]:
            raise ConfirmedDesignBundleError("source_hash_mismatch")
        try:
            source_texts.append(raw_content.decode("utf-8-sig"))
        except UnicodeError as exc:
            raise ConfirmedDesignBundleError("bundle_invalid") from exc
    confirmed_text = source_texts[1]
    for constraint in constraints:
        if constraint["source_quote"] not in confirmed_text:
            raise ConfirmedDesignBundleError("constraint_quote_mismatch")

    content = "## ROLE_DRAFT.md\n\n" + source_texts[0]
    if constraints:
        content += "\n\n## 已确认的角色相关约束\n\n" + "\n\n".join(
            constraint["source_quote"] for constraint in constraints
        )
    return ConfirmedDesignBundle(
        schema_version=MANIFEST_SCHEMA_VERSION,
        bundle_hash=bundle_hash,
        sources=tuple(sources),
        confirmed_refs={key: list(value) for key, value in confirmed_refs.items()},
        confirmed_constraint_refs=tuple(item["source_ref"] for item in constraints),
        content=content,
    )
