"""Explicit registry for verified site handlers; production starts empty."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping


class HandlerUnavailable(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class HandlerRegistration:
    site_id: str
    capability_id: str
    version: str
    verification_ref: str
    side_effect: Literal["none", "local_write", "external_write"]
    handler: Callable[..., Any]


class HandlerRegistry:
    """Immutable registry assembled only from code-owned verified entries.

    The application must not populate this registry from settings, network
    responses, UI input, test fixtures, or plugin auto-discovery. Unit tests
    may create a separate local registry; the exported production registry is
    deliberately empty until a real handler is reviewed and explicitly wired.
    """

    def __init__(self, registrations: tuple[HandlerRegistration, ...] = ()) -> None:
        entries: dict[str, HandlerRegistration] = {}
        for registration in registrations:
            if not registration.site_id or not registration.capability_id or not registration.version:
                raise ValueError("handler registration requires site, capability, and version")
            if not registration.verification_ref or len(registration.verification_ref) > 512:
                raise ValueError("handler registration requires a bounded verification reference")
            if not callable(registration.handler):
                raise ValueError("handler registration requires an explicit callable")
            if registration.capability_id in entries:
                raise ValueError(f"duplicate handler capability: {registration.capability_id}")
            entries[registration.capability_id] = registration
        self._entries: Mapping[str, HandlerRegistration] = MappingProxyType(entries)

    def has(self, capability_id: str) -> bool:
        return capability_id in self._entries

    def resolve(self, capability_id: str) -> HandlerRegistration:
        registration = self._entries.get(capability_id)
        if registration is None:
            raise HandlerUnavailable("needs_adapter_verification", "没有已核实并注册的站点 handler。")
        return registration

    def capability_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))


PRODUCTION_HANDLER_REGISTRY = HandlerRegistry()
