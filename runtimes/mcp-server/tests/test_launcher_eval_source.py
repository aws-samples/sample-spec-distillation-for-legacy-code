# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The evaluation is always pointed at the run's OWN source (never a path baked into a suite),
the Quick check works for any finished run (generic deterministic suite), and a subdir run is
scored only against key items inside its subdir. Uses the real eval/ci/run_suite.py in --smoke
mode (deterministic, no Bedrock) on synthetic fixtures."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import launcher_server as LS  # noqa: E402

yaml = pytest.importorskip("yaml")

REPO = HERE.parent.parent.parent
FIXTURE = REPO / "eval" / "keygen" / "tests" / "fixtures" / "mini-cs"
RUN_SUITE = REPO / "eval" / "ci" / "run_suite.py"


def _git_run(runs: Path, rid: str, subdir: str, name: str) -> Path:
    d = runs / rid
    (d / ".source").mkdir(parents=True)
    shutil.copytree(FIXTURE, d / ".source" / subdir)
    (d / "launcher.json").write_text(json.dumps({"run_id": rid, "state": "complete", "request": {
        "name": name, "source": {"type": "git", "url": "https://example.com/x.git", "subdir": subdir,
                                 "display_url": "https://example.com/x.git"}}}))
    (d / "source_provenance.json").write_text(json.dumps({"target": str(d / ".source" / subdir),
                                                          "subdir": subdir}))
    (d / "bottled_spec").mkdir()
    (d / "bottled_spec" / "bottled_spec.md").write_text(
        "# Spec\n\n## BR-1 Orders need a buyer\nAn order needs a buyer id (src/Domain/Order.cs:10-12).\n")
    return d


