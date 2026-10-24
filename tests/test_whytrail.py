import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from whytrail import queries as Q
from whytrail.__main__ import DEFAULT_Q, DEFAULT_SAMPLE_Q, default_question
from whytrail.generator import MockGenerator, parse_json
from whytrail.meterless.hmem import Memory, MemoryRetrieval, MemoryStore
from whytrail.meterless.world_model import IngestPipeline, WorldStore
from whytrail.orchestrator import WhyTrail, parse_question
from whytrail.zetaris import MockZetaris, ZetarisError, ZetarisRestClient

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



def test_rest_client_retry_uses_fresh_query_id_and_keeps_real_error():
    ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["queryId"] in ids:
            return httpx.Response(400, json={"msg": "Query id already exists"})
        ids.append(body["queryId"])
        return httpx.Response(400, json={"msg": "Table or view not found: NY_TAXI.taxi_daily"})

    c = ZetarisRestClient("http://zt:8888/api/v1.0", api_key="k", org_id="1", retries=1,
                          transport=httpx.MockTransport(handler))
    with pytest.raises(ZetarisError) as e:
        c.query("SELECT 1")
    assert len(ids) == 2 and ids[0] != ids[1]
    assert "Table or view not found" in str(e.value) and "already exists" not in str(e.value)


def test_rest_client_reads_columns_from_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"headers": ["trip_date", "trips"], "data": [["2025-01-13", "1"]],
                                         "total": 1, "timeUsed": 5})

    c = ZetarisRestClient("http://zt:8888/api/v1.0", api_key="k", org_id="1", transport=httpx.MockTransport(handler))
    assert c.query("SELECT trip_date, trips FROM NY_TAXI.taxi_daily").columns == ["trip_date", "trips"]

def test_api_run_sample_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from whytrail.api import app

    out = TestClient(app).post("/run", json={"run_id": "api1", "input": {"query": QUESTION},
                                            "options": {"sample_mode": True}}).json()
    assert out["status"] == "success" and out["log_file"].endswith("trace-api1.jsonl")


