"""Explicit module and capability registry; no module is loaded by discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .contracts import ModuleLifecycle, ModuleRuntimeState, ResourceClass, SideEffect


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    name: str
    side_effect: SideEffect = SideEffect.NONE
    code_implemented: bool = True
    configured: bool | None = None
    runtime_enabled: bool = False
    handler_available: bool = False
    status: str = "implemented"
    reason: str | None = None


@dataclass
class ModuleSpec:
    id: str
    name: str
    version: str | None = None
    lifecycle: ModuleLifecycle = ModuleLifecycle.ON_DEMAND
    resource_class: ResourceClass = ResourceClass.LIGHT
    dependencies: list[str] = field(default_factory=list)
    capabilities: list[CapabilitySpec] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    runtime_state: ModuleRuntimeState = ModuleRuntimeState.LAZY_IDLE
    enabled: bool = False
    code_implemented: bool = True
    configured: bool | None = None
    runtime_enabled: bool = False
    handler_available: bool = False
    config_defaults: dict[str, Any] = field(default_factory=dict)
    action_handler: Callable[..., Any] | None = None

    def public_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "enabled": self.enabled,
            "code_implemented": self.code_implemented,
            "configured": self.configured,
            "runtime_enabled": self.runtime_enabled,
            "handler_available": self.handler_available,
            "ready": self.runtime_state == ModuleRuntimeState.READY,
            "runtime_state": self.runtime_state.value,
            "lifecycle": self.lifecycle.value,
            "dependencies": list(self.dependencies),
            "capabilities": [
                {
                    "id": item.id,
                    "name": item.name,
                    "side_effect": item.side_effect.value,
                    "code_implemented": item.code_implemented,
                    "configured": item.configured,
                    "runtime_enabled": item.runtime_enabled,
                    "handler_available": item.handler_available,
                    "status": item.status,
                    "reason": item.reason,
                }
                for item in self.capabilities
            ],
            "config_schema": self.config_schema,
        }


class ModuleRegistry:
    def __init__(self):
        self._modules: dict[str, ModuleSpec] = {}

    def register(self, module: ModuleSpec) -> None:
        if module.id in self._modules:
            raise ValueError(f"duplicate module id: {module.id}")
        self._modules[module.id] = module

    def get(self, module_id: str) -> ModuleSpec | None:
        return self._modules.get(module_id)

    def modules(self) -> list[ModuleSpec]:
        return [self._modules[key] for key in sorted(self._modules)]

    def capability(self, module_id: str, capability_id: str) -> CapabilitySpec | None:
        module = self.get(module_id)
        if module is None:
            return None
        return next((item for item in module.capabilities if item.id == capability_id), None)

