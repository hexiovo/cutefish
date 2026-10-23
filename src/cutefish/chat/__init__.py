"""The shared conversation and model-response pipeline."""

from cutefish.core.contracts import (
    ModuleLifecycle,
    ModuleRuntimeState,
    ResourceClass,
    SideEffect,
)
from cutefish.core.registry import CapabilitySpec, ModuleRegistry, ModuleSpec


def register_module(registry: ModuleRegistry) -> ModuleSpec:
    """Register the chat surface without constructing providers or services."""
    module = ModuleSpec(
        id="chat",
        name="统一聊天",
        version="1",
        lifecycle=ModuleLifecycle.ON_DEMAND,
        resource_class=ResourceClass.LIGHT,
        capabilities=[
            CapabilitySpec(
                id="chat.turn.generate",
                name="生成聊天回复",
                side_effect=SideEffect.LOCAL_WRITE,
                status="manual_only",
                reason="仅由明确的用户交互触发；真实请求可能产生模型费用，注册本身不会调用模型。",
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