def _wait(L, rid, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        j = L.evals.get(rid)
        if j is not None and j.state not in LS.EVAL_ACTIVE:
            return j
        time.sleep(0.2)
    raise AssertionError("evaluation never finished")


@pytest.fixture()
def launcher(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    return runs, (lambda: LS.Launcher(tmp_path / "no.sh", runs, 1, eval_cmd=[sys.executable, str(RUN_SUITE)]))


def test_smoke_on_git_subdir_run_uses_its_own_source(launcher):
    runs, make = launcher
    d = _git_run(runs, "eshoponweb-1", "app", "eshoponweb")      # matches the eshoponweb suite by name
    L = make()
    st, j = L.eval_start("eshoponweb-1", {"smoke": True})
    assert st == 202 and j["suite"] == "eshoponweb"
    assert j["source_root"] == str(d / ".source" / "app") and j["repo_root"] == str(d / ".source")
    j = _wait(L, "eshoponweb-1")
    rep = json.loads((j.out_dir / "suite" / "suite.json").read_text())
    by = {s["id"]: s for s in rep["stages"]}
    assert by["corpus-sanity"]["status"] == "ok", by["corpus-sanity"]
    assert "/tmp/eshop" not in json.dumps(rep["stages"])          # nothing points at a fixed clone
    argv = (j.out_dir / "eval.log").read_text()
    assert f"source_root={d / '.source' / 'app'}" in argv and f"repo_root={d / '.source'}" in argv
    # the repo-wide eShopOnWeb key is re-based onto the subdir: every item lies outside "app/"
    assert j.key_alignment["subdir"] == "app" and j.key_alignment["kept"] == 0
    assert j.key_alignment["excluded"] > 200


def test_key_alignment_keeps_items_inside_subdir(launcher, tmp_path):
    runs, make = launcher
    d = _git_run(runs, "eshoponweb-2", "src/ApplicationCore", "eshoponweb")
    L = make()
    job = LS.EvalJob("e1", "eshoponweb-2", tmp_path / "ev", "smoke", "eshoponweb", "eshoponweb", 1, 0.0)
    job.source_root, job.repo_root = L.eval_source(L.get("eshoponweb-2"))
    out = L._align_key(job, L.get("eshoponweb-2"))
    k = yaml.safe_load(Path(out).read_text())
    full = yaml.safe_load((REPO / "eval" / "keys" / "eshoponweb" / "ANSWER_KEY.draft.yaml").read_text())
    inside = [i for i in full["items"] if any(l["file"].startswith("src/ApplicationCore/") for l in i["locations"])]
    assert len(k["items"]) == len(inside) > 0
    assert all(not l["file"].startswith("src/") for i in k["items"] for l in i["locations"])
    assert job.key_alignment["excluded"] == len(full["items"]) - len(inside)


def test_smoke_with_no_matching_suite_uses_generic(launcher):
    runs, make = launcher
    _git_run(runs, "something-else", "app", "something-else")
    L = make()
    assert L._infer_suite(L.get("something-else")) is None
    st, j = L.eval_start("something-else", {"smoke": True})
    assert st == 202 and j["suite"] == "generic" and j["answer_key"] is None
    j = _wait(L, "something-else")
    assert j.state == "complete", (j.state, j.error)
    rep = json.loads((j.out_dir / "suite" / "suite.json").read_text())
    by = {s["id"]: s for s in rep["stages"]}
    assert by["corpus-sanity"]["status"] == "ok"
    assert by["score-bottled"]["status"] == "ok"                 # citation validity etc., no key
    # Full evaluation without a key keeps the clear message; generic is never a Full suite
    with pytest.raises(LS.ApiError) as e:
        L.eval_start("something-else", {"plan_only": True, "budget_usd": 5})
    assert "no suite/answer key" in e.value.message
    with pytest.raises(LS.ApiError) as e:
        L.eval_start("something-else", {"plan_only": True, "budget_usd": 5, "suite": "generic"})
    assert "test data set" in e.value.message
    assert "generic" not in {k["id"] for k in L.eval_options()["answer_keys"]}


def test_eshoponweb_suite_reads_vars(tmp_path):
    sys.path.insert(0, str(REPO / "eval" / "ci"))
    import suite_lib
    cfg = suite_lib.load_config(REPO / "eval" / "ci" / "suites" / "eshoponweb.yaml",
                                {"source_root": str(tmp_path / "x"), "run": str(tmp_path / "r")})
    assert cfg["corpora"]["eshoponweb"]["source_root"] == str(tmp_path / "x")
    assert next(s for s in cfg["stages"] if s["id"] == "corpus-sanity")["path"] == str(tmp_path / "x")
    assert next(s for s in cfg["stages"] if s["id"] == "mutate-eshop")["corpus_path"] == str(tmp_path / "x")
    # defaults are unchanged for a manual CLI run, and the AP/JC suites still find their corpora
    assert suite_lib.load_config(REPO / "eval" / "ci" / "suites" / "eshoponweb.yaml")["vars"]["corpus"] == "/tmp/eshop"
    for sid, corpus in (("ap-invoices", "synthetic-corpus/ap-invoices"), ("jc-changeorders", "synthetic-corpus/jc-changeorders")):
        c = suite_lib.load_config(REPO / "eval" / "ci" / "suites" / f"{sid}.yaml")
        assert c["corpora"][sid]["source_root"] == corpus
    g = suite_lib.load_config(REPO / "eval" / "ci" / "suites" / "generic.yaml", {"source_root": "/s", "run": "/r"})
    assert g["corpora"]["generic"]["source_root"] == "/s"


def test_imported_run_without_source_keeps_suite_defaults(tmp_path):
    imp = tmp_path / "ap-fixture-run"
    shutil.copytree(HERE / "fixtures" / "eval" / "ap-fixture-run", imp)
    L = LS.Launcher(tmp_path / "no.sh", tmp_path / "runs", 1, import_runs=[imp],
                    eval_cmd=[sys.executable, str(HERE / "eval_suite_stub.py")])
    rid = next(iter(L.runs))
    assert L.eval_source(L.get(rid)) == (None, None)               # /work/demo/src is not here
    job = LS.EvalJob("e", rid, tmp_path / "e", "smoke", "ap-invoices", "ap-invoices", 1, 0.0)
    argv = L._eval_argv(job, L.get(rid), plan=False)
    assert not any(a.startswith("source_root=") for a in argv)
