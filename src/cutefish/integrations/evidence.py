"""Short-lived server-issued references to source evidence read by connectors."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable

from .contracts import SourceEvidence
from .readonly import FetchedDocument


class EvidenceStoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class _EvidenceEntry:
    evidence: SourceEvidence
    expires_at: float


class EvidenceStore:
    """Bounded, in-process evidence references; references expire on restart/TTL."""

    def __init__(
        self,
        *,
        max_items: int = 500,
        ttl_seconds: int = 1800,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(max_items) is not int or not 1 <= max_items <= 10_000:
            raise ValueError("max_items must be between 1 and 10000")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86_400:
            raise ValueError("ttl_seconds must be between 1 second and 24 hours")
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._items: dict[str, _EvidenceEntry] = {}
        self._lock = threading.RLock()

    def register(self, evidence: SourceEvidence) -> str:
        now = self._clock()
        with self._lock:
            self._discard_expired(now)
            if len(self._items) >= self.max_items:
                raise EvidenceStoreError("evidence_store_full", "来源证据引用已达到本机短期上限。")
            reference_id = str(uuid.uuid4())
            self._items[reference_id] = _EvidenceEntry(evidence=evidence, expires_at=now + self.ttl_seconds)
            return reference_id

    def register_document(self, document: FetchedDocument) -> str:
        return self.register(document.evidence)

    def resolve(self, reference_ids: Iterable[str]) -> tuple[SourceEvidence, ...]:
        refs = tuple(reference_ids)
        if len(refs) > 32:
            raise EvidenceStoreError("too_many_source_refs", "单个动作最多绑定32条来源证据。")
        if len(set(refs)) != len(refs):
            raise EvidenceStoreError("duplicate_source_ref", "来源证据引用不能重复。")
        now = self._clock()
        with self._lock:
            self._discard_expired(now)
            entries = [self._items.get(reference_id) for reference_id in refs]
            if any(entry is None for entry in entries):
                raise EvidenceStoreError("source_evidence_missing", "来源证据不存在或已过期，请重新读取来源。")
            return tuple(entry.evidence for entry in entries if entry is not None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _discard_expired(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            del self._items[key]
