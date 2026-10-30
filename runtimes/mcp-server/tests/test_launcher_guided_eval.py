# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The launcher's guided evaluation flow: the automatic grounding check (free smoke suite, once per
finished run), GET /api/runs/<id> -> grounding / score_state, "Approve all" for an answer key, and
a jsdom click-through of the page. No AWS: run_suite.py is tests/eval_suite_stub.py, the answer
key is built by eval/keygen/build_key.py (deterministic, no AI) on a synthetic fixture."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import launcher_server as LS  # noqa: E402
from test_launcher_eval import Api  # noqa: E402

pytest.importorskip("yaml")

REPO = HERE.parent.parent.parent
FIX = HERE / "fixtures" / "eval"
KEYGEN_FIX = REPO / "eval" / "keygen" / "tests" / "fixtures" / "mini-cs"
STUB = HERE / "eval_suite_stub.py"
JSDOM = Path("/tmp/uitest/node_modules/jsdom")
DEMO = Path("output")


def _mk_run(runs_dir: Path, run_id: str, source: dict, state: str = "complete", viewer: bool = False) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    (d / "launcher.json").write_text(json.dumps({"run_id": run_id, "state": state,
                                                 "request": {"name": run_id, "source": source}}))
    if viewer:
        (d / "viewer").mkdir()
        (d / "viewer" / "index.html").write_text("<html><body>viewer</body></html>")
    return d


