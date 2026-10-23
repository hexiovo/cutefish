"""Durable, conservative local usage reservations and settlement."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from .contracts import ProviderUsageEvidence
from .db import Database
from .models import BudgetEntry, BudgetPolicy, utc_now
from .settings import Settings


MONEY_SCALE = Decimal("0.000001")
OPEN_STATES = ("reserved", "dispatched", "unknown")
MODEL_CONTEXT_TOKENS = 1_048_576
MAX_MODEL_OUTPUT_TOKENS = 393_216
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
DEEPSEEK_FLASH_PRICE_VERSION = "deepseek-v4.1-flash-1017-cny-peak-2026-09-10-v1"
_SUPPORTED_PRICES = {
    ("deepseek", "deepseek-flash", DEEPSEEK_FLASH_PRICE_VERSION): (
        Decimal("2.000000"),
        Decimal("8.000000"),
    )
}
ENTRY_STATES = frozenset({"reserved", "dispatched", "unknown", "settled", "released"})


class BudgetError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _money(value: Decimal | str | int | float, *, upper_bound: bool) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BudgetError("invalid_cost", "费用必须是有效的十进制金额。", 422) from exc
    if not amount.is_finite() or amount < 0:
        raise BudgetError("invalid_cost", "费用不能为负数或非有限值。", 422)
    rounding = ROUND_CEILING if upper_bound else ROUND_HALF_UP
    return amount.quantize(MONEY_SCALE, rounding=rounding)


def _as_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


class BudgetService:
    def __init__(self, db: Database, settings: Settings, *, get_deployment_profile=None):
        self.db = db
        self.settings = settings
        self.get_deployment_profile = get_deployment_profile or (lambda: getattr(settings, "mode", "local_trial"))
        try:
            self._zone = ZoneInfo(settings.timezone)
        except ZoneInfoNotFoundError:
            if settings.timezone != "Asia/Shanghai":
                raise RuntimeError(f"当前运行时缺少时区数据：{settings.timezone}")
            # Windows Python installations do not always ship an IANA tzdb.
            # Mainland China has used fixed UTC+08:00 since 1991.
            self._zone = timezone(timedelta(hours=8), name="Asia/Shanghai")

    def local_date(self) -> str:
        return utc_now().astimezone(self._zone).date().isoformat()

    async def initialize(self) -> None:
        async with self.db.immediate_transaction() as connection:
            row = (await connection.execute(select(BudgetPolicy.__table__).where(BudgetPolicy.id == 1))).mappings().first()
            if row is None:
                await connection.execute(
                    BudgetPolicy.__table__.insert().values(
                        id=1,
                        daily_limit=Decimal("8.0000"),
                        background_soft_limit=Decimal("3.0000"),
                        background_borrow_limit=Decimal("3.0000"),
                        background_hard_limit=Decimal("6.0000"),
                        version=1,
                        updated_at=utc_now(),
                    )
                )

    async def reserve(
        self,
        *,
        idempotency_key: str,
        max_output_tokens: int,
        purpose: str,
        provider: str,
        model: str,
        price_version: str,
        request_id: str | None = None,
        person_id: str | None = None,
        account_id: str | None = None,
        is_background: bool = False,
    ) -> dict[str, Any]:
        if self.get_deployment_profile() != "local_trial":
            raise BudgetError("paid_calls_disabled", "target_staging 禁止调用付费模型。", 409)
        if not idempotency_key or len(idempotency_key) > 256:
            raise BudgetError("invalid_idempotency_key", "必须提供不超过256字符的请求幂等键。", 422)
        if not purpose or len(purpose) > 128:
            raise BudgetError("invalid_purpose", "必须提供有效用途。", 422)
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_MODEL_OUTPUT_TOKENS:
            raise BudgetError("invalid_token_cap", "输出token上限不在模型允许范围内。", 422)
        prices = _SUPPORTED_PRICES.get((provider, model, price_version))
        if prices is None:
            raise BudgetError("pricing_unavailable", "没有匹配的核验模型价格版本，拒绝付费派发。", 422)
        input_rate, output_rate = prices
        estimate = _money(
            (Decimal(MODEL_CONTEXT_TOKENS) * input_rate + Decimal(max_output_tokens) * output_rate)
            / Decimal(1_000_000),
            upper_bound=True,
        )
        max_input_tokens = MODEL_CONTEXT_TOKENS

        local_date = self.local_date()
        async with self.db.immediate_transaction() as connection:
            existing = (
                await connection.execute(
                    select(BudgetEntry.__table__).where(BudgetEntry.idempotency_key == idempotency_key)
                )
            ).mappings().first()
            if existing is not None:
                same_request = (
                    Decimal(existing["estimated_cost"]) == estimate
                    and existing["purpose"] == purpose
                    and existing["provider"] == provider
                    and existing["model"] == model
                    and existing["request_id"] == request_id
                    and existing["person_id"] == person_id
                    and existing["account_id"] == account_id
                    and bool(existing["is_background"]) == bool(is_background)
                    and existing["price_version"] == price_version
                    and existing["max_input_tokens"] == max_input_tokens
                    and existing["max_output_tokens"] == max_output_tokens
                    and Decimal(existing["input_price_per_million"]) == input_rate
                    and Decimal(existing["output_price_per_million"]) == output_rate
                )
                if not same_request:
                    raise BudgetError("idempotency_conflict", "同一幂等键对应了不同的费用请求。", 409)
                return self._serialize(existing)

            policy = await self._policy(connection)
            totals = await self._totals(connection, local_date)
            total_projected = totals["spent_today"] + totals["open_all"] + estimate
            if total_projected > Decimal(policy["daily_limit"]):
                raise BudgetError("daily_budget_exceeded", "请求费用上界超过今日剩余额度。", 429)
            background_projected = totals["background_spent_today"] + totals["background_open_all"]
            if is_background:
                background_projected += estimate
                if background_projected > Decimal(policy["background_hard_limit"]):
                    raise BudgetError("background_budget_exceeded", "请求费用上界超过后台硬额度。", 429)

            reservation_id = str(uuid.uuid4())
            now = utc_now()
            values = {
                "reservation_id": reservation_id,
                "idempotency_key": idempotency_key,
                "provider": provider,
                "model": model,
                "request_id": request_id,
                "purpose": purpose,
                "person_id": person_id,
                "account_id": account_id,
                "local_date": local_date,
                "currency": "CNY",
                "estimated_cost": estimate,
                "actual_cost": None,
                "state": "reserved",
                "is_background": bool(is_background),
                "input_tokens": None,
                "output_tokens": None,
                "cache_tokens": None,
                "max_input_tokens": max_input_tokens,
                "max_output_tokens": max_output_tokens,
                "input_price_per_million": input_rate,
                "output_price_per_million": output_rate,
                "price_version": price_version.strip(),
                "created_at": now,
            }
            await connection.execute(BudgetEntry.__table__.insert().values(**values))
            return self._serialize(values)

    async def mark_dispatched(self, reservation_id: str) -> dict[str, Any]:
        async with self.db.immediate_transaction() as connection:
            row = await self._entry(connection, reservation_id)
            if row is None:
                raise BudgetError("reservation_not_found", "费用预留不存在。", 404)
            if row["state"] == "dispatched":
                return self._serialize(row)
            if row["state"] != "reserved":
                raise BudgetError("invalid_reservation_state", "只有未派发的预留可以派发。", 409)
            now = utc_now()
            await connection.execute(
                BudgetEntry.__table__.update()
                .where(BudgetEntry.reservation_id == reservation_id)
                .values(state="dispatched", dispatched_at=now)
            )
            return self._serialize({**row, "state": "dispatched", "dispatched_at": now})

    async def settle_verified(self, evidence: ProviderUsageEvidence) -> dict[str, Any]:
        """Settle a dispatched call from usage evidence bound to its reservation."""
        return await self._settle_verified(evidence, require_unknown=False)

    async def reconcile_unknown(self, evidence: ProviderUsageEvidence) -> dict[str, Any]:
        """Resolve unknown only from a provider receipt tied to this reservation."""
        return await self._settle_verified(evidence, require_unknown=True)

    async def _settle_verified(
        self,
        evidence: ProviderUsageEvidence,
        *,
        require_unknown: bool,
    ) -> dict[str, Any]:
        if not isinstance(evidence, ProviderUsageEvidence):
            raise BudgetError("usage_evidence_required", "结算必须提供供应商核验用量凭据。", 422)
        provider_request_id = evidence.provider_request_id.strip()
        evidence_ref = evidence.evidence_ref.strip()
        if not provider_request_id or len(provider_request_id) > 256 or not evidence_ref or len(evidence_ref) > 512:
            raise BudgetError("usage_evidence_required", "结算必须提供有效供应商请求ID和证据引用。", 422)
        for name, value in (
            ("input_tokens", evidence.input_tokens),
            ("output_tokens", evidence.output_tokens),
        ):
            if type(value) is not int or value < 0:
                raise BudgetError("invalid_usage", f"{name}必须是非负整数。", 422)
        if evidence.cache_tokens is not None and (
            type(evidence.cache_tokens) is not int
            or evidence.cache_tokens < 0
            or evidence.cache_tokens > evidence.input_tokens
        ):
            raise BudgetError("invalid_usage", "cache_tokens不能超过input_tokens。", 422)
        actual = _money(evidence.actual_cost, upper_bound=False)

        async with self.db.immediate_transaction() as connection:
            row = await self._entry(connection, evidence.reservation_id)
            if row is None:
                raise BudgetError("reservation_not_found", "费用预留不存在。", 404)
            if row["provider"] != evidence.provider or row["model"] != evidence.model:
                raise BudgetError("usage_evidence_mismatch", "供应商用量凭据与费用预留不匹配。", 409)
            if row["provider_request_id"] and row["provider_request_id"] != provider_request_id:
                raise BudgetError("usage_evidence_mismatch", "供应商请求ID与已登记结果不匹配。", 409)
            if row["state"] == "settled":
                same_evidence = (
                    Decimal(row["actual_cost"]) == actual
                    and row["provider_request_id"] == provider_request_id
                    and row["settlement_evidence_ref"] == evidence_ref
                    and row["input_tokens"] == evidence.input_tokens
                    and row["output_tokens"] == evidence.output_tokens
                    and row["cache_tokens"] == evidence.cache_tokens
                )
                if same_evidence:
                    return self._serialize(row)
                raise BudgetError("settlement_conflict", "同一费用预留不能以不同供应商结果重复结算。", 409)
            if row["state"] not in ({"unknown"} if require_unknown else {"dispatched", "unknown"}):
                raise BudgetError("invalid_reservation_state", "只有已派发或未知的费用预留可以结算。", 409)
            if evidence.input_tokens > int(row["max_input_tokens"] or 0) or evidence.output_tokens > int(
                row["max_output_tokens"] or 0
            ):
                raise BudgetError("usage_exceeded_bound", "供应商用量超过该费用预留保存的token上限。", 409)

            now = utc_now()
            await connection.execute(
                BudgetEntry.__table__.update()
                .where(BudgetEntry.reservation_id == evidence.reservation_id)
                .values(
                    state="settled",
                    actual_cost=actual,
                    input_tokens=evidence.input_tokens,
                    output_tokens=evidence.output_tokens,
                    cache_tokens=evidence.cache_tokens,
                    provider_request_id=provider_request_id,
                    settlement_evidence_ref=evidence_ref,
                    settled_at=now,
                )
            )
            return self._serialize(
                {
                    **row,
                    "state": "settled",
                    "actual_cost": actual,
                    "input_tokens": evidence.input_tokens,
                    "output_tokens": evidence.output_tokens,
                    "cache_tokens": evidence.cache_tokens,
                    "provider_request_id": provider_request_id,
                    "settlement_evidence_ref": evidence_ref,
                    "settled_at": now,
                }
            )

    async def mark_unknown(self, reservation_id: str) -> dict[str, Any]:
        async with self.db.immediate_transaction() as connection:
            row = await self._entry(connection, reservation_id)
            if row is None:
                raise BudgetError("reservation_not_found", "费用预留不存在。", 404)
            if row["state"] in {"reserved", "settled", "released"}:
                raise BudgetError("invalid_reservation_state", "已结算或已释放的预留不能改为未知。", 409)
            if row["state"] == "unknown":
                return self._serialize(row)
            await connection.execute(
                BudgetEntry.__table__.update()
                .where(BudgetEntry.reservation_id == reservation_id)
                .values(state="unknown")
            )
            return self._serialize({**row, "state": "unknown"})

    async def release(self, reservation_id: str) -> dict[str, Any]:
        async with self.db.immediate_transaction() as connection:
            row = await self._entry(connection, reservation_id)
            if row is None:
                raise BudgetError("reservation_not_found", "费用预留不存在。", 404)
            if row["state"] == "released":
                return self._serialize(row)
            if row["state"] != "reserved":
                raise BudgetError("release_after_dispatch", "已派发请求不能释放费用预留；结果不明时保留为unknown。", 409)
            now = utc_now()
            await connection.execute(
                BudgetEntry.__table__.update()
                .where(BudgetEntry.reservation_id == reservation_id)
                .values(state="released", settled_at=now)
            )
            return self._serialize({**row, "state": "released", "settled_at": now})

    async def snapshot(self) -> dict[str, Any]:
        local_date = self.local_date()
        async with self.db.engine.connect() as connection:
            policy = await self._policy(connection)
            totals = await self._totals(connection, local_date)
        spent = totals["spent_today"]
        reserved = totals["reserved_all"]
        unknown = totals["unknown_all"]
        daily_limit = Decimal(policy["daily_limit"])
        background_soft = Decimal(policy["background_soft_limit"])
        background_borrow = Decimal(policy["background_borrow_limit"])
        background_hard = Decimal(policy["background_hard_limit"])
        background_spent = totals["background_spent_today"]
        background_reserved = totals["background_reserved_all"]
        background_unknown = totals["background_unknown_all"]
        open_total = reserved + unknown
        open_background = background_reserved + background_unknown
        return {
            "spent": _as_float(spent),
            "reserved": _as_float(reserved),
            "unknown": _as_float(unknown),
            "limit": _as_float(daily_limit),
            "available": _as_float(max(Decimal("0"), daily_limit - spent - open_total)),
            "timezone": self.settings.timezone,
            "local_date": local_date,
            "background_soft_limit": _as_float(background_soft),
            "background_borrow_limit": _as_float(background_borrow),
            "background_hard_limit": _as_float(background_hard),
            "background_spent": _as_float(background_spent),
            "background_reserved": _as_float(background_reserved),
            "background_unknown": _as_float(background_unknown),
            "background_available": _as_float(max(Decimal("0"), background_hard - background_spent - open_background)),
            "currency": "CNY",
            "state": "over_limit" if spent + open_total > daily_limit else "ready",
            "policy_version": int(policy["version"]),
        }

    async def list_entries(
        self,
        *,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if state is not None and state not in ENTRY_STATES:
            raise BudgetError("invalid_entry_state", "预算流水状态无效。", 422)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise BudgetError("invalid_pagination", "预算流水每页需为1到200条。", 422)
        if type(offset) is not int or offset < 0 or offset > 1_000_000:
            raise BudgetError("invalid_pagination", "预算流水偏移量无效。", 422)

        conditions = [BudgetEntry.__table__.c.state == state] if state is not None else []
        async with self.db.engine.connect() as connection:
            total = int(
                await connection.scalar(
                    select(func.count()).select_from(BudgetEntry.__table__).where(*conditions)
                )
                or 0
            )
            rows = (
                await connection.execute(
                    select(BudgetEntry.__table__)
                    .where(*conditions)
                    .order_by(BudgetEntry.__table__.c.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).mappings().all()
        return {"items": [self._serialize(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    async def _entry(self, connection, reservation_id: str):
        return (
            await connection.execute(
                select(BudgetEntry.__table__).where(BudgetEntry.reservation_id == reservation_id)
            )
        ).mappings().first()

    async def _policy(self, connection):
        row = (
            await connection.execute(select(BudgetPolicy.__table__).where(BudgetPolicy.id == 1))
        ).mappings().first()
        if row is None:
            # Initialize is required by Context.startup. Fail closed if a caller bypasses it.
            raise BudgetError("budget_not_initialized", "预算策略尚未初始化。", 503)
        return row

    async def _totals(self, connection, local_date: str) -> dict[str, Decimal]:
        table = BudgetEntry.__table__

        async def total(*conditions) -> Decimal:
            value = await connection.scalar(select(func.coalesce(func.sum(table.c.actual_cost), 0)).where(*conditions))
            return Decimal(value or 0)

        async def estimates(*conditions) -> Decimal:
            value = await connection.scalar(select(func.coalesce(func.sum(table.c.estimated_cost), 0)).where(*conditions))
            return Decimal(value or 0)

        settled_today = (table.c.state == "settled", table.c.local_date == local_date)
        open_states = table.c.state.in_(OPEN_STATES)
        unknown_state = table.c.state == "unknown"
        reserved_states = table.c.state.in_(("reserved", "dispatched"))
        background = table.c.is_background.is_(True)
        return {
            "spent_today": await total(*settled_today),
            "reserved_all": await estimates(reserved_states),
            "unknown_all": await estimates(unknown_state),
            "open_all": await estimates(open_states),
            "background_spent_today": await total(*settled_today, background),
            "background_reserved_all": await estimates(reserved_states, background),
            "background_unknown_all": await estimates(unknown_state, background),
            "background_open_all": await estimates(open_states, background),
        }

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        return {
            "reservation_id": row["reservation_id"],
            "idempotency_key": row["idempotency_key"],
            "provider": row.get("provider"),
            "model": row.get("model"),
            "request_id": row.get("request_id"),
            "provider_request_id": row.get("provider_request_id"),
            "purpose": row["purpose"],
            "person_id": row.get("person_id"),
            "account_id": row.get("account_id"),
            "local_date": row["local_date"],
            "estimated_cost": _as_float(Decimal(row["estimated_cost"])),
            "actual_cost": _as_float(Decimal(row["actual_cost"])) if row.get("actual_cost") is not None else None,
            "state": row["state"],
            "is_background": bool(row["is_background"]),
            "currency": row["currency"],
            "price_version": row.get("price_version"),
            "max_input_tokens": row.get("max_input_tokens"),
            "max_output_tokens": row.get("max_output_tokens"),
            "input_price_per_million": _as_float(
                Decimal(row["input_price_per_million"])
            ) if row.get("input_price_per_million") is not None else None,
            "output_price_per_million": _as_float(
                Decimal(row["output_price_per_million"])
            ) if row.get("output_price_per_million") is not None else None,
            "settlement_evidence_ref": row.get("settlement_evidence_ref"),
            "input_tokens": row.get("input_tokens"),
            "output_tokens": row.get("output_tokens"),
            "cache_tokens": row.get("cache_tokens"),
        }
