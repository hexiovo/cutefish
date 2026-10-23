"""Local image decoding, media source checks, and explicit capability gates."""

from .router import router
from .service import VisionError, VisionService

from cutefish.core.contracts import (
    ModuleLifecycle,
    ModuleRuntimeState,
    ResourceClass,
    SideEffect,
)
from cutefish.core.registry import CapabilitySpec, ModuleRegistry, ModuleSpec


def register_module(registry: ModuleRegistry) -> ModuleSpec:
    """Register local image handling and the gated visual observation surface."""
    module = ModuleSpec(
        id="vision",
        name="图像理解",
        version="1",
        lifecycle=ModuleLifecycle.ON_DEMAND,
        resource_class=ResourceClass.HIGH_MEMORY,
        capabilities=[
            CapabilitySpec(
                id="vision.image.decode",
                name="本地图像解码",
                side_effect=SideEffect.NONE,
                status="implemented",
                reason="仅解码已登记且获准使用的 PNG/JPEG；不声称模型已观察图像。",
            ),
            CapabilitySpec(
                id="vision.image.observe",
                name="视觉模型观察",
                side_effect=SideEffect.NONE,
                status="needs_verification",
                reason="DeepSeek typed adapter 与保守费用预留已接线；真实视觉模型能力及图像计价仍待验证。",
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


__all__ = ["VisionError", "VisionService", "register_module", "router"]
