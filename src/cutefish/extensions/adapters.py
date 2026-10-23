"""Small, explicit adapter status contract; no provider is auto-discovered."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class AdapterState(StrEnum):
    UNCONFIGURED = "unconfigured"
    DISABLED = "disabled"
    NOT_CONNECTED = "not_connected"
    NEEDS_LOGIN = "needs_login"
    READY = "ready"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LocalAdapterStatus:
    adapter_id: str
    state: AdapterState
    configured: bool
    connected: bool | None = False
    capabilities: tuple[str, ...] = ()
    detail: str | None = None
    evidence: tuple[dict[str, Any], ...] = ()

    def public_data(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "state": self.state.value,
            "configured": self.configured,
            "connected": self.connected,
            "capabilities": list(self.capabilities),
            "detail": self.detail,
            "evidence": [dict(item) for item in self.evidence],
        }


class Adapter(Protocol):
    """An opt-in adapter implementation; status is read-only."""

    @property
    def adapter_id(self) -> str: ...

    async def status(self) -> LocalAdapterStatus: ...


@dataclass(frozen=True, slots=True)
class UnconfiguredAdapter:
    """Safe placeholder for a capability whose account/provider is absent."""

    adapter_id: str
    capabilities: tuple[str, ...] = ()

    async def status(self) -> LocalAdapterStatus:
        return LocalAdapterStatus(
            adapter_id=self.adapter_id,
            state=AdapterState.UNCONFIGURED,
            configured=False,
            connected=False,
            capabilities=self.capabilities,
            detail="尚未配置此适配器。",
        )

    async def invoke(self, *_args, **_kwargs):
        raise RuntimeError("适配器尚未配置，不能执行操作。")


@dataclass(slots=True)
class ExplicitAdapterRegistry:
    """A fixed registry that never imports adapters by scanning a package."""

    _items: dict[str, Adapter] = field(default_factory=dict)

    def register(self, adapter: Adapter) -> None:
        key = adapter.adapter_id
        if not key or key in self._items:
            raise ValueError("adapter id must be non-empty and unique")
        self._items[key] = adapter

    def get(self, adapter_id: str) -> Adapter | None:
        return self._items.get(adapter_id)

    async def statuses(self) -> list[dict[str, Any]]:
        return [
            (await self._items[key].status()).public_data()
            for key in sorted(self._items)
        ]
