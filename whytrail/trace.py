"""Run trace: one JSONL file per run (logs/trace-<run_id>.jsonl) plus readable stdout."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Tracer:
    def __init__(self, run_id: str, log_dir: str | Path = "logs", echo: bool = True) -> None:
        self.run_id = run_id
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"trace-{run_id}.jsonl"
        self.path.write_text("", encoding="utf-8")
        self.echo = echo
        self.events: list[dict[str, Any]] = []

    def event(self, agent: str, action: str, input_summary: str = "", output_summary: str = "",
              status: str = "success", target_agent: str | None = None, confidence: float | None = None,
              retry_count: int = 0, **extra: Any) -> dict[str, Any]:
        rec = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_id": self.run_id,
            "agent_name": agent,
            "action": action,
            "input_summary": input_summary,
            "output_summary": output_summary,
            "target_agent": target_agent,
            "confidence": confidence,
            "retry_count": retry_count,
            "status": status,
        }
        rec.update(extra)
        self.events.append(rec)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if self.echo:
            level = "WARN" if status not in ("success", "skipped") else "INFO"
            msg = output_summary or input_summary
            print(f"[{level}] run_id={self.run_id} {agent}.{action}: {msg}", file=sys.stderr)
        return rec
