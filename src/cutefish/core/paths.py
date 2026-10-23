"""Logical-root path access with traversal and link-escape checks."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Mapping


class PathPolicyError(ValueError):
    pass


ROOT_KEYS = frozenset(
    {
        "data_root",
        "gallery_root",
        "room_root",
        "backup_root",
        "cache_root",
        "model_root",
        "temp_root",
    }
)


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction and is_junction(path):
        return True
    if os.name == "nt":
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            # Reject any reparse point, including junctions on Python 3.11 where
            # os.path.isjunction is not available.
            return bool(attributes & 0x400)
        except OSError:
            return False
    return False


def _assert_no_link_components(path: Path) -> None:
    anchor = Path(path.anchor)
    current = anchor
    for part in path.parts[1:]:
        current = current / part
        if _is_link_or_junction(current):
            raise PathPolicyError("路径中包含符号链接或目录联接")


class HostPathResolver:
    def __init__(self, roots: Mapping[str, str | Path]):
        normalized: dict[str, Path] = {}
        for key, value in roots.items():
            if key not in ROOT_KEYS:
                continue
            candidate = Path(value).expanduser()
            if candidate.is_absolute():
                normalized[key] = candidate
        self._roots = normalized

    @property
    def root_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._roots))

    def configured(self, root_key: str) -> bool:
        return root_key in self._roots

    def root_path(self, root_key: str) -> Path:
        if root_key not in ROOT_KEYS:
            raise PathPolicyError("未知逻辑路径根")
        root = self._roots.get(root_key)
        if root is None:
            raise PathPolicyError("逻辑路径根尚未配置")
        if not root.is_absolute():
            raise PathPolicyError("逻辑路径根必须是绝对路径")
        _assert_no_link_components(root)
        return root

    def status(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for key in sorted(ROOT_KEYS):
            root = self._roots.get(key)
            items.append(
                {
                    "root_key": key,
                    "configured": root is not None,
                    "available": bool(root and root.is_dir() and not _is_link_or_junction(root)),
                    "display_path": str(root) if root is not None else None,
                }
            )
        return items

    def resolve(
        self,
        root_key: str,
        relative_path: str | Path = "",
        *,
        must_exist: bool = False,
        for_write: bool = False,
    ) -> Path:
        root = self.root_path(root_key)
        raw = str(relative_path)
        if "\x00" in raw:
            raise PathPolicyError("路径含无效字符")
        windows_relative = PureWindowsPath(raw)
        posix_relative = Path(raw)
        if (
            windows_relative.is_absolute()
            or windows_relative.drive
            or posix_relative.is_absolute()
            or raw.startswith(("\\\\", "//"))
        ):
            raise PathPolicyError("只接受逻辑根目录下的相对路径")
        parts = [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]
        if any(part == ".." or ":" in part for part in parts):
            raise PathPolicyError("相对路径不能越过逻辑根目录")

        candidate = root.joinpath(*parts)
        _assert_no_link_components(root)
        current = root
        for part in parts:
            current = current / part
            if _is_link_or_junction(current):
                raise PathPolicyError("路径中包含符号链接或目录联接")

        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise PathPolicyError("解析后的路径越过逻辑根目录") from exc

        if must_exist and not candidate.exists():
            raise FileNotFoundError("逻辑路径目标不存在")
        if for_write:
            ancestor = candidate if candidate.exists() else candidate.parent
            while ancestor != root and not ancestor.exists():
                ancestor = ancestor.parent
            if ancestor.exists() and not ancestor.is_dir() and ancestor != candidate:
                raise PathPolicyError("写入路径的父项不是目录")
        return candidate

