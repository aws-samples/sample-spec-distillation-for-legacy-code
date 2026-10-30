# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression tests for the edge cases fixed in docs/EDGE_CASES.md (launcher, source input,
evaluation view, launcher UI script). No network, no AWS, no model call."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import eval_view  # noqa: E402
import launcher_server as LS  # noqa: E402
import source_fetch as SF  # noqa: E402

UI = HERE.parent / "launcher_ui" / "index.html"


# ── source input ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,ref,subdir,want", [
    # GitLab nested groups without /-/: the whole path is the repository (was split as org/repo/<dir>)
    ("https://gitlab.com/g/sub/repo", None, None, ("https://gitlab.com/g/sub/repo", None, None)),
    ("https://gitlab.com/g/sub/repo/", None, None, ("https://gitlab.com/g/sub/repo.git", None, None)),
    ("https://gitlab.com/g/sub/repo.git", None, None, ("https://gitlab.com/g/sub/repo.git", None, None)),
    # query strings / fragments / trailing slashes never reach git (git appended /info/refs after ?…)
    ("https://github.com/o/r?tab=readme-ov-file", None, None, ("https://github.com/o/r.git", None, None)),
    ("https://github.com/o/r#readme", None, None, ("https://github.com/o/r.git", None, None)),
    ("https://github.com/o/r/", None, None, ("https://github.com/o/r.git", None, None)),
    ("https://github.com/o/r.git/", None, "src", ("https://github.com/o/r.git", None, "src")),
    ("https://gitlab.com/g/r/-/tree/main?ref_type=heads", None, None, ("https://gitlab.com/g/r.git", "main", None)),
    # a slash ref typed in the ref field is removed from the browse path
    ("https://github.com/o/r/tree/feature/x/src", "feature/x", None, ("https://github.com/o/r.git", "feature/x", "src")),
    ("https://github.com/o/r/blob/feature/x/src/a.cs", "feature/x", None, ("https://github.com/o/r.git", "feature/x", "src")),
    # without it, the first segment is the ref (documented limitation)
    ("https://github.com/o/r/tree/feature/x/src", None, None, ("https://github.com/o/r.git", "feature", "x/src")),
    # web pages of the repository are not folders; a commit page pins that commit
    ("https://github.com/o/r/pulls", None, None, ("https://github.com/o/r.git", None, None)),
    ("https://github.com/o/r/issues/12", None, None, ("https://github.com/o/r.git", None, None)),
    ("https://github.com/o/r/commit/abc1234", None, None, ("https://github.com/o/r.git", "abc1234", None)),
    ("https://gitlab.com/g/r/-/merge_requests/3", None, None, ("https://gitlab.com/g/r.git", None, None)),
])
def test_browse_url_edge_cases(url, ref, subdir, want):
    assert SF.normalize_browse_url(url, ref, subdir) == want


@pytest.mark.parametrize("url", ["https://github.com/o/re\npo/tree/main", "https://github.com/o/r/tree/ma\tin",
                                 "https://github.com/o/r/tree/main\r\n"[:-1] + "x\r"])
def test_control_characters_in_a_browse_url_are_refused_not_dropped(url):
    u, r, s = SF.normalize_browse_url(url)
    with pytest.raises(SF.FetchError, match="not allowed"):
        SF.classify_url(u)


def test_scp_url_with_a_password_is_refused_without_echoing_it():
    with pytest.raises(SF.FetchError) as e:
        SF.classify_url("bob:hunter3pw@host:org/r.git")
    assert "password" in str(e.value) and "hunter3pw" not in str(e.value)
    assert SF.classify_url("git@github.com:org/r.git")["display"] == "github.com:org/r.git"


