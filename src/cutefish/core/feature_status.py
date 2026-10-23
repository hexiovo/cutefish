"""Read the host's separately maintained, four-stage feature evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, model_validator


ImplementedStatus = Literal["complete", "not_implemented", "partial", "unknown"]
LocalVerifiedStatus = Literal["passed", "partial", "failed", "not_run", "unknown"]
TargetVerifiedStatus = Literal["passed", "partial", "failed", "not_run", "unknown"]
ProductionEnabledStatus = Literal["enabled", "disabled", "unknown"]


class FeatureStageStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: StrictStr = Field(min_length=1, max_length=64)
    evidence_refs: list[StrictStr] = Field(default_factory=list, max_length=256)
    note: StrictStr | None = Field(default=None, max_length=2_000)


class ImplementedStageStatus(FeatureStageStatus):
    status: ImplementedStatus


class LocalVerifiedStageStatus(FeatureStageStatus):
    status: LocalVerifiedStatus


class TargetVerifiedStageStatus(FeatureStageStatus):
    status: TargetVerifiedStatus


class ProductionEnabledStageStatus(FeatureStageStatus):
    status: ProductionEnabledStatus


class FeatureStatusItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    feature_id: StrictStr = Field(min_length=1, max_length=128)
    name: StrictStr = Field(min_length=1, max_length=256)
    implemented: ImplementedStageStatus = Field(default_factory=lambda: ImplementedStageStatus(status="unknown"))
    local_verified: LocalVerifiedStageStatus = Field(default_factory=lambda: LocalVerifiedStageStatus(status="unknown"))
    target_verified: TargetVerifiedStageStatus = Field(default_factory=lambda: TargetVerifiedStageStatus(status="unknown"))
    production_enabled: ProductionEnabledStageStatus = Field(default_factory=lambda: ProductionEnabledStageStatus(status="unknown"))
    status_updated_at: StrictStr | None = None
    evidence_revision: StrictStr | StrictInt | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_stage_statuses(cls, value: Any) -> Any:
        """Map the old display labels into the current stage-specific contract."""
        if not isinstance(value, dict):
            return value
        result = dict(value)
        aliases = {
            "implemented": {"implemented": "complete"},
            "local_verified": {"local_verified": "passed"},
            "target_verified": {"target_verified": "passed", "target_pending": "not_run"},
            "production_enabled": {
                "production_enabled": "enabled",
                "production_disabled": "disabled",
            },
        }
        for field_name, field_aliases in aliases.items():
            stage = result.get(field_name)
            if not isinstance(stage, dict):
                continue
            normalized_stage = dict(stage)
            raw_status = normalized_stage.get("status")
            if isinstance(raw_status, str):
                normalized_stage["status"] = field_aliases.get(raw_status, raw_status)
            result[field_name] = normalized_stage
        return result


class FeatureStatusDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    features: list[FeatureStatusItem] = Field(max_length=256)


class FeatureStatusResponseItem(FeatureStatusItem):
    """Four-stage file state plus compatibility fields used by the UI."""

    requirement_status: StrictStr = "unknown"
    delivery_status: StrictStr = "unknown"
    module_id: StrictStr | None = None
    evidence_refs: list[StrictStr] = Field(default_factory=list, max_length=256)


class FeatureStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source_status: Literal["available", "unknown"]
    items: list[FeatureStatusResponseItem] = Field(max_length=256)


class FeatureStatusReader:
    """Return unknown on absent or malformed status files without exposing paths."""

    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> tuple[Literal["available", "unknown"], list[FeatureStatusItem]]:
        try:
            if not self.path.is_file() or self.path.stat().st_size > self.MAX_BYTES:
                return "unknown", []
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
            document = FeatureStatusDocument.model_validate(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
            return "unknown", []
        ids = [item.feature_id for item in document.features]
        if len(ids) != len(set(ids)):
            return "unknown", []
        return "available", list(document.features)


def unknown_feature_stage() -> dict[str, Any]:
    return {"status": "unknown", "evidence_refs": [], "note": "状态源不可用。"}


def runtime_production_stage(
    feature_id: str,
    stage: ProductionEnabledStageStatus | dict[str, Any],
    *,
    deployment_profile: str,
    runtime_policy_valid: bool,
    runtime_enabled_features: Any = (),
) -> ProductionEnabledStageStatus:
    """Clamp file evidence to the current runtime; static metadata never enables production."""
    if not isinstance(stage, ProductionEnabledStageStatus):
        stage = ProductionEnabledStageStatus.model_validate(stage)
    allowlist = runtime_enabled_features
    explicitly_enabled = (
        deployment_profile == "target_live"
        and runtime_policy_valid is True
        and isinstance(allowlist, (set, frozenset, tuple, list))
        and feature_id in allowlist
    )
    if stage.status == "enabled" and explicitly_enabled:
        return stage
    if stage.status == "disabled":
        return stage
    note = stage.note
    runtime_note = "正式启用由运行时策略控制；当前运行时未显式允许此功能，已按停用返回。"
    if note:
        note = f"{note} {runtime_note}"
    else:
        note = runtime_note
    return stage.model_copy(update={"status": "disabled", "note": note})
