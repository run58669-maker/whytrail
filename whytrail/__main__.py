"""CLI: python -m whytrail [--mock] "<question>"."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .generator import make_generator
from .orchestrator import WhyTrail
from .zetaris import make_client

DEFAULT_SAMPLE_Q = "Why did Manhattan taxi trips drop in the week of 2024-01-15 compared with the week of 2024-01-08?"
DEFAULT_Q = "Why did Manhattan taxi trips drop in the week of 2025-01-20 compared with the week of 2025-01-13?"


def default_question(sample_mode: bool) -> str:
    return DEFAULT_SAMPLE_Q if sample_mode else DEFAULT_Q


def build(mock_data: bool, mock_llm: bool, memory: str | None, log_dir: str, echo: bool = True,
          max_cost: float = 1.0) -> WhyTrail:
    return WhyTrail(make_client(mock_data), make_generator(mock_llm, max_cost), memory_path=memory, log_dir=log_dir,
                    echo=echo)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whytrail", description="Explain why a metric changed, with an evidence trail.")
    p.add_argument("question", nargs="?", default=None)
    p.add_argument("--mock", action="store_true", help="offline: mock Zetaris data + mock generator (same as SAMPLE_MODE=true)")
    p.add_argument("--mock-data", action="store_true", help="mock Zetaris only, live model")
    p.add_argument("--memory", default=os.environ.get("WHYTRAIL_MEMORY", ".whytrail/hmem.json"))
    p.add_argument("--reset-memory", action="store_true")
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--max-cost", type=float, default=float(os.environ.get("WHYTRAIL_MAX_COST_USD", "1.0")))
    p.add_argument("--json", action="store_true", help="print the full JSON result")
    p.add_argument("--quiet", action="store_true", help="no trace lines on stderr")
    a = p.parse_args(argv)

    sample = a.mock or os.environ.get("SAMPLE_MODE", "").lower() == "true"
    question = a.question or default_question(sample or a.mock_data)
    if a.reset_memory and os.path.exists(a.memory):
        os.remove(a.memory)
    wt = build(sample or a.mock_data, sample, a.memory, a.log_dir, echo=not a.quiet, max_cost=a.max_cost)
    res = wt.run(question)
    if a.json:
        print(json.dumps(res.to_dict(), indent=2, default=str))
        return 0 if res.status == "success" else 1
    if res.status != "success":
        err = res.error or {}
        print(f"ERROR {err.get('type', 'Error')}: {err.get('message', err)}")
        return 1
    r = res.result
    print("\n=== WhyTrail answer ===")
    print(res.answer)
    print(f"\nconfidence={r['confidence']}  supported={r['supported']}")
    print("\n=== why-trail ===")
    for step in res.trail:
        ev = step["evidence"]
        ev_ids = [e["fact_id"] if isinstance(e, dict) else e for e in ev]
        print(f"- {step['step']}: {step['claim']} | {step['because']}  [evidence {', '.join(ev_ids)}]")
    s = res.stats
    print(f"\nzetaris_queries={s['queries']} model_calls={s['model_calls']} cost=${s['model_cost_usd']} "
          f"world_facts={s['world_facts']} hmem_records={s['hmem_records']} seconds={s['seconds']}")
    print(f"trace: {res.log_file}\nworld: {res.world_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