@pytest.fixture()
def env(tmp_path, monkeypatch):
    src = tmp_path / "src"
    shutil.copytree(KEYGEN_FIX, src)
    runs_dir = tmp_path / "runs"
    _mk_run(runs_dir, "r1", {"type": "path", "path": str(src)})
    imports = tmp_path / "imports"
    shutil.copytree(FIX / "ap-fixture-run", imports / "ap-fixture-run")
    shutil.copytree(FIX / "ap-fixture-run", imports / "ap-invoices-demo")   # a shipped key matches
    argv_log = tmp_path / "suite_argv.jsonl"
    monkeypatch.setenv("STUB_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("STUB_WRITE_SCORE", "1")
    servers = []

    def make(*, auto=True, import_dirs=None, access_code=None, launcher_only=False):
        L = LS.Launcher(tmp_path / "no-pipeline.sh", runs_dir, 1,
                        import_runs=import_dirs if import_dirs is not None else
                        [imports / "ap-fixture-run", imports / "ap-invoices-demo"],
                        eval_cmd=[sys.executable, str(STUB)], auto_grounding=auto)
        if launcher_only:
            return L
        srv = LS.make_server(L, "127.0.0.1", 0, access_code=access_code)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        api = Api(f"http://127.0.0.1:{srv.server_address[1]}")
        api.launcher = L
        api.argv = lambda: [json.loads(x) for x in argv_log.read_text().splitlines()] if argv_log.exists() else []
        return api

    yield {"make": make, "src": src, "runs": runs_dir, "imports": imports, "tmp": tmp_path}
    for s in servers:
        s.shutdown()
        s.server_close()


def smokes(api_or_launcher, rid):
    L = getattr(api_or_launcher, "launcher", api_or_launcher)
    return [j for j in L.eval_history.get(rid, []) if j.mode == "smoke"]


def wait_idle(L, rid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = L.evals.get(rid)
        if j is None or j.state not in LS.EVAL_ACTIVE:
            return j
        time.sleep(0.05)
    raise AssertionError(f"{rid}: evaluation still active")


def wait_for(fn, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("condition never became true")


# ── automatic grounding check ─────────────────────────────────────────────────────────────────
def test_auto_check_fires_once_for_imported_and_complete_runs(env):
    api = env["make"]()
    for _ in range(3):                                    # listed many times: still one check per run
        assert api.call("GET", "/api/runs")[0] == 200
    for rid in ("imported-ap-fixture-run", "r1"):
        wait_idle(api.launcher, rid)
        js = smokes(api, rid)
        assert len(js) == 1 and js[0].trigger == "auto" and js[0].state == "complete", (rid, js)
    # ap-invoices-demo already has a smoke result (the repo's eval/ci/baselines smoke of that run):
    # no new check, and its grounding line is read from that result
    assert smokes(api, "imported-ap-invoices-demo") == []
    g = api.call("GET", "/api/runs/imported-ap-invoices-demo")[1]["grounding"]
    assert g["state"] in ("passed", "warning") and g["eval_id"] is None
    smoke_argvs = [a for a in api.argv() if "--smoke" in a]
    assert len(smoke_argvs) == 2
    # GET one run: the same, no second check
    _, d = api.call("GET", "/api/runs/r1")
    assert len(smokes(api, "r1")) == 1 and d["grounding"]["trigger"] == "auto"


def test_auto_check_never_for_failed_cancelled_running_or_disabled(env):
    _mk_run(env["runs"], "failed-no-output", {"type": "path", "path": str(env["src"])}, state="failed")
    _mk_run(env["runs"], "cancelled", {"type": "path", "path": str(env["src"])}, state="cancelled", viewer=True)
    _mk_run(env["runs"], "failed-with-viewer", {"type": "path", "path": str(env["src"])}, state="failed", viewer=True)
    L = env["make"](launcher_only=True, import_dirs=[])
    running = LS.Run("live", env["runs"] / "r1", {"name": "live", "source": {"type": "path", "path": str(env["src"])}})
    running.state = "running"
    L.runs["live"] = running
    L.list()
    for rid in ("failed-no-output", "cancelled", "failed-with-viewer", "live"):
        assert smokes(L, rid) == [], rid
    assert len(smokes(L, "r1")) == 1                      # the complete one did get its check
    wait_idle(L, "r1")
    off = env["make"](launcher_only=True, import_dirs=[env["imports"] / "ap-fixture-run"], auto=False)
    assert off.list() and smokes(off, "imported-ap-fixture-run") == []      # --no-auto-check


def test_auto_check_fires_when_a_run_completes(env):
    L = env["make"](launcher_only=True, import_dirs=[], auto=True)
    d = env["runs"] / "fresh"
    d.mkdir()
    (d / "viewer").mkdir()
    (d / "viewer" / "index.html").write_text("<html><body>v</body></html>")
    run = LS.Run("fresh", d, {"name": "fresh", "source": {"type": "path", "path": str(env["src"])}})

    class Proc:
        def __init__(self, rc):
            self.rc, self.pid = rc, 999999

        def wait(self, timeout=None):
            return self.rc

    run.state, run.proc = "running", Proc(0)
    (d / "run_status.json").write_text(json.dumps({"status": "complete"}))
    L.runs["fresh"] = run
    L._watch(run)
    assert run.state == "complete"
    js = smokes(L, "fresh")
    assert len(js) == 1 and js[0].trigger == "auto"
    wait_idle(L, "fresh")
    L.auto_ground(run)                                    # a second call never re-fires
    assert len(smokes(L, "fresh")) == 1
    # a run that ends failed gets no check
    d2 = env["runs"] / "broken"
    d2.mkdir()
    run2 = LS.Run("broken", d2, {"name": "broken", "source": {"type": "path", "path": str(env["src"])}})
    run2.state, run2.proc = "running", Proc(1)
    (d2 / "run_status.json").write_text(json.dumps({"status": "failed", "failed_stages": ["x"]}))
    L.runs["broken"] = run2
    L._watch(run2)
    assert run2.state == "failed" and smokes(L, "broken") == []


def test_restart_does_not_refire_and_existing_smoke_result_is_respected(env):
    api = env["make"]()
    api.call("GET", "/api/runs")
    for rid in ("r1", "imported-ap-fixture-run"):
        wait_idle(api.launcher, rid)
    n = len([a for a in api.argv() if "--smoke" in a])
    # a smoke suite.json already in an imported run directory: never auto-checked again
    pre = env["imports"] / "jc-copy"
    shutil.copytree(FIX / "jc-fixture-run", pre)
    (pre / "validation" / "smoke").mkdir()
    (pre / "validation" / "smoke" / "suite.json").write_text(json.dumps({
        "suite_schema_version": 1, "provenance": {"suite": "jc", "smoke": True, "plan": False},
        "started_at": "2026-10-07T00:00:00+00:00", "budget": {"cap_usd": 0, "spent_usd": 0},
        "stages": [], "verdict": "pass", "exit_code": 0}))
    api2 = env["make"](import_dirs=[env["imports"] / "ap-fixture-run", env["imports"] / "ap-invoices-demo", pre])
    api2.call("GET", "/api/runs")
    time.sleep(0.3)
    for rid in ("r1", "imported-ap-fixture-run"):
        wait_idle(api2.launcher, rid)
        assert len(smokes(api2, rid)) == 1, rid          # loaded from disk, not re-run
    assert smokes(api2, "imported-jc-copy") == []
    new = len([a for a in api2.argv() if "--smoke" in a])
    assert new == n                                       # nothing re-fired after the restart
    _, d = api2.call("GET", "/api/runs/imported-jc-copy")
    assert d["grounding"]["state"] in ("passed", "warning") and d["grounding"]["eval_id"] is None


# ── grounding summary ─────────────────────────────────────────────────────────────────────────
def test_grounding_summary_line(env):
    api = env["make"]()
    rid = "imported-ap-fixture-run"
    _, d = api.call("GET", f"/api/runs/{rid}")
    assert d["grounding"]["state"] in ("running", "passed")
    if d["grounding"]["state"] == "running":
        assert d["grounding"]["summary"] == "Checking grounding… (free)"
    wait_idle(api.launcher, rid)
    _, d = api.call("GET", f"/api/runs/{rid}")
    g = d["grounding"]
    assert g["state"] == "passed", g
    assert re.match(r"✓ Grounded — 2,336 lines read in \d[\d,]* files · all [\d,]+ citations point at real code", g["summary"]), g
    assert g["details"].endswith("#evaluation") and g["free"] is True
    # a failing check reads as a clear warning
    L = api.launcher
    run = L.get(rid)
    agg = L.eval_get(rid)[1]
    agg["exact"]["spec_artifacts"] = [{"artifact": "bottled_spec", "citations_total": {"status": "measured", "value": 40},
                                        "citations_invalid": {"status": "measured", "value": 3}}]
    agg["exact"]["graph"] = {"status": "measured", "parser_confirmed_edge_precision_pct": {"status": "measured", "value": 100.0},
                             "gate_violations": []}
    w = L.grounding(run, agg)
    assert w["state"] == "warning"
    assert w["summary"].startswith("⚠ Needs attention — 3 of 40 citations do not point at real code")
    assert "graph matches the code parser (100%)" in w["summary"]
    # a check that did not finish
    smokes(L, rid)[-1].state, smokes(L, rid)[-1].error = "failed", "run_suite.py exited 2"
    assert L.grounding(run, agg)["summary"] == "⚠ The grounding check did not finish — run_suite.py exited 2"


# ── score_state: the stepper ──────────────────────────────────────────────────────────────────
def test_score_state_no_key_build_approve_plan_start_result(env):
    api = env["make"](import_dirs=[])
    _, d = api.call("GET", "/api/runs/r1")
    ss = d["score_state"]
    assert ss["can_score"] and ss["key"]["readiness"] == "none" and ss["step"] == "key" and ss["result"] is None
    wait_idle(api.launcher, "r1")                         # the automatic check
    assert api.call("POST", "/api/runs/r1/dataset", {})[0] == 202
    _, d = api.call("GET", "/api/runs/r1")
    assert d["score_state"]["key"]["building"] or d["score_state"]["key"]["readiness"] == "draft"
    wait_for(lambda: api.call("GET", "/api/runs/r1/dataset/status")[1]["state"] == "ready")
    _, d = api.call("GET", "/api/runs/r1")
    k = d["score_state"]["key"]
    assert k["readiness"] == "draft" and k["provisional"] and k["dataset"]["total"] > 0 and d["score_state"]["step"] == "key"
    st, r = api.call("POST", "/api/runs/r1/dataset/review", {"approve": "corroborated"})
    assert st == 200 and r["counts"]["approved"] >= 1
    _, d = api.call("GET", "/api/runs/r1")
    ss = d["score_state"]
    assert ss["key"]["readiness"] == "approved" and ss["key"]["rules"] == r["counts"]["approved"]
    assert ss["step"] == "cost" and ss["plan"] is None and not ss["provisional"]
    use = ss["key"]["use"]
    assert use == {"suite": "dataset", "answer_key": "dataset"}
    st, p = api.call("POST", "/api/runs/r1/eval", {**use, "plan_only": True, "budget_usd": 500})
    assert st == 200, p
    _, d = api.call("GET", "/api/runs/r1")
    ss = d["score_state"]
    assert ss["step"] == "cost" and ss["plan"]["planned_usd"] == 2.5 and ss["plan"]["cap_usd"] == 3.13
    st, j = api.call("POST", "/api/runs/r1/eval", {**use, "budget_usd": ss["plan"]["cap_usd"]})
    assert st == 202, j
    _, d = api.call("GET", "/api/runs/r1")
    assert d["score_state"]["step"] in ("running", "results")
    wait_idle(api.launcher, "r1")
    _, d = api.call("GET", "/api/runs/r1")
    ss = d["score_state"]
    assert ss["step"] == "results" and ss["scoring"]["state"] == "complete" and ss["scoring"]["spent_usd"] == 1.25
    r = ss["result"]
    assert r["summary"].startswith("Scored: 81.2% of known rules found (range 75–88%)"), r
    assert "planted problems caught" in r["summary"] and r["traps_total"] >= 1
    assert r["double_check"] is False and r["provisional"] is None and r["details"]


def test_shipped_key_skips_step_one(env):
    api = env["make"](auto=False)
    _, d = api.call("GET", "/api/runs/imported-ap-invoices-demo")
    ss = d["score_state"]
    assert ss["key"]["readiness"] == "shipped" and ss["key"]["rules"] == 48
    assert ss["key"]["use"] == {"suite": "ap-invoices", "answer_key": "ap-invoices"}
    assert ss["step"] == "cost" and ss["result"] is None          # Step 1 is skipped
    st, _ = api.call("POST", "/api/runs/imported-ap-invoices-demo/eval", {**ss["key"]["use"], "plan_only": True, "budget_usd": 500})
    assert st == 200
    assert api.call("GET", "/api/runs/imported-ap-invoices-demo")[1]["score_state"]["step"] == "cost"


def test_double_check_shown_only_for_a_wide_range(env, monkeypatch):
    api = env["make"](auto=False)
    rid = "imported-ap-invoices-demo"
    use = api.call("GET", f"/api/runs/{rid}")[1]["score_state"]["key"]["use"]
    api.launcher.base_env["STUB_CI"] = "60,90"           # a 30-point range
    assert api.call("POST", f"/api/runs/{rid}/eval", {**use, "budget_usd": 5})[0] == 202
    wait_idle(api.launcher, rid)
    r = api.call("GET", f"/api/runs/{rid}")[1]["score_state"]["result"]
    assert r["ci_width"] == 30 and r["double_check"] is True and r["judge_runs"] == 1
    # re-graded with 3 passes: no further double-check offered
    assert api.call("POST", f"/api/runs/{rid}/eval", {**use, "budget_usd": 15, "judge_runs": 3})[0] == 202
    wait_idle(api.launcher, rid)
    r = api.call("GET", f"/api/runs/{rid}")[1]["score_state"]["result"]
    assert r["judge_runs"] == 3 and r["double_check"] is False
    api.launcher.base_env["STUB_CI"] = "75,90"           # exactly 15 points: not wider than 15
    assert api.call("POST", f"/api/runs/{rid}/eval", {**use, "budget_usd": 5})[0] == 202
    wait_idle(api.launcher, rid)
    assert api.call("GET", f"/api/runs/{rid}")[1]["score_state"]["result"]["double_check"] is False


# ── Approve all ───────────────────────────────────────────────────────────────────────────────
def test_approve_all(env):
    api = env["make"](auto=False, import_dirs=[])
    assert api.call("POST", "/api/runs/r1/dataset", {})[0] == 202
    wait_for(lambda: api.call("GET", "/api/runs/r1/dataset/status")[1]["state"] == "ready")
    _, items = api.call("GET", "/api/runs/r1/dataset/items?status=")
    first = items["items"][0]["id"]
    assert api.call("POST", "/api/runs/r1/dataset/review", {"decisions": [{"id": first, "action": "reject"}]})[0] == 200
    assert api.call("POST", "/api/runs/r1/dataset/review", {"approve": "everything"})[0] == 400
    st, r = api.call("POST", "/api/runs/r1/dataset/review", {"approve": "all", "reviewer": "demo"})
    assert st == 200
    c = r["counts"]
    assert c["approved"] == c["total"] - 1 and c["by_status"]["rejected"] == 1 and c["bulk_approved"] == c["total"] - 1
    assert r["changed"] == c["total"] - 1
    st, again = api.call("POST", "/api/runs/r1/dataset/review", {"approve": "all"})
    assert st == 200 and again["changed"] == 0 and again["counts"] == c          # idempotent
    dec = json.loads((env["runs"] / "_datasets" / "r1").glob("*/decisions.json").__next__().read_text())
    assert dec[first]["decision"] == "reject" and "bulk" not in dec[first]
    others = [v for k, v in dec.items() if k != first]
    assert all(v["bulk"] == "all" and v["reviewer"] == "demo" and v["at"] for v in others)
    key = r["key"]
    assert key["provisional"] is False and key["bulk_approved"] == c["total"] - 1
    assert key["note"].startswith("Answer key approved in bulk (not individually reviewed)")
    # results scored against it are not provisional, and carry the note on the scorecard
    _, d = api.call("GET", "/api/runs/r1")
    assert d["score_state"]["key"]["readiness"] == "approved" and d["score_state"]["key"]["note"]
    assert api.call("POST", "/api/runs/r1/eval", {"suite": "dataset", "answer_key": "dataset", "budget_usd": 5})[0] == 202
    wait_idle(api.launcher, "r1")
    _, agg = api.call("GET", "/api/runs/r1/eval")
    assert agg["key_note"].startswith("Answer key approved in bulk") and not agg.get("provisional")
    r = api.call("GET", "/api/runs/r1")[1]["score_state"]["result"]
    assert r["provisional"] is None and r["note"].startswith("Answer key approved in bulk")


# ── gate / Host / Origin unchanged for the detail route ───────────────────────────────────────
def test_detail_route_keeps_the_gate(env):
    api = env["make"](auto=False, import_dirs=[])
    assert api.call("GET", "/api/runs/r1", headers={"Host": "evil.example"})[0] == 403
    assert api.call("POST", "/api/runs/r1/dataset/review", {"approve": "all"}, headers={"Origin": "http://evil.example"})[0] == 403
    gated = env["make"](auto=False, import_dirs=[], access_code="letmein")
    assert gated.call("GET", "/api/runs/r1")[0] == 401
    assert gated.call("POST", "/api/runs/r1/dataset/review", {"approve": "all"})[0] == 401


# ── the page ──────────────────────────────────────────────────────────────────────────────────
def test_page_has_the_guided_flow(tmp_path):
    html = (HERE.parent / "launcher_ui" / "index.html").read_text()
    for s in ("Score this spec", "Answer key", "Build answer key", "Start scoring", "Estimated cost",
              "nothing spent yet", "Double-check this score", "grades twice more and takes the majority",
              "Re-check", "details →", "Advanced (for experts)",
              "Grounded = checked against your code, no AI. Score = compared with the known rules, graded by AI."):
        assert s in html, s
    for old in ("Quick check — free", "Build test data set — free", "Full evaluation — see cost first",
                'data-field="dataset"', "makeDatasetPanel"):
        assert old not in html, old
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    for i, js in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        p = tmp_path / f"s{i}.js"
        p.write_text(js)
        r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not JSDOM.is_dir() or not shutil.which("node"), reason="jsdom not installed at /tmp/uitest")
def test_jsdom_click_through(env, tmp_path):
    """The page in jsdom against a live launcher: grounding line, then Score this spec ->
    Build answer key -> Approve all (confirm) -> cost -> Start scoring -> result. Also the
    shipped-key path on imported copies of the demo outputs when they are on this machine."""
    imports = [env["imports"] / "ap-fixture-run"]
    demos = []
    for name in ("ap-invoices-demo", "jc-changeorders-v2"):
        if (DEMO / name).is_dir():
            dst = Path("/tmp") / f"guided-ui-{os.getpid()}" / name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(DEMO / name, dst, symlinks=True, ignore=shutil.ignore_patterns("stills", "telemetry", "*.db"))
            imports.append(dst)
            demos.append(f"imported-{name}")
    api = env["make"](import_dirs=imports)
    r = subprocess.run(["node", str(HERE / "ui_flow_sim.js"), api.base, "r1", ",".join(demos)],
                       capture_output=True, text=True, timeout=60,
                       env={**os.environ, "NODE_PATH": str(JSDOM.parent)})
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    out = r.stdout
    print(out)
    for s in ("GROUND ✓ Grounded —", "STEP1 To score correctness", "PROPOSED", "CONFIRM Approve all",
              "READY Answer key ready —", "COST Estimated cost $2.50 — nothing spent yet", "RESULT Scored: 81.2% of known rules found"):
        assert s in out, out[-4000:]
    for d in demos:
        assert f"SHIPPED {d} Answer key ready —" in out, out[-4000:]
    if demos:
        shutil.rmtree(Path("/tmp") / f"guided-ui-{os.getpid()}", ignore_errors=True)
