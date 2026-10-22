"""HTTP entry point: POST /run (JSON in, JSON out), GET /health."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .__main__ import DEFAULT_Q, build

app = FastAPI(title="WhyTrail", version="0.1.0")


class RunInput(BaseModel):
    query: str = DEFAULT_Q


class RunOptions(BaseModel):
    sample_mode: bool = Field(default_factory=lambda: os.environ.get("SAMPLE_MODE", "").lower() == "true")


class RunRequest(BaseModel):
    run_id: str | None = None
    input: RunInput = RunInput()
    options: RunOptions = RunOptions()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
def run(req: RunRequest) -> dict[str, Any]:
    sample = req.options.sample_mode
    wt = build(sample, sample, os.environ.get("WHYTRAIL_MEMORY", ".whytrail/hmem.json"), "logs", echo=True)
    return wt.run(req.input.query, run_id=req.run_id).to_dict()
