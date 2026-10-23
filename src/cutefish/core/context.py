"""Application dependency container for the local runtime."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from cutefish.chat.service import ChatService
from cutefish.chat.persona import PersonaService
from cutefish.integrations.evidence import EvidenceStore
from cutefish.integrations.registry import PRODUCTION_HANDLER_REGISTRY
from cutefish.memory.service import MemoryService
from cutefish.qq.service import QqError, QqService

from .auth import AuthService
from .authorization import AuthorizationService
from .batches import BatchService
from .budget import BudgetService
from .db import Database
from .feature_status import FeatureStatusReader
from .models import AppSetting, ModuleConfiguration, SecretRecord, utc_now
from .outbox import OutboxService
from .paths import HostPathResolver
from .registry import ModuleRegistry
from .settings import Settings, load_settings


class SecretStore:
    """Store secrets only as user-bound Windows DPAPI ciphertext."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def _dpapi(data: bytes, *, protect: bool) -> bytes:
        if os.name != "nt":
            raise RuntimeError("当前系统没有配置受支持的本机凭据保护器。")

        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        input_buffer = ctypes.create_string_buffer(data)
        input_blob = DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
        output_blob = DataBlob()
        if protect:
            function = crypt32.CryptProtectData
            function.argtypes = [
                ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob), ctypes.c_void_p,
                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
            ]
            function.restype = wintypes.BOOL
            ok = function(ctypes.byref(input_blob), "CuteFish local secret", None, None, None, 0x1, ctypes.byref(output_blob))
        else:
            function = crypt32.CryptUnprotectData
            function.argtypes = [
                ctypes.POINTER(DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DataBlob),
                ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
            ]
            function.restype = wintypes.BOOL
            description = wintypes.LPWSTR()
            ok = function(ctypes.byref(input_blob), ctypes.byref(description), None, None, None, 0x1, ctypes.byref(output_blob))
            if description:
                kernel32.LocalFree(description)
        if not ok:
            raise OSError(ctypes.get_last_error(), "本机凭据保护操作失败")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)

    async def put(self, name: str, value: str) -> None:
        if not name or len(name) > 128 or not value:
            raise ValueError("secret name and value are required")
        encrypted = self._dpapi(value.encode("utf-8"), protect=True)
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(SecretRecord).where(SecretRecord.name == name))
                if row is None:
                    session.add(SecretRecord(id=str(uuid.uuid4()), name=name, encrypted_value=encrypted))
                else:
                    row.encrypted_value = encrypted
                    row.updated_at = utc_now()

    async def get(self, name: str) -> str | None:
        async with self.session_factory() as session:
            row = await session.scalar(select(SecretRecord).where(SecretRecord.name == name))
            if row is None:
                return None
            return self._dpapi(bytes(row.encrypted_value), protect=False).decode("utf-8")

    async def is_configured(self, name: str) -> bool:
        """Report credential presence without decrypting or returning its value."""
        async with self.session_factory() as session:
            row_id = await session.scalar(select(SecretRecord.id).where(SecretRecord.name == name).limit(1))
            return row_id is not None

    async def delete(self, name: str) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(SecretRecord).where(SecretRecord.name == name))
                if row is None:
                    return False
                await session.delete(row)
                return True


