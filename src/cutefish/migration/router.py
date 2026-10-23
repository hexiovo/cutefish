"""Authenticated local-only migration controls; paths are logical-root relative."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cutefish import __version__
from cutefish.api.router import require_admin
from cutefish.core.paths import PathPolicyError

from .contracts import BundlePathRequest, ExportRequest, ImportRequest
from .service import MigrationError, MigrationPolicy, export_bundle, import_staging, inspect_source, migration_status, validate_bundle


router = APIRouter(prefix="/api/migration", tags=["migration"])


def _context(request: Request):
    return request.app.state.context


def _success(request: Request, data, *, status_code: int = 200):
    return JSONResponse(
        {
            "status": "success",
            "data": data,
            "evidence": [],
            "error": None,
            "usage": None,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "time": datetime.now(timezone.utc).isoformat(),
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _policy(context) -> MigrationPolicy:
    return MigrationPolicy(
        paths=context.paths,
        source_host_id=context.settings.host_id,
        app_version=__version__,
    )


@router.get("/status")
async def status(request: Request, _admin=Depends(require_admin)):
    return _success(request, migration_status(_context(request)))


@router.post("/inspect")
async def inspect(request: Request, _admin=Depends(require_admin)):
    result = await inspect_source(_context(request))
    return _success(request, result.model_dump())


@router.post("/export", status_code=201)
async def export(request: Request, payload: ExportRequest, _admin=Depends(require_admin)):
    context = _context(request)
    try:
        output_dir = context.paths.resolve(
            "backup_root",
            payload.destination_relative_path,
            for_write=True,
        )
    except (PathPolicyError, FileNotFoundError) as exc:
        raise MigrationError("invalid_destination", "导出目标必须是 backup_root 下的安全相对路径。", 422) from exc
    result = await export_bundle(context, output_dir, payload.media_ids, _policy(context))
    return _success(request, result.model_dump(), status_code=201)


@router.post("/validate")
async def validate(request: Request, payload: BundlePathRequest, _admin=Depends(require_admin)):
    context = _context(request)
    try:
        bundle_path = context.paths.resolve(
            "backup_root",
            payload.bundle_relative_path,
            must_exist=True,
        )
    except (PathPolicyError, FileNotFoundError) as exc:
        raise MigrationError("invalid_bundle_path", "迁移包必须位于已登记 backup_root 下。", 422) from exc
    result = await validate_bundle(bundle_path, _policy(context))
    return _success(request, result.model_dump())


@router.post("/import", status_code=201)
async def import_bundle(request: Request, payload: ImportRequest, _admin=Depends(require_admin)):
    context = _context(request)
    try:
        bundle_path = context.paths.resolve(
            "backup_root",
            payload.bundle_relative_path,
            must_exist=True,
        )
        target_dir = context.paths.resolve(
            "data_root",
            payload.target_relative_path,
            for_write=True,
        )
    except (PathPolicyError, FileNotFoundError) as exc:
        raise MigrationError("invalid_import_path", "导入包和目标必须位于已登记逻辑根目录内。", 422) from exc
    result = await import_staging(bundle_path, target_dir, _policy(context))
    return _success(request, result.model_dump(), status_code=201)
