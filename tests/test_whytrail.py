import json

import httpx
import pytest
from fastapi.testclient import TestClient

from whytrail import queries as Q
from whytrail.generator import MockGenerator, parse_json
from whytrail.meterless.hmem import Memory, MemoryRetrieval, MemoryStore
from whytrail.meterless.world_model import IngestPipeline, WorldStore
from whytrail.orchestrator import WhyTrail, parse_question
from whytrail.zetaris import MockZetaris, ZetarisRestClient

QUESTION = "Why did Manhattan taxi trips drop in the week of 2024-01-15 compared with the week of 2024-01-08?"


def make(tmp_path, **kw):
    return WhyTrail(MockZetaris(), MockGenerator(), memory_path=tmp_path / "hmem.json", log_dir=tmp_path / "logs",
                    echo=False, **kw)


def test_mock_zetaris_federated_join_uses_qualified_names():
    z = MockZetaris()
    sql = Q.render("daily_joined", seg="Manhattan", start="2024-01-15", end="2024-01-21")
    res = z.query(sql)
    assert len(res.rows) == 7
    assert res.rows[0][4] == "Martin Luther King Jr. Day"  # holiday joined from REF source
    assert res.rows[1][3] == 36.0  # snow joined from NOAA_GHCN source


def test_templates_reject_injection():
    with pytest.raises(Q.TemplateError):
        Q.render("metric_total", seg="x' OR '1'='1", start="2024-01-01", end="2024-01-07")
    with pytest.raises(Q.TemplateError):
        Q.render("metric_total", seg="Manhattan", start="2024-01-01; DROP", end="2024-01-07")


def test_end_to_end_mock_explains_drop_with_evidence(tmp_path):
    res = make(tmp_path).run(QUESTION, run_id="t1")
    assert res.status == "success"
    r = res.result
    assert r["change_pct"] < 0
    assert set(r["supported"]) == {"H_weather", "H_holiday"}
    assert any("H_price" in x for x in r["refuted"])
    assert all(c["ok"] for c in r["verification"])
    # every hypothesis step cites World Model facts with SQL provenance
    steps = [s for s in res.trail if s["step"].startswith("H_")]
    assert steps and all(e["sql"].startswith("SELECT") for s in steps for e in s["evidence"])
    lines = (tmp_path / "logs" / "trace-t1.jsonl").read_text(encoding="utf-8").splitlines()
    agents = {json.loads(l)["agent_name"] for l in lines}
    assert {"Planner", "Retriever", "Skeptic", "Verifier", "Writer"} <= agents


def test_hmem_second_run_needs_fewer_queries(tmp_path):
    first = make(tmp_path).run(QUESTION, run_id="a")
    second = make(tmp_path).run(QUESTION, run_id="b")
    assert second.stats["queries"] < first.stats["queries"]
    assert second.result["supported"] == first.result["supported"]
    assert any("earlier run" in x for x in second.result["refuted"])


def test_verifier_blocks_tampered_number(tmp_path):
    res = make(tmp_path, tamper={"metric_b": 0.10}).run(QUESTION, run_id="tamper")
    checks = res.result["verification"]
    assert not checks[1]["ok"]
    assert res.result["metric_b"] == checks[1]["recomputed"]  # corrected to the re-derived value
    events = [json.loads(l) for l in (tmp_path / "logs" / "trace-tamper.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["agent_name"] == "Verifier" and e["action"] == "block" for e in events)


def test_world_model_ingest_is_idempotent(tmp_path):
    store = WorldStore("w", tmp_path)
    ing = IngestPipeline(store)
    res = MockZetaris().query(Q.render("holiday", start="2024-01-15", end="2024-01-21"))
    f1 = ing.ingest_query("holiday", res, res.scalar(), ["hyp:H_holiday"], "Retriever")
    f2 = ing.ingest_query("holiday", res, res.scalar(), ["hyp:H_holiday"], "Retriever")
    assert f1.id == f2.id and len(store.facts) == 1
    assert f1.provenance["sources"] == ["REF.us_holidays"]


def test_hmem_persists_and_supersedes(tmp_path):
    path = tmp_path / "m.json"
    s = MemoryStore(path)
    s.add(Memory("old schema", "factual", "long_term", ["schema", "key:schema"], "z", "Retriever", "r1"))
    s.add(Memory("new schema", "factual", "long_term", ["schema", "key:schema"], "z", "Retriever", "r2"))
    s.add(Memory("scratch", "general", "short_term", ["tmp"], "z", "Retriever", "r2"))
    s.persist()
    s2 = MemoryStore(path)
    assert s2.hydrate() == 1  # superseded + short-term records dropped
    hit = MemoryRetrieval(s2).retrieve("schema", tags=["schema"])
    assert hit["memories"][0]["content"] == "new schema" and "retrievalReason" in hit


def test_parse_json_strips_fences_and_prose():
    assert parse_json('Sure:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('x [1, 2] y') == [1, 2]
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_invalid_model_output_falls_back(tmp_path):
    class Broken:
        calls = 0
        model = "broken"

        def __call__(self, *a, **k):
            self.calls += 1
            return {"text": "I think the weather did it."}

    wt = WhyTrail(MockZetaris(), Broken(), memory_path=None, log_dir=tmp_path, echo=False)
    res = wt.run(QUESTION, run_id="fb")
    assert res.status == "success"
    assert any(e["action"] == "fallback" for e in wt.t.events)


def test_parse_question_periods():
    q = parse_question(QUESTION)
    assert (q.segment, str(q.period_a), str(q.period_b)) == ("Manhattan", "2024-01-08..2024-01-14", "2024-01-15..2024-01-21")
    with pytest.raises(ValueError):
        parse_question("why did it drop?")


def test_rest_client_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [["1"]]})

    c = ZetarisRestClient("http://zt:8888/api/v1.0", api_key="k", org_id="1", transport=httpx.MockTransport(handler))
    assert c.ping()
    assert seen["url"] == "http://zt:8888/api/v1.0/sql-editor/sqls/run"
    assert seen["headers"]["authorization"] == "Bearer k" and seen["headers"]["x-org-id"] == "1"
    assert seen["body"]["sql"] == "SELECT 1" and seen["body"]["source"] == "SqlEditor"


def test_api_run_sample_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from whytrail.api import app

    out = TestClient(app).post("/run", json={"run_id": "api1", "input": {"query": QUESTION},
                                            "options": {"sample_mode": True}}).json()
    assert out["status"] == "success" and out["log_file"].endswith("trace-api1.jsonl")