class Context:
    def __init__(self, settings: Settings | None = None, *, data_dir: Path | None = None):
        if settings is None:
            settings = load_settings(data_dir=data_dir)
        elif data_dir is not None:
            resolved = Path(data_dir).expanduser().resolve(strict=False)
            roots = dict(settings.roots)
            roots.update(
                {
                    "data_root": str(resolved),
                    "cache_root": str(resolved / "cache"),
                    "temp_root": str(resolved / "tmp"),
                    "backup_root": str(resolved / "backup"),
                    "gallery_root": str(resolved / "gallery"),
                }
            )
            settings = settings.model_copy(
                update={"data_dir": resolved, "database_path": resolved / "cutefish.sqlite3", "roots": roots}
            )
        self.settings = settings
        self.feature_status = FeatureStatusReader(settings.feature_status_path)
        # The database-bound migration marker is read before any service can
        # attempt recovery or external work. Unknown profiles are closed as
        # target_staging rather than silently treated as a live local host.
        requested_profile = getattr(settings, "mode", "local_trial")
        self.deployment_profile = requested_profile if requested_profile in {"local_trial", "target_staging"} else "target_staging"
        self.runtime_policy_valid = True
        self.db = Database(settings.database_path)
        self.session_factory = self.db.session_factory
        self.paths = HostPathResolver(settings.roots)
        self.secrets = SecretStore(self.session_factory)
        self.registry = ModuleRegistry()
        self._register_application_modules()
        self.integration_handlers = PRODUCTION_HANDLER_REGISTRY
        self.evidence_store = EvidenceStore()
        self.auth = AuthService(self.session_factory)
        self.batches = BatchService(
            self.session_factory,
            self.registry,
            get_deployment_profile=lambda: self.deployment_profile,
        )
        self.budget = BudgetService(
            self.db,
            settings,
            get_deployment_profile=lambda: self.deployment_profile,
        )
        self.memory_service = MemoryService(self.session_factory)
        self.chat_service = ChatService(
            self.session_factory,
            self.budget,
            self.secrets,
            memory_service=self.memory_service,
        )
        self.persona_service = PersonaService(
            self.session_factory,
            self.chat_service,
            confirmed_design_import_root=settings.confirmed_design_import_root,
        )
        self.qq_service = QqService(
            self.session_factory,
            self.secrets,
            self.memory_service,
            chat_service=self.chat_service,
            batches=self.batches,
        )
        from cutefish.extensions import create_services

        self.extension_services = create_services(self)
        self.authorizations = AuthorizationService(
            self.db,
            self.session_factory,
            get_delivery_guard=self.get_conversation_delivery_guard,
            get_deployment_profile=lambda: self.deployment_profile,
        )
        voice_service = self.extension_services["voice"]
        self.outbox = OutboxService(
            self.db,
            self.session_factory,
            self.batches,
            self.authorizations,
            get_delivery_guard=self.get_conversation_delivery_guard,
            get_deployment_profile=lambda: self.deployment_profile,
            voice_binding_provider=voice_service.resolve_voice_outbox_binding,
            voice_validator=getattr(voice_service, "voice_validator", None),
            voice_status_provider=voice_service.status,
            action_handlers=self.integration_handlers,
            sender=None,
        )
        self._started = False

    def _register_application_modules(self) -> None:
        """Register only the modules shipped with this application.

        This is a fixed allowlist, not package discovery. Each registrar is
        declarative: registration must not enable modules, start workers, probe
        services, or attach external action handlers.
        """

        from cutefish.chat import register_module as register_chat_module
        from cutefish.extensions import register_module as register_extensions_module
        from cutefish.integrations import register_module as register_integrations_module
        from cutefish.memory import register_module as register_memory_module
        from cutefish.migration import register_module as register_migration_module
        from cutefish.proactivity import register_module as register_proactivity_module
        from cutefish.qq import register_module as register_qq_module
        from cutefish.vision import register_module as register_vision_module

        for register_module in (
            register_chat_module,
            register_memory_module,
            register_qq_module,
            register_vision_module,
            register_extensions_module,
            register_proactivity_module,
            register_integrations_module,
            register_migration_module,
        ):
            register_module(self.registry)

    async def startup(self) -> None:
        if not self._started:
            try:
                await self.db.initialize()
                await self._load_deployment_profile()
                await self.budget.initialize()
                await self.chat_service.startup()
                await self.qq_service.recover_pending_events()
                await self.outbox.recover_inflight()
            except BaseException:
                # A failed migration must not leave SQLite connections pinned,
                # especially on Windows where that can keep a database file
                # locked after startup has aborted.
                await self.db.close()
                raise
            self._started = True

    async def _load_deployment_profile(self) -> None:
        """Load the durable staging gate before recovering any pending work."""
        async with self.session_factory() as session:
            row = await session.get(AppSetting, "migration_runtime")
        if row is None:
            return
        value = row.value if isinstance(row.value, dict) else {}
        if value.get("profile") != "target_staging":
            # A marker exists but is malformed or claims an unsupported mode;
            # preserve the closed behavior instead of falling back to local_trial.
            self.deployment_profile = "target_staging"
            self.runtime_policy_valid = False
            return
        expected_false = (
            "external_actions_enabled",
            "outbox_dispatch_enabled",
            "paid_provider_calls_enabled",
            "scheduling_enabled",
            "auto_start_enabled",
            "task_auto_resume",
        )
        self.deployment_profile = "target_staging"
        self.runtime_policy_valid = (
            value.get("policy_version") == 1
            and all(value.get(key) is False for key in expected_false)
            and value.get("credentials_state") == "reconfigure_on_target"
            and value.get("requires_host_bindings") is True
        )

    def assert_local_trial(self, *, operation: str) -> None:
        if self.deployment_profile != "local_trial":
            raise RuntimeError(f"{operation} is disabled while deployment_profile=target_staging")

    async def get_conversation_delivery_guard(self, conversation_id: str) -> dict[str, Any] | None:
        service = self.chat_service
        if service is None:
            return None
        guard = await service.get_delivery_guard(conversation_id)
        if not isinstance(guard, dict):
            return None
        if guard.get("channel") == "qq" or guard.get("delivery_mode") == "qq":
            try:
                await self.qq_service.validate_delivery_target(guard)
            except QqError:
                return None
        return guard

    async def shutdown(self) -> None:
        if self._started:
            await self.db.close()
            self._started = False

    async def save_ui_settings(self, theme: str, locale: str, expected_version: int) -> dict[str, Any]:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(AppSetting, "ui")
                current = row.version if row else 1
                if expected_version != current:
                    raise SettingsVersionConflict(current)
                value = {"theme": theme, "locale": locale}
                if row is None:
                    row = AppSetting(key="ui", value=value, version=2, updated_at=utc_now())
                    session.add(row)
                else:
                    row.value = value
                    row.version += 1
                    row.updated_at = utc_now()
                await session.flush()
                return {**value, "version": row.version, "updated_at": row.updated_at.isoformat()}

    async def get_ui_settings(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(AppSetting, "ui")
            if row is None:
                return {"theme": "system", "locale": "zh-CN", "version": 1}
            return {**row.value, "version": row.version, "updated_at": row.updated_at.isoformat()}

    async def get_module_config(self, module_id: str) -> dict[str, Any]:
        module = self.registry.get(module_id)
        if module is None:
            raise ResourceNotFound("module_not_found", "模块尚未注册。")
        async with self.session_factory() as session:
            row = await session.get(ModuleConfiguration, module_id)
            return {
                "schema": module.config_schema,
                "values": dict(row.value) if row is not None else dict(module.config_defaults),
                "version": row.version if row is not None else 1,
            }

    async def deepseek_credential_status(self) -> dict[str, Any]:
        return {
            "provider": "deepseek",
            "configured": await self.secrets.is_configured("deepseek_api_key"),
        }

    async def configure_deepseek_api_key(self, api_key: str) -> dict[str, Any]:
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
            raise ValueError("DeepSeek API Key不能为空且最多4096个字符。")
        # The fixed, provider-specific key name keeps credentials out of generic
        # module settings. The secret value is never returned to the caller.
        await self.secrets.put("deepseek_api_key", api_key.strip())
        return await self.deepseek_credential_status()

    async def save_module_config(self, module_id: str, values: dict[str, Any], expected_version: int) -> dict[str, Any]:
        module = self.registry.get(module_id)
        if module is None:
            raise ResourceNotFound("module_not_found", "模块尚未注册。")
        if _contains_secret_key(values):
            raise ValueError("敏感凭据必须单独存入本机凭据存储，不能写入模块设置。")
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(ModuleConfiguration, module_id)
                current = row.version if row else 1
                if expected_version != current:
                    raise SettingsVersionConflict(current)
                if row is None:
                    row = ModuleConfiguration(module_id=module_id, enabled=module.enabled, value=values, version=2)
                    session.add(row)
                else:
                    row.value = values
                    row.version += 1
                    row.updated_at = utc_now()
                await session.flush()
                return {"schema": module.config_schema, "values": dict(row.value), "version": row.version}


class SettingsVersionConflict(Exception):
    def __init__(self, current_version: int):
        super().__init__("设置已被其他请求修改，请刷新后重试。")
        self.current_version = current_version


class ResourceNotFound(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_SECRET_KEY_PARTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "cookie", "credential")


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                return True
            if _contains_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False

