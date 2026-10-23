"""Static capability statements; all external adapters remain unconfigured."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .registry import HandlerRegistry, PRODUCTION_HANDLER_REGISTRY


class ReadCapabilityRegistry(Protocol):
    def has(self, site_id: str, capability_id: str) -> bool: ...


CapabilityStatus = Literal[
    "needs_configuration",
    "needs_adapter_verification",
    "unsupported_action",
    "disabled",
    "ready_local",
]


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    site_id: str
    capability_id: str
    name: str
    status: CapabilityStatus
    reason: str
    target_configured: bool = False
    read_registered: bool = False
    handler_registered: bool = False

    def public_data(self) -> dict[str, object]:
        return {
            "site_id": self.site_id,
            "capability_id": self.capability_id,
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "target_configured": self.target_configured,
            "read_registered": self.read_registered,
            "handler_registered": self.handler_registered,
        }


def capability_snapshot(
    *,
    configured_sites: frozenset[str] = frozenset(),
    read_registry: ReadCapabilityRegistry | None = None,
    handler_registry: HandlerRegistry = PRODUCTION_HANDLER_REGISTRY,
) -> tuple[CapabilityDescriptor, ...]:
    """Return capability state without probing the network or auto-discovering handlers."""

    result: list[CapabilityDescriptor] = []
    for item in DEFAULT_CAPABILITIES:
        target_configured = item.site_id in configured_sites
        read_registered = bool(read_registry and read_registry.has(item.site_id, item.capability_id))
        handler_registered = handler_registry.has(item.capability_id)
        if item.status in {"disabled", "unsupported_action"}:
            status = item.status
            reason = item.reason
        elif not target_configured:
            status = "needs_configuration"
            reason = "没有配置明确目标；不会发起网络请求。"
        elif item.site_id == "hexi" and not handler_registered:
            status = "needs_adapter_verification"
            reason = "没有已核实并注册的 handler；不会请求目标站点。"
        elif item.site_id != "hexi" and not read_registered:
            status = "needs_adapter_verification"
            reason = "没有已核实并注册的只读 connector；不会请求目标站点。"
        else:
            status = "ready_local"
            reason = "目标已配置且对应 connector/handler 已在本机显式注册；外部写动作仍需独立授权。"
        result.append(
            CapabilityDescriptor(
                site_id=item.site_id,
                capability_id=item.capability_id,
                name=item.name,
                status=status,
                reason=reason,
                target_configured=target_configured,
                read_registered=read_registered,
                handler_registered=handler_registered,
            )
        )
    return tuple(result)


_BILIBILI_READS = (
    ("bilibili.search", "搜索"),
    ("bilibili.details", "详情"),
    ("bilibili.subtitles", "字幕"),
    ("bilibili.open", "打开"),
    ("bilibili.scroll", "滚动"),
    ("bilibili.play", "播放"),
    ("bilibili.screenshot", "截图"),
)
_HEXI_WRITES = (
    ("hexi.post.create_draft", "创建自有草稿"),
    ("hexi.media.upload", "上传获批媒体"),
    ("hexi.post.edit", "编辑已登记草稿"),
    ("hexi.post.publish", "发布自有草稿"),
    ("hexi.discussion.reply", "获批讨论回复"),
)


DEFAULT_CAPABILITIES: tuple[CapabilityDescriptor, ...] = tuple(
    CapabilityDescriptor(
        site_id="bilibili",
        capability_id=capability_id,
        name=f"B站只读：{name}",
        status="needs_configuration",
        reason="没有配置目标或已核实的站点路径；不会发起网络请求。",
    )
    for capability_id, name in _BILIBILI_READS
) + tuple(
    CapabilityDescriptor(
        site_id="hexi",
        capability_id=capability_id,
        name=f"Hexi：{name}",
        status="needs_adapter_verification",
        reason="候选 API handler 和权限契约尚未核实；不能准备或派发外部写动作。",
    )
    for capability_id, name in _HEXI_WRITES
) + (
    CapabilityDescriptor(
        site_id="codex",
        capability_id="codex.task.events",
        name="Codex 官方任务事件",
        status="needs_adapter_verification",
        reason="尚未接入可验证的官方事件源；不以窗口或本地文件推断任务状态。",
    ),
    CapabilityDescriptor(
        site_id="room",
        capability_id="room.energy.read",
        name="指定房间电量只读",
        status="needs_configuration",
        reason="尚未配置已确认的房间 API；不会发现或访问其他房间。",
    ),
)
