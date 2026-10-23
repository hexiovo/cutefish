"""Explicitly bounded HTTPS reads with source evidence and no write surface."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Literal, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from PIL import Image, UnidentifiedImageError

from .contracts import SourceEvidence


class FetchError(Exception):
    """A fail-closed error from URL validation, transport, or response validation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ConnectorPolicy:
    """Code-owned allowlist for exactly one site capability.

    These policies are never read from a request body. The application starts
    with no live policies until a site-specific route and response contract are
    verified.
    """

    site_id: str
    capability_id: str
    origin: str
    allowed_paths: frozenset[str]
    allowed_query_keys: frozenset[str] = frozenset()
    allowed_content_types: frozenset[str] = frozenset(
        {"application/json", "text/html", "text/plain", "text/vtt", "image/jpeg", "image/png", "image/webp"}
    )
    max_response_bytes: int = 2_000_000
    timeout_seconds: float = 5.0
    max_query_fields: int = 8
    max_query_value_length: int = 256

    def __post_init__(self) -> None:
        origin = urlsplit(self.origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            or origin.port not in {None, 443}
        ):
            raise ValueError("connector origin must be a bare HTTPS origin")
        host = origin.hostname.casefold()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError("connector origin cannot target a local hostname")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("connector origin cannot target a non-public IP address")
        if not self.allowed_paths or any(not _is_safe_literal_path(path) for path in self.allowed_paths):
            raise ValueError("connector policy must contain exact, safe paths")
        if type(self.max_response_bytes) is not int or not 1 <= self.max_response_bytes <= 10_000_000:
            raise ValueError("max_response_bytes must be between 1 byte and 10 MB")
        if not 0 < self.timeout_seconds <= 15:
            raise ValueError("timeout_seconds must be between 0 and 15 seconds")
        if not 0 <= self.max_query_fields <= 16 or not 1 <= self.max_query_value_length <= 512:
            raise ValueError("query limits are outside the supported bounds")
        if any(not key or len(key) > 64 for key in self.allowed_query_keys):
            raise ValueError("allowed query keys must be bounded names")
        normalized_types = frozenset(value.split(";", 1)[0].strip().lower() for value in self.allowed_content_types)
        if not normalized_types or any("/" not in value for value in normalized_types):
            raise ValueError("allowed_content_types must contain media types")
        object.__setattr__(self, "origin", f"https://{host}")
        object.__setattr__(self, "allowed_content_types", normalized_types)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str


