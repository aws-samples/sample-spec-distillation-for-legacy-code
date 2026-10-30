# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test data sets in the launcher: POST/GET /api/runs/<id>/dataset, /dataset/items, /dataset/review,
and their use as the default answer key of a Full evaluation. No AWS: build_key.py runs for real
(deterministic, no AI) on the synthetic eval/keygen fixture; run_suite.py is the eval stub."""
from __future__ import annotations

import json
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
import launcher_datasets as LD  # noqa: E402
import launcher_server as LS  # noqa: E402
from test_launcher_eval import Api, _snapshot  # noqa: E402

pytest.importorskip("yaml")

REPO = HERE.parent.parent.parent
FIXTURE = REPO / "eval" / "keygen" / "tests" / "fixtures" / "mini-cs"
STUB = HERE / "eval_suite_stub.py"
IMPORT_FIX = HERE / "fixtures" / "eval" / "ap-fixture-run"
ESHOP = Path("/tmp/eshop")


def _mk_run(runs_dir: Path, run_id: str, source: dict, state: str = "complete") -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    (d / "launcher.json").write_text(json.dumps({"run_id": run_id, "state": state,
                                                 "request": {"name": run_id, "source": source}}))
    return d


@pytest.fixture()
def env(tmp_path, monkeypatch):
    src = tmp_path / "src"
    shutil.copytree(FIXTURE, src)
    runs_dir = tmp_path / "runs"
    _mk_run(runs_dir, "r1", {"type": "path", "path": str(src)})
    imp = tmp_path / "imports" / "ap-fixture-run"
    shutil.copytree(IMPORT_FIX, imp)
    argv_log = tmp_path / "suite_argv.jsonl"
    monkeypatch.setenv("STUB_ARGV_LOG", str(argv_log))
    servers = []

    def make(*, access_code=None):
        L = LS.Launcher(tmp_path / "no-pipeline.sh", runs_dir, 1, import_runs=[imp],
                        eval_cmd=[sys.executable, str(STUB)])
        srv = LS.make_server(L, "127.0.0.1", 0, access_code=access_code)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        api = Api(f"http://127.0.0.1:{srv.server_address[1]}")
        api.launcher = L
        api.argv = lambda: [json.loads(x) for x in argv_log.read_text().splitlines()] if argv_log.exists() else []
        return api

    yield {"make": make, "src": src, "runs": runs_dir, "imp": imp, "tmp": tmp_path}
    for s in servers:
        s.shutdown()
        s.server_close()


def wait_ready(api, rid="r1", timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        st, d = api.call("GET", f"/api/runs/{rid}/dataset/status")
        if d.get("state") not in ("building",):
            return d
        time.sleep(0.1)
    raise AssertionError("build never finished")


def built(env):
    api = env["make"]()
    st, d = api.call("POST", "/api/runs/r1/dataset", {})
    assert st == 202, d
    d = wait_ready(api)
    assert d["state"] == "ready", d
    return api, d


# ── build ───────────────────────────────────────────────────────────────────────────────────
def test_build_counts_items_and_excerpts(env):
    api, d = built(env)
    c = d["counts"]
    assert c["total"] == 32 and c["corroborated"] == 2 and c["approved"] == 0
    assert c["by_source"] == {"code": 31, "tests": 3}
    assert c["by_status"]["unreviewed"] == 32
    assert d["key"]["provisional"] is True
    assert d["key"]["label"] == "Provisional — test data set not yet reviewed (0 of 32 items approved)"
    assert "not from the generated spec" in d["independence"]
    st, items = api.call("GET", "/api/runs/r1/dataset/items?status=corroborated")
    assert st == 200 and items["total"] == 2
    it = items["items"][0]
    assert it["corroborated"] and it["sources"] == ["code", "tests"] and it["evidence_class"] == "Verified"
    ex = it["excerpts"][0]
    assert any(l["cited"] for l in ex["lines"]) and all(isinstance(l["n"], int) for l in ex["lines"])
    # filters, search, paging
    _, page = api.call("GET", "/api/runs/r1/dataset/items?source=tests")
    assert page["total"] == 3
    _, page = api.call("GET", "/api/runs/r1/dataset/items?q=buyer")
    assert page["total"] >= 1 and all("buyer" in i["statement"].lower() for i in page["items"])
    _, page = api.call("GET", "/api/runs/r1/dataset/items?page=2")
    assert page["page"] == 2 and len(page["items"]) == 32 - LD.PAGE_SIZE
    assert api.call("GET", "/api/runs/r1/dataset/items?status=bogus")[0] == 400
    assert api.call("GET", "/api/runs/r1/dataset/items?page=x")[0] == 400


def test_build_argv_has_only_source_and_dataset_dir(env):
    api, d = built(env)
    ds = api.launcher.datasets.get("r1")
    argv = ds.build_argv
    assert argv[1].endswith("eval/keygen/build_key.py")
    assert argv[argv.index("--source") + 1] == str(env["src"].resolve())
    out = Path(argv[argv.index("--out") + 1])
    assert out.is_relative_to(env["runs"].resolve() / "_datasets" / "r1")
    run_dir = str((env["runs"] / "r1").resolve())
    assert not any(a == run_dir or a.startswith(run_dir + "/") for a in argv)
    assert not any(x in " ".join(argv) for x in ("bottled_spec", "master_blend", "viewer", "--llm-draft"))


def test_independence_rule_refuses_output_dirs(env):
    api = env["make"]()
    runs = env["runs"]
    out = runs / "r1"
    # a path run pointing at its own output, at a parent of the runs dir, or at a viewer
    _mk_run(runs, "self", {"type": "path", "path": str(runs / "self")})
    _mk_run(runs, "parent", {"type": "path", "path": str(env["tmp"])})
    viewer = env["tmp"] / "someout"
    (viewer / "viewer").mkdir(parents=True)
    (viewer / "run_status.json").write_text("{}")
    _mk_run(runs, "viewer", {"type": "path", "path": str(viewer)})
    api = env["make"]()
    for rid in ("self", "parent", "viewer"):
        st, d = api.call("POST", f"/api/runs/{rid}/dataset", {})
        assert st == 409 and d["reason"] == "independence", (rid, d)
        assert "not from the generated spec" in d["error"]
    assert LD.independence_refusal(out / ".source" / "sub", out, runs) is None


def test_source_unavailable_and_not_finished(env):
    runs = env["runs"]
    _mk_run(runs, "gone", {"type": "path", "path": str(env["tmp"] / "missing")})
    _mk_run(runs, "busy", {"type": "path", "path": str(env["src"])}, state="estimated")
    api = env["make"]()
    st, d = api.call("POST", "/api/runs/gone/dataset", {})
    assert st == 409 and d["reason"] == "source_unavailable"
    st, d = api.call("POST", "/api/runs/busy/dataset", {})
    assert st == 409 and "a test data set" in d["error"]


def test_git_run_uses_source_clone_with_subdir(env):
    runs = env["runs"]
    d = _mk_run(runs, "g1", {"type": "git", "url": "https://example.com/x.git", "subdir": "app",
                             "display_url": "https://example.com/x.git"})
    shutil.copytree(FIXTURE, d / ".source" / "app")
    (d / "source_provenance.json").write_text(json.dumps({"target": str(d / ".source" / "app"), "subdir": "app"}))
    api = env["make"]()
    assert api.launcher.dataset_source(api.launcher.get("g1")) == str(d / ".source" / "app")
    assert api.call("POST", "/api/runs/g1/dataset", {})[0] == 202
    assert wait_ready(api, "g1")["counts"]["total"] == 32


def test_body_validation(env):
    api = env["make"]()
    assert api.call("POST", "/api/runs/r1/dataset", {"aws_secret": "x"})[0] == 400
    assert api.call("POST", "/api/runs/r1/dataset", {"source": "/etc"})[0] == 400
    assert api.call("POST", "/api/runs/r1/dataset", {"ai_suggestions": True})[0] == 400       # cap required
    assert api.call("POST", "/api/runs/r1/dataset", {"ai_suggestions": True, "max_usd": 1e6})[0] == 400
    assert api.call("POST", "/api/runs/r1/dataset", {"max_usd": 2})[0] == 400
    assert api.call("POST", "/api/runs/r1/dataset", b"{}", headers={"Content-Type": "text/plain"})[0] == 415
    assert api.call("POST", "/api/runs/nope/dataset", {})[0] == 404


def test_ai_suggestions_pass_cap_to_llm_draft(env):
    api = env["make"]()
    L = api.launcher
    ds = LD.Dataset("x", "r1", env["tmp"] / "x", str(env["src"]), True, 1.5)
    argv = L.datasets.build_argv(ds, "c")
    assert argv[-3:] == ["--llm-draft", "--max-usd", "1.5000"]


def test_one_build_per_run(env):
    api = env["make"]()
    L = api.launcher
    slow = env["tmp"] / "slow.py"
    slow.write_text("import time; time.sleep(1.5)\n")
    L.datasets.build_argv = lambda ds, corpus: [sys.executable, str(slow)]
    assert api.call("POST", "/api/runs/r1/dataset", {})[0] == 202
    st, d = api.call("POST", "/api/runs/r1/dataset", {})
    assert st == 409 and "one build per run" in d["error"]
    d = wait_ready(api)
    assert d["state"] == "failed" and "without a key" in d["error"]       # the stand-in wrote none
    assert api.call("POST", "/api/runs/r1/dataset", {})[0] == 202        # lock released


# ── review ──────────────────────────────────────────────────────────────────────────────────
def test_review_bulk_edit_reject_idempotent_and_persistent(env):
    api, d = built(env)
    st, r = api.call("POST", "/api/runs/r1/dataset/review", {"approve": "corroborated", "reviewer": "Manasi"})
    assert st == 200 and r["changed"] == 2 and r["counts"]["approved"] == 2
    assert r["key"]["provisional"] is False
    assert r["key"]["label"].startswith("Using this run's test data set (2 approved items)")
    st, r = api.call("POST", "/api/runs/r1/dataset/review", {"approve": "corroborated"})
    assert r["changed"] == 0                                              # idempotent
    _, items = api.call("GET", "/api/runs/r1/dataset/items?status=needs_review")
    a, b = items["items"][0]["id"], items["items"][1]["id"]
    body = {"decisions": [{"id": a, "action": "edit", "statement": "Orders need a buyer."},
                          {"id": b, "action": "reject"}], "reviewer": "Manasi"}
    st, r = api.call("POST", "/api/runs/r1/dataset/review", body)
    assert st == 200 and r["changed"] == 2
    assert api.call("POST", "/api/runs/r1/dataset/review", body)[1]["changed"] == 0
    c = r["counts"]
    assert c["by_status"] == {"unreviewed": 28, "approved": 2, "edited": 1, "rejected": 1}
    assert c["approved"] == 3 and c["reviewed"] == 4
    _, items = api.call("GET", "/api/runs/r1/dataset/items?status=approved")
    ed = next(i for i in items["items"] if i["id"] == a)
    assert ed["statement"] == "Orders need a buyer." and ed["original_statement"] and ed["reviewer"] == "Manasi"
    # reviewed + filtered keys via review.py
    import yaml
    ds = api.launcher.datasets.get("r1")
    rk = yaml.safe_load(ds.reviewed.read_text())
    assert rk["metadata"]["reviewer"] == "Manasi"
    sk = yaml.safe_load(ds.scored.read_text())
    assert len(sk["items"]) == 3 and {i["status"] for i in sk["items"]} == {"accepted", "edited"}
    # persistence across a launcher restart
    api2 = env["make"]()
    _, d2 = api2.call("GET", "/api/runs/r1/dataset")
    assert d2["state"] == "ready" and d2["counts"]["approved"] == 3 and d2["counts"]["by_status"]["rejected"] == 1
    # rebuild keeps decisions (ids are stable)
    assert api2.call("POST", "/api/runs/r1/dataset", {})[0] == 202
    assert wait_ready(api2)["counts"]["approved"] == 3


def test_review_validation(env):
    api, d = built(env)
    P = "/api/runs/r1/dataset/review"
    assert api.call("POST", P, {})[0] == 400
    assert api.call("POST", P, {"approve": "everything"})[0] == 400   # "all" is valid (Approve all)
    assert api.call("POST", P, {"decisions": [{"id": "BR-zzzzzz", "action": "approve"}]})[0] == 400
    assert api.call("POST", P, {"decisions": [{"id": "../x", "action": "approve"}]})[0] == 400
    _, items = api.call("GET", "/api/runs/r1/dataset/items")
    iid = items["items"][0]["id"]
    assert api.call("POST", P, {"decisions": [{"id": iid, "action": "delete"}]})[0] == 400
    assert api.call("POST", P, {"decisions": [{"id": iid, "action": "edit"}]})[0] == 400
    assert api.call("POST", P, {"decisions": [{"id": iid, "action": "approve", "statement": "x"}]})[0] == 400
    assert api.call("POST", P, {"approve": "corroborated", "reviewer": "x" * 81})[0] == 400
    assert api.call("POST", P, {"approve": "corroborated", "session_token": "x"})[0] == 400
    _mk_run(env["runs"], "r2", {"type": "path", "path": str(env["src"])})
    api2 = env["make"]()
    assert api2.call("POST", "/api/runs/r2/dataset/review", {"approve": "corroborated"})[0] == 409


def test_excerpt_path_traversal(env, tmp_path):
    api, d = built(env)
    (tmp_path / "secret.txt").write_text("TOPSECRET\n")
    root = str(env["src"])
    for bad in ("../secret.txt", "/etc/passwd", "src/../../secret.txt", "..\\secret.txt", "a\x00b"):
        assert LD.excerpt(root, {"file": bad, "start_line": 1, "end_line": 1}) is None, bad
    link = env["src"] / "link.txt"
    link.symlink_to(tmp_path / "secret.txt")
    assert LD.excerpt(root, {"file": "link.txt", "start_line": 1, "end_line": 1}) is None
    # a key whose location points outside the source never leaks file content
    import yaml
    ds = api.launcher.datasets.get("r1")
    k = yaml.safe_load(ds.draft.read_text())
    k["items"][0]["locations"] = [{"file": "../secret.txt", "start_line": 1, "end_line": 1}]
    ds.draft.write_text(yaml.safe_dump(k))
    _, items = api.call("GET", "/api/runs/r1/dataset/items?status=")
    assert "TOPSECRET" not in json.dumps(items)


# ── evaluation integration ──────────────────────────────────────────────────────────────────
def test_options_and_full_eval_plan_use_dataset(env):
    api, d = built(env)
    _, o = api.call("GET", "/api/eval/options")
    assert o["dataset"] is None and all(k["id"] != "dataset" for k in o["answer_keys"])
    _, o = api.call("GET", "/api/eval/options?run=r1")
    assert o["dataset"]["provisional"] is True and o["answer_keys"][0]["id"] == "dataset"
    assert api.call("GET", "/api/eval/options?run=nope")[0] == 404
    # provisional: only a draft exists
    st, j = api.call("POST", "/api/runs/r1/eval", {"plan_only": True, "budget_usd": 5})
    assert st == 200, j
    assert j["suite"] == "dataset" and j["provisional"] is True
    assert j["key_label"].startswith("Provisional — test data set not yet reviewed (0 of 32")
    cfg = Path(j["config"])
    argv = api.argv()[-1]
    assert argv[argv.index("--config") + 1] == str(cfg) and "--plan" in argv
    import yaml
    c = yaml.safe_load(cfg.read_text())
    assert c["judge_runs"] == 1 and c["vars"]["key"].endswith("ANSWER_KEY.draft.yaml")
    assert c["vars"]["corpus"] == str(env["src"].resolve())
    assert {s["evaluator"] for s in c["stages"]} == {"sanity", "score"}
    _, ev = api.call("GET", "/api/runs/r1/eval")
    assert ev["provisional"].startswith("Provisional") and ev["estimated"]["provisional"] == ev["provisional"]
    # reviewed: approve corroborated -> the filtered key
    api.call("POST", "/api/runs/r1/dataset/review", {"approve": "corroborated"})
    st, j = api.call("POST", "/api/runs/r1/eval", {"plan_only": True, "budget_usd": 5})
    assert j["provisional"] is False and j["key_label"].startswith("Using this run's test data set (2 approved")
    c = yaml.safe_load(Path(j["config"]).read_text())
    assert c["vars"]["key"].endswith("/ANSWER_KEY.yaml")
    _, ev = api.call("GET", "/api/runs/r1/eval")
    assert "provisional" not in ev and ev["dataset"]["approved"] == 2
    # explicit id works; mixing it with another suite does not
    assert api.call("POST", "/api/runs/r1/eval", {"plan_only": True, "budget_usd": 5, "answer_key": "dataset"})[0] == 200
    assert api.call("POST", "/api/runs/r1/eval", {"plan_only": True, "budget_usd": 5, "answer_key": "dataset",
                                                  "suite": "ap-invoices"})[0] == 400


def test_quick_check_unaffected(env):
    api, d = built(env)
    st, j = api.call("POST", "/api/runs/r1/eval", {"smoke": True, "suite": "ap-invoices"})
    assert st == 202 and j["suite"] == "ap-invoices" and not j["provisional"] and j["config"] is None
    # without a suite the Quick check falls back to the generic deterministic suite, not the data set
    end = time.time() + 20
    while api.call("GET", "/api/runs/r1/eval/status")[1]["state"] in LS.EVAL_ACTIVE and time.time() < end:
        time.sleep(0.1)
    st, j = api.call("POST", "/api/runs/r1/eval", {"smoke": True})
    assert st == 202 and j["suite"] == "generic" and not j["provisional"]


def test_no_dataset_no_key_message_unchanged(env):
    api = env["make"]()
    st, d = api.call("POST", "/api/runs/r1/eval", {"plan_only": True, "budget_usd": 5})
    assert st == 400 and "no suite/answer key" in d["error"]


# ── imported runs, guards ───────────────────────────────────────────────────────────────────
def test_imported_run_read_only(env):
    imp = env["imp"]
    before = _snapshot(imp)
    api = env["make"]()
    rid = next(r for r in api.launcher.runs if r.startswith("imported-"))
    st, d = api.call("POST", f"/api/runs/{rid}/dataset", {})
    assert st == 409 and d["reason"] == "source_unavailable"          # /work/demo/src is not here
    # with its recorded source present, the build works and writes only under _datasets
    rj = json.loads((imp / "run.json").read_text())
    rj["env_snapshot"]["DISTILL_TARGET"] = str(env["src"])
    (imp / "run.json").write_text(json.dumps(rj))
    before = _snapshot(imp)
    st, d = api.call("POST", f"/api/runs/{rid}/dataset", {})
    assert st == 202, d
    assert wait_ready(api, rid)["state"] == "ready"
    api.call("POST", f"/api/runs/{rid}/dataset/review", {"approve": "corroborated"})
    assert _snapshot(imp) == before
    assert (env["runs"] / "_datasets" / rid).is_dir()


def test_host_origin_and_gate(env):
    api, d = built(env)
    for path in ("/api/runs/r1/dataset", "/api/runs/r1/dataset/status", "/api/runs/r1/dataset/items"):
        assert api.call("GET", path, headers={"Host": "evil.example"})[0] == 403
    for path, body in (("/api/runs/r1/dataset", {}), ("/api/runs/r1/dataset/review", {"approve": "corroborated"})):
        assert api.call("POST", path, body, headers={"Host": "evil.example"})[0] == 403
        assert api.call("POST", path, body, headers={"Origin": "http://evil.example"})[0] == 403
    gated = env["make"](access_code="letmein")
    for path in ("/api/runs/r1/dataset", "/api/runs/r1/dataset/status", "/api/runs/r1/dataset/items"):
        assert gated.call("GET", path)[0] == 401
    assert gated.call("POST", "/api/runs/r1/dataset", {})[0] == 401
    assert gated.call("POST", "/api/runs/r1/dataset/review", {"approve": "corroborated"})[0] == 401
    assert gated.call("GET", "/api/eval/options?run=r1")[0] == 401
    # the embedded viewer's folded per-run form reaches the same route
    assert api.call("GET", "/api/runs/r1/api/dataset/status")[1]["state"] == "ready"


# ── UI ──────────────────────────────────────────────────────────────────────────────────────
def test_ui_has_dataset_section_and_scripts_parse(tmp_path):
    html = (HERE.parent / "launcher_ui" / "index.html").read_text()
    # the data set is Step 1 ("Answer key") of the guided "Score this spec" flow
    for s in ("Build answer key", "Approve all ${n} rules", "Approve only the ${m} confirmed by both code and tests",
              "Review individually", "Approve all ${n} proposed rules? You can still reject individual ones later.",
              "not from the generated spec", "AI suggestions", "reviewed</b>",
              "/dataset/review", "/dataset/items", 'approve: act === "approve-all-yes" ? "all" : "corroborated"',
              "setInterval(refresh, 10000)", "Cost breakdown"):
        assert s in html, s
    assert 'class="eval-panel ds-panel"' not in html and "Build test data set — free" not in html
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    import re
    for i, js in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        p = tmp_path / f"s{i}.js"
        p.write_text(js)
        r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


# ── a real code base (eShopOnWeb), when it is checked out ──────────────────────────────────
@pytest.mark.skipif(not (ESHOP / "src" / "ApplicationCore").is_dir(), reason="/tmp/eshop not checked out")
def test_eshop_application_core(env):
    src = env["tmp"] / "eshop-core"
    shutil.copytree(ESHOP / "src" / "ApplicationCore", src / "src" / "ApplicationCore")
    if (ESHOP / "tests" / "UnitTests").is_dir():
        shutil.copytree(ESHOP / "tests" / "UnitTests", src / "tests" / "UnitTests")
    _mk_run(env["runs"], "eshop", {"type": "path", "path": str(src)})
    api = env["make"]()
    assert api.call("POST", "/api/runs/eshop/dataset", {})[0] == 202
    d = wait_ready(api, "eshop", timeout=120)
    assert d["state"] == "ready" and d["counts"]["total"] > 20 and d["counts"]["corroborated"] >= 1
    _, r = api.call("POST", "/api/runs/eshop/dataset/review", {"approve": "corroborated"})
    assert r["counts"]["approved"] == d["counts"]["corroborated"]
