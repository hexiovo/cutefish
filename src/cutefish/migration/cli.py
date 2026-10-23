"""Small offline CLI for validating a bundle and importing target staging."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

from cutefish.core.paths import HostPathResolver, PathPolicyError
from cutefish.core.settings import load_settings

from .service import MigrationError, MigrationPolicy, import_staging, validate_bundle


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cutefish-migrate",
        description="离线校验已生成的迁移包，或导入到新的 target_staging 目录。不会读取源主机数据库。",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="校验 backup_root 下的迁移包")
    validate.add_argument("--bundle-relative-path", required=True, help="相对于 backup_root 的包目录")

    stage = commands.add_parser("import-staging", help="导入至 data_root 下的新 target_staging 子目录")
    stage.add_argument("--bundle-relative-path", required=True, help="相对于 backup_root 的包目录")
    stage.add_argument("--target-relative-path", required=True, help="相对于 data_root 的新目标目录")
    return parser


def _policy_from_settings() -> tuple[MigrationPolicy, HostPathResolver]:
    settings = load_settings()
    paths = HostPathResolver(settings.roots)
    return (
        MigrationPolicy(paths=paths, source_host_id=settings.host_id),
        paths,
    )


def _resolve(paths: HostPathResolver, root_key: str, relative: str, *, must_exist: bool, for_write: bool = False) -> Path:
    try:
        return paths.resolve(root_key, relative, must_exist=must_exist, for_write=for_write)
    except (PathPolicyError, FileNotFoundError) as exc:
        if root_key == "backup_root":
            raise MigrationError("invalid_bundle_path", "迁移包必须位于已登记 backup_root 下。", 422) from exc
        raise MigrationError("invalid_target_path", "导入目标必须是 data_root 下的新安全目录。", 422) from exc


async def _run(args: argparse.Namespace) -> int:
    policy, paths = _policy_from_settings()
    try:
        bundle = _resolve(paths, "backup_root", args.bundle_relative_path, must_exist=True)
        if args.command == "validate":
            result = await validate_bundle(bundle, policy)
        else:
            target = _resolve(paths, "data_root", args.target_relative_path, must_exist=False, for_write=True)
            result = await import_staging(bundle, target, policy)
    except MigrationError as exc:
        print(json.dumps({"status": "failed", "code": exc.code, "message": exc.message}, ensure_ascii=False))
        return 2
    print(result.model_dump_json(indent=2))
    return 0 if getattr(result, "valid", True) else 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except MigrationError as exc:
        print(json.dumps({"status": "failed", "code": exc.code, "message": exc.message}, ensure_ascii=False))
        return 2
    except (OSError, ValueError) as exc:
        # Keep host paths and package contents out of generic CLI diagnostics.
        print(json.dumps({"status": "failed", "code": "migration_failed", "message": "迁移操作未完成。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())

