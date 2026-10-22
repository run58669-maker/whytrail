"""Meterless H-MEM (minimum set): MemoryStoreService + MemoryRetrievalService.

Persists across runs (JSON file). Mined from runtime events (schema discovery,
hypothesis verdicts, run outcomes). Retrieved memories are injected into the
Planner as a structured payload with trace metadata.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LAYER_WEIGHT = {"long_term": 1.0, "working": 0.8, "short_term": 0.5}


@dataclass
class Memory:
    content: str
    type: str  # factual | procedural | general
    layer: str  # short_term | working | long_term
    tags: list[str]
    source: str
    agentId: str
    runId: str
    data: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.9
    id: str = field(default_factory=lambda: "m_" + uuid.uuid4().hex[:10])
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    accessCount: int = 0
    supersedes: str | None = None


class MemoryStore:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.records: dict[str, Memory] = {}

    def hydrate(self) -> int:
        if self.path and self.path.exists():
            for raw in json.loads(self.path.read_text(encoding="utf-8")):
                m = Memory(**raw)
                self.records[m.id] = m
        # short-term memories do not survive into a new run
        self.records = {k: v for k, v in self.records.items() if v.layer != "short_term"}
        return len(self.records)

    def add(self, m: Memory) -> Memory:
        key = next((t for t in m.tags if t.startswith("key:")), None)
        if key:  # newer record with the same key supersedes the older one
            for old in list(self.records.values()):
                if key in old.tags and old.id != m.id:
                    m.supersedes = old.id
                    del self.records[old.id]
        self.records[m.id] = m
        return m

    def persist(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps([asdict(m) for m in self.records.values()], indent=1), encoding="utf-8")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_:.\-]+", text.lower()))


class MemoryRetrieval:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def retrieve(self, query: str, tags: list[str] | None = None, top_n: int = 8, threshold: float = 0.15) -> dict[str, Any]:
        q = _tokens(query)
        qtags = set(tags or [])
        scored = []
        for m in self.store.records.values():
            kw = len(q & _tokens(m.content)) / (len(q) or 1)
            tg = len(qtags & set(m.tags)) / (len(qtags) or 1)
            score = (0.4 * kw + 0.6 * tg) * LAYER_WEIGHT.get(m.layer, 0.5) * m.confidence
            if score >= threshold:
                scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        picked = scored[:top_n]
        for _, m in picked:
            m.accessCount += 1
        return {
            "retrievalReason": f"query={query!r} tags={sorted(qtags)}",
            "strategy": "task_focused" if qtags else "minimal",
            "memories": [{"id": m.id, "content": m.content, "type": m.type, "layer": m.layer,
                          "tags": m.tags, "data": m.data, "score": round(s, 3), "runId": m.runId}
                         for s, m in picked],
        }
