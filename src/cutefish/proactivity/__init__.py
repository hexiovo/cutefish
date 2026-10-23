"""Manual, finite, review-only proactivity feature package."""

from cutefish.core.contracts import (
    ModuleLifecycle,
    ModuleRuntimeState,
    ResourceClass,
    SideEffect,
)
from cutefish.core.registry import CapabilitySpec, ModuleRegistry, ModuleSpec


def register_module(registry: ModuleRegistry) -> ModuleSpec:
    """Register manual candidate generation without enabling a batch runner."""
    module = ModuleSpec(
        id="proactivity",
        name="主动关心候选",
        version="1",
        lifecycle=ModuleLifecycle.ON_DEMAND,
        resource_class=ResourceClass.LIGHT,
        capabilities=[
            CapabilitySpec(
                id="proactivity.candidate.generate",
                name="生成人工审核候选",
                side_effect=SideEffect.LOCAL_WRITE,
                status="manual_only",
                reason="每次需人工发起且只保存待审核候选，不自动发送；生成可能调用受预算约束的付费模型。",
            ),
        ],
        runtime_state=ModuleRuntimeState.DISABLED,
        enabled=False,
        configured=None,
        runtime_enabled=False,
        handler_available=False,
        action_handler=None,
    )
    registry.register(module)
    return module


__all__ = ["register_module"]
