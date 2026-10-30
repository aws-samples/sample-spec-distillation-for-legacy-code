# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""launcher_server.py: the JSON API over run_distillation.sh, against a repo copy whose LLM stages
are stubbed (pipeline_stubs; no AWS, no model call)."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher_server as LS  # noqa: E402
import pipeline_stubs  # noqa: E402
from gitfixture import Remote  # noqa: E402
from test_pipeline_reuse import REPO, _exe  # noqa: E402


@pytest.fixture(scope="module")
def stub_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("launcher")
    repo = root / "repo"
    shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", "output", ".backup", "tests"))
    pipeline_stubs.install(repo)
    vbin = repo / "runtimes" / "mcp-server" / ".venv" / "bin"
    vbin.mkdir(parents=True)
    (vbin / "activate").write_text("")
    _exe(vbin / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    tbin = root / "bin"
    tbin.mkdir()
    _exe(tbin / "aws", "#!/bin/sh\nexit 0\n")
    _exe(tbin / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    src = root / "src"
    src.mkdir()
    (src / "a.py").write_text("def a():\n    # DataProgram entry point\n    return 1\n")
    (src / "b.py").write_text("def b():\n    return 2\n")
    return {"repo": repo, "bin": tbin, "src": src, "root": root}


class Api:
    def __init__(self, base: str):
        self.base = base
        self.cookie = None            # "name=value" retained from any Set-Cookie, resent on later calls

    def _store_cookie(self, headers):
        # Retain the first morsel (name=value) of any Set-Cookie; additive and off until one is seen.
        raw = headers.get("Set-Cookie")
        if raw:
            self.cookie = raw.split(";", 1)[0].strip()

    def call(self, method: str, path: str, body=None, headers=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        if self.cookie and "Cookie" not in h:
            h["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                self._store_cookie(r.headers)
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            self._store_cookie(e.headers)
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw

    def wait(self, run_id: str, states: set[str], timeout: float = 180):
        end = time.time() + timeout
        while time.time() < end:
            _, r = self.call("GET", f"/api/runs/{run_id}")
            if r["status"] in states:
                return r
            time.sleep(0.3)
        raise AssertionError(f"run {run_id} never reached {states}: {r}")


@pytest.fixture()
def make_api(stub_repo, tmp_path):
    servers = []

    def make(max_concurrent: int = 1, *, access_code=None, allowed_hosts_extra=None,
             session_ttl: int = 43200, cors_origins=None, **env_over):
        env = dict(os.environ)
        env.pop("AWS_PROFILE", None)
        env.update({"PATH": f"{stub_repo['bin']}:{env['PATH']}", "STUB_LOG": str(tmp_path / "calls.log"),
                    "ORCHESTRATION_STRATEGY": "page-centric", "PLATE_STRATEGY": "single-plate",
                    "DISTILL_TARGET": "/should/be/ignored"})
        env.update(env_over)
        (tmp_path / "calls.log").write_text("")
        launcher = LS.Launcher(stub_repo["repo"] / "run_distillation.sh", tmp_path / f"runs{len(servers)}",
                               max_concurrent, base_env=env)
        srv = LS.make_server(launcher, "127.0.0.1", 0, cors_origins=cors_origins,
                             access_code=access_code, allowed_hosts_extra=allowed_hosts_extra,
                             session_ttl=session_ttl)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        api = Api(f"http://127.0.0.1:{srv.server_address[1]}")
        api.launcher = launcher
        return api

    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


def _body(stub_repo, **kw):
    b = {"source": {"type": "path", "path": str(stub_repo["src"])}, "name": "t1", "max_usd": 50,
         "stills": ["rule", "data"]}
    b.update(kw)
    return b


# ── validation ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("patch,msg", [
    ({"max_usd": -1}, "max_usd must be a number > 0"),
    ({"max_usd": 10_000}, "ceiling"),
    ({"name": "../x"}, "name must be"),
    ({"source": {"type": "svn", "url": "x"}}, "source.type"),
    ({"source": {"type": "path", "path": "relative/dir"}}, "absolute"),
    ({"source": {"type": "git", "url": "https://u:tok@example.com/r.git"}}, "credentials"),
    ({"source": {"type": "git", "url": "http://example.com/r.git"}}, "not allowed"),
    ({"source": {"type": "git", "url": "git@h:r.git", "ref": "--upload-pack=x"}}, "invalid git ref"),
    ({"source": {"type": "git", "url": "git@h:r.git", "subdir": "../.."}}, "invalid subdir"),
    ({"stills": ["nope"]}, "unknown still"),
    ({"facts": "yes"}, "facts must be true or false"),
    ({"extra": 1}, "unknown field"),
])
def test_validation_errors(make_api, stub_repo, patch, msg):
    api = make_api()
    body = _body(stub_repo)
    for k, v in patch.items():
        if v is None:
            body.pop(k)
        else:
            body[k] = v
    st, r = api.call("POST", "/api/runs", body)
    assert st == 400, r
    assert msg in r["error"], r
    assert api.call("GET", "/api/runs")[1]["runs"] == []        # nothing created, nothing run


@pytest.mark.parametrize("field", [{"aws_access_key_id": "AKIA"}, {"aws_secret_access_key": "x"},
                                   {"aws_session_token": "x"}, {"credentials": {"a": 1}},
                                   {"password": "x"}, {"token": "x"}])
def test_no_endpoint_accepts_credential_fields(make_api, stub_repo, field):
    api = make_api()
    body = _body(stub_repo, **field)
    st, r = api.call("POST", "/api/runs", body)
    assert st == 400 and "credential fields are not accepted" in r["error"], r
    body = _body(stub_repo)
    body["source"] = {**body["source"], **field}                  # nested too
    st, r = api.call("POST", "/api/runs", body)
    assert st == 400 and "credential fields are not accepted" in r["error"], r


def test_content_type_host_and_origin_guards(make_api, stub_repo):
    api = make_api()
    st, r = api.call("POST", "/api/runs", json.dumps(_body(stub_repo)).encode(),
                     headers={"Content-Type": "text/plain"})
    assert st == 415
    st, r = api.call("GET", "/api/runs", headers={"Host": "evil.example.com"})
    assert st == 403                                              # DNS-rebinding guard
    st, r = api.call("POST", "/api/runs", _body(stub_repo), headers={"Origin": "http://evil.example.com"})
    assert st == 403 and "cross-origin" in r["error"]


# ── estimate / launch / status ──────────────────────────────────────────────────────────────
def test_over_estimate_is_409_with_no_spend(make_api, stub_repo, tmp_path):
    api = make_api()
    st, r = api.call("POST", "/api/runs", _body(stub_repo, max_usd=0.05))
    assert st == 409, r
    assert r["status"] == "over_estimate" and r["estimate_usd"] > 0.05 and r["suggested_max_usd"] > 0
    _, run = api.call("GET", f"/api/runs/{r['run_id']}")
    assert run["status"] == "over_estimate" and run["spent_usd"] == 0
    assert (tmp_path / "calls.log").read_text() == ""             # no stage ran, no model call
    st_file = json.loads((api.launcher.runs_dir / r["run_id"] / "run_status.json").read_text())
    assert "DISTILL_ESTIMATE_ONLY" not in st_file["resume_command"]  # the suggested command really runs
    # allow_over_estimate starts anyway; the stage gates still stop it cleanly at the cap
    st, r = api.call("POST", "/api/runs", _body(stub_repo, max_usd=0.05, allow_over_estimate=True))
    assert st == 202, r
    run = api.wait(r["run_id"], LS.FINAL_STATES - {"estimated"})
    assert run["status"] == "budget_exhausted" and run["exit_code"] == 6 and run["resume_available"]


def test_launch_runs_to_complete_with_outputs_and_log(make_api, stub_repo):
    api = make_api()
    st, r = api.call("POST", "/api/runs", _body(stub_repo))
    assert st == 202, r
    assert r["status"] in ("queued", "running") and r["estimate_usd"] > 0
    run_id = r["run_id"]
    run = api.wait(run_id, LS.FINAL_STATES - {"estimated"})
    assert run["status"] == "complete", run
    assert run["exit_code"] == 0 and run["started"] and run["finished"] and run["phase"] == "done"
    assert run["spent_usd"] == 0 and run["source"] == {"type": "path", "path": str(stub_repo["src"].resolve())}
    for k in ("issues_md", "bottled_spec", "knowledge_graph_index", "viewer", "run_status"):
        assert k in run["links"], run["links"]
    st, body = api.call("GET", run["links"]["issues_md"])
    assert st == 200 and b"# issues" in body
    st, _ = api.call("GET", f"/runs/{run_id}/files/../../etc/passwd")
    assert st == 404
    st, _ = api.call("GET", f"/runs/{run_id}/files/.enumeration/candidates.txt")
    assert st == 404                                              # only deliverables are served
    st, log = api.call("GET", f"/api/runs/{run_id}/log?tail=5")
    assert st == 200 and len(log["lines"]) == 5 and any("Distillation complete" in ln or "viewer" in ln
                                                         for ln in api.call("GET", f"/api/runs/{run_id}/log?tail=400")[1]["lines"])
    out_dir = api.launcher.runs_dir / run_id
    assert (out_dir / "run_status.json").is_file() and (out_dir / "run.log").is_file()
    st, runs = api.call("GET", "/api/runs")
    assert [x["run_id"] for x in runs["runs"]] == [run_id]
    st, r = api.call("GET", f"/api/runs/{run_id}/resume")
    assert st == 409


def test_estimate_only_then_start(make_api, stub_repo):
    api = make_api()
    st, r = api.call("POST", "/api/runs", _body(stub_repo, estimate_only=True))
    assert st == 200 and r["status"] == "estimated", r
    assert r["estimate"]["pages"] == 1 and r["estimate"]["stills"] == ["rule", "data"]
    st, r2 = api.call("POST", f"/api/runs/{r['run_id']}/start")
    assert st == 202, r2
    assert api.wait(r["run_id"], LS.FINAL_STATES - {"estimated"})["status"] == "complete"
    st, _ = api.call("POST", f"/api/runs/{r['run_id']}/start")
    assert st == 409


def test_max_usd_optional_omitted_means_no_cap(make_api, stub_repo):
    # Omitting max_usd estimates and starts without a ceiling: the run proceeds, reports max_usd
    # null, and DISTILL_MAX_USD is NOT set in the launched environment.
    api = make_api()
    body = _body(stub_repo)
    body.pop("max_usd")
    st, r = api.call("POST", "/api/runs", {**body, "estimate_only": True})
    assert st == 200 and r["status"] == "estimated", r
    assert r["max_usd"] is None and r["estimate_usd"] > 0        # estimate still runs, no cap shown
    run = api.launcher.runs[r["run_id"]]
    assert "max_usd" not in run.request
    assert "DISTILL_MAX_USD" not in api.launcher._env(run, estimate_only=False)
    st, r2 = api.call("POST", f"/api/runs/{r['run_id']}/start")
    assert st == 202, r2
    assert api.wait(r["run_id"], LS.FINAL_STATES - {"estimated"})["status"] == "complete"


def test_name_optional_derived_from_source(make_api, stub_repo, tmp_path):
    # Omitting name derives a NAME_RX-safe slug from the source; here a path source → basename.
    api = make_api()
    body = _body(stub_repo)
    body.pop("name")
    st, r = api.call("POST", "/api/runs", {**body, "estimate_only": True})
    assert st == 200, r
    run = api.launcher.runs[r["run_id"]]
    slug = run.request["name"]
    assert LS.NAME_RX.match(slug), slug
    assert slug == "src"                                         # os.path.basename of stub_repo["src"]
    assert r["run_id"].endswith(f"-{slug}-" + r["run_id"].rsplit("-", 1)[-1])
    # a git source derives from the repo name with .git stripped
    remote = Remote(tmp_path / "git")
    remote.commit({"mod/a.py": "def a():\n    # DataProgram\n    return 1\n"})
    assert LS._slug_from_source({"type": "git", "url": "https://github.com/dotnet-architecture/eShopOnWeb.git"}) == "eshoponweb"
    assert LS._slug_from_source({"type": "git", "url": "git@h:org/My_Repo.git/"}) == "my_repo"
    assert LS._slug_from_source({"type": "path", "path": "/"}) == "source"  # empty basename fallback


def test_queue_and_cancel(make_api, stub_repo, tmp_path):
    api = make_api(STUB_MAP_SLEEP_S="60")
    st, a = api.call("POST", "/api/runs", _body(stub_repo, name="first"))
    assert st == 202
    st, b = api.call("POST", "/api/runs", _body(stub_repo, name="second"))
    assert st == 202 and b["status"] == "queued"                  # one at a time by default
    api.wait(a["run_id"], {"running"})
    time.sleep(1.5)
    st, r = api.call("POST", f"/api/runs/{a['run_id']}/cancel")
    assert st == 200, r
    run = api.wait(a["run_id"], {"cancelled"}, timeout=30)
    assert run["status"] == "cancelled" and run["finished"]
    assert "Distillation complete" not in "\n".join(api.call("GET", f"/api/runs/{a['run_id']}/log?tail=500")[1]["lines"])
    # the process group is gone (no orphaned stub invoker)
    pid = api.launcher.runs[a["run_id"]].pid
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)
    # the queued run started once the slot freed; cancel it too
    api.wait(b["run_id"], {"running"}, timeout=30)
    assert api.call("POST", f"/api/runs/{b['run_id']}/cancel")[0] == 200
    assert api.wait(b["run_id"], {"cancelled"}, timeout=30)["status"] == "cancelled"
    assert api.call("POST", f"/api/runs/{b['run_id']}/cancel")[0] == 409


def test_budget_exhausted_exposes_resume(make_api, stub_repo):
    api = make_api(STUB_MAP_BUDGET="1")
    st, r = api.call("POST", "/api/runs", _body(stub_repo))
    assert st == 202, r
    run = api.wait(r["run_id"], LS.FINAL_STATES - {"estimated"})
    assert run["status"] == "budget_exhausted" and run["resume_available"] and run["phase"] == "2 map"
    st, rs = api.call("GET", f"/api/runs/{r['run_id']}/resume")
    assert st == 200 and "DISTILL_MAX_USD=" in rs["resume_command"] and "run_distillation.sh" in rs["resume_command"]
    assert "AWS_SECRET" not in rs["resume_command"]


def test_git_source_estimate_records_commit(make_api, stub_repo, tmp_path):
    remote = Remote(tmp_path / "git")
    sha = remote.commit({"mod/a.py": "def a():\n    # DataProgram\n    return 1\n", "other/x.txt": "x\n"})
    api = make_api()
    st, r = api.call("POST", "/api/runs", {"source": {"type": "git", "url": remote.url, "subdir": "mod"},
                                           "name": "g", "max_usd": 0.01, "stills": ["rule", "data"]})
    assert st == 409 and r["status"] == "over_estimate", r
    _, run = api.call("GET", f"/api/runs/{r['run_id']}")
    assert run["source"]["commit"] == sha and run["source"]["subdir"] == "mod" and run["spent_usd"] == 0
    st, r = api.call("POST", "/api/runs", {"source": {"type": "git", "url": remote.url, "ref": "nope"},
                                           "name": "g2", "max_usd": 1})
    assert st == 422 and "unknown ref 'nope'" in r["error"]


def test_form_page_and_config(make_api):
    api = make_api()
    st, html = api.call("GET", "/")
    assert st == 200 and b"Synthetic or approved non-production code only" in html and b"/api/runs" in html
    st, cfg = api.call("GET", "/api/config")
    assert st == 200 and "rule" in cfg["stills"] and cfg["max_concurrent"] == 1


# ── binding ─────────────────────────────────────────────────────────────────────────────────
def test_binds_loopback_by_default_and_refuses_non_loopback(tmp_path, stub_repo):
    launcher = LS.Launcher(stub_repo["repo"] / "run_distillation.sh", tmp_path / "runs", base_env={})
    srv = LS.make_server(launcher, port=0)
    assert srv.server_address[0] == "127.0.0.1"
    srv.server_close()
    for host in ("0.0.0.0", "192.168.1.10", "::", "example.com"):
        with pytest.raises(SystemExit, match="refusing to bind"):
            LS.make_server(launcher, host, 0)
    assert LS.is_loopback("localhost") and LS.is_loopback("::1") and LS.is_loopback("127.0.0.2")
    import io
    err = io.StringIO()
    LS.check_bind("0.0.0.0", True, err=err)                        # explicit flag → loud warning
    assert "WARNING" in err.getvalue() and "NON-LOOPBACK" in err.getvalue()
    # the CLI: default host is loopback; a non-loopback host without the flag exits non-zero
    script = Path(LS.__file__)
    p = subprocess.run([sys.executable, str(script), "--host", "0.0.0.0", "--port", "0",
                        "--runs-dir", str(tmp_path / "r2")], capture_output=True, text=True, timeout=30)
    assert p.returncode != 0 and "refusing to bind" in p.stderr and "Traceback" not in p.stderr
    assert not (tmp_path / "r2").exists()                          # refused before any side effect
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(script), "--port", str(port), "--runs-dir", str(tmp_path / "r3")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = proc.stdout.readline()
        assert f"http://127.0.0.1:{port}/" in line, line
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ── shared-access-code gate (Part 1 §1.12) ────────────────────────────────────────────────────
GATE = dict(access_code="s3cr3t", allowed_hosts_extra=["pub.example.com"])


def test_gate_html_challenge_on_root(make_api):
    # GET / with no cookie → 200 and the access-code page (not index.html), regardless of Accept (AC-3).
    api = make_api(**GATE)
    st, html = api.call("GET", "/")
    assert st == 200 and isinstance(html, bytes)
    assert b"Access code required" in html and b"/api/auth" in html


def test_gate_api_routes_401_without_cookie(make_api, stub_repo):
    # API routes with no cookie → 401 JSON and nothing created (AC-4).
    api = make_api(**GATE)
    st, r = api.call("GET", "/api/runs")
    assert st == 401 and r["error"] == "authentication required" and r["login"] == "/api/auth", r
    st, r = api.call("POST", "/api/runs", _body(stub_repo))
    assert st == 401 and r["login"] == "/api/auth", r
    st, r = api.call("GET", "/runs/some-run-id/files/issues.md")
    assert st == 401 and r["login"] == "/api/auth", r
    # the gate ran before any launcher work: no run exists. Authenticate, then confirm the list is empty.
    st, _ = api.call("POST", "/api/auth", {"code": "s3cr3t"})
    assert st == 200
    assert api.call("GET", "/api/runs")[1]["runs"] == []


def test_gate_non_html_route_401_without_accept(make_api):
    # GET /api/config with no cookie AND no Accept header → 401 JSON (not the HTML challenge page).
    api = make_api(**GATE)
    st, r = api.call("GET", "/api/config")
    assert st == 401 and r["error"] == "authentication required", r


def test_gate_auth_wrong_then_right_cookie_round_trip(make_api):
    # POST /api/auth wrong code → 401, no Set-Cookie; correct code → 200 + Set-Cookie; then the SAME
    # Api carries the cookie and reaches /api/runs (AC-5).
    api = make_api(**GATE)
    st, r = api.call("POST", "/api/auth", {"code": "nope"})
    assert st == 401 and r["error"] == "incorrect access code", r
    assert api.cookie is None                                      # no Set-Cookie on failure
    st, r = api.call("POST", "/api/auth", {"code": "s3cr3t"})
    assert st == 200 and r.get("ok") is True, r
    assert api.cookie and api.cookie.startswith("__Host-distill_session=")
    st, r = api.call("GET", "/api/runs")                           # same Api → cookie resent
    assert st == 200 and r["runs"] == []


def test_gate_healthz_on_off_and_unknown_host(make_api):
    # GET /healthz → 200 small body with the gate ON and OFF, no cookie, and with an unknown Host (AC-8).
    on = make_api(**GATE)
    st, r = on.call("GET", "/healthz")
    assert st == 200 and r == {"status": "ok"}
    st, r = on.call("GET", "/healthz", headers={"Host": "10.0.0.5"})   # unknown Host still 200
    assert st == 200 and r == {"status": "ok"}
    off = make_api()                                               # gate disabled
    st, r = off.call("GET", "/healthz")
    assert st == 200 and r == {"status": "ok"}


def test_gate_host_acceptance(make_api):
    # pub.example.com passes _host_ok (so the gate challenges rather than 403s); unknown host → 403 (AC-9).
    api = make_api(**GATE)
    st, html = api.call("GET", "/", headers={"Host": "pub.example.com"})
    assert st == 200 and b"Access code required" in html          # host allowed → gate challenge
    st, r = api.call("GET", "/api/runs", headers={"Host": "unknown.example.com"})
    assert st == 403, r


def test_gate_origin_acceptance(make_api, stub_repo):
    # Authenticated POST: Origin https://pub.example.com is accepted by _origin_ok (so it passes the
    # CSRF check and reaches the handler), while https://evil.example.com is refused 403 (AC-10). We
    # key on the origin verdict only — a trusted origin must NOT be rejected cross-origin, regardless
    # of what the downstream run then does — so the assertion is independent of pipeline execution.
    api = make_api(**GATE)
    assert api.call("POST", "/api/auth", {"code": "s3cr3t"})[0] == 200
    st, r = api.call("POST", "/api/runs", _body(stub_repo, max_usd=0.05),
                     headers={"Host": "pub.example.com", "Origin": "https://pub.example.com"})
    assert not (st == 403 and isinstance(r, dict) and "cross-origin" in r.get("error", "")), r
    st, r = api.call("POST", "/api/runs", _body(stub_repo),
                     headers={"Host": "pub.example.com", "Origin": "https://evil.example.com"})
    assert st == 403 and "cross-origin" in r["error"], r


def test_gate_access_code_as_run_field_still_screened(make_api, stub_repo):
    # An authenticated request that smuggles the access code as a /api/runs field is still 400 by the
    # existing credential/unknown-field screening (AC-7).
    api = make_api(**GATE)
    assert api.call("POST", "/api/auth", {"code": "s3cr3t"})[0] == 200
    st, r = api.call("POST", "/api/runs", _body(stub_repo, code="s3cr3t"))
    assert st == 400 and "unknown field" in r["error"], r


def test_gate_options_is_a_no_op(make_api):
    # OPTIONS preflight with the gate on and no cookie is handled by do_OPTIONS, not challenged by the
    # gate: 204 for an allowed cors origin, 403 otherwise — confirming _gate() is a no-op for OPTIONS.
    api = make_api(cors_origins=["http://cors.example.com"], **GATE)
    st, _ = api.call("OPTIONS", "/api/runs", headers={"Origin": "http://cors.example.com"})
    assert st == 204                                               # allowed cors origin → 204, never challenged
    st, _ = api.call("OPTIONS", "/api/runs", headers={"Origin": "https://evil.example.com"})
    assert st == 403                                               # not a cors origin → 403, never challenged


def test_bare_host_quirks():
    # The two load-bearing _bare_host quirks pinned by §1.5/§1.12.
    assert LS._bare_host("[::1]:8090") == "[::1]"                  # bracketed IPv6 keeps its brackets
    assert LS._bare_host("::1") == "::1"                           # unbracketed IPv6 left intact
