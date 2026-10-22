"""WhyTrail orchestrator: a bounded state machine over five agents.

    parse -> hydrate H-MEM -> Planner -> Retriever (Zetaris) -> Analyst verdicts
          -> Skeptic challenges -> Retriever follow-up (federated join) -> Verifier
          -> (Skeptic downgrades / Verifier blocks = needs_revision) -> Writer -> mine H-MEM

Planner, Skeptic and Writer go through the injected generator. Retriever, Analyst and
Verifier are deterministic code: numbers in the answer always come from Zetaris query
results recorded in the World Model, never from model text.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import queries as Q
from .generator import Generator, MockGenerator, parse_json
from .meterless.hmem import Memory, MemoryRetrieval, MemoryStore
from .meterless.world_model import ContextBuilder, IngestPipeline, WorldStore
from .trace import Tracer
from .zetaris import QueryResult, ZetarisError, to_number

VERIFY_TOLERANCE = 0.005


class NoDataError(ValueError):
    """Asked periods have no rows (or baseline is zero); user can rephrase inside the available range."""


AGENTS = [
    {"name": "Planner", "role": "Splits the question into competing, falsifiable hypotheses (reads H-MEM)"},
    {"name": "Retriever", "role": "Runs restricted SQL templates through Zetaris; ingests results into the World Model"},
    {"name": "Skeptic", "role": "Challenges every supported hypothesis with an alternative and demands a follow-up test"},
    {"name": "Verifier", "role": "Re-derives every cited number through an independent Zetaris query; blocks mismatches"},
    {"name": "Writer", "role": "Writes the answer and the why-trail from World Model context only"},
]


@dataclass
class Period:
    start: str
    end: str

    def __str__(self) -> str:
        return f"{self.start}..{self.end}"

    def days(self) -> list[str]:
        d0, d1 = date.fromisoformat(self.start), date.fromisoformat(self.end)
        return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


@dataclass
class Question:
    text: str
    metric: str
    segment: str
    period_a: Period
    period_b: Period

    @property
    def signature(self) -> str:
        return f"{self.metric}|{self.segment}|{self.period_a}|{self.period_b}"


def parse_question(text: str, segments: list[str] | None = None) -> Question:
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if len(dates) == 4:
        a, b = Period(dates[0], dates[1]), Period(dates[2], dates[3])
    elif len(dates) >= 2:
        # "week of X ... week of Y": 7-day windows; the later date is the period being explained
        d1, d2 = sorted(date.fromisoformat(d) for d in dates[:2])
        a = Period(d1.isoformat(), (d1 + timedelta(days=6)).isoformat())
        b = Period(d2.isoformat(), (d2 + timedelta(days=6)).isoformat())
    else:
        raise ValueError("question must name two periods as ISO dates, e.g. 'week of 2024-01-15 vs week of 2024-01-08'")
    seg = next((s for s in (segments or ["Manhattan", "Brooklyn", "Queens"]) if s.lower() in text.lower()), "Manhattan")
    return Question(text, "taxi_trips", seg, a, b)


@dataclass
class RunResult:
    run_id: str
    status: str
    answer: str
    result: dict[str, Any]
    trail: list[dict[str, Any]]
    stats: dict[str, Any]
    log_file: str
    world_file: str
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "status": self.status,
                "output": {"summary": self.answer, "result": self.result, "why_trail": self.trail},
                "agents": AGENTS, "trace_id": f"trace-{self.run_id}", "log_file": self.log_file,
                "world_file": self.world_file, "stats": self.stats, "error": self.error}


SYSTEM = {
    "planner": ("ROLE: planner\nYou plan a falsification-first investigation. Choose competing hypotheses only from "
                "the catalog. Skip a hypothesis only if a memory shows it was refuted for the same signature. "
                'Reply with JSON only: {"hypotheses":[{"id":..,"why":..}],"skipped":[{"id":..,"memory_id":..}]}'),
    "skeptic": ("ROLE: skeptic\nFor each supported hypothesis give the strongest alternative explanation and request "
                'the follow-up test "isolate_daytype". Reply with JSON only: '
                '{"challenges":[{"hypothesis":..,"alternative":..,"test":"isolate_daytype"}]}'),
    "writer": ("ROLE: writer\nWrite a 3-6 sentence plain-English answer for an operations analyst. Use only numbers "
               "present in the context. Name ruled-out hypotheses and end with what evidence would overturn the "
               "conclusion. Plain text, no markdown."),
}


def _raise(e: Exception):
    raise e


class WhyTrail:
    def __init__(self, zetaris: Any, generator: Generator, memory_path: str | Path | None = ".whytrail/hmem.json",
                 log_dir: str | Path = "logs", echo: bool = True, tamper: dict[str, float] | None = None) -> None:
        self.z = zetaris
        self.gen = generator
        self.memory_path = memory_path
        self.log_dir = Path(log_dir)
        self.echo = echo
        self._tamper = tamper or {}  # test hook: corrupt a cited number to prove the Verifier blocks it
        self._fallback = MockGenerator()

    # ---------- helpers ----------
    def _ask(self, role: str, prompt: str, attachments: dict, validate) -> dict:
        """Call the injected generator; on invalid output fall back to the offline planner and log it."""
        for attempt in range(2):
            try:
                out = self.gen(prompt, attachments=attachments, system_prompt=SYSTEM[role])
                parsed = out["text"] if role == "writer" else parse_json(out["text"])
                validate(parsed)
                self.t.event(role.capitalize(), "generate", output_summary=f"model={getattr(self.gen, 'model', '?')} ok",
                             retry_count=attempt, usage=out.get("usage"))
                return parsed
            except Exception as e:  # noqa: BLE001 - any model/parse failure triggers retry then fallback
                self.t.event(role.capitalize(), "generate", output_summary=f"invalid output: {e}", status="retry",
                             retry_count=attempt)
        out = self._fallback(prompt, attachments=attachments, system_prompt=SYSTEM[role])
        self.t.event(role.capitalize(), "fallback", output_summary="used offline generator", status="fallback")
        return out["text"] if role == "writer" else parse_json(out["text"])

    def _q(self, agent: str, kind: str, template: str, entities: list[str], **params: str) -> tuple[QueryResult, Any]:
        sql = Q.render(template, **params)
        try:
            res = self.z.query(sql)
        except ZetarisError as e:
            self.t.event(agent, "zetaris_query", input_summary=sql, output_summary=str(e), status="error")
            raise
        if template in ("daily_joined", "verify_rows", "segments", "schema_probe"):
            value: Any = res.rows
        elif template == "data_range":
            value = res.rows[0] if res.rows else [None, None]
        else:
            value = res.scalar()
        if template == "weather":
            value = {"prcp_mm": to_number(res.rows[0][0]) or 0.0, "snow_mm": to_number(res.rows[0][1]) or 0.0}
        fact = self.ingest.ingest_query(kind, res, value, entities, agent)
        self.queries += 1
        self.t.event(agent, "zetaris_query", input_summary=sql,
                     output_summary=f"{len(res.rows)} rows via {res.source} -> {fact.id}",
                     fact_id=fact.id, channel=res.source, elapsed_ms=round(res.elapsed_ms, 1))
        return res, fact

    def _raise_no_data(self, q: Question, ents: dict, metric_a: Any, metric_b: Any) -> None:
        _, fr = self._q("Retriever", "data_range", "data_range", [ents["seg"]], seg=q.segment)
        row = list(fr.value) if isinstance(fr.value, (list, tuple)) else [None, None]
        first_day, last_day = (row + [None, None])[:2]
        empty: list[str] = []
        if metric_a is None or metric_a == 0:
            empty.append(str(q.period_a))
        if metric_b is None:
            empty.append(str(q.period_b))
        periods = " and ".join(empty) if empty else f"{q.period_a} and {q.period_b}"
        if first_day is None or last_day is None:
            msg = f"no {Q.TAXI} rows for {q.segment}; the segment has no data at all."
        else:
            msg = (f"no {Q.TAXI} rows for {q.segment} in {periods}; "
                   f"available data: {first_day}..{last_day}. Ask about weeks inside that range.")
        self.t.event("Orchestrator", "no_data", output_summary=msg, status="error")
        raise NoDataError(msg)

    # ---------- main loop ----------
    def run(self, question_text: str, run_id: str | None = None) -> RunResult:
        run_id = run_id or "run-" + uuid.uuid4().hex[:8]
        t0 = time.perf_counter()
        self.t = Tracer(run_id, self.log_dir, echo=self.echo)
        self.world = WorldStore(run_id, self.log_dir)
        self.ingest = IngestPipeline(self.world)
        self.ctx = ContextBuilder(self.world)
        self.queries = 0
        mem = MemoryStore(self.memory_path)
        n = mem.hydrate()
        retr = MemoryRetrieval(mem)
        self.t.event("Orchestrator", "start", input_summary=question_text, output_summary=f"H-MEM hydrated {n} memories")
        try:
            return self._run(question_text, run_id, mem, retr, t0)
        except Exception as e:  # structured error instead of a crash
            self.t.event("Orchestrator", "error", output_summary=f"{type(e).__name__}: {e}", status="error")
            return RunResult(run_id, "error", "", {}, [], {"queries": self.queries}, str(self.t.path), str(self.world.path),
                             error={"type": type(e).__name__, "message": str(e),
                                    "recoverable": isinstance(e, (ZetarisError, NoDataError))})

    def _run(self, question_text: str, run_id: str, mem: MemoryStore, retr: MemoryRetrieval, t0: float) -> RunResult:
        # 1. schema discovery, unless H-MEM already knows the schema
        schema_mem = retr.retrieve("schema columns segments", tags=["schema"], top_n=1)
        if schema_mem["memories"]:
            known = schema_mem["memories"][0]["data"]
            segments = known["segments"]
            self.t.event("Planner", "hmem_retrieve", output_summary=f"schema from memory {schema_mem['memories'][0]['id']}; "
                         f"skipped {len(Q.TABLES) + 1} discovery queries", retrievalReason=schema_mem["retrievalReason"])
        else:
            cols = {}
            for table in Q.TABLES:
                res, _ = self._q("Retriever", "schema", "schema_probe", [f"source:{table}"], table=table)
                cols[table] = res.columns
            res, _ = self._q("Retriever", "segments", "segments", ["metric:taxi_trips"])
            segments = sorted(r[0] for r in res.rows)
            mem.add(Memory("Schema of NY_TAXI.taxi_daily, NOAA_GHCN.central_park_daily, REF.us_holidays and segment list",
                           "factual", "long_term", ["schema", "key:schema"], "zetaris", "Retriever", run_id,
                           data={"columns": cols, "segments": segments}))

        q = parse_question(question_text, segments)
        if q.segment not in segments:
            raise ValueError(f"unknown segment {q.segment}")
        ents = {"metric": f"metric:{q.metric}", "seg": f"segment:{q.segment}",
                "A": f"period:{q.period_a}", "B": f"period:{q.period_b}"}
        for k, e in ents.items():
            self.world.upsert_entity(e, e.split(":")[0])
        self.t.event("Orchestrator", "parse", output_summary=f"segment={q.segment} A={q.period_a} B={q.period_b}")

        # 2. the metric itself
        _, fa = self._q("Retriever", "metric", "metric_total", [ents["metric"], ents["seg"], ents["A"]],
                        seg=q.segment, start=q.period_a.start, end=q.period_a.end)
        _, fb = self._q("Retriever", "metric", "metric_total", [ents["metric"], ents["seg"], ents["B"]],
                        seg=q.segment, start=q.period_b.start, end=q.period_b.end)
        metric_a, metric_b = fa.value, fb.value
        if metric_a is None or metric_b is None or metric_a == 0:
            self._raise_no_data(q, ents, metric_a, metric_b)
        if "metric_b" in self._tamper:  # test hook only
            metric_b = fb.value * (1 + self._tamper["metric_b"])
        change = (metric_b - metric_a) / metric_a * 100 if metric_a else 0.0

        # 3. Planner (reads H-MEM)
        mem_ctx = retr.retrieve(f"hypothesis verdict {q.signature}", tags=["verdict", f"sig:{q.signature}"])
        self.t.event("Planner", "hmem_retrieve", output_summary=f"{len(mem_ctx['memories'])} memories injected",
                     retrievalReason=mem_ctx["retrievalReason"], memory_ids=[m["id"] for m in mem_ctx["memories"]])

        def v_plan(p):
            assert all(h["id"] in Q.HYPOTHESES for h in p["hypotheses"]) and p["hypotheses"]

        plan = self._ask("planner", f"Question: {q.text}\nMetric change: {change:+.1f}%",
                         {"signature": q.signature, "catalog": Q.HYPOTHESES, "memory": mem_ctx}, v_plan)
        hyps = [h["id"] for h in plan["hypotheses"]]
        skipped = {s["id"]: s.get("memory_id") for s in plan.get("skipped", [])}
        self.t.event("Planner", "delegate_task", output_summary=f"testing {hyps}; skipped from memory {list(skipped)}",
                     target_agent="Retriever")

        # Retriever + Analyst: test every competing hypothesis against Zetaris
        verdicts = {h: self._test(h, q, ents, metric_a, metric_b) for h in hyps}
        rounds = 1
        supported = [h for h, v in verdicts.items() if v["verdict"] == "supported"]
        attribution: list[dict[str, Any]] = []
        if supported:
            # 4. Skeptic challenges every supported hypothesis; Retriever runs the follow-up
            rounds = 2

            def v_sk(p):
                assert isinstance(p["challenges"], list)

            sk = self._ask("skeptic", "Challenge these supported hypotheses.",
                           {"supported": supported,
                            "context": self.ctx.build("challenge", "Skeptic", [f"hyp:{h}" for h in supported])}, v_sk)
            self.t.event("Skeptic", "request_followup", output_summary=f"{len(sk['challenges'])} challenges",
                         target_agent="Retriever", challenges=sk["challenges"])
            attribution = self._attribution(q, ents)
            for h in supported:
                daytype = {"H_weather": "snow", "H_holiday": "holiday"}.get(h)
                if not daytype:
                    continue
                share = next((c["share_pct"] for c in attribution if c["daytype"] == daytype), 0.0)
                if share < 10:
                    verdicts[h]["verdict"] = "refuted"
                    verdicts[h]["reason"] += f"; Skeptic follow-up: {daytype} days explain only {share:.0f}% of the drop"
                    self.t.event("Skeptic", "needs_revision", output_summary=f"{h} downgraded to refuted",
                                 status="needs_revision", target_agent="Planner")
                else:
                    verdicts[h]["reason"] += f"; Skeptic follow-up: {daytype} days explain {share:.0f}% of the drop"
        if not attribution:
            attribution = self._attribution(q, ents)

        # 5. Verifier: independent re-derivation of every number the Writer will cite
        checks = self._verify(q, ents, {"metric_a": metric_a, "metric_b": metric_b})
        bad = [c for c in checks if not c["ok"]]
        if bad:
            self.t.event("Verifier", "block", output_summary=f"{len(bad)} cited numbers did not reproduce",
                         status="needs_revision", target_agent="Writer", failed=bad)
            metric_a, metric_b = checks[0]["recomputed"], checks[1]["recomputed"]
            change = (metric_b - metric_a) / metric_a * 100 if metric_a else 0.0
            self.t.event("Verifier", "correct", output_summary="replaced cited numbers with re-derived values")

        refuted = [f"{h} ({Q.HYPOTHESES[h]['claim']})" for h, v in verdicts.items() if v["verdict"] == "refuted"]
        refuted += [f"{h} (ruled out in an earlier run, memory {mid})" for h, mid in skipped.items()]
        supported = [h for h, v in verdicts.items() if v["verdict"] == "supported"]
        explained = sum(c["share_pct"] for c in attribution if c["daytype"] != "other")
        confidence = round(min(0.95, max(0.1, explained / 100)) * (0.5 if bad else 1.0), 2)
        result = {
            "question": q.text, "segment": q.segment, "period_a": str(q.period_a), "period_b": str(q.period_b),
            "metric_a": metric_a, "metric_b": metric_b, "change_pct": round(change, 2),
            "supported": supported, "refuted": refuted, "attribution": attribution,
            "confidence": confidence, "verification": checks, "rounds": rounds,
            "falsifier": ("ordinary (non-holiday, dry) days in period B showed a drop of similar size, or the fare per "
                          "trip rose, or another borough without snow dropped just as much."),
        }
        writer_ctx = self.ctx.build("write_answer", "Writer")
        answer = self._ask("writer", f"Question: {q.text}", {"result": result, "world": writer_ctx},
                           lambda s: (isinstance(s, str) and len(s) > 20) or _raise(ValueError("empty answer")))
        trail = self._trail(verdicts, attribution, checks)

        # 6. mine H-MEM (verdicts + outcome); persisted for the next run's Planner
        for h, v in verdicts.items():
            mem.add(Memory(f"{h} {v['verdict']} for {q.signature}: {v['reason']}", "factual", "working",
                           ["verdict", f"sig:{q.signature}", f"key:{q.signature}:{h}"], "analyst", "Analyst", run_id,
                           data={"hypothesis": h, "verdict": v["verdict"], "signature": q.signature,
                                 "fact_ids": v["fact_ids"]}))
        mem.add(Memory(f"run {run_id} answered {q.signature} with {self.queries} queries", "procedural", "working",
                       ["outcome"], "orchestrator", "Orchestrator", run_id, data={"queries": self.queries}))
        mem.persist()
        stats = {"queries": self.queries, "model_calls": getattr(self.gen, "calls", 0),
                 "model_cost_usd": round(getattr(self.gen, "cost_usd", 0.0), 4), "rounds": rounds,
                 "hmem_records": len(mem.records), "world_facts": len(self.world.facts),
                 "context_manifest": self.ctx.manifest, "seconds": round(time.perf_counter() - t0, 2)}
        self.t.event("Writer", "final_synthesis", output_summary=answer[:160], confidence=confidence)
        self.t.event("Orchestrator", "done", output_summary=f"{self.queries} Zetaris queries, {stats['model_calls']} model calls")
        return RunResult(run_id, "success", answer, result, trail, stats, str(self.t.path), str(self.world.path))

    # ---------- analyst / verifier ----------
    def _test(self, h: str, q: Question, ents: dict, metric_a: float, metric_b: float) -> dict[str, Any]:
        tmpl = Q.HYPOTHESES[h]["test"]
        hid = f"hyp:{h}"
        self.world.upsert_entity(hid, "hypothesis", claim=Q.HYPOTHESES[h]["claim"])
        seg = {"seg": q.segment} if tmpl in ("price", "coverage", "peers") else {}
        _, fa = self._q("Retriever", tmpl, tmpl, [hid, ents["A"]], start=q.period_a.start, end=q.period_a.end, **seg)
        _, fb = self._q("Retriever", tmpl, tmpl, [hid, ents["B"]], start=q.period_b.start, end=q.period_b.end, **seg)
        a, b = fa.value, fb.value
        drop = metric_b < metric_a
        if tmpl == "weather":
            ok = drop and (b["snow_mm"] - a["snow_mm"] > 5 or b["prcp_mm"] - a["prcp_mm"] > 20)
            reason = f"snow {a['snow_mm']:.0f}mm -> {b['snow_mm']:.0f}mm, precipitation {a['prcp_mm']:.0f}mm -> {b['prcp_mm']:.0f}mm"
        elif tmpl == "holiday":
            ok = drop and b > a
            reason = f"holidays in period: {a:.0f} -> {b:.0f}"
        elif tmpl == "price":
            ok = drop and b > a * 1.05
            reason = f"fare per trip ${a:.2f} -> ${b:.2f}"
        elif tmpl == "coverage":
            ok = b < a
            reason = f"days with data {a:.0f} -> {b:.0f}"
        else:  # peers
            seg_chg = (metric_b - metric_a) / metric_a
            peer_chg = (b - a) / a if a else 0.0
            ok = abs(seg_chg - peer_chg) > 0.10
            reason = f"{q.segment} {seg_chg:+.1%} vs other segments {peer_chg:+.1%}"
        verdict = "supported" if ok else "refuted"
        self.world.add_edge(fa.id, "supports" if ok else "refutes", hid)
        self.world.add_edge(fb.id, "supports" if ok else "refutes", hid)
        self.t.event("Analyst", "verdict", input_summary=h, output_summary=f"{verdict}: {reason}", hypothesis=h)
        return {"verdict": verdict, "reason": reason, "fact_ids": [fa.id, fb.id]}

    def _attribution(self, q: Question, ents: dict) -> list[dict[str, Any]]:
        """Federated join (taxi x weather x holidays) for both periods; weekday-matched gap per day type."""
        ra, fa = self._q("Retriever", "daily", "daily_joined", [ents["seg"], ents["A"]], seg=q.segment,
                         start=q.period_a.start, end=q.period_a.end)
        rb, fb = self._q("Retriever", "daily", "daily_joined", [ents["seg"], ents["B"]], seg=q.segment,
                         start=q.period_b.start, end=q.period_b.end)
        base: dict[int, list[float]] = {}
        for d, trips, *_ in ra.rows:
            base.setdefault(date.fromisoformat(d).weekday(), []).append(to_number(trips))
        overall = [x for v in base.values() for x in v]
        groups: dict[str, dict[str, Any]] = {}
        for d, trips, prcp, snow, holiday in rb.rows:
            wd = date.fromisoformat(d).weekday()
            ref = base.get(wd) or overall
            gap = sum(ref) / len(ref) - to_number(trips)
            kind = "holiday" if holiday else ("snow" if (to_number(snow) or 0) > 0 else "other")
            g = groups.setdefault(kind, {"daytype": kind, "dates": [], "lost_trips": 0.0})
            g["dates"].append(d)
            g["lost_trips"] += gap
        total = sum(max(0.0, g["lost_trips"]) for g in groups.values()) or 1.0  # gross lost trips
        out = []
        for g in groups.values():
            g["lost_trips"] = round(g["lost_trips"])
            g["share_pct"] = round(max(0.0, g["lost_trips"]) / total * 100, 1)
            g["fact_ids"] = [fa.id, fb.id]
            out.append(g)
        return sorted(out, key=lambda g: -g["lost_trips"])

    def _verify(self, q: Question, ents: dict, cited: dict[str, float]) -> list[dict[str, Any]]:
        checks = []
        for key, period, ent in (("metric_a", q.period_a, ents["A"]), ("metric_b", q.period_b, ents["B"])):
            res, f = self._q("Verifier", "verify", "verify_rows", [ents["seg"], ent], seg=q.segment,
                             start=period.start, end=period.end)
            recomputed = sum(to_number(r[1]) for r in res.rows)
            ok = abs(recomputed - cited[key]) <= VERIFY_TOLERANCE * max(1.0, abs(recomputed))
            checks.append({"claim": key, "cited": cited[key], "recomputed": recomputed, "ok": ok, "fact_id": f.id})
            self.t.event("Verifier", "check", output_summary=f"{key}: cited {cited[key]:,.0f} vs re-derived {recomputed:,.0f} -> "
                         f"{'ok' if ok else 'MISMATCH'}", status="success" if ok else "needs_revision")
        return checks

    def _trail(self, verdicts: dict, attribution: list, checks: list) -> list[dict[str, Any]]:
        trail = []
        for h, v in verdicts.items():
            facts = [self.world.facts[i] for i in v["fact_ids"]]
            trail.append({"step": f"{h} -> {v['verdict']}", "claim": Q.HYPOTHESES[h]["claim"], "because": v["reason"],
                          "evidence": [{"fact_id": f.id, "sql": f.provenance["sql"], "sources": f.provenance["sources"],
                                        "rows": f.provenance["row_count"], "sha256": f.provenance["result_sha256"]}
                                       for f in facts]})
        for c in attribution:
            trail.append({"step": f"attribution:{c['daytype']}", "claim": f"{c['daytype']} days: {c['lost_trips']:,} lost trips "
                          f"({c['share_pct']}%)", "because": "weekday-matched comparison, federated join "
                          "NY_TAXI x NOAA_GHCN x REF", "evidence": c["fact_ids"]})
        for c in checks:
            trail.append({"step": f"verify:{c['claim']}", "claim": f"{c['cited']:,.0f}",
                          "because": f"re-derived {c['recomputed']:,.0f} from raw rows -> {'ok' if c['ok'] else 'mismatch'}",
                          "evidence": [c["fact_id"]]})
        return trail
