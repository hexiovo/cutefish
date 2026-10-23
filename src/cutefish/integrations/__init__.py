"""Bounded, evidence-preserving integrations.

No network connector or external action handler is registered by default. A
site-specific policy must be verified and explicitly wired by the application.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cutefish.core.registry import ModuleRegistry

from .capabilities import DEFAULT_CAPABILITIES, CapabilityDescriptor, CapabilityStatus, capability_snapshot
from .contracts import ActionPreview, ActionScope, SourceEvidence, SourceTimeRange, build_action_preview
from .evidence import EvidenceStore, EvidenceStoreError
from .readonly import (
    BoundedReadOnlyConnector,
    ConnectorPolicy,
    FetchError,
    FetchedDocument,
    HttpResponse,
    ImageMetadata,
    validate_image,
)
from .registry import HandlerRegistration, HandlerRegistry, HandlerUnavailable, PRODUCTION_HANDLER_REGISTRY
from .service import (
    IntegrationError,
    IntegrationService,
    PRODUCTION_READ_REGISTRY,
    ReadConnectorRegistry,
    ReadRegistration,
)

__all__ = [
    "ActionPreview",
    "ActionScope",
    "BoundedReadOnlyConnector",
    "CapabilityDescriptor",
    "CapabilityStatus",
    "ConnectorPolicy",
    "DEFAULT_CAPABILITIES",
    "EvidenceStore",
    "EvidenceStoreError",
    "FetchError",
    "FetchedDocument",
    "HandlerRegistration",
    "HandlerRegistry",
    "HandlerUnavailable",
    "IntegrationError",
    "IntegrationService",
    "HttpResponse",
    "ImageMetadata",
    "SourceEvidence",
    "SourceTimeRange",
    "PRODUCTION_HANDLER_REGISTRY",
    "PRODUCTION_READ_REGISTRY",
    "ReadConnectorRegistry",
    "ReadRegistration",
    "build_action_preview",
    "capability_snapshot",
    "validate_image",
    "register_module",
]


def register_module(registry: "ModuleRegistry") -> None:
    """Register the code-owned capability surface without marking it ready."""

    from cutefish.core.contracts import ModuleLifecycle, ModuleRuntimeState, ResourceClass, SideEffect
    from cutefish.core.registry import CapabilitySpec, ModuleSpec

    capabilities = [
        CapabilitySpec(
            id=item.capability_id,
            name=item.name,
            side_effect=SideEffect.EXTERNAL_WRITE if item.site_id == "hexi" else SideEffect.NONE,
        )
        for item in DEFAULT_CAPABILITIES
    ]
    registry.register(
        ModuleSpec(
            id="integrations",
            name="外部平台集成",
            lifecycle=ModuleLifecycle.ON_DEMAND,
            resource_class=ResourceClass.LIGHT,
            capabilities=capabilities,
            runtime_state=ModuleRuntimeState.NOT_CONNECTED,
            enabled=False,
            action_handler=None,
        )
    )
