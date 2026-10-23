"""Authenticated local control-panel routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select

from cutefish import __version__
from cutefish.api.schemas import (
    DeepSeekCredentialPut,
    DeepSeekCredentialStatus,
    ModuleConfigPut,
    OutboxAuthorizationCreate,
    OutboxPrepare,
    PasswordRequest,
    TrialBatchCreate,
    UiSettingsPatch,
)
from cutefish.core.auth import AuthError, CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, SessionIssue
from cutefish.core.batches import BatchError
from cutefish.core.context import Context, ResourceNotFound, SettingsVersionConflict
from cutefish.core.budget import BudgetError
from cutefish.core.outbox import OutboxError
from cutefish.core.models import FeatureState, TrialBatch
from cutefish.core.feature_status import (
    FeatureStatusResponse,
    FeatureStatusResponseItem,
    runtime_production_stage,
    unknown_feature_stage,
)


router = APIRouter(prefix="/api")


def _context(request: Request) -> Context:
    return request.app.state.context


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid.uuid4()))


def _success(request: Request, data, *, status_code: int = 200) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        {
            "status": "success",
            "data": data,
            "evidence": [],
            "error": None,
            "usage": None,
            "request_id": _request_id(request),
            "time": datetime.now(timezone.utc).isoformat(),
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


async def require_admin(request: Request):
    context = _context(request)
    token = request.cookies.get(SESSION_COOKIE)
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        await context.auth.verify_csrf(
            token=token,
            cookie_token=request.cookies.get(CSRF_COOKIE),
            header_token=request.headers.get(CSRF_HEADER),
        )
    return await context.auth.require_admin(token)


async def _verify_pre_auth_csrf(request: Request) -> None:
    await _context(request).auth.verify_csrf(
        token=request.cookies.get(SESSION_COOKIE),
        cookie_token=request.cookies.get(CSRF_COOKIE),
        header_token=request.headers.get(CSRF_HEADER),
    )


def _set_cookies(response: Response, issue: SessionIssue) -> None:
    max_age = 12 * 60 * 60
    response.set_cookie(
        SESSION_COOKIE,
        issue.token,
        max_age=max_age,
        httponly=True,
        secure=False,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        issue.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=False,
        samesite="strict",
        path="/",
    )


@router.get("/auth/bootstrap")
async def auth_bootstrap(request: Request):
    data = await _context(request).auth.bootstrap(request.cookies.get(SESSION_COOKIE))
    response = _success(request, data)
    response.set_cookie(
        CSRF_COOKIE,
        str(data["csrf_token"]),
        max_age=12 * 60 * 60,
        httponly=False,
        secure=False,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/session")
async def auth_session(request: Request):
    return _success(request, await _context(request).auth.session_data(request.cookies.get(SESSION_COOKIE)))


@router.post("/auth/setup")
async def auth_setup(request: Request, payload: PasswordRequest):
    await _verify_pre_auth_csrf(request)
    issue = await _context(request).auth.create_admin(payload.password)
    data = {
        "authenticated": True,
        "setup_required": False,
        "principal": {"id": "owner", "role": "owner", "display_name": "管理员"},
        "csrf_token": issue.csrf_token,
    }
    response = _success(request, data)
    _set_cookies(response, issue)
    response.status_code = 201
    return response


@router.post("/auth/login")
async def auth_login(request: Request, payload: PasswordRequest):
    await _verify_pre_auth_csrf(request)
    issue = await _context(request).auth.login(payload.password)
    data = {
        "authenticated": True,
        "setup_required": False,
        "principal": {"id": "owner", "role": "owner", "display_name": "管理员"},
        "csrf_token": issue.csrf_token,
    }
    response = _success(request, data)
    _set_cookies(response, issue)
    return response


@router.post("/auth/logout")
async def auth_logout(request: Request, _admin=Depends(require_admin)):
    context = _context(request)
    await context.auth.logout(request.cookies.get(SESSION_COOKIE))
    response = _success(request, {"authenticated": False})
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", samesite="strict")
    return response


@router.get("/overview")
async def overview(request: Request, _admin=Depends(require_admin)):
    context = _context(request)
    async with context.session_factory() as session:
        active = await session.scalar(
            select(TrialBatch)
            .where(TrialBatch.state == "running")
            .order_by(TrialBatch.started_at.desc())
            .limit(1)
        )
    data = {
        "mode": context.settings.mode,
        "host_id": context.settings.host_id,
        "active_trial_batch": context.batches.serialize(active) if active else None,
        "pending_outbox_count": await context.outbox.pending_count(),
        "budget_snapshot": await context.budget.snapshot(),
        "app_version": __version__,
        "services": {
            "qq": {"status": "not_connected", "connected": False, "label": "QQ桥"},
            "model": {"status": "not_connected", "connected": False, "label": "模型服务"},
            "database": {"status": "ready", "connected": True, "label": "本机数据库"},
            "ui": {"status": "ready", "available": True, "label": "本机界面"},
        },
        "database": {"status": "ready", "connected": True},
    }
    return _success(request, data)


@router.get("/providers/deepseek/credential")
async def deepseek_credential_status(request: Request, _admin=Depends(require_admin)):
    state = await _context(request).deepseek_credential_status()
    return _success(request, DeepSeekCredentialStatus(**state).model_dump())


@router.put("/providers/deepseek/credential")
async def configure_deepseek_credential(
    request: Request,
    payload: DeepSeekCredentialPut,
    _admin=Depends(require_admin),
):
    state = await _context(request).configure_deepseek_api_key(payload.api_key)
    return _success(request, DeepSeekCredentialStatus(**state).model_dump())


@router.get("/modules")
async def modules(request: Request, _admin=Depends(require_admin)):
    items = [module.public_data() for module in _context(request).registry.modules()]
    return _success(request, {"items": items})


@router.get("/features")
async def features(request: Request, _admin=Depends(require_admin)):
    context = _context(request)
    async with _context(request).session_factory() as session:
        rows = (await session.scalars(select(FeatureState).order_by(FeatureState.feature_id))).all()
    legacy_by_id = {row.feature_id: row for row in rows}
    source_status, source_items = context.feature_status.read()
    items: list[FeatureStatusResponseItem] = []
    if source_status == "available":
        for item in source_items:
            legacy = legacy_by_id.get(item.feature_id)
            legacy_refs = list(legacy.evidence_refs or []) if legacy is not None else []
            feature_data = item.model_dump()
            feature_data["production_enabled"] = runtime_production_stage(
                item.feature_id,
                item.production_enabled,
                deployment_profile=context.deployment_profile,
                runtime_policy_valid=context.runtime_policy_valid,
                runtime_enabled_features=getattr(context, "runtime_production_enabled_features", ()),
            )
            items.append(
                FeatureStatusResponseItem(
                    **feature_data,
                    requirement_status=legacy.requirement_status if legacy is not None else "unknown",
                    delivery_status=legacy.delivery_status if legacy is not None else "unknown",
                    module_id=legacy.module_id if legacy is not None else None,
                    evidence_refs=legacy_refs,
                )
            )
    else:
        for row in rows:
            items.append(
                FeatureStatusResponseItem(
                    feature_id=row.feature_id,
                    name=row.name,
                    implemented=unknown_feature_stage(),
                    local_verified=unknown_feature_stage(),
                    target_verified=unknown_feature_stage(),
                    production_enabled=runtime_production_stage(
                        row.feature_id,
                        unknown_feature_stage(),
                        deployment_profile=context.deployment_profile,
                        runtime_policy_valid=context.runtime_policy_valid,
                        runtime_enabled_features=getattr(context, "runtime_production_enabled_features", ()),
                    ),
                    requirement_status=row.requirement_status,
                    delivery_status=row.delivery_status,
                    module_id=row.module_id,
                    evidence_refs=list(row.evidence_refs or []),
                )
            )
    response = FeatureStatusResponse(source_status=source_status, items=items)
    return _success(request, response.model_dump(mode="json"))


@router.get("/budgets")
async def budgets(request: Request, _admin=Depends(require_admin)):
    return _success(request, await _context(request).budget.snapshot())


@router.get("/budgets/entries")
async def budget_entries(
    request: Request,
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    _admin=Depends(require_admin),
):
    return _success(
        request,
        await _context(request).budget.list_entries(state=state, limit=limit, offset=offset),
    )


@router.get("/outbox")
async def list_outbox(
    request: Request,
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    _admin=Depends(require_admin),
):
    return _success(request, await _context(request).outbox.list(state=state, limit=limit, offset=offset))


@router.get("/outbox/{item_id}")
async def get_outbox_item(request: Request, item_id: str, _admin=Depends(require_admin)):
    return _success(request, await _context(request).outbox.get(item_id))


@router.post("/outbox/prepare", status_code=201)
async def prepare_outbox_item(request: Request, payload: OutboxPrepare, _admin=Depends(require_admin)):
    values = payload.model_dump()
    values["request_id"] = values["request_id"] or _request_id(request)
    data = await _context(request).outbox.prepare(values, actor_id=f"admin:{_admin.admin_id}")
    return _success(request, data, status_code=201)


@router.post("/outbox/authorizations", status_code=201)
async def create_outbox_authorization(
    request: Request,
    payload: OutboxAuthorizationCreate,
    principal=Depends(require_admin),
):
    context = _context(request)
    if payload.action_id == "qq.voice.send":
        data = await context.outbox.issue_voice_authorization(payload.model_dump(), principal=principal)
    else:
        data = await context.authorizations.issue_message_grant(
            principal=principal,
            conversation_id=payload.conversation_id,
            expected_revision=payload.expected_revision,
            batch_id=payload.batch_id,
            payload=payload.payload or "",
            action_id=payload.action_id,
            expires_in_seconds=payload.expires_in_seconds,
        )
        data = {**data, "authorization_id": data["id"]}
    return _success(request, data, status_code=201)


@router.post("/outbox/{item_id}/cancel")
async def cancel_outbox_item(request: Request, item_id: str, _admin=Depends(require_admin)):
    return _success(request, await _context(request).outbox.cancel(item_id))


@router.post("/outbox/{item_id}/preflight")
async def preflight_outbox_item(request: Request, item_id: str, _admin=Depends(require_admin)):
    return _success(request, await _context(request).outbox.preflight(item_id))


@router.post("/outbox/{item_id}/dispatch")
async def dispatch_outbox_item(request: Request, item_id: str, _admin=Depends(require_admin)):
    return _success(request, await _context(request).outbox.dispatch_one(item_id))


@router.get("/trial-batches")
async def list_batches(request: Request, _admin=Depends(require_admin)):
    return _success(request, {"items": await _context(request).batches.list()})


@router.post("/trial-batches", status_code=201)
async def create_batch(request: Request, payload: TrialBatchCreate, _admin=Depends(require_admin)):
    data = await _context(request).batches.create(payload.model_dump())
    return _success(request, data, status_code=201)


@router.post("/trial-batches/{batch_id}/start")
async def start_batch(request: Request, batch_id: str, _admin=Depends(require_admin)):
    return _success(request, await _context(request).batches.start(batch_id))


@router.post("/trial-batches/{batch_id}/stop")
async def stop_batch(request: Request, batch_id: str, _admin=Depends(require_admin)):
    return _success(request, await _context(request).batches.stop(batch_id))


@router.get("/settings/ui")
async def get_ui_settings(request: Request, _admin=Depends(require_admin)):
    return _success(request, await _context(request).get_ui_settings())


@router.patch("/settings/ui")
async def patch_ui_settings(request: Request, payload: UiSettingsPatch, _admin=Depends(require_admin)):
    data = await _context(request).save_ui_settings(
        theme=payload.theme,
        locale=payload.locale,
        expected_version=payload.expected_version,
    )
    return _success(request, data)


@router.get("/settings/host")
async def get_host_settings(request: Request, _admin=Depends(require_admin)):
    context = _context(request)
    return _success(
        request,
        {
            "host_id": context.settings.host_id,
            "mode": context.settings.mode,
            "timezone": context.settings.timezone,
            "roots": context.paths.status(),
        },
    )


@router.get("/modules/{module_id}/config")
async def get_module_config(request: Request, module_id: str, _admin=Depends(require_admin)):
    return _success(request, await _context(request).get_module_config(module_id))


@router.put("/modules/{module_id}/config")
async def put_module_config(
    request: Request,
    module_id: str,
    payload: ModuleConfigPut,
    _admin=Depends(require_admin),
):
    return _success(
        request,
        await _context(request).save_module_config(
            module_id,
            payload.values,
            payload.expected_version,
        ),
    )

