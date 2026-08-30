from __future__ import annotations

from dataclasses import dataclass, field


MEMORY_SCOPES = {"global", "project"}
MEMORY_KINDS = {"preference", "decision", "reference"}


@dataclass(frozen=True)
class MemoryCandidate:
    scope: str
    kind: str
    title: str
    content: str
    evidence: str
    scope_evidence: str
    durability: str
    confidence: float


@dataclass(frozen=True)
class MemoryItem:
    id: str
    scope: str
    project_id: str | None
    kind: str
    title: str
    content: str
    source: str
    source_session_id: str | None
    source_prompt_index: int | None
    evidence: str | None
    pinned: bool
    status: str
    created_at: str
    updated_at: str


@dataclass
class MemoryProcessResult:
    created: list[MemoryItem] = field(default_factory=list)
    duplicates: list[MemoryItem] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
