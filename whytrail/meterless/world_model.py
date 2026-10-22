"""Meterless World Model (minimum set): shared, provenance-carrying evidence ledger.

Entities: metric, segment, period, source, hypothesis.
Facts: query results with provenance (SQL, source tables, row count, result hash, time).
Edges: fact --measures/derived_from--> entity, fact --supports/refutes--> hypothesis.

Ingest is idempotent: a fact id is derived from (sql, result hash), so ingesting the
same observation twice is a no-op. Every write is appended to logs/world-<run_id>.jsonl.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_RE = re.compile(r"\b([A-Z][A-Z0-9_]+)\.([a-z][a-z0-9_]*)\b")


@dataclass
class Fact:
    id: str
    kind: str
    value: Any
    entities: list[str]
    provenance: dict[str, Any]


@dataclass
class Edge:
    src: str
    rel: str
    dst: str


class WorldStore:
    def __init__(self, run_id: str, log_dir: str | Path) -> None:
        self.run_id = run_id
        self.entities: dict[str, dict[str, Any]] = {}
        self.facts: dict[str, Fact] = {}
        self.edges: list[Edge] = []
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"world-{run_id}.jsonl"
        self.path.write_text("", encoding="utf-8")

    def _append(self, op: str, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"op": op, **payload}, default=str) + "\n")

    def upsert_entity(self, eid: str, etype: str, **attrs: Any) -> None:
        cur = self.entities.get(eid, {"id": eid, "type": etype})
        cur.update(attrs)
        self.entities[eid] = cur
        self._append("entity", cur)

    def add_edge(self, src: str, rel: str, dst: str) -> None:
        e = Edge(src, rel, dst)
        if e not in self.edges:
            self.edges.append(e)
            self._append("edge", asdict(e))


class IngestPipeline:
    """Canonical write path for query results (subset of the 10-step lifecycle)."""

    def __init__(self, store: WorldStore) -> None:
        self.store = store

    def ingest_query(self, kind: str, result: Any, value: Any, entities: list[str], agent: str) -> Fact:
        fid = "f_" + hashlib.sha256(f"{result.sql}|{result.sha256}".encode()).hexdigest()[:10]
        if fid in self.store.facts:  # idempotent
            return self.store.facts[fid]
        sources = sorted({f"{s}.{t}" for s, t in SOURCE_RE.findall(result.sql)})
        prov = {
            "sql": result.sql,
            "sources": sources,
            "row_count": len(result.rows),
            "result_sha256": result.sha256,
            "channel": result.source,
            "agent": agent,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        fact = Fact(fid, kind, value, entities, prov)
        self.store.facts[fid] = fact
        for src in sources:
            self.store.upsert_entity(f"source:{src}", "source")
            self.store.add_edge(fid, "derived_from", f"source:{src}")
        for e in entities:
            self.store.add_edge(fid, "measures", e)
        self.store._append("fact", asdict(fact))
        return fact


class ContextBuilder:
    """Returns a structured payload (not a rendered string) for a consuming agent.

    `manifest` records which consumer read which facts, so the trace shows the
    World Model is actually loaded, not only stored.
    """

    def __init__(self, store: WorldStore) -> None:
        self.store = store
        self.manifest: list[dict[str, Any]] = []

    def build(self, intent: str, consumer: str, entity_ids: list[str] | None = None, budget: int = 40) -> dict[str, Any]:
        wanted = set(entity_ids or [])
        items = []
        for f in self.store.facts.values():
            rel = 1.0 if wanted & set(f.entities) else (0.3 if not wanted else 0.0)
            if rel > 0:
                items.append({"fact_id": f.id, "kind": f.kind, "value": f.value, "entities": f.entities,
                              "sources": f.provenance["sources"], "relevance": rel})
        items.sort(key=lambda x: -x["relevance"])
        items = items[:budget]
        payload = {
            "intent": intent,
            "consumer": consumer,
            "strategy": "task_focused" if wanted else "comprehensive",
            "selectionReason": f"facts linked to {sorted(wanted)}" if wanted else "all facts",
            "facts": items,
            "hypothesis_edges": [asdict(e) for e in self.store.edges if e.rel in ("supports", "refutes")],
        }
        self.manifest.append({"consumer": consumer, "intent": intent, "fact_ids": [i["fact_id"] for i in items]})
        return payload
