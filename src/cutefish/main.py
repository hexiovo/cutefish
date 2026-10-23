"""Single-worker, loopback-only entry point for the CuteFish control panel."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cutefish.api.router import router
from cutefish.chat.persona_router import router as persona_router
from cutefish.chat.router import router as chat_router
from cutefish.chat.service import ChatError
from cutefish.core.auth import AuthError
from cutefish.core.authorization import AuthorizationError
from cutefish.core.batches import BatchError
from cutefish.core.budget import BudgetError
from cutefish.core.context import Context, ResourceNotFound, SettingsVersionConflict
from cutefish.core.outbox import OutboxError
from cutefish.core.settings import Settings, load_settings
from cutefish.extensions import router as extensions_router
from cutefish.integrations.router import router as integrations_router
from cutefish.migration.router import router as migration_router
from cutefish.memory.router import router as memory_router
from cutefish.qq.router import router as qq_router
from cutefish.qq.service import QqError
from cutefish.vision.router import router as vision_router
from cutefish.core.tool_action_router import router as tool_action_router
from cutefish.proactivity.router import router as proactivity_router


def _is_loopback_url(value: str) -> bool:
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    except ValueError:
        return False


def _error_response(request: Request, status_code: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(
        {
            "status": "failed",
            "data": None,
            "evidence": [],
            "error": {
                "code": code,
                "message": message,
                "retryable": status_code >= 500,
                "details": details,
            },
            "usage": None,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "time": datetime.now(timezone.utc).isoformat(),
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def create_app(
    *,
    settings: Settings | None = None,
    data_dir: Path | None = None,
    context: Context | None = None,
) -> FastAPI:
    context = context or Context(settings=settings, data_dir=data_dir)
    app = FastAPI(title="CuteFish Local Control Panel", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.context = context
    app.include_router(router)
    app.include_router(chat_router)
    app.include_router(persona_router)
    app.include_router(memory_router)
    app.include_router(qq_router)
    app.include_router(extensions_router)
    app.include_router(integrations_router)
    app.include_router(vision_router)
    app.include_router(proactivity_router)
    app.include_router(migration_router)
    app.include_router(tool_action_router)

    local_origins = tuple(
        origin
        for origin in context.settings.allowed_origins
        if origin.startswith("http://") and _is_loopback_url(origin)
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(local_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "X-CSRF-Token"],
    )

    @app.middleware("http")
    async def request_context_and_local_guard(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        if request.url.path == "/api" or request.url.path.startswith("/api/"):
            host = request.headers.get("host", "")
            origin = request.headers.get("origin")
            if not _is_loopback_url(host):
                return _error_response(request, 403, "local_host_required", "本机接口只接受loopback主机名。")
            if origin is not None and origin not in local_origins:
                return _error_response(request, 403, "origin_not_allowed", "请求来源不在本机界面允许范围内。")
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError):
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(ChatError)
    async def chat_error_handler(request: Request, exc: ChatError):
        return _error_response(request, exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(QqError)
    async def qq_error_handler(request: Request, exc: QqError):
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(BatchError)
    async def batch_error_handler(request: Request, exc: BatchError):
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(BudgetError)
    async def budget_error_handler(request: Request, exc: BudgetError):
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(AuthorizationError)
    async def authorization_error_handler(request: Request, exc: AuthorizationError):
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(OutboxError)
    async def outbox_error_handler(request: Request, exc: OutboxError):
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(SettingsVersionConflict)
    async def version_error_handler(request: Request, exc: SettingsVersionConflict):
        return _error_response(
            request,
            409,
            "version_conflict",
            str(exc),
            {"current_version": exc.current_version},
        )

    @app.exception_handler(ResourceNotFound)
    async def resource_not_found_handler(request: Request, exc: ResourceNotFound):
        return _error_response(request, 404, exc.code, exc.message)

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return _error_response(request, 422, "invalid_value", str(exc))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        safe_details = [
            {"loc": list(item.get("loc", ())), "msg": item.get("msg", "Invalid value"), "type": item.get("type")}
            for item in exc.errors()
        ]
        return _error_response(request, 422, "validation_error", "请求字段不符合接口要求。", safe_details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        status = exc.status_code
        code = "not_found" if status == 404 else "http_error"
        message = "接口不存在。" if status == 404 else "请求无法处理。"
        return _error_response(request, status, code, message)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        # Never include exception text or request data; those can contain credentials.
        return _error_response(request, 500, "internal_error", "本机服务遇到内部错误。")

    @app.on_event("startup")
    async def start_context():
        await context.startup()

    @app.on_event("shutdown")
    async def stop_context():
        await context.shutdown()

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def frontend_fallback(path: str, request: Request):
        if path == "api" or path.startswith("api/"):
            return _error_response(request, 404, "not_found", "接口不存在。")
        dist = context.settings.frontend_dist.resolve(strict=False)
        candidate = (dist / path).resolve(strict=False) if path else dist / "index.html"
        try:
            candidate.relative_to(dist)
        except ValueError:
            return _error_response(request, 404, "not_found", "页面不存在。")
        if candidate.is_file():
            return FileResponse(candidate)
        index_file = dist / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return _error_response(request, 404, "frontend_not_built", "前端尚未构建。")

    return app


app = create_app()


def run() -> None:
    import uvicorn

    # Keep one process and bind only to loopback; data dir may be injected through
    # CUTEFISH_DATA_DIR before this module is imported.
    uvicorn.run("cutefish.main:app", host="127.0.0.1", port=8000, workers=1, reload=False)


if __name__ == "__main__":
    run()

