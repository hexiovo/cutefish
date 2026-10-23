"""NapCat-compatible QQ bridge adapters."""

from cutefish.core.contracts import (
    ModuleLifecycle,
    ModuleRuntimeState,
    ResourceClass,
    SideEffect,
)
from cutefish.core.registry import CapabilitySpec, ModuleRegistry, ModuleSpec


def register_module(registry: ModuleRegistry) -> ModuleSpec:
    """Register QQ's typed capabilities without enabling or constructing a sender."""
    module = ModuleSpec(
        id="qq",
        name="QQ / OneBot",
        version="1",
        lifecycle=ModuleLifecycle.ON_DEMAND,
        resource_class=ResourceClass.LIGHT,
        capabilities=[
            CapabilitySpec(
                id="qq.message.send",
                name="QQ文本消息",
                side_effect=SideEffect.EXTERNAL_WRITE,
                status="disabled",
                reason="QQ发送器默认未挂载；外发保持关闭。",
            ),
            CapabilitySpec(
                id="qq.voice.send",
                name="QQ语音消息",
                side_effect=SideEffect.EXTERNAL_WRITE,
                status="needs_transport_format_verification",
                reason="NapCat语音传输格式未核实；发送器默认关闭且不会读取素材或发请求。",
            ),
        ],
        runtime_state=ModuleRuntimeState.DISABLED,
        enabled=False,
        action_handler=None,
    )
    registry.register(module)
    return module


__all__ = ["register_module"]

