"""The single injected generator (Meterless rule: engines never call a model directly).

    generator(prompt, attachments=None, system_prompt=None) -> {"text": str, "usage": {...}}

- `AnthropicGenerator`: Claude via the Anthropic SDK (model from WHYTRAIL_MODEL,
  default claude-opus-5-5), low effort, server-side refusal fallback enabled.
- `MockGenerator`: deterministic, offline. Used by SAMPLE_MODE / --mock so judges can
  run the full loop with no credentials. It derives its answers from the structured
  attachments it receives (the same data a live model would get), not from fixed text.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from .queries import HYPOTHESES

Generator = Callable[..., dict[str, Any]]

PRICES_PER_MTOK = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.1, 0.5)}


def parse_json(text: str) -> Any:
    """Robust JSON extraction: strips code fences and surrounding prose."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i : j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object found in model output")


class AnthropicGenerator:
    def __init__(self, model: str | None = None, api_key: str | None = None, max_cost_usd: float = 1.0) -> None:
        import anthropic  # imported lazily so mock mode needs no SDK

        self.model = model or os.environ.get("WHYTRAIL_MODEL", "claude-opus-5-5")
        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"), timeout=90.0)
        self.max_cost_usd = max_cost_usd
        self.cost_usd = 0.0
        self.calls = 0

    def __call__(self, prompt: str, attachments: dict | None = None, system_prompt: str | None = None) -> dict[str, Any]:
        if self.cost_usd >= self.max_cost_usd:
            raise RuntimeError(f"run budget ${self.max_cost_usd:.2f} reached")
        content = prompt
        if attachments is not None:
            content += "\n\nCONTEXT (JSON):\n" + json.dumps(attachments, default=str)
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=4000,
            system=system_prompt or "",
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        )
        self.calls += 1
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined the request")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        pin, pout = PRICES_PER_MTOK.get(self.model, (4.0, 20.0))
        cost = (resp.usage.input_tokens * pin + resp.usage.output_tokens * pout) / 1e6
        self.cost_usd += cost
        return {"text": text, "usage": {"input_tokens": resp.usage.input_tokens,
                                        "output_tokens": resp.usage.output_tokens, "cost_usd": round(cost, 5)}}


class MockGenerator:
    """Offline generator. Dispatches on the ROLE line of the system prompt."""

    calls = 0
    cost_usd = 0.0
    model = "mock"

    def __call__(self, prompt: str, attachments: dict | None = None, system_prompt: str | None = None) -> dict[str, Any]:
        self.calls += 1
        role = re.search(r"ROLE:\s*(\w+)", system_prompt or "")
        role = role.group(1).lower() if role else ""
        a = attachments or {}
        out = getattr(self, f"_{role}", self._unknown)(a)
        text = out if isinstance(out, str) else json.dumps(out)
        return {"text": text, "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}}

    def _unknown(self, a: dict) -> dict:
        return {}

    def _planner(self, a: dict) -> dict:
        skip = {m["data"]["hypothesis"]: m["id"] for m in a.get("memory", {}).get("memories", [])
                if m["data"].get("verdict") == "refuted" and m["data"].get("signature") == a.get("signature")}
        chosen = [h for h in HYPOTHESES if h not in skip]
        return {"hypotheses": [{"id": h, "why": "competing explanation; test can falsify it"} for h in chosen],
                "skipped": [{"id": h, "memory_id": mid} for h, mid in skip.items()]}

    def _skeptic(self, a: dict) -> dict:
        # The offline Skeptic always asks for "isolate_daytype" so the recorded sample runs stay reproducible;
        # a live Skeptic may pick any test from queries.FOLLOWUP_TESTS (e.g. "baseline_variation").
        challenges = []
        for h in a.get("supported", []):
            if h == "H_weather":
                alt = "The drop may be the holiday alone; check whether snow days drop on their own."
            elif h == "H_holiday":
                alt = "The holiday may coincide with bad weather; check the holiday day in isolation."
            else:
                alt = "A coincident event may explain this; isolate the affected days."
            challenges.append({"hypothesis": h, "alternative": alt, "test": "isolate_daytype"})
        return {"challenges": challenges}

    def _writer(self, a: dict) -> str:
        r = a["result"]
        lines = [f"{r['segment']} trips changed {r['change_pct']:+.1f}% "
                 f"({r['metric_a']:,.0f} -> {r['metric_b']:,.0f}) from {r['period_a']} to {r['period_b']}."]
        causes = [c for c in r["attribution"] if c["daytype"] != "other" and c["lost_trips"] > 0]
        if causes:
            parts = [f"{c['daytype']} days ({', '.join(c['dates'])}) account for {c['lost_trips']:,.0f} fewer trips "
                     f"({c['share_pct']:.0f}% of the gross loss)" for c in causes]
            lines.append("Why: " + "; ".join(parts) + ".")
        other = next((c for c in r["attribution"] if c["daytype"] == "other"), None)
        if other:
            lines.append(f"Ordinary days changed by {-other['lost_trips']:+,.0f} trips, so the remainder is noise-level.")
        if r["refuted"]:
            lines.append("Ruled out: " + ", ".join(r["refuted"]) + ".")
        lines.append("This conclusion would be overturned if " + r["falsifier"])
        return " ".join(lines)


def make_generator(mock: bool, max_cost_usd: float = 1.0) -> Generator:
    return MockGenerator() if mock else AnthropicGenerator(max_cost_usd=max_cost_usd)
