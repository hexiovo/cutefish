"""Application services for bounded source reads and evidence-bound previews."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

from cutefish.core.contracts import ToolActionPreview

from .capabilities import DEFAULT_CAPABILITIES, capability_snapshot
from .contracts import ActionPreview, ActionScope, ContractError, SourceEvidence, build_action_preview
from .evidence import EvidenceStore, EvidenceStoreError
from .readonly import BoundedReadOnlyConnector, ConnectorPolicy, FetchError
from .registry import HandlerRegistry, HandlerUnavailable, PRODUCTION_HANDLER_REGISTRY


ReadScope = Literal["metadata", "content", "subtitle", "frame"]


class IntegrationError(Exception):
    """Safe error returned by integration services."""

    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ReadRegistration:
    """A code-owned endpoint binding; callers cannot choose URL, path, or method."""

    site_id: str
    capability_id: str
    path: str
    connector: BoundedReadOnlyConnector
    read_scope: ReadScope = "content"

    def __post_init__(self) -> None:
        policy = self.connector.policy
        if (self.site_id, self.capability_id) != (policy.site_id, policy.capability_id):
            raise ValueError("read registration must match its connector policy")
        if self.path not in policy.allowed_paths:
            raise ValueError("read registration path must be in its connector policy")


class ReadConnectorRegistry:
    """Immutable registry of reviewed, code-owned read endpoints."""

    def __init__(self, registrations: tuple[ReadRegistration, ...] = ()) -> None:
        entries: dict[tuple[str, str], ReadRegistration] = {}
        for registration in registrations:
            key = (registration.site_id, registration.capability_id)
            if key in entries:
                raise ValueError(f"duplicate read capability: {registration.capability_id}")
            entries[key] = registration
        self._entries: Mapping[tuple[str, str], ReadRegistration] = MappingProxyType(entries)

    def has(self, site_id: str, capability_id: str) -> bool:
        return (site_id, capability_id) in self._entries

    def resolve(self, site_id: str, capability_id: str) -> ReadRegistration:
        registration = self._entries.get((site_id, capability_id))
        if registration is None:
            raise IntegrationError("needs_configuration", "没有配置此来源的已核实只读路径；不会发起网络请求。")
        return registration


PRODUCTION_READ_REGISTRY = ReadConnectorRegistry()


class IntegrationService:
    def __init__(
        self,
        *,
        evidence_store: EvidenceStore,
        read_registry: ReadConnectorRegistry = PRODUCTION_READ_REGISTRY,
        action_registry: HandlerRegistry = PRODUCTION_HANDLER_REGISTRY,
        outbox=None,
    ) -> None:
        self.evidence_store = evidence_store
        self.read_registry = read_registry
        self.action_registry = action_registry
        self.outbox = outbox

    def capabilities(self) -> tuple[dict[str, object], ...]:
        # Production has neither target aliases nor reviewed handlers. The
        # snapshot is static and performs no site probes or credential reads.
        return tuple(
            item.public_data()
            for item in capability_snapshot(
                read_registry=self.read_registry,
                handler_registry=self.action_registry,
            )
        )

    def read(self, *, site_id: str, capability_id: str, object_id: str, query: Mapping[str, str]) -> dict[str, object]:
        registration = self.read_registry.resolve(site_id, capability_id)
        policy = registration.connector.policy
        if len(query) > policy.max_query_fields:
            raise IntegrationError("query_too_large", "来源查询参数超过策略数量上限。", 422)
        for key, value in query.items():
            if key not in policy.allowed_query_keys:
                raise IntegrationError("query_key_blocked", "来源查询参数不在只读策略内。", 422)
            if not isinstance(value, str) or len(value) > policy.max_query_value_length:
                raise IntegrationError("query_value_blocked", "来源查询值无效或超过策略长度上限。", 422)

        origin = urlsplit(policy.origin)
        url = urlunsplit(("https", origin.netloc, registration.path, urlencode(sorted(query.items())), ""))
        try:
            document = registration.connector.fetch(
                url,
                object_id=object_id,
                read_scope=registration.read_scope,
            )
            source_ref_id = self.evidence_store.register_document(document)
        except FetchError as exc:
            raise IntegrationError(exc.code, exc.message, 422 if exc.code.endswith("blocked") else 502) from exc
        except EvidenceStoreError as exc:
            raise IntegrationError(exc.code, exc.message, 503) from exc

        return {
            "source_ref_id": source_ref_id,
            "evidence": document.evidence.model_dump(mode="json"),
            "content_base64": base64.b64encode(document.content).decode("ascii"),
        }

    async def preview(
        self,
        *,
        site_id: str,
        account_ref: str | None,
        object_id: str,
        action: str,
        payload: dict[str, object],
        source_ref_ids: list[str],
        scope: ActionScope,
        actor_id: str,
        expected_revision: int,
        expires_in_seconds: int = 600,
    ) -> dict[str, object]:
        known_capability = next(
            (item for item in DEFAULT_CAPABILITIES if item.site_id == site_id and item.capability_id == action),
            None,
        )
        if known_capability is None:
            raise IntegrationError("unsupported_action", "此站点动作不在本机能力清单内。", 422)
        try:
            registration = self.action_registry.resolve(action)
        except HandlerUnavailable as exc:
            raise IntegrationError(exc.code, exc.message, 409) from exc
        if registration.site_id != site_id or registration.side_effect == "none":
            raise IntegrationError("unsupported_action", "该动作没有匹配的可授权 action handler。", 409)
        if self.outbox is None:
            raise IntegrationError("outbox_unavailable", "本机动作 Outbox 尚未装配。", 503)
        if account_ref is None:
            raise IntegrationError("needs_configuration", "动作预览需要已配置的本机账号别名。", 409)
        if scope.site_id != site_id or scope.object_id != object_id or scope.action != action:
            raise IntegrationError("action_scope_mismatch", "动作 scope 必须精确绑定请求中的站点、对象和动作。", 422)

        try:
            evidence = self.evidence_store.resolve(source_ref_ids)
            preview = build_action_preview(
                site_id=site_id,
                account_ref=account_ref,
                object_id=object_id,
                action=action,
                payload=payload,
                source_refs=evidence,
                scope=scope,
            )
        except EvidenceStoreError as exc:
            raise IntegrationError(exc.code, exc.message, 422) from exc
        except ContractError as exc:
            raise IntegrationError("invalid_preview", "动作预览字段未通过规范化校验。", 422) from exc

        core_preview = ToolActionPreview(
            preview_id=preview.preview_id,
            site=site_id,
            action_id=action,
            capability_id=action,
            handler_version=registration.version,
            account_id=account_ref,
            resource_id=object_id,
            action=action,
            payload_json=preview.payload_json,
            payload_hash=preview.payload_hash,
            expected_revision=expected_revision,
            actor_id=actor_id,
            scope=scope.model_dump(mode="json"),
            source_refs=[item.model_dump(mode="json") for item in evidence],
            expires_in_seconds=expires_in_seconds,
        )
        try:
            outbox_preview = await self.outbox.record_action_preview(core_preview, actor_id=actor_id)
        except Exception as exc:
            # Outbox exposes a safe code/message pair. Avoid leaking adapter or
            # database details into the API response.
            code = getattr(exc, "code", "preview_rejected")
            message = getattr(exc, "message", "本机 Outbox 拒绝记录此动作预览。")
            status = getattr(exc, "status_code", 409)
            raise IntegrationError(str(code), str(message), int(status)) from exc

        return {
            **preview.model_dump(mode="json"),
            "outbox_preview": outbox_preview,
        }
