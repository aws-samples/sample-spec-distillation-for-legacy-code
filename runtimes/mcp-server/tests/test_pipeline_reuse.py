# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""End-to-end run_distillation.sh tests with the LLM stages stubbed (no AWS, no models).

Covers:
  - stale reuse (P0 #2): change one file, delete one, add one -> pages + every downstream stage
    recompute and the pages hold the NEW source; an unchanged re-run recomputes nothing.
  - fail closed: refusal output halts at the barrier; a viewer failure fails the run with no
    "Distillation complete" banner; marker-only pages halt before any Map-Reduce spend.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_stubs  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
STAGES = ["map:rule", "map:data", "knowledge:rule", "knowledge:data", "synthesis", "format",
          "code_graph", "arbitrate", "concept_symbols", "issues", "reconcile", "master_blend", "bottling"]
ALWAYS = {"normalize", "viewer"}  # cheap deterministic steps that run every time by design


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def rig(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", "output", ".backup", "tests"))
    pipeline_stubs.install(repo)
    # fake venv: activate is a no-op, python is this interpreter
    vbin = repo / "runtimes" / "mcp-server" / ".venv" / "bin"
    vbin.mkdir(parents=True)
    (vbin / "activate").write_text("")
    # exec wrappers, not symlinks: a symlinked venv interpreter would lose its site-packages
    _exe(vbin / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    # fake aws CLI (check_creds) + python3 on PATH = this interpreter
    tbin = tmp_path / "bin"
    tbin.mkdir()
    _exe(tbin / "aws", "#!/bin/sh\nexit 0\n")
    _exe(tbin / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    src = tmp_path / "src"
    src.mkdir()
    # `DataProgram` is the symbol pipeline_stubs' code_graph/concept_symbols stubs join on. It has
    # to be REALLY in the source: export_graph now verifies that a bridge link's identifier occurs
    # in the file it cites and drops the link when it does not, so a stub whose symbol was nowhere
    # in the corpus would (correctly) produce zero links.
    (src / "a.py").write_text("def a():\n    # DataProgram entry point\n    return 1\n")
    (src / "b.py").write_text("def b():\n    return 2\n")
    (src / "logo.png").write_bytes(b"\x89PNG")
    log = tmp_path / "calls.log"

    def run(**env_over):
        log.write_text("")
        env = dict(os.environ)
        env.update({"PATH": f"{tbin}:{env['PATH']}", "DISTILL_TARGET": str(src),
                    "DISTILL_OUTPUT": str(tmp_path / "out"), "DISTILL_STILLS": "rule data",
                    "STUB_LOG": str(log), "ORCHESTRATION_STRATEGY": "page-centric",
                    "PLATE_STRATEGY": "single-plate"})
        env.pop("AWS_PROFILE", None)
        env.update(env_over)
        p = subprocess.run(["bash", str(repo / "run_distillation.sh")], env=env,
                           capture_output=True, text=True, timeout=600)
        calls = [c for c in log.read_text().splitlines() if c]
        return p.returncode, p.stdout + p.stderr, calls

    return {"run": run, "src": src, "out": tmp_path / "out", "repo": repo}


def _pages_text(out: Path) -> str:
    return "".join(p.read_text() for p in sorted((out / ".pages").glob("page_*/concat.txt")))


def test_unchanged_rerun_recomputes_nothing_and_changes_recompute(rig):
    rc, out, calls = rig["run"]()
    assert rc == 0, out[-4000:]
    assert "Distillation complete." in out
    for s in STAGES:
        assert s in calls, (s, calls)
    status = json.loads((rig["out"] / "run_status.json").read_text())
    assert status["status"] == "complete"
    # graph artifacts at the documented path
    idx = json.loads((rig["out"] / "knowledge_graph" / "index.json").read_text())
    assert idx["artifacts"]["code_graph"]["source_files"] == 1
    assert idx["artifacts"]["concept_code_bridge"]["links"] >= 1
    assert (rig["out"] / "knowledge_graph" / "knowledge_graph.json").is_file()
    # explicit exclusion log
    excl = (rig["out"] / ".enumeration" / "excluded.tsv").read_text()
    assert "logo.png\tbinary image/font asset" in excl

    # unchanged re-run: nothing recomputes
    rc, out, calls = rig["run"]()
    assert rc == 0, out[-4000:]
    assert set(calls) <= ALWAYS, calls
    assert "Using existing enumeration (source identity" in out
    assert "Reusing 1 existing pages" in out

    # change one, delete one, add one
    (rig["src"] / "a.py").write_text("def a():\n    return 42\n")
    (rig["src"] / "b.py").unlink()
    (rig["src"] / "c.py").write_text("def c():\n    return 3\n")
    rc, out, calls = rig["run"]()
    assert rc == 0, out[-4000:]
    assert "Source identity changed" in out
    assert "changed      1: a.py" in out and "deleted      1: b.py" in out and "added        1: c.py" in out
    for s in STAGES:
        assert s in calls, (s, calls)
    pages = _pages_text(rig["out"])
    assert "return 42" in pages and "return 1\n" not in pages
    assert "c.py" in pages and "b.py" not in pages
    verdict = json.loads((rig["out"] / ".identity_verdict.json").read_text())
    assert verdict["state"] in ("source_mismatch", "palm_mismatch")

    # and once more unchanged: nothing recomputes
    rc, out, calls = rig["run"]()
    assert rc == 0 and set(calls) <= ALWAYS, (calls, out[-3000:])


def test_downstream_script_change_recomputes_only_downstream(rig):
    rc, out, _ = rig["run"]()
    assert rc == 0, out[-4000:]
    # a downstream script edit invalidates that stage and everything after it, nothing before
    repo_blend = rig["repo"] / "runtimes" / "mcp-server" / "master_blend.py"
    repo_blend.write_text(repo_blend.read_text() + "\n# edited\n")
    rc, out, calls = rig["run"]()
    assert rc == 0, out[-4000:]
    assert set(calls) - ALWAYS == {"master_blend", "bottling"}, calls
    # a stage rebuilt for any reason (here: its stamp removed) cascades to everything after it
    (rig["out"] / ".stage_identity" / "reconcile.id").unlink()
    rc, out, calls = rig["run"]()
    assert rc == 0, out[-4000:]
    assert set(calls) - ALWAYS == {"reconcile", "master_blend", "bottling"}, calls


def test_refusal_fails_barrier(rig):
    rc, out, calls = rig["run"](STUB_MAP_MODE="refusal")
    assert rc == 4, out[-4000:]
    assert "FAILED barrier: refusal" in out
    assert "Distillation complete" not in out
    assert "knowledge:rule" not in calls
    # refused output moved aside so a re-run recomputes instead of resuming from it
    assert not (rig["out"] / "stills" / "rule" / "rule_raw_concat.md").exists()
    rc, out, calls = rig["run"]()
    assert rc == 0, out[-4000:]
    assert "map:rule" in calls and "map:data" in calls


def test_required_viewer_failure_fails_run(rig):
    rc, out, _ = rig["run"](STUB_VIEWER_FAIL="1")
    assert rc == 5, out[-4000:]
    assert "Distillation complete" not in out
    assert "Distillation FAILED" in out and "6 viewer" in out
    assert json.loads((rig["out"] / "run_status.json").read_text())["status"] == "failed"


def test_code_graph_failure_fails_run_and_can_be_disabled(rig):
    rc, out, _ = rig["run"](STUB_CG_FAIL="1")
    assert rc == 5 and "2.8 code graph" in out, out[-4000:]
    assert "Distillation complete" not in out
    # once the code graph succeeds, everything built WITHOUT it recomputes; upstream does not
    rc, out, calls = rig["run"]()
    assert rc == 0, out[-4000:]
    assert set(calls) - ALWAYS == {"code_graph", "arbitrate", "crosscheck", "concept_symbols",
                                   "symbol_backmap", "backmap_residue", "issues", "reconcile",
                                   "master_blend", "bottling"}, calls
    rc, out, calls = rig["run"](STUB_CG_FAIL="1", DISTILL_CODE_GRAPH="0")
    assert rc == 0, out[-4000:]
    assert "code_graph" not in calls


def test_marker_only_pages_halt_before_spend(rig):
    for f in rig["src"].iterdir():
        f.unlink()
    (rig["src"] / "blob.weird").write_bytes(b"\x00\x01\x02binary\x00")
    rc, out, calls = rig["run"]()
    assert rc == 3, out[-4000:]
    assert "no page carries source content" in out
    assert not any(c.startswith("map:") for c in calls)
    report = json.loads((rig["out"] / "preflight_report.json").read_text())
    assert report["verdict"] == "CRITICAL"
    assert "safe to run" not in report["verdict_meaning"]


def test_empty_issues_report_fails_run(rig):
    rc, out, calls = rig["run"](STUB_ISSUES_EMPTY="1")
    assert rc == 5, out[-4000:]
    assert "2.98 issues & insights" in out and "Distillation complete" not in out
    status = json.loads((rig["out"] / "run_status.json").read_text())
    assert status["status"] == "failed" and "2.98 issues & insights" in status["failed_stages"]
    # fixed on re-run: only the issues stage (plus always-run steps) recomputes
    rc, out, calls = rig["run"]()
    assert rc == 0, out[-4000:]
    assert set(calls) - ALWAYS == {"issues"}, calls