class ReadOnlyTransport(Protocol):
    def get(self, url: str, *, timeout_seconds: float, max_response_bytes: int) -> HttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibReadOnlyTransport:
    """Small HTTPS-only transport; ignores ambient proxy and cookie settings."""

    def get(self, url: str, *, timeout_seconds: float, max_response_bytes: int) -> HttpResponse:
        _require_public_dns(urlsplit(url).hostname or "")
        request = Request(
            url,
            headers={"Accept": "application/json, text/html, text/plain, text/vtt, image/*", "User-Agent": "CuteFish-local-readonly/1"},
            method="GET",
        )
        opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                declared_size = _response_header(response.headers, "content-length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > max_response_bytes:
                            raise FetchError("response_too_large", "来源响应超过大小上限。")
                    except ValueError as exc:
                        raise FetchError("invalid_content_length", "来源响应长度无效。") from exc
                body = response.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    raise FetchError("response_too_large", "来源响应超过大小上限。")
                headers = {"content-type": _response_header(response.headers, "content-type") or ""}
                return HttpResponse(
                    status=int(response.status),
                    headers=headers,
                    body=body,
                    final_url=response.geturl(),
                )
        except FetchError:
            raise
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise FetchError("redirect_blocked", "来源重定向已拒绝。") from exc
            raise FetchError("http_error", f"来源返回 HTTP {exc.code}。") from exc
        except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            raise FetchError("source_unavailable", "来源读取失败或超时。") from exc


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    evidence: SourceEvidence
    content: bytes


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    format: str
    width: int
    height: int
    mode: str


class BoundedReadOnlyConnector:
    """A single policy-bound GET connector. No arbitrary method or header input."""

    def __init__(self, policy: ConnectorPolicy, transport: ReadOnlyTransport | None = None):
        self.policy = policy
        self.transport = transport or UrllibReadOnlyTransport()

    def fetch(
        self,
        url: str,
        *,
        object_id: str,
        read_scope: Literal["metadata", "content", "subtitle", "frame"] = "content",
    ) -> FetchedDocument:
        validated_url = self._validate_url(url)
        response = self.transport.get(
            validated_url,
            timeout_seconds=self.policy.timeout_seconds,
            max_response_bytes=self.policy.max_response_bytes,
        )
        if response.status != 200:
            raise FetchError("unexpected_status", "只接受 HTTP 200 的只读来源响应。")
        if _canonical_url(response.final_url) != _canonical_url(validated_url):
            raise FetchError("redirect_blocked", "来源地址发生变化，已拒绝响应。")
        if len(response.body) > self.policy.max_response_bytes:
            raise FetchError("response_too_large", "来源响应超过大小上限。")
        content_type = _response_header(response.headers, "content-type")
        if not content_type:
            raise FetchError("content_type_required", "来源响应缺少 Content-Type。")
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if normalized_type not in self.policy.allowed_content_types:
            raise FetchError("content_type_blocked", "来源响应类型未列入只读策略。")
        evidence = SourceEvidence(
            site_id=self.policy.site_id,
            capability_id=self.policy.capability_id,
            source_url=validated_url,
            object_id=object_id,
            content_type=normalized_type,
            fetched_at=datetime.now(timezone.utc),
            sha256=hashlib.sha256(response.body).hexdigest(),
            byte_length=len(response.body),
            read_scope=read_scope,
        )
        return FetchedDocument(evidence=evidence, content=response.body)

    def _validate_url(self, url: str) -> str:
        if not isinstance(url, str) or len(url) > 4096:
            raise FetchError("url_invalid", "来源 URL 无效或过长。")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise FetchError("url_blocked", "仅接受策略允许的 HTTPS 来源 URL。")
        if parsed.hostname.casefold() != urlsplit(self.policy.origin).hostname:
            raise FetchError("origin_blocked", "来源主机不在 connector allowlist 中。")
        if parsed.path not in self.policy.allowed_paths or "%" in parsed.path or "\\" in parsed.path:
            raise FetchError("path_blocked", "来源路径不在 connector allowlist 中。")
        if len(parsed.query) > 2048:
            raise FetchError("query_too_large", "来源查询参数超过长度上限。")
        try:
            query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=self.policy.max_query_fields)
        except ValueError as exc:
            raise FetchError("query_invalid", "来源查询参数格式无效。") from exc
        seen: set[str] = set()
        for key, value in query:
            folded = key.casefold()
            if folded in seen:
                raise FetchError("query_duplicate", "来源 URL 含重复查询参数。")
            seen.add(folded)
            if key not in self.policy.allowed_query_keys:
                raise FetchError("query_key_blocked", "来源查询参数不在 connector allowlist 中。")
            if _looks_sensitive_key(key) or len(value) > self.policy.max_query_value_length:
                raise FetchError("query_value_blocked", "来源查询参数疑似包含凭据或超出长度上限。")
        return url


def validate_image(document: FetchedDocument, *, max_pixels: int = 20_000_000) -> ImageMetadata:
    """Decode an allowed image in memory with a strict pixel bound."""

    if not document.evidence.content_type.startswith("image/"):
        raise FetchError("image_type_required", "来源不是图像响应。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(document.content)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > max_pixels:
                    raise FetchError("image_pixel_limit", "图像尺寸超过像素解码上限。")
                image.verify()
            with Image.open(BytesIO(document.content)) as image:
                image.load()
                return ImageMetadata(
                    format=str(image.format or "unknown").upper(),
                    width=width,
                    height=height,
                    mode=image.mode,
                )
    except FetchError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise FetchError("image_decode_failed", "图像无法安全解码。") from exc


def _is_safe_literal_path(path: str) -> bool:
    return (
        isinstance(path, str)
        and path.startswith("/")
        and len(path) <= 512
        and path != "//"
        and ".." not in path.split("/")
        and "\\" not in path
        and "?" not in path
        and "#" not in path
        and "%" not in path
        and "\x00" not in path
    )


def _response_header(headers: Mapping[str, str], key: str) -> str | None:
    target = key.casefold()
    for name, value in headers.items():
        if name.casefold() == target:
            return str(value)
    return None


def _canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    port = parsed.port
    netloc = host if port in {None, 443} else f"{host}:{port}"
    return parsed._replace(netloc=netloc).geturl()


def _looks_sensitive_key(value: str) -> bool:
    folded = value.casefold().replace("-", "_")
    parts = set(folded.split("_"))
    return bool(parts.intersection({"auth", "authorization", "cookie", "csrf", "key", "password", "secret", "session", "signature", "sig", "token"}))


def _require_public_dns(host: str) -> None:
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError("dns_failed", "来源主机名无法解析。") from exc
    if not records:
        raise FetchError("dns_failed", "来源主机名没有地址记录。")
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0])
        except ValueError as exc:
            raise FetchError("dns_failed", "来源主机名返回无效地址。") from exc
        if not address.is_global:
            raise FetchError("private_address_blocked", "来源主机解析到非公网地址，已拒绝连接。")
