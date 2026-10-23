"""Offline, fail-closed SQLite and registered-media migration support."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cutefish.core.registry import ModuleRegistry

from .contracts import (
    BundleResult,
    ImportResult,
    MigrationManifest,
    MigrationPolicyVersion,
    Preflight,
    ValidationResult,
)
from .service import (
    MigrationPolicy,
    export_bundle,
    import_staging,
    inspect_source,
    validate_bundle,
)

__all__ = [
    "BundleResult",
    "ImportResult",
    "MigrationManifest",
    "MigrationPolicy",
    "MigrationPolicyVersion",
    "Preflight",
    "ValidationResult",
    "export_bundle",
    "import_staging",
    "inspect_source",
    "validate_bundle",
    "register_module",
]


def register_module(registry: "ModuleRegistry") -> None:
    """Expose implemented offline migration capabilities without enabling a runner."""

    from cutefish.core.contracts import ModuleLifecycle, ModuleRuntimeState, ResourceClass, SideEffect
    from cutefish.core.registry import CapabilitySpec, ModuleSpec

    registry.register(
        ModuleSpec(
            id="migration",
            name="本机数据迁移",
            lifecycle=ModuleLifecycle.ON_DEMAND,
            resource_class=ResourceClass.LIGHT,
            capabilities=[
                CapabilitySpec(
                    id="migration.inspect",
                    name="检查本机迁移源",
                    side_effect=SideEffect.NONE,
                ),
                CapabilitySpec(
                    id="migration.export",
                    name="导出脱敏迁移包",
                    side_effect=SideEffect.LOCAL_WRITE,
                ),
                CapabilitySpec(
                    id="migration.validate",
                    name="验证迁移包",
                    side_effect=SideEffect.NONE,
                ),
                CapabilitySpec(
                    id="migration.import_staging",
                    name="导入隔离暂存区",
                    side_effect=SideEffect.LOCAL_WRITE,
                ),
            ],
            runtime_state=ModuleRuntimeState.LAZY_IDLE,
            enabled=False,
            runtime_enabled=False,
            handler_available=False,
            action_handler=None,
        )
    )
