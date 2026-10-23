"""Local administrator sessions and double-submit CSRF protection."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import AdminAccount, AuthSession, utc_now


SESSION_COOKIE = "cutefish_session"
CSRF_COOKIE = "cutefish_csrf"
CSRF_HEADER = "x-csrf-token"
SESSION_TTL = timedelta(hours=12)


class AuthError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class SessionIssue:
    token: str
    csrf_token: str
    session_id: str


@dataclass(frozen=True, slots=True)
class OwnerPrincipal:
    """Authenticated local administrator identity; distinct from chat actors."""

    admin_id: int
    session_id: str
    role: Literal["owner"] = "owner"


class AuthService:
    def __init__(self, session_factory: async_sessionmaker):
        self.session_factory = session_factory
        self.password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)

    async def setup_required(self) -> bool:
        async with self.session_factory() as session:
            return await session.scalar(select(AdminAccount.id).limit(1)) is None

    async def bootstrap(self, token: str | None = None) -> dict[str, object]:
        auth_session = await self.get_session(token)
        csrf_token = auth_session.csrf_token if auth_session is not None else secrets.token_urlsafe(32)
        return {"setup_required": await self.setup_required(), "csrf_token": csrf_token}

    async def create_admin(self, password: str) -> SessionIssue:
        if len(password) < 10 or len(password) > 256:
            raise AuthError("password_policy", "管理员口令长度需为10到256个字符。", 422)
        async with self.session_factory() as session:
            async with session.begin():
                existing = await session.scalar(select(AdminAccount.id).limit(1))
                if existing is not None:
                    raise AuthError("setup_complete", "管理员已设置，请使用登录。", 409)
                account = AdminAccount(username="owner", password_hash=self.password_hasher.hash(password))
                session.add(account)
                await session.flush()
                return await self._new_session(session)

    async def login(self, password: str) -> SessionIssue:
        async with self.session_factory() as session:
            account = await session.scalar(select(AdminAccount).limit(1))
            if account is None:
                raise AuthError("setup_required", "请先设置管理员口令。", 409)
            try:
                valid = self.password_hasher.verify(account.password_hash, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                valid = False
            if not valid:
                raise AuthError("invalid_credentials", "口令不正确。", 401)
            if self.password_hasher.check_needs_rehash(account.password_hash):
                account.password_hash = self.password_hasher.hash(password)
            issue = await self._new_session(session)
            await session.commit()
            return issue

    async def _new_session(self, session) -> SessionIssue:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session_id = secrets.token_urlsafe(24)
        session.add(
            AuthSession(
                id=session_id,
                token_hash=self._hash(token),
                csrf_token=csrf_token,
                created_at=utc_now(),
                expires_at=utc_now() + SESSION_TTL,
            )
        )
        await session.flush()
        return SessionIssue(token=token, csrf_token=csrf_token, session_id=session_id)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def get_session(self, token: str | None) -> AuthSession | None:
        if not token:
            return None
        async with self.session_factory() as session:
            row = await session.scalar(select(AuthSession).where(AuthSession.token_hash == self._hash(token)))
            if row is None or row.revoked_at is not None:
                return None
            expires_at = row.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= utc_now():
                return None
            return row

    async def session_data(self, token: str | None) -> dict[str, object]:
        setup_required = await self.setup_required()
        auth_session = await self.get_session(token)
        if auth_session is None:
            return {
                "authenticated": False,
                "setup_required": setup_required,
                "principal": None,
                "csrf_token": None,
            }
        return {
            "authenticated": True,
            "setup_required": False,
            "principal": {"id": "owner", "role": "owner", "display_name": "管理员"},
            "csrf_token": auth_session.csrf_token,
        }

    async def require_admin(self, token: str | None) -> OwnerPrincipal:
        auth_session = await self.get_session(token)
        if auth_session is None:
            raise AuthError("needs_login", "此操作需要管理员登录。", 401)
        return OwnerPrincipal(admin_id=1, session_id=auth_session.id)

    async def verify_csrf(self, *, token: str | None, cookie_token: str | None, header_token: str | None) -> None:
        if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
            raise AuthError("csrf_failed", "请求保护令牌无效，请刷新页面后重试。", 403)
        auth_session = await self.get_session(token)
        if auth_session is not None and not hmac.compare_digest(auth_session.csrf_token, header_token):
            raise AuthError("csrf_failed", "请求保护令牌已过期，请刷新页面后重试。", 403)

    async def logout(self, token: str | None) -> None:
        if not token:
            return
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(AuthSession).where(AuthSession.token_hash == self._hash(token)))
                if row is not None and row.revoked_at is None:
                    row.revoked_at = utc_now()

