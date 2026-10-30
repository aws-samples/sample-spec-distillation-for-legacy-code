# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Evaluation in the launcher: GET/POST /api/runs/<id>/eval, /eval/status, /api/eval/compare,
--import-run, and the viewer's Evaluation page. No AWS: run_suite.py is replaced by
tests/eval_suite_stub.py, and the aggregation reads fixtures trimmed from the real AP and JC
artifacts (tests/fixtures/eval/, synthetic corpora only)."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import eval_view  # noqa: E402
import launcher_server as LS  # noqa: E402
import viewer_api  # noqa: E402

pytest.importorskip("yaml")              # the suites and answer keys are YAML (as for run_suite.py)

FIX = HERE / "fixtures" / "eval"
STUB = HERE / "eval_suite_stub.py"


def _snapshot(d: Path) -> dict:
    return {str(p.relative_to(d)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(d.rglob("*")) if p.is_file()}


class Api:
    def __init__(self, base: str):
        self.base = base
        self.cookie = None

    def call(self, method, path, body=None, headers=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        if self.cookie and "Cookie" not in h:
            h["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                c = r.headers.get("Set-Cookie")
                if c:
                    self.cookie = c.split(";", 1)[0]
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw

    def wait_eval(self, run_id, states, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            _, s = self.call("GET", f"/api/runs/{run_id}/eval/status")
            if s["state"] in states:
                return s
            time.sleep(0.1)
        raise AssertionError(f"eval of {run_id} never reached {states}: {s}")


@pytest.fixture()
def runs(tmp_path):
    """Copies of the two fixture run directories (the test owns them; snapshots prove read-only)."""
    out = {}
    for name in ("ap-fixture-run", "jc-fixture-run"):
        shutil.copytree(FIX / name, tmp_path / "imports" / name)
        out[name] = tmp_path / "imports" / name
    return out


@pytest.fixture()
def make_api(tmp_path, runs, monkeypatch):
    servers = []
    argv_log = tmp_path / "suite_argv.jsonl"
    monkeypatch.setenv("STUB_ARGV_LOG", str(argv_log))

    def make(*, access_code=None, allowed_hosts_extra=None, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        script = tmp_path / "no-pipeline" / "run_distillation.sh"   # never executed here
        launcher = LS.Launcher(script, tmp_path / f"runs{len(servers)}", 1,
                               import_runs=[runs["ap-fixture-run"], runs["jc-fixture-run"]],
                               eval_cmd=[sys.executable, str(STUB)])
        srv = LS.make_server(launcher, "127.0.0.1", 0, access_code=access_code,
                             allowed_hosts_extra=allowed_hosts_extra)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        api = Api(f"http://127.0.0.1:{srv.server_address[1]}")
        api.launcher = launcher
        api.argv = lambda: [json.loads(x) for x in argv_log.read_text().splitlines()] if argv_log.exists() else []
        return api

    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


AP, JC = "imported-ap-fixture-run", "imported-jc-fixture-run"


# ── GET /api/runs/<id>/eval: aggregation ────────────────────────────────────────────────────
def test_eval_aggregation_separates_exact_and_estimated(make_api):
    api = make_api()
    st, d = api.call("GET", f"/api/runs/{AP}/eval")
    assert st == 200
    E, S = d["exact"], d["estimated"]
    assert d["legend"]["exact"].startswith("Exact: same answer every time, no AI")
    assert "LLM-as-a-judge (Claude on Bedrock)" in d["legend"]["estimated"]
    assert "synthetic samples with known answers" in d["banner"]
    # exact: lines conserved, citation validity, graph vs the parser, cost of calls made
    assert E["conservation"]["lines"] == {"value": 2336, "status": "measured"} and E["conservation"]["ok"]
    bott = next(a for a in E["spec_artifacts"] if a["artifact"] == "bottled_spec")
    assert bott["citation_validity_pct"]["value"] == 100.0 and bott["citations_valid"]["value"] == 725
    g = E["graph"]
    assert g["parser_confirmed_edge_precision_pct"]["value"] == 97.56
    assert g["parser_confirmed_edge_precision_pct"]["confirmed"] == 40
    assert g["parser_edge_recall_pct"]["value"] == 66.67
    assert g["by_language"]["vbnet"]["recall_pct"]["value"] == 0.0           # a real 0, measured
    assert g["by_language"]["sql"]["recall_pct"]["value"] == 97.56
    assert g["by_language"]["csharp"]["status"] == "not_measured"            # no C# in the corpus
    assert E["cost"]["pipeline_usd"]["value"] == 17.2092
    # estimated: judged numbers with their 95% CI, passes, kappa, golden agreement
    b = next(a for a in S["spec_artifacts"] if a["artifact"] == "bottled_spec")
    any_ = b["metrics"]["recall_any_match_pct"]
    assert any_["value"] == 95.8 and any_["ci95"] == [89.6, 100.0]
    assert b["metrics"]["recall_strict_full_pct"]["ci95"] == [58.3, 83.3]
    assert b["judge_runs"] == 3 and b["agreement"]["fleiss_kappa_verdicts"]["value"] == 0.8997
    assert b["golden"]["agreement_pct"] == 86.7
    # the two layers never share a metric: no judged name in exact, no citation name in estimated
    ex_txt, es_txt = json.dumps(E), json.dumps(S["spec_artifacts"])
    assert "recall_any_match_pct" not in ex_txt and "strict_full" not in ex_txt
    assert "citation_validity_pct" not in es_txt


def test_traps_carry_judge_vs_human_note(make_api):
    api = make_api()
    _, d = api.call("GET", f"/api/runs/{AP}/eval")
    t = d["estimated"]["traps"]
    assert t["status"] == "measured" and t["key"].endswith("ap-invoices/ANSWER_KEY.yaml")
    rows = {r["id"]: r for r in t["rows"]}
    assert {"BR-018", "BR-019", "BR-020", "BR-021"} <= set(rows)
    br20 = rows["BR-020"]
    assert t["totals"]["bottled_spec"] == {"full": 4, "partial": 2, "missed": 0}
    assert br20["categories"] == ["inconsistency"] and br20["outcomes"]["bottled_spec"] == "full"
    assert br20["judge_vs_human"] and br20["judge_vs_human"][0]["human"] == "partial"
    assert br20["locations"][0]["ref"].startswith("ClientSide/AP/AP/modAPGlobals.vb:")
    assert "url" not in br20["locations"][0]          # no repo URL recorded: no broken links


def test_missing_and_null_are_not_measured_never_zero(make_api, runs):
    # a scorer that reported nulls, and a graph_eval whose parity was skipped
    sp = runs["ap-fixture-run"] / "validation" / "bottled" / "score.json"
    d = json.loads(sp.read_text())
    d["recall"]["any_match_pct"] = None
    d["judge"]["bootstrap_ci95"].pop("recall_any_match_pct")
    sp.write_text(json.dumps(d))
    gp = runs["ap-fixture-run"] / "validation" / "graph" / "graph_eval.json"
    g = json.loads(gp.read_text())
    g["parity"] = {"status": "skipped", "note": "no facts/graph.json"}
    for k in list(g["thresholds"]):
        if k.startswith("parser_"):
            g["thresholds"][k] = None
    gp.write_text(json.dumps(g))
    api = make_api()
    _, a = api.call("GET", f"/api/runs/{AP}/eval")
    m = next(x for x in a["estimated"]["spec_artifacts"] if x["artifact"] == "bottled_spec")["metrics"]
    assert m["recall_any_match_pct"]["value"] is None and m["recall_any_match_pct"]["status"] == "not_measured"
    gr = a["exact"]["graph"]
    assert gr["parser_edge_recall_pct"] == {"value": None, "status": "not_measured",
                                            "reason": "parser comparison skipped: no facts/graph.json"}
    assert all(v["status"] == "not_measured" for v in gr["by_language"].values())
    # benefits: AP has none -> each not_measured with a reason, never 0
    for b in a["estimated"]["benefits"].values():
        assert b["status"] == "not_measured" and b["reason"] and b["metrics"] == {}
    # JC: impact reported not_measured with a null rule recall; field app was never started
    _, j = api.call("GET", f"/api/runs/{JC}/eval")
    B = j["estimated"]["benefits"]
    assert B["qa"]["status"] == "measured" and B["qa"]["metrics"]["correct_pct"]["value"] == 16.0
    assert B["impact"]["status"] == "not_measured" and "rule coverage graded for 0" in B["impact"]["reason"]
    assert all(m["status"] == "not_measured" for m in B["impact"]["metrics"].values())   # never quoted
    assert B["impact"]["metrics"]["location_recall_pct"]["recorded_value"] == 51.4
    assert B["fieldapp"]["status"] == "not_measured" and "budget_exceeded" in B["fieldapp"]["reason"]
    # one judge pass: kappa needs >= 2, so it is not measured rather than 0 or 1
    jb = next(x for x in j["estimated"]["spec_artifacts"] if x["artifact"] == "bottled_spec")
    assert jb["judge_runs"] == 1
    assert jb["agreement"]["fleiss_kappa_verdicts"]["status"] == "not_measured"
    assert jb["golden"]["status"] == "not_measured"


def test_artifact_of_another_run_is_skipped(make_api):
    api = make_api()
    _, j = api.call("GET", f"/api/runs/{JC}/eval")
    assert j["exact"]["graph"]["source"].endswith("validation/graph/graph_eval.json")
    assert j["exact"]["graph"]["parser_edge_recall_pct"]["value"] == 100.0
    skipped = {Path(s["source"]).parent.name: s["reason"] for s in j["skipped_sources"]}
    assert "graph-first-run" in skipped and "output/jc-changeorders" in skipped["graph-first-run"]


def test_old_scorer_without_ci_shows_noise_band_not_bare_number(make_api):
    api = make_api()
    _, d = api.call("GET", f"/api/runs/{AP}/eval")
    mb = next(x for x in d["estimated"]["spec_artifacts"] if x["artifact"] == "master_blend")
    full = mb["metrics"]["recall_strict_full_pct"]
    assert full["value"] == 89.6 and full["ci95"] is None and full["noise_band"] == 11.0
    assert "no CI recorded" in full["uncertainty"]
    assert mb["judge_runs"]["status"] == "not_measured"


# ── POST /api/runs/<id>/eval: validation ────────────────────────────────────────────────────
@pytest.mark.parametrize("body,code,msg", [
    ({"budget_usd": 1, "aws_secret_access_key": "x"}, 400, "credential fields are not accepted"),
    ({"budget_usd": 1, "session_token": "x"}, 400, "credential fields are not accepted"),
    ({"budget_usd": 1, "nonsense": 1}, 400, "unknown field"),
    ({"budget_usd": 1, "answer_key": "/etc/passwd"}, 400, "paths are never accepted"),
    ({"budget_usd": 1, "answer_key": "../synthetic-corpus/ap-invoices/ANSWER_KEY.yaml"}, 400, "paths are never accepted"),
    ({"budget_usd": 1, "answer_key": "synthetic-corpus/ap-invoices/ANSWER_KEY.yaml"}, 400, "paths are never accepted"),
    ({"budget_usd": 1, "suite": "eval/ci/suites/ap-invoices.yaml"}, 400, "paths are never accepted"),
    ({"budget_usd": 1, "suite": "no-such-suite"}, 400, "known ids"),
    ({"budget_usd": 1, "suite": "ap-invoices", "answer_key": "jc-changeorders"}, 400, "is not the key of suite"),
    ({}, 400, "budget_usd is required"),
    ({"budget_usd": 0}, 400, "budget_usd must be a number > 0"),
    ({"budget_usd": True}, 400, "budget_usd must be a number > 0"),
    ({"budget_usd": 10_000}, 400, "ceiling"),
    ({"budget_usd": 1, "judge_runs": 0}, 400, "judge_runs must be"),
    ({"budget_usd": 1, "judge_runs": 6}, 400, "judge_runs must be"),
    ({"budget_usd": 1, "judge_runs": "3"}, 400, "judge_runs must be"),
    ({"smoke": True, "plan_only": True}, 400, "plan_only does not apply"),
    ({"smoke": "yes"}, 400, "smoke must be true or false"),
    ([1, 2], 400, "JSON object"),
])
def test_post_eval_validation(make_api, body, code, msg):
    api = make_api()
    st, r = api.call("POST", f"/api/runs/{AP}/eval", body)
    assert st == code and msg in r["error"], r
    assert api.argv() == []                           # nothing was started, not even a plan


def test_post_eval_content_type_and_unknown_run(make_api):
    api = make_api()
    st, r = api.call("POST", f"/api/runs/{AP}/eval", b'{"smoke": true}', headers={"Content-Type": "text/plain"})
    assert st == 415
    st, r = api.call("POST", "/api/runs/nope/eval", {"smoke": True})
    assert st == 404
    st, r = api.call("GET", "/api/runs/nope/eval")
    assert st == 404


# ── plan → 409 / plan_only / full run / smoke / lock ───────────────────────────────────────
def test_plan_over_budget_is_409_and_nothing_runs(make_api):
    api = make_api(STUB_PLAN_USD=5.0)
    st, r = api.call("POST", f"/api/runs/{AP}/eval", {"budget_usd": 1.0})
    assert st == 409 and r["state"] == "over_budget"
    assert r["planned_usd"] == 5.0 and r["budget_usd"] == 1.0 and r["suggested_budget_usd"] > 5.0
    assert "Nothing was spent" in r["error"] and r["plan"]["stages"]
    calls = api.argv()
    assert len(calls) == 1 and "--plan" in calls[0]   # only the plan ran
    assert calls[0][calls[0].index("--budget-usd") + 1] == "1.0000"
    _, s = api.call("GET", f"/api/runs/{AP}/eval/status")
    assert s["state"] == "over_budget" and s["spent_usd"] == 0.0


def test_plan_only_then_full_run_passes_cap_and_judge_runs(make_api, runs):
    api = make_api(STUB_PLAN_USD=0.8, STUB_SPEND_USD=0.75)
    st, r = api.call("POST", f"/api/runs/{AP}/eval", {"budget_usd": 3, "judge_runs": 2, "plan_only": True})
    assert st == 200 and r["state"] == "planned" and r["planned_usd"] == 0.8 and r["mode"] == "plan"
    assert r["suite"] == "ap-invoices"                        # inferred from the run's corpus
    st, r = api.call("POST", f"/api/runs/{AP}/eval", {"budget_usd": 3, "judge_runs": 2})
    assert st == 202 and r["state"] == "running" and r["planned_usd"] == 0.8
    s = api.wait_eval(AP, {"complete", "failed"})
    assert s["state"] == "complete" and s["spent_usd"] == 0.75 and s["verdict"] == "pass"
    assert any("stub suite: pass" in ln for ln in s["log_tail"])
    plan1, plan2, full = api.argv()
    assert "--plan" in plan1 and "--plan" in plan2 and "--plan" not in full
    assert full[full.index("--budget-usd") + 1] == "3.0000" and full[full.index("--judge-runs") + 1] == "2"
    assert f"run={runs['ap-fixture-run']}" in full
    # the finished suite shows up in the aggregate (launcher-owned eval dir)
    _, d = api.call("GET", f"/api/runs/{AP}/eval")
    assert any(x["mode"] == "full" and x["spent_usd"] == 0.75 for x in d["suites"])


def test_smoke_is_one_click_free_and_never_displaces_judged_numbers(make_api):
    api = make_api()
    st, r = api.call("POST", f"/api/runs/{AP}/eval", {"smoke": True})
    assert st == 202 and r["mode"] == "smoke" and r["budget_usd"] == 0.0 and r["planned_usd"] == 0.0
    s = api.wait_eval(AP, {"complete", "failed"})
    assert s["state"] == "complete" and s["spent_usd"] == 0.0
    (smoke,) = api.argv()
    assert "--smoke" in smoke and "--budget-usd" not in smoke and "--plan" not in smoke
    _, d = api.call("GET", f"/api/runs/{AP}/eval")
    assert any(x["mode"] == "smoke" for x in d["suites"])
    b = next(x for x in d["estimated"]["spec_artifacts"] if x["artifact"] == "bottled_spec")
    assert b["method"] == "llm" and b["metrics"]["recall_any_match_pct"]["value"] == 95.8
    assert any(Path(a).parent.name == "score-bottled" for a in b["alternatives"])   # smoke kept as alt


def test_deterministic_only_score_is_not_a_judged_recall(make_api, runs):
    (runs["ap-fixture-run"] / "validation" / "bottled" / "score.json").unlink()
    api = make_api()
    api.call("POST", f"/api/runs/{AP}/eval", {"smoke": True})
    api.wait_eval(AP, {"complete"})
    _, d = api.call("GET", f"/api/runs/{AP}/eval")
    b = next(x for x in d["estimated"]["spec_artifacts"] if x["artifact"] == "bottled_spec")
    assert b["method"] == "deterministic"
    assert b["metrics"]["recall_any_match_pct"]["status"] == "not_measured"
    assert "not a judged recall" in b["metrics"]["recall_any_match_pct"]["reason"]
    e = next(x for x in d["exact"]["spec_artifacts"] if x["artifact"] == "bottled_spec")
    assert e["citation_validity_pct"]["value"] == 100.0           # the exact part still counts


def test_only_one_eval_at_a_time_per_run(make_api):
    api = make_api(STUB_SLEEP=1.5)
    st, _ = api.call("POST", f"/api/runs/{AP}/eval", {"smoke": True})
    assert st == 202
    st, r = api.call("POST", f"/api/runs/{AP}/eval", {"smoke": True})
    assert st == 409 and "only one evaluation at a time" in r["error"]
    st, r = api.call("POST", f"/api/runs/{AP}/eval", {"budget_usd": 2})
    assert st == 409
    st, _ = api.call("POST", f"/api/runs/{JC}/eval", {"smoke": True})   # another run is independent
    assert st == 202
    api.wait_eval(AP, {"complete"})
    st, _ = api.call("POST", f"/api/runs/{AP}/eval", {"smoke": True})   # free again once finished
    assert st == 202
    api.wait_eval(AP, {"complete"})
    api.wait_eval(JC, {"complete"})


# ── imported runs ───────────────────────────────────────────────────────────────────────────
def test_import_run_is_read_only(make_api, runs, tmp_path):
    before = {k: _snapshot(v) for k, v in runs.items()}
    api = make_api()
    st, lst = api.call("GET", "/api/runs")
    got = {r["run_id"]: r for r in lst["runs"]}
    assert set(got) == {AP, JC}
    assert got[AP]["imported"] and got[AP]["read_only"] and got[AP]["status"] == "complete"
    assert got[AP]["links"]["eval"] == f"/api/runs/{AP}/eval"
    assert got[AP]["links"]["viewer_eval"].endswith("viewer/index.html#evaluation")
    for verb in ("start", "cancel"):
        st, r = api.call("POST", f"/api/runs/{AP}/{verb}")
        assert st == 409 and "read-only" in r["error"]
    api.call("POST", f"/api/runs/{AP}/eval", {"smoke": True})
    api.wait_eval(AP, {"complete"})
    api.call("GET", f"/runs/{AP}/files/viewer/pages/evaluation.html")
    after = {k: _snapshot(v) for k, v in runs.items()}
    assert after == before                                   # not one byte written into an import
    assert not (runs["ap-fixture-run"] / "launcher.json").exists()
    assert list((api.launcher.runs_dir / "_evals" / AP).glob("*/suite/suite.json"))
    # the CLI refuses a directory that is not a run output
    with pytest.raises(ValueError):
        LS.Launcher(tmp_path / "x.sh", tmp_path / "r2", import_runs=[tmp_path / "missing"])
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        LS.Launcher(tmp_path / "x.sh", tmp_path / "r3", import_runs=[tmp_path / "empty"])


# ── compare ─────────────────────────────────────────────────────────────────────────────────
def test_compare_flags_differences_inside_judge_noise(make_api):
    api = make_api()
    st, c = api.call("GET", f"/api/eval/compare?runs={AP},{JC}")
    assert st == 200 and c["runs"] == [AP, JC]
    assert c["comparable"] is False and "different answer keys" in c["warning"]
    rows = {r["name"]: r for r in c["rows"]}
    r = rows["bottled_spec.recall_any_match_pct"]                     # 95.8 [89.6-100] vs 94.1 [88.2-100]
    assert r["kind"] == "estimated" and r["within_noise"] is True and "95% CI" in r["basis"]
    r = rows["graph.recall_vbnet_pct"]                                # 0 -> 100: exact, real
    assert r["kind"] == "exact" and r["within_noise"] is False and r["delta"] == 100.0
    r = rows["benefit.qa.correct_pct"]                                 # absent on one side
    assert r["within_noise"] is None and r["a"]["status"] == "not_measured"
    st, same = api.call("GET", f"/api/eval/compare?runs={JC},{JC}")
    assert same["comparable"] is True
    assert all(x["within_noise"] in (True, None) for x in same["rows"])
    assert api.call("GET", f"/api/eval/compare?runs={AP}")[0] == 400
    assert api.call("GET", f"/api/eval/compare?runs={AP},nope")[0] == 404


def test_within_noise_uses_band_when_no_ci():
    a = {"value": 89.6, "ci95": None}
    assert eval_view._within(a, {"value": 80.0, "ci95": None}, "estimated", "recall.strict_full_pct")[0] is True
    assert eval_view._within(a, {"value": 70.0, "ci95": None}, "estimated", "recall.strict_full_pct")[0] is False
    assert eval_view._within(a, {"value": 89.0, "ci95": None}, "estimated", "recall.any_match_pct")[0] is True
    assert eval_view._within(a, {"value": 87.0, "ci95": None}, "estimated", "recall.any_match_pct")[0] is False


# ── guards: the new routes sit behind the same Host / Origin / gate checks ───────────────────
def test_eval_routes_honour_host_and_origin_guards(make_api):
    api = make_api()
    for method, path, body in (("GET", f"/api/runs/{AP}/eval", None), ("GET", f"/api/runs/{AP}/eval/status", None),
                               ("GET", f"/api/eval/compare?runs={AP},{JC}", None), ("GET", "/api/eval/options", None),
                               ("GET", f"/runs/{AP}/files/viewer/pages/evaluation.html", None),
                               ("POST", f"/api/runs/{AP}/eval", {"smoke": True})):
        st, _ = api.call(method, path, body, headers={"Host": "evil.example.com"})
        assert st == 403, (method, path)
    st, r = api.call("POST", f"/api/runs/{AP}/eval", {"smoke": True}, headers={"Origin": "http://evil.example.com"})
    assert st == 403 and "cross-origin" in r["error"]
    assert api.argv() == []


def test_eval_routes_honour_the_access_code_gate(make_api):
    api = make_api(access_code="s3cret-code", allowed_hosts_extra=["demo.example.net"])
    for method, path, body in (("GET", f"/api/runs/{AP}/eval", None), ("GET", f"/api/runs/{AP}/eval/status", None),
                               ("GET", f"/api/eval/compare?runs={AP},{JC}", None), ("GET", "/api/eval/options", None),
                               ("GET", f"/api/runs/{AP}/api/eval", None),
                               ("POST", f"/api/runs/{AP}/eval", {"smoke": True})):
        st, r = api.call(method, path, body)
        assert st == 401 and r["login"] == "/api/auth", (method, path)
    st, page = api.call("GET", f"/runs/{AP}/files/viewer/pages/evaluation.html", headers={"Accept": "text/html"})
    assert st == 200 and b"EVAL_API_BASE" not in page and b"access" in page.lower()   # the challenge page
    assert api.argv() == []
    st, _ = api.call("POST", "/api/auth", {"code": "s3cret-code"})
    assert st == 200 and api.cookie
    st, d = api.call("GET", f"/api/runs/{AP}/eval")
    assert st == 200 and d["exact"]["conservation"]["status"] == "measured"


# ── the viewer's Evaluation page ────────────────────────────────────────────────────────────
def test_viewer_evaluation_page_and_tab(make_api, runs):
    api = make_api()
    st, page = api.call("GET", f"/runs/{AP}/files/viewer/pages/evaluation.html")
    assert st == 200 and f'const EVAL_API_BASE = "/api/runs/{AP}";'.encode() in page
    assert b"/api/eval" in page and b"Not measured" in page
    assert not (runs["ap-fixture-run"] / "viewer" / "pages" / "evaluation.html").exists()   # served, not written
    st, shell = api.call("GET", f"/runs/{AP}/files/viewer/index.html")
    assert st == 200 and b'"pages/evaluation.html"' in shell and b"Evaluation" in shell
    assert shell.count(b"launcher: Evaluation tab") == 1
    # the viewer's API_BASE + "/api/eval" form reaches the same route
    st1, a = api.call("GET", f"/api/runs/{AP}/api/eval")
    st2, b = api.call("GET", f"/api/runs/{AP}/eval")
    assert st1 == st2 == 200 and a["exact"] == b["exact"]
    st, s = api.call("GET", f"/api/runs/{AP}/api/eval/status")
    assert st == 200 and s["state"] == "idle"
    # a run with no viewer still gets the page (the launcher detail links to it)
    st, _ = api.call("GET", f"/runs/{JC}/files/viewer/pages/evaluation.html")
    assert st == 200
    assert api.call("GET", "/runs/nope/files/viewer/pages/evaluation.html")[0] == 404


def test_viewer_index_injection_is_idempotent(tmp_path):
    shell = tmp_path / "index.html"
    shell.write_text('<div id="tabs"><button data-page="pages/evaluation.html">Evaluation</button></div></body>')
    assert LS.Launcher.viewer_index(shell) == shell.read_bytes()      # a new viewer already has the tab


def test_launcher_page_has_the_evaluation_ui(make_api):
    api = make_api()
    st, html = api.call("GET", "/")
    assert st == 200
    for s in (b"Evaluation", b"Score this spec", b"Advanced (for experts)", b"Compare evaluations",
              b"Grounded = checked against your code, no AI. Score = compared with the known rules, graded by AI.",
              b"/api/eval/options", b"plan_only", b"setInterval(refresh, 10000)"):
        assert s in html, s
    for old in ("Quick check — free", "Full evaluation — see cost first", "Build test data set — free"):
        assert old.encode() not in html, old
    st, o = api.call("GET", "/api/eval/options")
    assert st == 200 and {s["id"] for s in o["suites"]} >= {"ap-invoices", "jc-changeorders"}
    assert all("abs_path" not in k for k in o["answer_keys"])


def test_serve_viewer_entry_point_matches(runs):
    d = viewer_api.eval_summary(str(runs["ap-fixture-run"]))
    assert d["exact"]["conservation"]["lines"]["value"] == 2336
    assert d["estimated"]["traps"]["status"] == "measured"


def test_generator_writes_the_evaluation_page(tmp_path):
    pytest.importorskip("markdown")
    gen = HERE.parents[1] / "pipeline" / "generate-viewer-v3.py"
    spec = importlib.util.spec_from_file_location("gen_viewer_v3", gen)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.write_evaluation_page(tmp_path, "/api/runs/r1")
    html = (tmp_path / "evaluation.html").read_text()
    assert 'const EVAL_API_BASE = "/api/runs/r1";' in html and '"__EVAL_API_BASE__"' not in html
    assert mod.write_evaluation_page(tmp_path, "")
    assert 'const EVAL_API_BASE = "";' in (tmp_path / "evaluation.html").read_text()
