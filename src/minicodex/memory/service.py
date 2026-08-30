from __future__ import annotations

import re

from .extractor import MemoryExtractionError, MemoryExtractor
from .models import MemoryCandidate, MemoryProcessResult
from .store import MemoryStore


SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(api[_ -]?key|token|password|secret)\s*(?:是|[:=])\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
GLOBAL_SCOPE_MARKERS = (
    "所有项目", "全部项目", "全局", "以后都", "我的偏好", "我习惯", "我喜欢",
    "all projects", "globally", "my preference", "i prefer", "i always",
)


class MemoryService:
    def __init__(self, store: MemoryStore, extractor: MemoryExtractor) -> None:
        self.store = store
        self.extractor = extractor

    def _reject_reason(self, candidate: MemoryCandidate, user_text: str, project_id: str) -> str | None:
        if candidate.durability != "explicit":
            return "memory is not explicitly durable"
        if not candidate.evidence or candidate.evidence not in user_text:
            return "evidence is not present in recent user text"
        if not candidate.scope_evidence or candidate.scope_evidence not in user_text:
            return "scope evidence is not present in recent user text"
        if not 1 <= len(candidate.title.strip()) <= 40 or not 1 <= len(candidate.content.strip()) <= 300:
            return "memory length is invalid"
        if len(candidate.evidence) > 200 or "```" in candidate.content or candidate.content.count("\n") > 4:
            return "memory contains too much code or text"
        if any(pattern.search(f"{candidate.content}\n{candidate.evidence}") for pattern in SECRET_PATTERNS):
            return "memory may contain a secret"
        if candidate.scope == "project" and not project_id:
            return "project memory requires a project"
        if candidate.scope == "global" and not any(marker in candidate.scope_evidence.casefold() for marker in GLOBAL_SCOPE_MARKERS):
            return "global memory lacks explicit cross-project or personal-preference evidence"
        return None

    def process_completed_prompt(
        self,
        *,
        project_id: str,
        project_name: str,
        session_id: str,
        prompt_index: int,
        recent_user_messages: list[str],
    ) -> MemoryProcessResult:
        result = MemoryProcessResult()
        try:
            candidates = self.extractor.extract(
                project_name=project_name,
                recent_user_messages=recent_user_messages,
                existing_index=self.store.index(project_id),
            )
        except Exception as exc:
            result.error = str(exc) if isinstance(exc, MemoryExtractionError) else f"memory extraction failed: {exc}"
            return result
        user_text = "\n".join(recent_user_messages[-2:])
        for candidate in candidates:
            reason = self._reject_reason(candidate, user_text, project_id)
            if reason:
                result.rejected.append({"title": candidate.title, "reason": reason})
                continue
            owner = project_id if candidate.scope == "project" else None
            duplicate = self.store.find_duplicate(scope=candidate.scope, project_id=owner, content=candidate.content)
            if duplicate:
                result.duplicates.append(duplicate)
                continue
            result.created.append(
                self.store.remember(
                    scope=candidate.scope,
                    project_id=owner,
                    kind=candidate.kind,
                    title=candidate.title,
                    content=candidate.content,
                    source="auto",
                    source_session_id=session_id,
                    source_prompt_index=prompt_index,
                    evidence=candidate.evidence,
                )
            )
        return result
