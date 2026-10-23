"""Local task, candidate, media, room, and migration extension services.

Extensions are imported explicitly by the host application. Importing this
package does not discover providers, start workers, or touch the filesystem.
"""

from .adapters import AdapterState, LocalAdapterStatus
from .router import router
from .services import create_services
from .schema import ensure_compatible_schema
from cutefish.core.contracts import ModuleLifecycle, ModuleRuntimeState, ResourceClass, SideEffect
from cutefish.core.registry import CapabilitySpec, ModuleRegistry, ModuleSpec


def register_models() -> None:
    """Explicitly register extension tables on the shared SQLAlchemy Base."""
    from . import models as _models  # noqa: F401


def create_router():
    """Return this extension's authenticated local API router."""
    return router


def register_module(registry: ModuleRegistry) -> ModuleSpec:
    """Declare local extension capabilities without attaching external actions."""
    module = ModuleSpec(
        id="extensions",
        name="本机扩展",
        version="1",
        lifecycle=ModuleLifecycle.ON_DEMAND,
        resource_class=ResourceClass.LIGHT,
        capabilities=[
            CapabilitySpec(
                id="extensions.voice.preview",
                name="本机语音试听素材",
                side_effect=SideEffect.LOCAL_WRITE,
                runtime_enabled=False,
                handler_available=False,
                status="manual_only",
                reason="仅由管理员在语音设置页分步创建试听；此模块不注册QQ发送能力。",
            )
        ],
        runtime_state=ModuleRuntimeState.LAZY_IDLE,
        enabled=False,
        action_handler=None,
    )
    registry.register(module)
    return module

__all__ = [
    "AdapterState",
    "LocalAdapterStatus",
    "create_router",
    "create_services",
    "ensure_compatible_schema",
    "register_models",
    "register_module",
    "router",
]