def test_local_path_that_is_a_file_is_a_clear_error(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    with pytest.raises(SF.FetchError, match="is a file, not a git repository folder"):
        SF.classify_url(str(f))


def test_non_string_git_url_is_400_not_500():
    for bad in (123, ["x"], {"a": 1}):
        with pytest.raises(LS.ApiError) as e:
            LS.validate_request({"source": {"type": "git", "url": bad}}, [], 500.0)
        assert e.value.status == 400 and "source.url must be a string" in e.value.message


# ── launcher: HTTP edge cases ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def server(tmp_path):
    srvs = []

    def make(host="127.0.0.1", launcher=None):
        launcher = launcher or LS.Launcher(tmp_path / "no-pipeline" / "run_distillation.sh", tmp_path / "runs", 1,
                                           base_env={})
        srv = LS.make_server(launcher, host, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        srvs.append(srv)
        return srv, launcher

    yield make
    for s in srvs:
        s.shutdown()
        s.server_close()


def _raw(port: int, payload: bytes, family=socket.AF_INET, host="127.0.0.1") -> bytes:
    s = socket.socket(family, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((host, port))
    s.sendall(payload)
    data = b""
    try:
        while True:
            c = s.recv(65536)
            if not c:
                break
            data += c
    except (socket.timeout, ConnectionResetError):
        pass
    s.close()
    return data


@pytest.mark.parametrize("length", [b"-1", b"abc"])
def test_bad_content_length_is_400_promptly(server, length):
    srv, _ = server()
    port = srv.server_address[1]
    t0 = time.time()
    r = _raw(port, b"POST /api/runs HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
             b"Content-Length: " + length + b"\r\n\r\n{}")
    assert r.split(b"\r\n", 1)[0].endswith(b"400 Bad Request"), r[:80]
    assert b"Content-Length" in r and time.time() - t0 < 4      # no read-until-EOF hang


def test_ipv6_loopback_bind(server):
    try:                                        # the host itself must support IPv6 loopback
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe.bind(("::1", 0))
        probe.close()
    except OSError as e:                        # pragma: no cover - IPv6 disabled on the host
        pytest.skip(f"no IPv6 loopback: {e}")
    srv, _ = server("::1")                      # was socket.gaierror (AF_INET server)
    r = _raw(srv.server_address[1], b"GET /healthz HTTP/1.1\r\nHost: [::1]\r\nConnection: close\r\n\r\n",
             socket.AF_INET6, "::1")
    assert b" 200 " in r.split(b"\r\n", 1)[0] and b'"ok"' in r


def _fake_run(d: Path, state: str, **extra) -> None:
    d.mkdir(parents=True)
    (d / "launcher.json").write_text(json.dumps({"run_id": d.name, "state": state, "created": "2026-10-08T00:00:00+00:00",
                                                 "request": {"name": "x", "source": {"type": "path", "path": str(d)}},
                                                 **extra}))


def test_eval_is_refused_for_runs_without_output(tmp_path):
    runs = tmp_path / "runs"
    for rid, state in (("r-running", "complete"), ("r-cancelled", "cancelled"), ("r-failed", "failed"),
                       ("r-failed-viewer", "failed"), ("r-budget", "budget_exhausted"), ("r-ok", "complete")):
        _fake_run(runs / rid, state)
    (runs / "r-failed-viewer" / "viewer").mkdir()
    (runs / "r-failed-viewer" / "viewer" / "index.html").write_text("<html></html>")
    L = LS.Launcher(tmp_path / "no-pipeline" / "run_distillation.sh", runs, 1, base_env={})
    L.runs["r-running"].state = "running"
    for rid, frag in (("r-running", "becomes available when it completes"),
                      ("r-cancelled", "cancelled and produced no output"),
                      ("r-failed", "failed and produced no output"),
                      ("r-budget", "budget_exhausted and produced no output")):
        with pytest.raises(LS.ApiError) as e:
            L.eval_start(rid, {"smoke": True, "suite": "ap-invoices"})
        assert e.value.status == 409 and frag in e.value.message, (rid, e.value.message)
    assert L.eval_refusal(L.runs["r-failed-viewer"]) is None and L.eval_refusal(L.runs["r-ok"]) is None


def test_complete_run_without_a_phase_line_reports_done(tmp_path):
    runs = tmp_path / "runs"
    _fake_run(runs / "r1", "complete")
    L = LS.Launcher(tmp_path / "no-pipeline" / "run_distillation.sh", runs, 1, base_env={})
    assert L.describe(L.runs["r1"])["phase"] == "done"


def test_restart_stops_the_orphaned_pipeline_of_an_active_run(tmp_path):
    """A run's pipeline runs in its own session; when the launcher dies and restarts, the run is
    'interrupted' and must not keep running (and spending) unwatched."""
    script = tmp_path / "pipe" / "run_distillation.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/bash\nsleep 300 &\nwait\n")
    proc = subprocess.Popen(["bash", str(script)], start_new_session=True)
    try:
        runs = tmp_path / "runs"
        _fake_run(runs / "r1", "running", pid=proc.pid)
        L = LS.Launcher(script, runs, 1, base_env={})
        r = L.runs["r1"]
        assert r.state == "interrupted" and "process group was stopped" in r.error
        proc.wait(timeout=15)
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_restart_leaves_an_unrelated_process_alone(tmp_path):
    other = subprocess.Popen(["sleep", "30"], start_new_session=True)       # not the pipeline script
    try:
        runs = tmp_path / "runs"
        _fake_run(runs / "r1", "running", pid=other.pid)
        L = LS.Launcher(tmp_path / "pipe" / "run_distillation.sh", runs, 1, base_env={})
        assert L.runs["r1"].state == "interrupted" and "stopped" not in L.runs["r1"].error
        assert other.poll() is None
    finally:
        other.kill()


# ── evaluation view: a copied run keeps its own scores; another run's are still skipped ─────
def test_copied_run_directory_keeps_its_scores_and_other_runs_stay_skipped(tmp_path):
    def run_dir(d: Path, run_id: str):
        (d / "bottled_spec").mkdir(parents=True)
        (d / "bottled_spec" / "bottled_spec.md").write_text("spec")
        (d / "run.json").write_text(json.dumps({"run_id": run_id, "created_at": "2026-10-06T00:16:03Z"}))
        (d / "run_manifest.json").write_text(json.dumps({"run_identity": {"palm_print": "sha256:" + run_id}}))
    orig, other = tmp_path / "orig" / "ap", tmp_path / "other" / "ap2"
    run_dir(orig, "ap-1")
    run_dir(other, "ap-2")
    for d, spec_of in ((orig, orig), (other, other)):
        (d / "validation" / "bottled").mkdir(parents=True)
        (d / "validation" / "bottled" / "score.json").write_text(json.dumps({
            "spec": str(spec_of / "bottled_spec" / "bottled_spec.md"), "method": "llm",
            "recall": {"any_match_pct": 90.0}, "citations": {"validity_pct": 100.0}}))
    copy = tmp_path / "copy" / "ap"
    shutil.copytree(orig, copy)
    agg = eval_view.aggregate(copy, repo=tmp_path)
    assert [a["artifact"] for a in agg["exact"]["spec_artifacts"]] == ["bottled_spec"]   # was skipped
    # shared repo records keep strict path matching: a record of the original is not the copy's
    rec = tmp_path / "eval" / "runs" / "r1"
    rec.mkdir(parents=True)
    shutil.copy(orig / "validation" / "bottled" / "score.json", rec / "score.json")
    shutil.rmtree(copy / "validation")
    assert eval_view.aggregate(copy, repo=tmp_path)["exact"]["spec_artifacts"] == []
    assert eval_view.aggregate(orig, repo=tmp_path)["exact"]["spec_artifacts"]
    shutil.copytree(orig / "validation", copy / "validation")
    # a run whose validation/ holds the score of a DIFFERENT run (other run id) is still skipped
    shutil.copy(other / "validation" / "bottled" / "score.json", copy / "validation" / "bottled" / "score.json")
    agg = eval_view.aggregate(copy, repo=tmp_path)
    assert agg["exact"]["spec_artifacts"] == [] and agg["skipped_sources"]


# ── launcher UI script (node, fake DOM) ───────────────────────────────────────────────────────
NODE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[1], "utf8");
const src = html.split("<script>")[1].split("</script>")[0];
const el = () => new Proxy({ addEventListener() {}, querySelectorAll: () => [], querySelector: () => null,
  options: [], dataset: {}, style: {} }, { get: (t, k) => (k in t ? t[k] : (k === "hidden" ? false : "")), set: () => true });
const ctx = { document: { getElementById: el, querySelectorAll: () => [], querySelector: () => null,
  createElement: el, activeElement: null }, fetch: () => new Promise(() => {}), setInterval() {}, setTimeout,
  console, CSS: { escape: (s) => s }, confirm: () => true };
vm.createContext(ctx);
vm.runInContext(src.replace(/^"use strict";/, "") + `
;globalThis.__out = {
  steps: ["Phase 1.2", "Phase 1.3", "Phase 2.97", "Phase 2.98", "Phase 6.5", "pre-run estimate", "2 map", "done"].map(phaseToStep),
  be: resolveStepper({}, { status: "budget_exhausted", phase: "2 map" }).steps.map((s) => s.state),
  oe: resolveStepper({}, { status: "over_estimate", phase: "pre-run estimate" }).steps.map((s) => s.state),
  run: resolveStepper({}, { status: "running", phase: "Phase 2" }).steps.map((s) => s.state),
  canEval: [canEvaluate({ status: "running" }), canEvaluate({ status: "cancelled" }), canEvaluate({ status: "complete" }),
            canEvaluate({ status: "failed", links: { viewer: "v" } }), canEvaluate({ status: "failed", links: {} })],
};`, ctx);
console.log(JSON.stringify(ctx.__out));
"""


def test_launcher_ui_steps_and_stops():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    p = subprocess.run([node, "-e", NODE_HARNESS, str(UI)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    o = json.loads(p.stdout)
    assert o["steps"] == ["Prepare pages", "Prepare pages", "Build knowledge graph", "Build knowledge graph",
                          "Generate viewer", "Estimate cost", "Extract knowledge", "Generate viewer"]
    # a budget stop / over-estimate marks the step it stopped at, it is not shown as in progress
    assert o["be"][3] == "failed" and "current" not in o["be"]
    assert o["oe"][2] == "failed" and "current" not in o["oe"]
    assert o["run"][3] == "current"
    assert o["canEval"] == [False, False, True, True, False]


def test_launcher_ui_keeps_the_poll_and_cost_breakdown():
    html = UI.read_text()
    assert "setInterval(refresh, 10000)" in html and 'details class="cost-detail"' in html
