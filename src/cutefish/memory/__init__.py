"""Person-scoped durable memory records."""

from cutefish.core.contracts import (
    ModuleLifecycle,
    ModuleRuntimeState,
    ResourceClass,
    SideEffect,
)
from cutefish.core.registry import CapabilitySpec, ModuleRegistry, ModuleSpec


def register_module(registry: ModuleRegistry) -> ModuleSpec:
    """Register bounded memory capabilities without opening the database."""
    module = ModuleSpec(
        id="memory",
        name="人物记忆",
        version="1",
        lifecycle=ModuleLifecycle.ON_DEMAND,
        resource_class=ResourceClass.LIGHT,
        capabilities=[
            CapabilitySpec(
                id="memory.records.manage",
                name="按人物管理记忆",
                side_effect=SideEffect.LOCAL_WRITE,
                status="implemented",
                reason="人物隔离的本地记忆读取、补充和纠正；登记不读取或修改记录。",
            ),
        ],
        runtime_state=ModuleRuntimeState.LAZY_IDLE,
        enabled=False,
        configured=None,
        runtime_enabled=False,
        handler_available=False,
        action_handler=None,
    )
    registry.register(module)
    return module


__all__ = ["register_module"]