def test_no_data_period_gives_clear_error(tmp_path):
    q = ("Why did Manhattan taxi trips drop in the week of 2023-06-12 "
         "compared with the week of 2023-06-05?")
    res = make(tmp_path).run(q, run_id="nodata")
    assert res.status == "error"
    assert res.error is not None
    assert res.error["type"] == "NoDataError"
    assert res.error["recoverable"] is True
    msg = res.error["message"]
    assert "available data" in msg and "2024-01-" in msg
    assert "TypeError" not in msg
    events = [json.loads(l) for l in (tmp_path / "logs" / "trace-nodata.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["agent_name"] == "Orchestrator" and e["action"] == "no_data" for e in events)


def test_data_range_template_renders():
    sql = Q.render("data_range", seg="Manhattan")
    assert "MIN(trip_date)" in sql and "MAX(trip_date)" in sql and "Manhattan" in sql
    with pytest.raises(Q.TemplateError):
        Q.render("data_range", seg="x' OR '1'='1")


def test_cli_default_question_by_mode():
    assert default_question(True) == DEFAULT_SAMPLE_Q
    assert default_question(False) == DEFAULT_Q
    assert "2024-01-15" in DEFAULT_SAMPLE_Q
    assert "2025-01-20" in DEFAULT_Q


def test_cli_mock_data_default_question_in_mock_range(tmp_path, monkeypatch, capsys):
    # --mock-data without a question: default must land inside the synthetic 2024-01 range, no model call.
    from whytrail import __main__ as cli
    seen = {}
    real_build = cli.build

    def fake_build(mock_data, mock_llm, memory, log_dir, echo=True, max_cost=1.0):
        seen["mock_data"], seen["mock_llm"] = mock_data, mock_llm
        return real_build(mock_data, True, memory, log_dir, echo=echo, max_cost=max_cost)  # force offline generator

    monkeypatch.setattr(cli, "build", fake_build)
    monkeypatch.setattr(cli, "load_dotenv", lambda: {})  # do not pick up a developer's .env here
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SAMPLE_MODE", raising=False)
    rc = cli.main(["--mock-data", "--quiet", "--memory", str(tmp_path / "hmem.json"), "--log-dir", str(tmp_path / "logs")])
    out = capsys.readouterr().out
    assert rc == 0
    assert seen == {"mock_data": True, "mock_llm": False}
    assert "2024-01-15..2024-01-21" in out and "NoDataError" not in out
    assert default_question(False or True) == DEFAULT_SAMPLE_Q


def test_dotenv_loader_sets_missing_keys_only(tmp_path, monkeypatch):
    from whytrail import env

    (tmp_path / ".env").write_text(
        "# comment\n\nANTHROPIC_API_KEY=from-file\nexport ZETARIS_ORG_ID='7'\n"
        'WHYTRAIL_MODEL="claude-haiku-5-5"\nSAMPLE_MODE=true # trailing comment\nnot a valid line\n',
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for k in ("ANTHROPIC_API_KEY", "ZETARIS_ORG_ID", "WHYTRAIL_MODEL", "SAMPLE_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    loaded = env.load_dotenv()
    assert os.environ["ANTHROPIC_API_KEY"] == "from-shell"  # an exported variable is never overridden
    assert os.environ["ZETARIS_ORG_ID"] == "7" and os.environ["WHYTRAIL_MODEL"] == "claude-haiku-5-5"
    assert os.environ["SAMPLE_MODE"] == "true"
    assert set(loaded) == {"ZETARIS_ORG_ID", "WHYTRAIL_MODEL", "SAMPLE_MODE"}
    assert env.load_dotenv(tmp_path / "missing.env") == {}


def test_cli_reads_dotenv_without_shell_source(tmp_path, monkeypatch, capsys):
    # README Path B: `cp .env.example .env`, fill it in, run. The CLI itself must read the file.
    (tmp_path / ".env").write_text("SAMPLE_MODE=true\nWHYTRAIL_MEMORY=mem-from-dotenv.json\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for k in ("SAMPLE_MODE", "WHYTRAIL_MEMORY", "ANTHROPIC_API_KEY", "ZETARIS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    from whytrail import __main__ as cli

    rc = cli.main(["--quiet", "--log-dir", str(tmp_path / "logs")])
    assert rc == 0 and "=== WhyTrail answer ===" in capsys.readouterr().out
    assert (tmp_path / "mem-from-dotenv.json").exists()  # --memory default came from .env


def test_skeptic_requested_followup_is_executed_and_traced(tmp_path):
    class SteeringSkeptic(MockGenerator):
        def _skeptic(self, a):
            out = super()._skeptic(a)
            out["challenges"].append({"hypothesis": a["supported"][0], "test": "baseline_variation",
                                      "alternative": "ordinary week-to-week variation could produce this shift"})
            out["challenges"].append({"hypothesis": a["supported"][0], "test": "not_a_real_test", "alternative": "x"})
            return out

    plain = make(tmp_path / "plain").run(QUESTION, run_id="plain")
    wt = WhyTrail(MockZetaris(), SteeringSkeptic(), memory_path=tmp_path / "hmem.json", log_dir=tmp_path / "logs", echo=False)
    res = wt.run(QUESTION, run_id="steer")
    assert res.status == "success"
    assert res.stats["queries"] == plain.stats["queries"] + 1  # exactly one extra Zetaris query for the extra test
    fu = res.result["skeptic_followups"]
    assert len(fu) == 1 and fu[0]["test"] == "baseline_variation" and fu[0]["baseline_period"] == "2024-01-01..2024-01-07"
    assert fu[0]["within_ordinary_variation"] is False  # -7.5% is larger than the New Year week -> week A move
    assert res.result["confidence"] == plain.result["confidence"]
    assert any(s["step"] == "skeptic:baseline_variation" and s["evidence"] == fu[0]["fact_ids"] for s in res.trail)
    events = [json.loads(l) for l in (tmp_path / "logs" / "trace-steer.jsonl").read_text(encoding="utf-8").splitlines()]
    executed = [e["input_summary"] for e in events if e["agent_name"] == "Skeptic" and e["action"] == "followup_executed"]
    assert executed == ["isolate_daytype", "baseline_variation"]
    assert any(e["action"] == "followup_rejected" and "not_a_real_test" in e["output_summary"] for e in events)
    sql = [e["input_summary"] for e in events if e["action"] == "zetaris_query" and "2024-01-01' AND '2024-01-07" in e["input_summary"]]
    assert len(sql) == 1 and sql[0].startswith("SELECT SUM(trips)")
