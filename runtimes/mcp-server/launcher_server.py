#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""launcher_server.py: a small local JSON API (plus one HTML form) to launch and monitor
run_distillation.sh runs. API reference: docs/LAUNCHER_API.md.

Usage:
    python3 launcher_server.py [--port 8090] [--max-concurrent 1] [--runs-dir output/runs]

  GET  /                          the form + runs table (launcher_ui/index.html)
  POST /api/runs                  validate, estimate (no model call), then queue the run
                                  (body "estimate_only": true → estimate only; start it later)
  POST /api/runs/<id>/start       start a run that was estimated with estimate_only
  GET  /api/runs                  all runs (newest first)
  GET  /api/runs/<id>             one run: status, phase, spend, estimate, times, output links
  GET  /api/runs/<id>/log?tail=N  last N lines of run.log
  POST /api/runs/<id>/cancel      stop a queued or running run (process group SIGTERM, then KILL)
  GET  /api/runs/<id>/resume      the exact resume command of a budget_exhausted run
  GET  /api/config                stills available, defaults, limits
  GET  /runs/<id>/files/<path>    run outputs (issues, bottled spec, knowledge graph, viewer)
  GET  /api/runs/<id>/eval        the run's evaluation, aggregated: exact vs estimated (eval_view.py)
  POST /api/runs/<id>/eval        plan (run_suite.py --plan), then start a budget-capped evaluation;
                                  {"smoke": true} = the free deterministic check
  GET  /api/runs/<id>/eval/status the evaluation job: state, planned and live spend, log tail
  GET  /api/eval/options          suites and answer keys the server knows (ids only, never paths)
  GET  /api/eval/compare?runs=a,b two runs' evaluations side by side, with a within-noise flag
  POST /api/runs/<id>/dataset     build a test data set (answer key) from the run's SOURCE code and
                                  tests with eval/keygen/build_key.py (no AI unless asked, capped)
  GET  /api/runs/<id>/dataset[/status]  build state and counts (proposed, confirmed, approved, ...)
  GET  /api/runs/<id>/dataset/items?status=&source=&q=&page=  items with code excerpts
  POST /api/runs/<id>/dataset/review    approve / edit / reject decisions, or {"approve": "corroborated"}

Safety:
  * Binds 127.0.0.1 by default. A non-loopback --host is refused unless --allow-non-loopback is
    also passed, and then a warning is printed. Do not expose this server publicly.
  * Never accepts AWS credentials: runs use this process's environment / AWS profile only. A
    request body with an unknown field, or any field whose name looks like a credential (aws_*,
    secret, token, password, access_key, session, ...), is rejected with 400.
  * Host header must be a loopback name (DNS-rebinding guard); POST requires
    Content-Type: application/json and a same-origin (or --cors-origin) Origin when one is sent.
  * Git URLs carrying credentials are refused (source_fetch.py); use ssh-agent or a credential
    helper.

Each run gets its own output directory <runs-dir>/<run_id>/ (run.log, launcher.json, and every
pipeline output). One run at a time by default; further runs are queued. --import-run DIR
(repeatable) registers an existing completed output directory as a read-only "imported" run: the
launcher never writes into it, and evaluations of it go to <runs-dir>/_evals/<run_id>/.
"""
from __future__ import annotations

import argparse
import datetime
import hmac
import http.cookies
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import budget  # noqa: E402
import launcher_datasets as LD  # noqa: E402  (test data sets: build, review, ad-hoc suite)
import eval_view  # noqa: E402  (stdlib + yaml only; reads eval artifacts, never writes)
import source_fetch  # noqa: E402
import viewer_api  # noqa: E402  (torch deferred inside its functions — safe to import here)

REPO = HERE.parent.parent
DEFAULT_SCRIPT = REPO / "run_distillation.sh"
DEFAULT_RUNS_DIR = REPO / "output" / "runs"
UI_FILE = HERE / "launcher_ui" / "index.html"
ACCESS_UI_FILE = HERE / "launcher_ui" / "access.html"
# The viewer's Evaluation page. generate-viewer-v3.py copies it into new viewers; the launcher also
# serves it for every run (so older and read-only imported viewers get it), with its API base set.
EVAL_PAGE_TEMPLATE = HERE.parent / "pipeline" / "viewer_evaluation.html"
EVAL_PAGE_REL = "viewer/pages/evaluation.html"
EXIT_BUDGET = 6

# Stills run_distillation.sh skips when auto-discovering (meta / synthesis stills).
META_STILLS = {"00_conventions", "bottling", "reconciliation", "master_blend", "artifact_selector",
               "domain_partitioner", "cross_domain_blend", "redistillation"}

ALLOWED_FIELDS = {"source", "name", "max_usd", "model_id", "stills", "facts", "issues",
                  "allow_over_estimate", "estimate_only"}
ALLOWED_SOURCE_FIELDS = {"git": {"type", "url", "ref", "subdir"}, "path": {"type", "path"}}
# Per-route body field allowlist for the per-run viewer routes. The existing credential_fields
# check also runs on each; none of these names collide with CREDENTIAL_KEY_RX.
VIEWER_ROUTE_FIELDS = {
    "chat": {"question", "top_k"},
    "validate": {"title", "block", "evidence"},
    "dependencies": {"title", "still"},
    "translate": {"code", "language"},
}
# POST /api/runs/<id>/eval body allowlist. answer_key / suite are ids from GET /api/eval/options;
# a path is never accepted. None of these collide with CREDENTIAL_KEY_RX.
EVAL_FIELDS = {"answer_key", "suite", "judge_runs", "budget_usd", "plan_only", "smoke"}
EVAL_JUDGE_RUNS_MAX = 5
EVAL_ACTIVE = {"planning", "running"}
EVALS_DIRNAME = "_evals"
# eval/ci/suites/generic.yaml: deterministic checks for any run (the Quick check fallback when no
# other suite matches); it has no answer key, so a Full evaluation never uses it
GENERIC_SUITE = "generic"
# run_suite.py exit codes (eval/ci/run_suite.py): 0 pass, 1 thresholds, 2 config/infra, 3 budget,
# 4 an LLM call during --smoke
SUITE_EXIT_STATE = {0: "complete", 1: "complete", 2: "failed", 3: "budget_exceeded", 4: "failed"}
CREDENTIAL_KEY_RX = re.compile(r"(aws|secret|token|passw|credential|access[_-]?key|session|api[_-]?key|"
                               r"private[_-]?key|auth|cookie|bearer)", re.I)
NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MODEL_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
# Environment the launcher sets per run; inherited values for these are dropped so the operator's
# shell cannot leak a conflicting DISTILL_TARGET etc. into a launched run.
RUN_ENV_KEYS = {"DISTILL_TARGET", "DISTILL_OUTPUT", "DISTILL_GIT_URL", "DISTILL_GIT_REF",
                "DISTILL_GIT_SUBDIR", "DISTILL_GIT_COMMIT", "DISTILL_GIT_REF_REQUESTED",
                "DISTILL_MAX_USD", "DISTILL_ALLOW_OVER_ESTIMATE", "DISTILL_ESTIMATE_ONLY",
                "DISTILL_STILLS", "DISTILL_FACTS", "DISTILL_ISSUES", "DISTILL_TARGET_NAME",
                "DISTILL_TELEMETRY_DIR", "DISTILL_VIEWER_API_BASE", "DISTILL_SOURCE_REPO_URL"}
FINAL_STATES = {"complete", "failed", "budget_exhausted", "over_estimate", "cancelled", "interrupted",
                "estimated"}
# Output links (relative to the run dir) reported when the file exists.
OUTPUT_LINKS = {
    "issues_md": "issues/issues.md",
    "issues_json": "issues/issues.json",
    "bottled_spec": "bottled_spec/bottled_spec.md",
    "bottled_summary": "bottled_spec/README.md",
    "master_blend": "master_blend/master_blend.md",
    "knowledge_graph_index": "knowledge_graph/index.json",
    "knowledge_graph": "knowledge_graph/knowledge_graph.json",
    "code_graph": "knowledge_graph/code_graph.json",
    "viewer": "viewer/index.html",
    "run_status": "run_status.json",
    "budget_estimate": "budget_estimate.json",
    "source_provenance": "source_provenance.json",
    "run_manifest": "run_manifest.json",
}
SERVABLE_PREFIXES = ("issues/", "bottled_spec/", "master_blend/", "knowledge_graph/", "viewer/")
SERVABLE_FILES = {"run_status.json", "budget_estimate.json", "source_provenance.json", "run_manifest.json",
                  "preflight_report.json", "conservation_report.json", "run.log"}
PHASE_RX = re.compile(r"\]\s+(Phase\s+[0-9][0-9.]*[a-z]?)\b")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class ApiError(Exception):
    def __init__(self, http_status: int, message: str, **extra):
        super().__init__(message)
        self.status = http_status
        self.message = message
        self.extra = extra


# ── Bind safety ─────────────────────────────────────────────────────────────────────────────
def is_loopback(host: str) -> bool:
    h = host.strip().strip("[]").lower()
    if h in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _bare_host(host: str) -> str:
    """Normalize a Host header to its bare name, matching _host_ok's historical quirks exactly.

    (a) A bracketed IPv6 literal KEEPS its brackets: "[::1]:8090" -> "[::1]".
    (b) A trailing :port is stripped ONLY when there is EXACTLY one colon, so a bare unbracketed
        IPv6 ("::1") is left intact rather than mangled to "::".
    """
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _parse_allowed_hosts(s: str | None) -> list[str]:
    """Comma-split, strip, drop blanks, lowercase. Returns [] for None/empty."""
    if not s:
        return []
    return [h.strip().lower() for h in s.split(",") if h.strip()]


def positive_int(s: str) -> int:
    """argparse type: a strictly positive integer (seconds). Rejects 0 and negatives."""
    v = int(s)
    if v <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer (seconds)")
    return v


class SessionStore:
    """In-memory token store for the shared-access-code gate. Tokens do not survive a restart.

    issue()  -> a fresh opaque 256-bit token, stored with its monotonic issue time.
    valid(t) -> True iff t is a known, unexpired token (lazy-expiry delete, lock-held).
    """

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._store: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._store[token] = time.monotonic()
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            issued = self._store.get(token)
            if issued is None:
                return False
            if time.monotonic() - issued > self.ttl:
                del self._store[token]
                return False
            return True


def check_bind(host: str, allow_non_loopback: bool, err=sys.stderr) -> None:
    """Refuse a non-loopback bind unless explicitly allowed (then warn loudly)."""
    if is_loopback(host):
        return
    if not allow_non_loopback:
        raise SystemExit(f"launcher: refusing to bind to non-loopback address {host!r}. This server "
                         "launches paid Bedrock runs and must stay local (default 127.0.0.1). Pass "
                         "--allow-non-loopback only on an isolated, access-controlled network.")
    print("=" * 78, file=err)
    print(f"WARNING: launcher bound to NON-LOOPBACK address {host!r} (--allow-non-loopback).", file=err)
    print("  Anyone who can reach this address can launch paid runs with THIS machine's AWS", file=err)
    print("  credentials. Never expose it publicly; synthetic or approved non-production code only.", file=err)
    print("=" * 78, file=err)


# ── Request validation ──────────────────────────────────────────────────────────────────────
def credential_fields(obj, prefix: str = "") -> list[str]:
    """Names of any (nested) keys that look like credentials."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}{k}"
            if CREDENTIAL_KEY_RX.search(str(k)):
                found.append(path)
            found += credential_fields(v, path + ".")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += credential_fields(v, f"{prefix}{i}.")
    return found


def available_stills(script: Path) -> list[str]:
    specs = script.parent / "still_specs"
    if not specs.is_dir():
        return []
    return sorted(d.name for d in specs.iterdir()
                  if d.is_dir() and d.name not in META_STILLS and (d / "prompt.md").is_file())


def _slug_from_source(src: dict) -> str:
    """Derive a NAME_RX-safe run slug from a normalized source object (out["source"]).

    Used when the request omits "name": the server owns the run-id/slug/filesystem rules, so the
    slug is derived here (never client-side) and the derived value is re-validated with a hard
    post-condition — NAME_RX is otherwise only applied to a supplied "name" field.
    """
    if src["type"] == "git":
        raw = (src.get("url") or src.get("display_url") or "").rstrip("/").rsplit("/", 1)[-1]
        if raw.endswith(".git"):
            raw = raw[:-4]
    else:  # path source
        raw = os.path.basename(src["path"].rstrip("/"))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.lower())  # charset
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")        # collapse repeats, trim
    slug = slug[:40]                                      # explicit cap, well under NAME_RX's 64
    slug = slug.strip("-.") or "source"                   # re-trim after cut; fallback literal "source"
    if not slug[:1].isalnum():                            # satisfy NAME_RX's leading-char rule ^[A-Za-z0-9]
        slug = "s" + slug
    assert NAME_RX.match(slug), slug                      # post-condition: never emit a bad name
    return slug


def validate_request(body, stills_available: list[str], max_usd_ceiling: float) -> dict:
    """-> normalized request. Raises ApiError(400/422)."""
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object")
    creds = credential_fields(body)
    if creds:
        raise ApiError(400, "credential fields are not accepted: " + ", ".join(sorted(creds)) +
                       ". The launcher uses its own process environment / AWS profile only; set "
                       "AWS_PROFILE (or instance credentials) where the launcher runs.",
                       fields=sorted(creds))
    unknown = sorted(set(body) - ALLOWED_FIELDS)
    if unknown:
        raise ApiError(400, f"unknown field(s): {', '.join(unknown)}", fields=unknown)
    src = body.get("source")
    if not isinstance(src, dict):
        raise ApiError(400, "source is required: {\"type\": \"git\", \"url\": ...} or {\"type\": \"path\", \"path\": ...}",
                       field="source")
    stype = src.get("type")
    if stype not in ALLOWED_SOURCE_FIELDS:
        raise ApiError(400, "source.type must be \"git\" or \"path\"", field="source.type")
    bad = sorted(set(src) - ALLOWED_SOURCE_FIELDS[stype])
    if bad:
        raise ApiError(400, f"unknown source field(s) for type {stype}: {', '.join(bad)}", fields=bad)
    out: dict = {}
    try:
        if stype == "git":
            if src.get("url") is not None and not isinstance(src.get("url"), str):
                raise ApiError(400, "source.url must be a string", field="source.url")
            # Accept what the browser shows (…/tree/<ref>/<folder>): split into URL, ref and folder.
            url, b_ref, b_sub = source_fetch.normalize_browse_url(
                (src.get("url") or "").strip(), src.get("ref") or None, src.get("subdir") or None)
            info = source_fetch.classify_url(url)
            ref = source_fetch.check_ref(b_ref or "")
            subdir = source_fetch.check_subdir(b_sub or "")
            out["source"] = {"type": "git", "url": url, "display_url": info["display"],
                             "ref": ref or None, "subdir": subdir or None}
        else:
            p = src.get("path")
            if not isinstance(p, str) or not p.strip():
                raise ApiError(400, "source.path is required", field="source.path")
            pp = Path(os.path.expanduser(p.strip()))
            if not pp.is_absolute():
                raise ApiError(400, "source.path must be an absolute path", field="source.path")
            if not pp.is_dir():
                raise ApiError(422, f"source.path {str(pp)!r} is not an existing directory", field="source.path")
            out["source"] = {"type": "path", "path": str(pp.resolve())}
    except source_fetch.FetchError as e:
        raise ApiError(400, str(e), field="source")
    name = body.get("name")
    # "name" is optional: a non-empty string is validated with NAME_RX exactly as before; an empty
    # string or a missing key derives the slug from the source; an explicitly supplied non-string
    # still fails as before.
    if isinstance(name, str) and name:
        if not NAME_RX.match(name):
            raise ApiError(400, "name must be 1-64 characters: letters, digits, '.', '_' or '-'", field="name")
        out["name"] = name
    elif name is None or name == "":
        out["name"] = _slug_from_source(out["source"])
    else:
        raise ApiError(400, "name must be a string of 1-64 characters: letters, digits, '.', '_' or '-'",
                       field="name")
    # "max_usd" is optional: omitted/blank → no cap (DISTILL_MAX_USD left unset, estimate still
    # runs but cannot trip over_estimate); present → validated and enforced exactly as before.
    mx = body.get("max_usd")
    if mx is not None:
        if isinstance(mx, bool) or not isinstance(mx, (int, float)) or not (mx > 0):
            raise ApiError(400, "max_usd must be a number > 0 (omit it for no cap)", field="max_usd")
        if mx > max_usd_ceiling:
            raise ApiError(400, f"max_usd {mx} is over the launcher ceiling ${max_usd_ceiling:g} "
                                "(--max-usd-ceiling)", field="max_usd")
        out["max_usd"] = float(mx)
    model = body.get("model_id")
    if model is not None:
        if not isinstance(model, str) or not MODEL_RX.match(model):
            raise ApiError(400, "model_id must be a Bedrock model or inference-profile id", field="model_id")
        out["model_id"] = model
    stills = body.get("stills")
    if stills is not None:
        if isinstance(stills, str):
            stills = [s for s in stills.replace(",", " ").split() if s]
        if not isinstance(stills, list) or not stills or not all(isinstance(s, str) for s in stills):
            raise ApiError(400, "stills must be a non-empty list of still names", field="stills")
        unknown_stills = sorted(set(stills) - set(stills_available))
        if unknown_stills:
            raise ApiError(400, f"unknown still(s): {', '.join(unknown_stills)} (available: "
                                f"{', '.join(stills_available)})", field="stills")
        out["stills"] = list(dict.fromkeys(stills))
    for flag in ("facts", "issues", "allow_over_estimate", "estimate_only"):
        v = body.get(flag)
        if v is not None and not isinstance(v, bool):
            raise ApiError(400, f"{flag} must be true or false", field=flag)
        if v is not None:
            out[flag] = v
    return out


def validate_eval_request(body, suites: dict, keys: dict, max_usd_ceiling: float) -> dict:
    """POST /api/runs/<id>/eval body -> normalized request. Raises ApiError(400).

    Same screening style as validate_request: credential-looking keys first, then an allowlist,
    then per-field checks. ``answer_key`` and ``suite`` must be ids the server lists in
    GET /api/eval/options; a client-supplied path is refused, so no client string ever reaches
    the run_suite.py command line as a path."""
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object")
    creds = credential_fields(body)
    if creds:
        raise ApiError(400, "credential fields are not accepted: " + ", ".join(sorted(creds)) +
                       ". The launcher uses its own process environment / AWS profile only; set "
                       "AWS_PROFILE (or instance credentials) where the launcher runs.",
                       fields=sorted(creds))
    unknown = sorted(set(body) - EVAL_FIELDS)
    if unknown:
        raise ApiError(400, f"unknown field(s): {', '.join(unknown)}", fields=unknown)
    out: dict = {}
    for flag in ("plan_only", "smoke"):
        v = body.get(flag)
        if v is not None and not isinstance(v, bool):
            raise ApiError(400, f"{flag} must be true or false", field=flag)
        out[flag] = bool(v)
    if out["smoke"] and out["plan_only"]:
        raise ApiError(400, "plan_only does not apply to a smoke check (it is free: no model call, "
                            "budget forced to $0)", field="plan_only")
    for field, known in (("answer_key", keys), ("suite", suites)):
        v = body.get(field)
        if v is None or v == "":
            continue
        if not isinstance(v, str) or any(c in v for c in "/\\") or ".." in v or v not in known:
            raise ApiError(400, f"{field} must be one of the server's known ids "
                                f"({', '.join(sorted(known)) or 'none configured'}); paths are never "
                                "accepted", field=field)
        out[field] = v
    jr = body.get("judge_runs", 1)
    if isinstance(jr, bool) or not isinstance(jr, int) or not (1 <= jr <= EVAL_JUDGE_RUNS_MAX):
        raise ApiError(400, f"judge_runs must be an integer from 1 to {EVAL_JUDGE_RUNS_MAX} "
                            "(1 = one judge pass; use 3 for a number you intend to report)",
                       field="judge_runs")
    out["judge_runs"] = jr
    b = body.get("budget_usd")
    if b is None:
        if not out["smoke"]:
            raise ApiError(400, "budget_usd is required: the evaluation is planned first and refused "
                                "if the plan is over it (a smoke check needs none)", field="budget_usd")
    else:
        if isinstance(b, bool) or not isinstance(b, (int, float)) or not (b > 0):
            raise ApiError(400, "budget_usd must be a number > 0", field="budget_usd")
        if b > max_usd_ceiling:
            raise ApiError(400, f"budget_usd {b} is over the launcher ceiling ${max_usd_ceiling:g} "
                                "(--max-usd-ceiling)", field="budget_usd")
        out["budget_usd"] = float(b)
    if out["smoke"]:
        out["budget_usd"] = 0.0
    return out


def load_eval_catalog(repo: Path) -> tuple[dict, dict]:
    """Suites (eval/ci/suites/*.yaml) and the answer keys they score against, read on the server.
    -> (suites by id, keys by id). A key id is its corpus name; the client only ever sees ids."""
    suites: dict = {}
    keys: dict = {}
    sdir = repo / "eval" / "ci" / "suites"
    try:
        sys.path.insert(0, str(repo / "eval" / "ci"))
        import suite_lib  # noqa: E402  (also used by run_suite.py; yaml needed to read configs)
    except Exception as e:                                   # pragma: no cover
        sys.stderr.write(f"launcher: eval catalog unavailable ({type(e).__name__}: {e})\n")
        return suites, keys
    for cfg_path in sorted(sdir.glob("*.yaml")) if sdir.is_dir() else []:
        try:
            cfg = suite_lib.load_config(cfg_path)
        except Exception as e:
            sys.stderr.write(f"launcher: skipping suite {cfg_path.name}: {e}\n")
            continue
        sid = cfg_path.stem
        corpora = cfg.get("corpora") or {}
        corpus = next(iter(corpora), None)
        kpath = (corpora.get(corpus) or {}).get("key") if corpus else None
        kabs = (repo / kpath) if kpath and not Path(kpath).is_absolute() else (Path(kpath) if kpath else None)
        suites[sid] = {"id": sid, "suite": cfg.get("suite"), "description": " ".join(str(cfg.get("description") or "").split()),
                       "default_budget_usd": cfg.get("budget_usd"), "judge_runs": cfg.get("judge_runs"),
                       "corpus": corpus, "corpora": sorted(corpora), "answer_key": corpus,
                       "config": str(cfg_path.relative_to(repo)) if cfg_path.is_relative_to(repo) else str(cfg_path),
                       "stages": [s.get("id") for s in cfg.get("stages") or []]}
        if sid == GENERIC_SUITE:
            suites[sid]["answer_key"] = None           # deterministic only: no answer key
            continue
        if corpus and kabs is not None:
            keys[corpus] = {"id": corpus, "suite": sid, "path": kpath, "abs_path": str(kabs),
                            "exists": kabs.is_file(), "label": f"{corpus} answer key ({kpath})"}
    return suites, keys


# ── Runs ────────────────────────────────────────────────────────────────────────────────────
class Run:
    def __init__(self, run_id: str, run_dir: Path, request: dict):
        self.run_id = run_id
        self.dir = run_dir
        self.request = request
        self.state = "estimating"
        self.created = _now()
        self.started: str | None = None
        self.finished: str | None = None
        self.exit_code: int | None = None
        self.estimate: dict | None = None
        self.error: str | None = None
        self.pid: int | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel_requested = False
        self.imported = False             # --import-run: read-only, never written to

    def public_request(self) -> dict:
        r = json.loads(json.dumps(self.request))
        if r.get("source", {}).get("type") == "git":
            r["source"]["url"] = r["source"].pop("display_url", None)  # never echo a raw URL
        return r

    def save(self) -> None:
        if self.imported:                 # an imported output directory is never written to
            return
        data = {"run_id": self.run_id, "state": self.state, "created": self.created,
                "started": self.started, "finished": self.finished, "exit_code": self.exit_code,
                "estimate": self.estimate, "error": self.error, "pid": self.pid,
                "request": self.request}
        tmp = self.dir / "launcher.json.tmp"
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "launcher.json")

    @classmethod
    def load(cls, run_dir: Path) -> "Run | None":
        try:
            d = json.loads((run_dir / "launcher.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None
        r = cls(d["run_id"], run_dir, d.get("request") or {})
        for k in ("state", "created", "started", "finished", "exit_code", "estimate", "error", "pid"):
            setattr(r, k, d.get(k))
        return r

    @classmethod
    def imported_from(cls, run_dir: Path, run_id: str) -> "Run":
        """A read-only Run over an existing, completed output directory (--import-run)."""
        rj = _read_json(run_dir / "run.json") or {}
        st = _read_json(run_dir / "run_status.json") or {}
        target = (rj.get("env_snapshot") or {}).get("DISTILL_TARGET")
        r = cls(run_id, run_dir, {"name": run_dir.name,
                                  "source": {"type": "path", "path": target or str(run_dir)}})
        r.imported = True
        r.state = st.get("status") if st.get("status") in FINAL_STATES else "imported"
        r.created = rj.get("created_at") or _now()
        r.finished = st.get("finished")
        return r


class EvalJob:
    """One evaluation of one run: run_suite.py --plan, then (unless plan_only) run_suite.py in the
    background with the budget cap and judge_runs passed down. Outputs live under
    <runs-dir>/_evals/<run_id>/<eval_id>/ (plan/, suite/, eval.log, eval_job.json)."""

    FIELDS = ("eval_id", "run_id", "mode", "suite", "answer_key", "judge_runs", "budget_usd",
              "state", "created", "started", "finished", "exit_code", "verdict", "error",
              "planned_usd", "plan", "spent_usd", "config", "key_label", "provisional",
              "source_root", "repo_root", "key_alignment", "trigger")

    def __init__(self, eval_id: str, run_id: str, out_dir: Path, mode: str, suite: str,
                 answer_key: str | None, judge_runs: int, budget_usd: float):
        self.eval_id, self.run_id, self.out_dir, self.mode = eval_id, run_id, out_dir, mode
        self.suite, self.answer_key, self.judge_runs, self.budget_usd = suite, answer_key, judge_runs, budget_usd
        self.state = "planning"
        self.created = _now()
        self.started = self.finished = None
        self.exit_code: int | None = None
        self.verdict: str | None = None
        self.error: str | None = None
        self.planned_usd: float | None = None
        self.plan: dict | None = None
        self.spent_usd: float | None = None
        self.config: str | None = None          # ad-hoc suite config (a run's own test data set)
        self.key_label: str | None = None       # plain-language "what this is scored against"
        self.provisional = False                # scored against an unreviewed test data set
        self.source_root: str | None = None     # the run's own analysed source, passed to the suite
        self.repo_root: str | None = None
        self.key_alignment: dict | None = None  # subdir run vs a repo-wide key: kept / excluded
        self.trigger = "user"                   # "auto" = the launcher's automatic grounding check
        self.proc: subprocess.Popen | None = None

    @property
    def log(self) -> Path:
        return self.out_dir / "eval.log"

    def live_spend(self) -> float:
        """Exact spend so far: the cost of every Bedrock call the running suite logged."""
        if self.spent_usd is not None:
            return self.spent_usd
        total = 0.0
        root = self.out_dir / "suite"
        for p in root.rglob("bedrock_calls.jsonl") if root.is_dir() else []:
            if "_plan" in p.parts:
                continue
            for line in p.read_text(errors="replace").splitlines():
                try:
                    total += float(json.loads(line).get("cost_usd") or 0.0)
                except (ValueError, AttributeError):
                    continue
        return round(total, 6)

    def public(self) -> dict:
        d = {k: getattr(self, k) for k in self.FIELDS}
        d["spent_usd"] = self.live_spend()
        d["out_dir"] = str(self.out_dir)
        return d

    def save(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.out_dir / "eval_job.json.tmp"
        tmp.write_text(json.dumps({k: getattr(self, k) for k in self.FIELDS}, indent=2), encoding="utf-8")
        tmp.replace(self.out_dir / "eval_job.json")

    @classmethod
    def load(cls, out_dir: Path) -> "EvalJob | None":
        d = _read_json(out_dir / "eval_job.json")
        if not d:
            return None
        j = cls(d["eval_id"], d["run_id"], out_dir, d.get("mode") or "full", d.get("suite") or "",
                d.get("answer_key"), d.get("judge_runs") or 1, d.get("budget_usd") or 0.0)
        for k in cls.FIELDS:
            if k in d:
                setattr(j, k, d[k])
        return j


def suite_plan_summary(report: dict) -> dict:
    """The parts of a --plan suite.json a person needs to decide: per-stage estimates and caps."""
    b = report.get("budget") or {}
    # budget.planned_usd counts only the stages that fit under the cap; when the plan is over it,
    # the stages a real run would never reach are still priced (status budget_exceeded / not_run
    # with an estimate), and the full price is what a person needs to choose a budget.
    priced = [s.get("estimated_usd") for s in report.get("stages") or []
              if s.get("status") in ("planned", "budget_exceeded", "not_run")
              and isinstance(s.get("estimated_usd"), (int, float))]
    full = round(sum(priced), 4) if priced else b.get("planned_usd")
    return {"planned_usd": full, "fits_cap_usd": b.get("planned_usd"), "cap_usd": b.get("cap_usd"),
            "verdict": report.get("verdict"), "exit_code": report.get("exit_code"),
            "judge_runs": (report.get("provenance") or {}).get("judge_runs_default"),
            "stages": [{"id": s.get("id"), "status": s.get("status"),
                        "estimated_usd": s.get("estimated_usd"),
                        "estimate_source": (s.get("estimate") or {}).get("source"),
                        "cap_passed_usd": s.get("cap_passed_usd"),
                        "reason": (s.get("reason") or "")[:240] or None}
                       for s in report.get("stages") or []]}


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def tail_lines(path: Path, n: int) -> list[str]:
    if not path.is_file() or n <= 0:
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = min(size, max(64 * 1024, n * 400))
        f.seek(size - block)
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    if block < size:
        lines = lines[1:]  # first line may be partial
    return lines[-n:]


def last_phase(log: Path) -> str | None:
    phase = None
    for ln in tail_lines(log, 4000):
        m = PHASE_RX.search(ln)
        if m:
            phase = m.group(1)
        elif "Distillation complete" in ln:
            phase = "done"
    return phase


class Launcher:
    def __init__(self, script: str | Path = DEFAULT_SCRIPT, runs_dir: str | Path = DEFAULT_RUNS_DIR,
                 max_concurrent: int = 1, base_env: dict | None = None,
                 estimate_timeout_s: int = 1800, max_usd_ceiling: float = 500.0,
                 import_runs: list | None = None, eval_cmd: list[str] | None = None,
                 eval_repo: str | Path = REPO, eval_plan_timeout_s: int = 1800,
                 auto_grounding: bool = False):
        self.script = Path(script).resolve()
        self.runs_dir = Path(runs_dir).resolve()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.max_concurrent = max(1, int(max_concurrent))
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.estimate_timeout_s = estimate_timeout_s
        self.max_usd_ceiling = max_usd_ceiling
        self.runs: dict[str, Run] = {}
        self.queue: list[str] = []
        self.lock = threading.RLock()
        # evaluation: run_suite.py is a black box, started as a subprocess (stubbed in tests)
        self.eval_repo = Path(eval_repo).resolve()
        self.eval_cmd = list(eval_cmd or [sys.executable, str(self.eval_repo / "eval" / "ci" / "run_suite.py")])
        self.eval_plan_timeout_s = eval_plan_timeout_s
        self.eval_suites, self.eval_keys = load_eval_catalog(self.eval_repo)
        self.evals: dict[str, EvalJob] = {}
        self.eval_history: dict[str, list[EvalJob]] = {}   # every job of a run, oldest first
        # automatic grounding check: the free smoke suite, started once per finished run
        self.auto_grounding = bool(auto_grounding)
        self._auto_fired: set[str] = set()
        self._auto_errors: dict[str, str] = {}
        self._key_items_n: dict[str, int | None] = {}
        # test data sets live in <runs-dir>/_datasets/<run_id>/ (never inside a run directory)
        self.datasets = LD.DatasetManager(self.runs_dir, self.eval_repo)
        self._load_existing()
        for d in import_runs or []:
            self.import_run(d)
        self._load_evals()

    # persistence ------------------------------------------------------------------------------
    def _load_existing(self) -> None:
        for d in sorted(self.runs_dir.iterdir()):
            if not d.is_dir() or d.name in (EVALS_DIRNAME, LD.DATASETS_DIRNAME):
                continue
            r = Run.load(d)
            if r is None:
                continue
            if r.state not in FINAL_STATES:
                # A run started by a previous launcher process keeps running in its own session
                # (start_new_session) and keeps spending; nothing watches it any more. Stop it.
                stopped = self._stop_orphan(r.pid)
                st = _read_json(d / "run_status.json") or {}
                r.state = st.get("status") if st.get("status") in FINAL_STATES else "interrupted"
                r.error = r.error or (("launcher restarted while this run was active"
                                       + ("; its leftover process group was stopped" if stopped else ""))
                                      if r.state == "interrupted" else None)
                r.finished = r.finished or _now()
                r.save()
            self.runs[r.run_id] = r

    def _stop_orphan(self, pid) -> bool:
        """SIGTERM (then SIGKILL) the process group a previous launcher started for a run, if it
        is still alive and still this launcher's pipeline script (a reused pid is left alone)."""
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            return False
        try:   # ps, not os.getpgid: macOS refuses getpgid across sessions (EPERM)
            out = subprocess.run(["ps", "-o", "pgid=", "-o", "command=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return False
        pgid, _, cmd = out.partition(" ")
        # we start every run as its own process-group leader running the pipeline script
        if pgid.strip() != str(pid) or self.script.name not in cmd:
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return False
        for _ in range(100):
            time.sleep(0.1)
            try:
                os.killpg(pid, 0)
            except (ProcessLookupError, PermissionError):   # gone (macOS: EPERM once only a zombie is left)
                return True
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        return True

    def import_run(self, run_dir: str | Path) -> Run:
        """Register an existing completed output directory as a read-only run (--import-run).
        Nothing is written into it: launcher.json is not created, start/cancel are refused, and
        its evaluations are written under <runs-dir>/_evals/<run_id>/."""
        d = Path(os.path.expanduser(str(run_dir))).resolve()
        if not d.is_dir():
            raise ValueError(f"--import-run {str(run_dir)!r} is not an existing directory")
        if not any((d / f).exists() for f in ("run_status.json", "run.json", "run_manifest.json", "viewer")):
            raise ValueError(f"--import-run {str(d)!r} does not look like a run_distillation.sh output "
                             "directory (no run_status.json, run.json, run_manifest.json or viewer/)")
        base = "imported-" + _slug_from_source({"type": "path", "path": str(d)})
        run_id, i = base, 2
        with self.lock:
            for r in self.runs.values():
                if r.dir.resolve() == d:
                    return r
            while run_id in self.runs:
                run_id, i = f"{base}-{i}", i + 1
            r = Run.imported_from(d, run_id)
            self.runs[run_id] = r
        return r

    # environment ------------------------------------------------------------------------------
    def _env(self, run: Run, estimate_only: bool) -> dict:
        req = run.request
        env = {k: v for k, v in self.base_env.items() if k not in RUN_ENV_KEYS}
        env.update({"DISTILL_OUTPUT": str(run.dir),
                    "DISTILL_TARGET_NAME": req["name"], "PYTHONUNBUFFERED": "1"})
        # Budget cap is optional: set DISTILL_MAX_USD only when a cap was requested; when absent,
        # leave it unset so run_distillation.sh runs with no ceiling (its existing "no cap" path).
        if "max_usd" in req:
            env["DISTILL_MAX_USD"] = f"{req['max_usd']:.4f}"
        src = req["source"]
        if src["type"] == "git":
            env["DISTILL_GIT_URL"] = src["url"]
            if src.get("subdir"):
                env["DISTILL_GIT_SUBDIR"] = src["subdir"]
            commit = (run.estimate or {}).get("commit")
            if not estimate_only and commit:
                # the run analyses exactly the commit that was estimated
                env["DISTILL_GIT_REF"] = commit
                env["DISTILL_GIT_REF_REQUESTED"] = src.get("ref") or "HEAD"
            elif src.get("ref"):
                env["DISTILL_GIT_REF"] = src["ref"]
        else:
            env["DISTILL_TARGET"] = src["path"]
        if req.get("model_id"):
            env["BEDROCK_MODEL_ID"] = req["model_id"]
        if req.get("stills"):
            env["DISTILL_STILLS"] = " ".join(req["stills"])
        if "facts" in req:
            env["DISTILL_FACTS"] = "1" if req["facts"] else "0"
        if "issues" in req:
            env["DISTILL_ISSUES"] = "1" if req["issues"] else "0"
        if req.get("allow_over_estimate"):
            env["DISTILL_ALLOW_OVER_ESTIMATE"] = "1"
        if estimate_only:
            env["DISTILL_ESTIMATE_ONLY"] = "1"
        else:
            # The embedded viewer's /api/* fetches must target this run's per-run routes, served
            # by this same launcher. Only the real run generates a viewer, so only it needs this.
            env["DISTILL_VIEWER_API_BASE"] = f"/api/runs/{run.run_id}"
        # A GitHub blob base for evidence links, if the request carried one (the form does not
        # collect one yet). Registered in RUN_ENV_KEYS so a stray inherited value cannot leak in;
        # the shell records it into run_manifest.json's source.repo_url for /validate to read.
        repo_url = req.get("source_repo_url")
        if isinstance(repo_url, str) and repo_url:
            env["DISTILL_SOURCE_REPO_URL"] = repo_url
        return env

    def _popen(self, run: Run, estimate_only: bool) -> subprocess.Popen:
        log = open(run.dir / "run.log", "ab")
        log.write(f"\n===== launcher: {'estimate' if estimate_only else 'run'} started {_now()} =====\n".encode())
        log.flush()
        try:
            return subprocess.Popen(["bash", str(self.script)], cwd=str(self.script.parent),
                                    env=self._env(run, estimate_only), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            log.close()

    # API operations ---------------------------------------------------------------------------
    def create(self, body) -> tuple[int, dict]:
        req = validate_request(body, available_stills(self.script), self.max_usd_ceiling)
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{req['name']}-{secrets.token_hex(2)}"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True)
        run = Run(run_id, run_dir, req)
        with self.lock:
            self.runs[run_id] = run
            run.save()
        self._estimate(run)
        if run.state == "over_estimate":
            est = run.estimate or {}
            raise ApiError(409, f"estimate ${est.get('estimate_usd', 0):.2f} is over max_usd "
                                f"${req['max_usd']:.2f}; raise max_usd (suggested "
                                f"${est.get('suggested_max_usd') or 0:.2f}) or set allow_over_estimate: true. "
                                "No model call was made.",
                           run_id=run_id, status="over_estimate", estimate_usd=est.get("estimate_usd"),
                           max_usd=req["max_usd"], suggested_max_usd=est.get("suggested_max_usd"))
        if run.state == "failed":
            raise ApiError(422, f"pre-run estimate failed: {run.error}", run_id=run_id, status="failed",
                           log_tail=tail_lines(run.dir / "run.log", 20))
        if req.get("estimate_only"):
            return 200, self.describe(run)
        self._enqueue(run)
        return 202, self.describe(run)

    def start(self, run_id: str) -> tuple[int, dict]:
        run = self.get(run_id)
        self._refuse_imported(run, "started")
        with self.lock:
            if run.state != "estimated":
                raise ApiError(409, f"run {run_id} is {run.state}; only an estimated run can be started")
        self._enqueue(run)
        return 202, self.describe(run)

    def _estimate(self, run: Run) -> None:
        proc = self._popen(run, estimate_only=True)
        try:
            rc = proc.wait(timeout=self.estimate_timeout_s)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            rc = -1
        st = _read_json(run.dir / "run_status.json") or {}
        est = _read_json(run.dir / "budget_estimate.json") or {}
        prov = _read_json(run.dir / "source_provenance.json") or {}
        with self.lock:
            run.estimate = {
                "estimate_usd": est.get("projected_total_usd"),
                "remaining_usd_by_stage": est.get("remaining_usd_by_stage"),
                # full_usd_by_stage drives the UI's per-phase "Cost breakdown" panel (the full
                # price of each stage for this run, independent of what a resume already finished).
                "full_usd_by_stage": est.get("full_usd_by_stage"),
                "pages": (est.get("plan") or {}).get("pages"),
                "stills": (est.get("plan") or {}).get("stills"),
                "unit_cost_source": est.get("unit_cost_source"),
                "model_id": est.get("model_id"),
                "suggested_max_usd": st.get("suggested_max_usd"),
                "commit": prov.get("commit"),
            }
            if rc == 0 and st.get("status") == "estimated":
                run.state = "estimated"
            elif rc == EXIT_BUDGET and st.get("status") == "over_estimate":
                run.state = "over_estimate"
                run.finished = _now()
            else:
                run.state = "failed"
                run.finished = _now()
                run.exit_code = rc
                run.error = ("; ".join(st.get("failed_stages") or []) if st.get("status") == "failed"
                             else (f"estimate timed out after {self.estimate_timeout_s}s" if rc == -1
                                   else f"estimate exited {rc}"))
            run.save()

    def _enqueue(self, run: Run) -> None:
        with self.lock:
            run.state = "queued"
            run.save()
            self.queue.append(run.run_id)
        self._pump()

    def _running_count(self) -> int:
        return sum(1 for r in self.runs.values() if r.state == "running")

    def _pump(self) -> None:
        with self.lock:
            while self.queue and self._running_count() < self.max_concurrent:
                run = self.runs[self.queue.pop(0)]
                if run.state != "queued":
                    continue
                # the estimate's run_status.json ("estimated") must not be mistaken for the result
                try:
                    (run.dir / "run_status.json").unlink()
                except FileNotFoundError:
                    pass
                run.proc = self._popen(run, estimate_only=False)
                run.pid = run.proc.pid
                run.state = "running"
                run.started = _now()
                run.save()
                threading.Thread(target=self._watch, args=(run,), daemon=True).start()

    def _watch(self, run: Run) -> None:
        rc = run.proc.wait()
        st = _read_json(run.dir / "run_status.json") or {}
        with self.lock:
            run.exit_code = rc
            run.finished = _now()
            if run.cancel_requested:
                run.state = "cancelled"
            elif st.get("status") in ("complete", "failed", "budget_exhausted", "over_estimate"):
                run.state = st["status"]
                if run.state == "failed":
                    run.error = "; ".join(st.get("failed_stages") or []) or f"exit {rc}"
            else:
                run.state = "complete" if rc == 0 else "failed"
                if rc != 0:
                    run.error = f"run_distillation.sh exited {rc} without a final run_status.json"
            if rc == 0 and run.state == "complete" and st.get("status") != "complete":
                run.state = "failed"  # exit 0 without "complete" (e.g. a stop checkpoint)
                run.error = "run ended without run_status.json = complete (checkpoint stop?)"
            run.proc = None
            run.save()
        self._pump()
        if run.state == "complete":
            self.auto_ground(run)

    @staticmethod
    def _kill_group(proc: subprocess.Popen, grace_s: float = 15.0) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

    @staticmethod
    def _refuse_imported(run: Run, what: str) -> None:
        if run.imported:
            raise ApiError(409, f"run {run.run_id} is an imported, read-only output directory; it "
                                f"cannot be {what}", status=run.state, imported=True)

    def cancel(self, run_id: str) -> tuple[int, dict]:
        run = self.get(run_id)
        self._refuse_imported(run, "cancelled")
        with self.lock:
            if run.state == "queued":
                run.state = "cancelled"
                run.finished = _now()
                run.save()
                return 200, self.describe(run)
            if run.state == "estimated":
                run.state = "cancelled"
                run.finished = _now()
                run.save()
                return 200, self.describe(run)
            if run.state != "running" or run.proc is None:
                raise ApiError(409, f"run {run_id} is {run.state}; nothing to cancel")
            run.cancel_requested = True
            proc = run.proc
        self._kill_group(proc)
        for _ in range(100):  # let the watcher record the final state
            if run.state != "running":
                break
            time.sleep(0.05)
        return 200, self.describe(run)

    def get(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if run is None:
            raise ApiError(404, f"no run {run_id!r}")
        return run

    def resume(self, run_id: str) -> tuple[int, dict]:
        run = self.get(run_id)
        st = _read_json(run.dir / "run_status.json") or {}
        if st.get("status") != "budget_exhausted" or run.state != "budget_exhausted":
            raise ApiError(409, f"run {run_id} is {run.state}; a resume command exists only for "
                                "budget_exhausted runs", status=run.state)
        return 200, {"run_id": run_id, "status": "budget_exhausted", "resume_command": st.get("resume_command"),
                     "suggested_max_usd": st.get("suggested_max_usd"), "spent_usd": st.get("spent_usd"),
                     "stage_reached": st.get("stage_reached"),
                     "remaining_estimate_usd": st.get("remaining_estimate_usd"),
                     "note": "Run it in a shell with the same AWS profile; completed stages are reused at $0."}

    def log(self, run_id: str, n: int) -> tuple[int, dict]:
        run = self.get(run_id)
        n = max(1, min(int(n), 5000))
        return 200, {"run_id": run_id, "tail": n, "lines": tail_lines(run.dir / "run.log", n)}

    # per-run viewer paths ---------------------------------------------------------------------
    def _run_paths(self, run: Run) -> tuple[Path, Path, str, str]:
        """Resolve the four values every per-run viewer route needs:
        (vector_db, viewer_dir, source_root, repo_url).

        source_root is the *analysed* directory, resolved in this order:
          1. git run  — source_provenance.json["target"] (absolute, subdir already resolved
             into it by source_fetch.fetch)
          2. path run — run_manifest.json's source.path (the abspath the shell wrote), used
             only when there is no source_provenance.json
          3. git-run fallback — run.dir/.source/(subdir) only if a .source clone is present
        No bare run.dir/.source last resort: a path run has no .source, so that would make
        /validate report FILE NOT FOUND for every ref.

        repo_url is the GitHub blob base (run_manifest.json's source.repo_url), recorded by
        the shell from DISTILL_SOURCE_REPO_URL; "" when absent (links are then omitted).
        """
        vector_db = run.dir / "stills" / "shared_vectors.db"
        viewer_dir = run.dir / "viewer"
        prov = _read_json(run.dir / "source_provenance.json")
        manifest = _read_json(run.dir / "run_manifest.json") or {}
        source_root = ""
        if prov and isinstance(prov.get("target"), str) and os.path.isabs(prov["target"]):
            source_root = prov["target"]
        elif prov is None:
            path = ((manifest.get("source") or {}).get("path"))
            if isinstance(path, str) and path:
                source_root = path
        if not source_root and prov is not None:
            clone = run.dir / ".source" / (prov.get("subdir") or "")
            if clone.exists():
                source_root = str(clone)
        repo_url = (manifest.get("source") or {}).get("repo_url") or ""
        return vector_db, viewer_dir, source_root, repo_url

    # views ------------------------------------------------------------------------------------
    def describe(self, run: Run) -> dict:
        st = _read_json(run.dir / "run_status.json") or {}
        prov = _read_json(run.dir / "source_provenance.json")
        try:
            spent = round(budget.spent_usd(run.dir), 4)
        except Exception:
            spent = None
        links = {k: f"/runs/{run.run_id}/files/{rel}" for k, rel in OUTPUT_LINKS.items()
                 if (run.dir / rel).is_file()}
        links["log"] = f"/api/runs/{run.run_id}/log?tail=200"
        viewer_cmd = None
        if (run.dir / "viewer" / "index.html").is_file():
            viewer_cmd = (f"{HERE / '.venv/bin/python'} {HERE / 'serve_viewer.py'} {run.dir / 'viewer'} "
                          f"{run.dir / 'stills/shared_vectors.db'} --port 8080")
        est = run.estimate or {}
        source = run.public_request()["source"]
        if prov:
            source.update({"commit": prov.get("commit"), "ref_resolved": prov.get("ref_resolved")})
        return {
            "run_id": run.run_id, "name": run.request.get("name"), "status": run.state,
            "phase": st.get("stage_reached") if run.state in ("budget_exhausted", "over_estimate")
            else (last_phase(run.dir / "run.log") or ("done" if run.state == "complete" else None)),
            "spent_usd": spent, "estimate_usd": est.get("estimate_usd"),
            "max_usd": run.request.get("max_usd"), "estimate": est or None,
            "created": run.created, "started": run.started, "finished": run.finished,
            "exit_code": run.exit_code, "error": run.error,
            "source": source, "request": run.public_request(),
            "resume_available": run.state == "budget_exhausted" and bool(st.get("resume_command")),
            "links": {**links, **self._eval_links(run)}, "viewer_command": viewer_cmd,
            "imported": run.imported, "read_only": run.imported,
            "eval_state": (self.evals.get(run.run_id).state if self.evals.get(run.run_id) else None),
        }

    def _eval_links(self, run: Run) -> dict:
        rid = run.run_id
        links = {"eval": f"/api/runs/{rid}/eval", "eval_status": f"/api/runs/{rid}/eval/status",
                 "eval_page": f"/runs/{rid}/files/{EVAL_PAGE_REL}"}
        if (run.dir / "viewer" / "index.html").is_file():
            links["viewer_eval"] = f"/runs/{rid}/files/viewer/index.html#evaluation"
        return links

    def list(self) -> list[dict]:
        with self.lock:
            runs = sorted(self.runs.values(), key=lambda r: r.created or "", reverse=True)
        for r in runs:                       # imported / restarted runs: grounding when first listed
            self.auto_ground(r)
        return [self.describe(r) for r in runs]

    def detail(self, run: Run) -> dict:
        """GET /api/runs/<id>: describe() plus the evaluation summaries the guided UI renders
        (grounding = the free automatic check; score_state = where "Score this spec" stands)."""
        self.auto_ground(run)
        d = self.describe(run)
        agg = None
        if self.eval_refusal(run) is None:
            try:
                agg = self.eval_get(run.run_id)[1]
            except Exception:                # the run row must render even if aggregation fails
                agg = None
        d["grounding"] = self.grounding(run, agg)
        d["score_state"] = self.score_state(run, agg)
        return d

    def config(self) -> dict:
        return {"stills": available_stills(self.script), "max_concurrent": self.max_concurrent,
                "max_usd_ceiling": self.max_usd_ceiling,
                "default_model_id": self.base_env.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6"),
                "git_max_mb": float(self.base_env.get("DISTILL_GIT_MAX_MB", "500") or 500),
                "aws_profile": self.base_env.get("AWS_PROFILE") or None,
                "banner": "Synthetic or approved non-production code only."}

    def file_path(self, run_id: str, rel: str) -> Path:
        run = self.get(run_id)
        rel = unquote(rel).lstrip("/")
        if not (rel in SERVABLE_FILES or rel.startswith(SERVABLE_PREFIXES)) or ".." in rel.split("/"):
            raise ApiError(404, "not a served output")
        p = (run.dir / rel).resolve()
        if not p.is_relative_to(run.dir.resolve()) or not p.is_file():
            raise ApiError(404, "no such output")
        return p

    # viewer Evaluation page ---------------------------------------------------------------------
    def viewer_eval_page(self, run_id: str) -> bytes:
        """viewer/pages/evaluation.html for any run, rendered from the shared template with this
        run's API base. Served even for viewers generated before the page existed (and for
        read-only imported runs), so their Evaluation tab works without regenerating anything."""
        self.get(run_id)                                       # 404 for an unknown run
        html = EVAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
        return html.replace('"__EVAL_API_BASE__"', json.dumps(f"/api/runs/{run_id}")).encode("utf-8")

    @staticmethod
    def viewer_index(p: Path) -> bytes:
        """The viewer shell, plus an Evaluation tab when the shell predates it. The added script is
        inert unless the shell has the tab bar (#tabs) and loadPage() every generated shell has."""
        body = p.read_bytes()
        if b'data-page="pages/evaluation.html"' in body or b"</body>" not in body:
            return body
        tab = (b'<script>/* launcher: Evaluation tab for a viewer generated before it existed */'
               b'(function(){var t=document.getElementById("tabs");if(!t||typeof loadPage!=="function")return;'
               b'var s=document.createElement("span");s.className="sep";t.appendChild(s);'
               b'var b=document.createElement("button");b.className="tab-btn";'
               b'b.setAttribute("data-page","pages/evaluation.html");b.textContent="Evaluation";'
               b'b.onclick=function(){loadPage(b)};t.appendChild(b);'
               b'if(location.hash==="#evaluation")loadPage(b);})();</script>')
        i = body.rfind(b"</body>")
        return body[:i] + tab + body[i:]

    # evaluation ---------------------------------------------------------------------------------
    def _evals_dir(self, run_id: str) -> Path:
        return self.runs_dir / EVALS_DIRNAME / run_id

    def _load_evals(self) -> None:
        root = self.runs_dir / EVALS_DIRNAME
        for rid in self.runs:
            d = root / rid
            dirs = sorted((x for x in d.iterdir() if x.is_dir()), key=lambda x: x.name) if d.is_dir() else []
            jobs = []
            for jd in dirs:
                j = EvalJob.load(jd)
                if j is None:
                    continue
                if j.state in EVAL_ACTIVE:
                    j.state, j.error = "interrupted", "launcher restarted while this evaluation was active"
                    j.finished = j.finished or _now()
                    j.save()
                jobs.append(j)
            jobs.sort(key=lambda j: (str(j.created or ""), j.eval_id))
            if jobs:
                self.eval_history[rid] = jobs
                self.evals[rid] = jobs[-1]

    def _dataset_key(self, run_id: str | None) -> dict | None:
        """The run's own test data set as an answer-key option (id "dataset"), or None."""
        info = self.datasets.key_info(run_id) if run_id else None
        if not info:
            return None
        return {"id": "dataset", "suite": "dataset", "label": info["label"], "provisional": info["provisional"],
                "approved": info["approved"], "total": info["total"], "exists": True, "default_for_full": True,
                "path": None}

    def eval_options(self, run_id: str | None = None) -> dict:
        if run_id:
            self.get(run_id)                                   # 404 for an unknown run
        dk = self._dataset_key(run_id)
        suites = [{k: v for k, v in s.items()} for s in self.eval_suites.values()]
        keys = [{k: v for k, v in kk.items() if k != "abs_path"} for kk in self.eval_keys.values()]
        if dk:
            suites.insert(0, {"id": "dataset", "suite": "dataset", "corpus": "this run's test data set",
                              "description": dk["label"], "answer_key": "dataset", "judge_runs": 1,
                              "stages": ["corpus-sanity", "score-master-blend", "score-bottled"]})
            keys.insert(0, dk)
        return {"suites": suites, "answer_keys": keys, "dataset": dk,
                "judge_runs": {"default": 1, "max": EVAL_JUDGE_RUNS_MAX,
                               "note": "1 pass for a routine check; 3 for a number you intend to report"},
                "budget_ceiling_usd": self.max_usd_ceiling,
                "smoke": "free and deterministic: no model call, budget forced to $0, fails if any call is made",
                "legend": eval_view.LEGEND, "banner": eval_view.BANNER}

    def eval_get(self, run_id: str) -> tuple[int, dict]:
        run = self.get(run_id)
        # the same entry point serve_viewer.py's GET /api/eval uses, plus this launcher's eval jobs
        agg = viewer_api.eval_summary(str(run.dir), extra_dirs=[self._evals_dir(run_id)], run_id=run_id,
                                      known_keys=list(self.eval_keys.values()), repo=self.eval_repo)
        job = self.evals.get(run_id)
        agg["run"] = {"status": run.state, "imported": run.imported, "read_only": run.imported}
        agg["eval_job"] = job.public() if job else None
        agg["dataset"] = self.datasets.key_info(run_id)
        last_full = next((j for j in reversed(self.eval_history.get(run_id, [])) if j.mode == "full"), None)
        if (last_full is not None and last_full.suite == "dataset" and agg["dataset"]
                and agg["dataset"].get("note")):
            agg["key_note"] = agg["dataset"]["note"]     # "approved in bulk", shown on the scorecard
        if job is not None and job.provisional and job.mode != "smoke":
            # every estimated (judged) result of this evaluation carries the provisional label
            agg["provisional"] = job.key_label
            if isinstance(agg.get("estimated"), dict):
                agg["estimated"]["provisional"] = job.key_label
                for a in agg["estimated"].get("spec_artifacts") or []:
                    if isinstance(a, dict):
                        a["provisional"] = job.key_label
        return 200, agg

    def eval_status(self, run_id: str, tail: int = 40) -> tuple[int, dict]:
        self.get(run_id)
        job = self.evals.get(run_id)
        if job is None:
            return 200, {"run_id": run_id, "state": "idle", "log_tail": []}
        n = max(1, min(int(tail), 1000))
        return 200, {**job.public(), "log_tail": tail_lines(job.log, n)}

    def eval_compare(self, ids: list[str]) -> tuple[int, dict]:
        if len(ids) != 2 or not all(ids):
            raise ApiError(400, "runs must name exactly two run ids: ?runs=<id1>,<id2>")
        a = self.eval_get(ids[0])[1]
        b = self.eval_get(ids[1])[1]
        return 200, eval_view.compare(a, b)

    def _infer_suite(self, run: Run) -> str | None:
        rj = _read_json(run.dir / "run.json") or {}
        names = [(rj.get("env_snapshot") or {}).get("DISTILL_TARGET_NAME"), run.request.get("name"),
                 run.dir.name]
        best = None
        for sid, s in self.eval_suites.items():
            if sid == GENERIC_SUITE:
                continue
            for c in [s.get("corpus") or ""] + list(s.get("corpora") or []):
                if c and any(isinstance(nm, str) and (nm == c or nm.startswith(c + "-")) for nm in names):
                    if best is None or len(c) > len(self.eval_suites[best].get("corpus") or ""):
                        best = sid
        return best

    def eval_source(self, run: Run) -> tuple[str | None, str | None]:
        """(source_root, repo_root) of the run's own analysed source, when it is on disk: the
        suite is always pointed at the code this run analysed, never at a path baked into a
        suite config. repo_root is <run>/.source for a git run (key locations are repo-relative)."""
        src = self.dataset_source(run)
        if not src or not Path(src).is_dir():
            return None, None
        clone = run.dir / ".source"
        repo = str(clone) if (run.request.get("source") or {}).get("type") == "git" and clone.is_dir() else src
        return src, repo

    def _align_key(self, job: EvalJob, run: Run) -> str | None:
        """A subdir run scored against a repo-wide key: keep only key items located inside the
        analysed subdir, with their locations re-based onto it. -> path of the aligned key."""
        if job.config or not job.source_root or not job.repo_root or job.source_root == job.repo_root:
            return None
        k = self.eval_keys.get(job.answer_key or "")
        if not k or not k.get("exists"):
            return None
        try:
            sub = Path(job.source_root).resolve().relative_to(Path(job.repo_root).resolve()).as_posix()
        except ValueError:
            return None
        if sub in ("", "."):
            return None
        import yaml
        d = yaml.safe_load(Path(k["abs_path"]).read_text(encoding="utf-8")) or {}
        kept, excluded = [], 0
        pre = sub.rstrip("/") + "/"
        for it in d.get("items") or []:
            locs = [l for l in it.get("locations") or [] if str(l.get("file", "")).startswith(pre)]
            if not locs:
                excluded += 1
                continue
            it = dict(it)
            it["locations"] = [{**l, "file": l["file"][len(pre):]} for l in locs]
            kept.append(it)
        d["items"] = kept
        meta = d.setdefault("metadata", {}) if isinstance(d.get("metadata", {}), dict) else {}
        job.key_alignment = {"subdir": sub, "kept": len(kept), "excluded": excluded,
                             "note": f"{excluded} key item(s) lie outside the analysed folder {sub} "
                                     "and are not counted"}
        meta["subdir_alignment"] = job.key_alignment
        out = job.out_dir / "key.aligned.yaml"
        job.out_dir.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return str(out)

    def _eval_argv(self, job: EvalJob, run: Run, *, plan: bool) -> list[str]:
        cfg = job.config or self.eval_suites[job.suite]["config"]
        argv = self.eval_cmd + ["--config", str(self.eval_repo / cfg), "--var", f"run={run.dir}",
                                "--out", str(job.out_dir / ("plan" if plan else "suite"))]
        if job.source_root:
            argv += ["--var", f"source_root={job.source_root}", "--var", f"repo_root={job.repo_root}"]
        aligned = self._align_key(job, run)
        if aligned:
            argv += ["--var", f"key={aligned}"]
        if job.mode == "smoke":
            return argv + ["--smoke"]
        argv += ["--budget-usd", f"{job.budget_usd:.4f}", "--judge-runs", str(job.judge_runs)]
        return argv + (["--plan"] if plan else [])

    def _eval_env(self) -> dict:
        env = {k: v for k, v in self.base_env.items() if k not in RUN_ENV_KEYS}
        env["PYTHONUNBUFFERED"] = "1"
        return env

    @staticmethod
    def eval_refusal(run: Run) -> str | None:
        """Why this run cannot be evaluated (None = it can). The same rule as the form's
        canEvaluate(): finished with output; running, queued and output-less runs are refused."""
        if run.state in ("complete", "imported"):
            return None
        if run.state in ("failed", "budget_exhausted") and (run.dir / "viewer" / "index.html").is_file():
            return None
        if run.state in ("running", "queued", "estimating", "estimated"):
            return f"run {run.run_id} is {run.state}; evaluation becomes available when it completes"
        return f"run {run.run_id} is {run.state} and produced no output to evaluate"

    def eval_start(self, run_id: str, body, *, trigger: str = "user") -> tuple[int, dict]:
        run = self.get(run_id)
        dk = self._dataset_key(run_id)
        known_suites = {**self.eval_suites, **({"dataset": {"answer_key": "dataset"}} if dk else {})}
        known_keys = {**self.eval_keys, **({"dataset": {"suite": "dataset"}} if dk else {})}
        req = validate_eval_request(body, known_suites, known_keys, self.max_usd_ceiling)
        why = self.eval_refusal(run)
        if why:
            raise ApiError(409, why, status=run.state)
        suite = req.get("suite")
        key = req.get("answer_key")
        ds_cfg = ds_info = None
        # Full evaluation (and its plan) defaults to the run's own test data set; Quick check
        # (smoke) is unchanged unless the data set is asked for by id.
        if (dk and (key == "dataset" or suite == "dataset"
                    or (not key and not suite and not req["smoke"]))):
            if (key and key != "dataset") or (suite and suite != "dataset"):
                raise ApiError(400, "answer_key 'dataset' goes with suite 'dataset'", field="answer_key")
            try:
                ds_cfg, ds_info = self.datasets.suite_config(run_id, run.dir, self.dataset_template())
            except LD.DatasetError as e:
                raise ApiError(e.status, e.message, **e.extra)
            suite = key = "dataset"
        known = self.eval_keys
        if key and suite and suite != "dataset" and known[key]["suite"] != suite:
            raise ApiError(400, f"answer_key {key!r} is not the key of suite {suite!r} (its suite is "
                                f"{self.eval_keys[key]['suite']!r})", field="answer_key")
        suite = suite or (known[key]["suite"] if key else None) or self._infer_suite(run)
        if not suite and req["smoke"] and GENERIC_SUITE in self.eval_suites:
            suite = GENERIC_SUITE          # Quick check works for any finished run, key or not
        if suite == GENERIC_SUITE and not req["smoke"]:
            raise ApiError(400, "no answer key for this run yet: the generic suite has deterministic "
                                "checks only (use the Quick check). Build a test data set from the "
                                "run's code and tests to score the spec.", field="suite")
        if not suite:
            raise ApiError(400, "no suite/answer key matches this run's source; choose one of "
                                f"{', '.join(sorted(self.eval_suites)) or 'none configured'}. Without a key "
                                "only the Exact checks apply (shown from the run's own artifacts).",
                           field="suite")
        mode = "smoke" if req["smoke"] else ("plan" if req["plan_only"] else "full")
        eval_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{mode}-{secrets.token_hex(2)}"
        with self.lock:
            cur = self.evals.get(run_id)
            if cur is not None and cur.state in EVAL_ACTIVE:
                raise ApiError(409, f"an evaluation of run {run_id} is already {cur.state} "
                                    f"({cur.eval_id}); only one evaluation at a time per run",
                               eval_id=cur.eval_id, state=cur.state)
            job = EvalJob(eval_id, run_id, self._evals_dir(run_id) / eval_id, mode, suite,
                          key or self.eval_suites[suite].get("answer_key"), req["judge_runs"],
                          req["budget_usd"])
            job.trigger = trigger
            job.source_root, job.repo_root = self.eval_source(run)
            if ds_cfg is not None:
                job.config, job.key_label, job.provisional = str(ds_cfg), ds_info["label"], ds_info["provisional"]
            elif mode != "smoke":
                job.key_label = (self.eval_keys.get(job.answer_key) or {}).get("label")
            self.evals[run_id] = job                     # holds the per-run lock from here on
            self.eval_history.setdefault(run_id, []).append(job)
            job.save()
        try:
            if mode == "smoke":
                job.planned_usd = 0.0
                job.plan = {"planned_usd": 0.0, "note": "smoke: deterministic stages only, budget forced "
                                                        "to $0, run_suite.py exits 4 if any call is made"}
                self._eval_launch(job, run)
                return 202, job.public()
            self._eval_plan(job, run)
        except ApiError:
            raise
        except Exception as e:                           # never leave the per-run lock held
            with self.lock:
                job.state, job.error, job.finished = "failed", f"{type(e).__name__}: {e}", _now()
                job.save()
            raise
        if job.state == "over_budget":
            raise ApiError(409, f"planned evaluation cost ${job.planned_usd or 0:.2f} is over "
                                f"budget_usd ${job.budget_usd:.2f}; raise the budget (suggested "
                                f"${self._suggest(job.planned_usd):.2f}) or lower judge_runs. Nothing "
                                "was spent.", eval_id=job.eval_id, state=job.state,
                           planned_usd=job.planned_usd, budget_usd=job.budget_usd,
                           suggested_budget_usd=self._suggest(job.planned_usd), plan=job.plan)
        if job.state == "failed":
            raise ApiError(422, f"evaluation plan failed: {job.error}", eval_id=job.eval_id,
                           state=job.state, log_tail=tail_lines(job.log, 30))
        if mode == "plan":
            return 200, job.public()
        self._eval_launch(job, run)
        return 202, job.public()

    @staticmethod
    def _suggest(planned) -> float:
        return round(float(planned or 0) * 1.15 + 0.01, 2)

    def _eval_plan(self, job: EvalJob, run: Run) -> None:
        """run_suite.py --plan: deterministic stages run, LLM stages are priced. No model call."""
        argv = self._eval_argv(job, run, plan=True)
        job.out_dir.mkdir(parents=True, exist_ok=True)
        with open(job.log, "ab") as log:
            log.write(f"===== launcher: eval plan started {_now()} =====\n$ {' '.join(argv)}\n".encode())
            log.flush()
            try:
                rc = subprocess.run(argv, cwd=str(self.eval_repo), env=self._eval_env(), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    timeout=self.eval_plan_timeout_s).returncode
            except subprocess.TimeoutExpired:
                rc = -1
        report = _read_json(job.out_dir / "plan" / "suite.json")
        with self.lock:
            if report:
                job.plan = suite_plan_summary(report)
                job.planned_usd = job.plan.get("planned_usd")
            if rc == 3 or (report and (job.planned_usd or 0) > job.budget_usd + 1e-9):
                job.state, job.finished = "over_budget", _now()
            elif report is None or rc not in (0, 1):
                job.state, job.finished = "failed", _now()
                job.error = ("plan timed out" if rc == -1 else
                             f"run_suite.py --plan exited {rc}" + ("" if report else " without a suite.json"))
            else:
                job.state = "planned"
                if job.mode == "plan":
                    job.finished = _now()
            job.save()

    def _eval_launch(self, job: EvalJob, run: Run) -> None:
        argv = self._eval_argv(job, run, plan=False)
        job.out_dir.mkdir(parents=True, exist_ok=True)
        log = open(job.log, "ab")
        try:
            log.write(f"===== launcher: eval {job.mode} started {_now()} =====\n$ {' '.join(argv)}\n".encode())
            log.flush()
            proc = subprocess.Popen(argv, cwd=str(self.eval_repo), env=self._eval_env(),
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        finally:
            log.close()
        with self.lock:
            job.proc, job.state, job.started = proc, "running", _now()
            job.save()
        threading.Thread(target=self._eval_watch, args=(job,), daemon=True).start()

    def _eval_watch(self, job: EvalJob) -> None:
        rc = job.proc.wait()
        report = _read_json(job.out_dir / "suite" / "suite.json") or {}
        with self.lock:
            job.exit_code, job.finished, job.proc = rc, _now(), None
            job.verdict = report.get("verdict")
            job.spent_usd = (report.get("budget") or {}).get("spent_usd")
            if not report:
                job.state, job.error = "failed", f"run_suite.py exited {rc} without a suite.json"
            else:
                job.state = SUITE_EXIT_STATE.get(rc, "failed")
                if job.state == "failed":
                    job.error = report.get("verdict") or f"run_suite.py exited {rc}"
            job.save()

    # automatic grounding check and the guided "Score this spec" state --------------------------
    def auto_ground(self, run: Run) -> None:
        """Start the free grounding check (the smoke suite: deterministic, $0) once per finished run:
        when a run reaches complete, and for imported / earlier runs when first listed. Never for a
        run that cannot be evaluated, never while an evaluation is active, never when a smoke result
        already exists (a launcher job, or a smoke suite.json in the run directory)."""
        rid = run.run_id
        if not self.auto_grounding or rid in self._auto_fired:
            return
        if run.state not in ("complete", "imported") or self.eval_refusal(run):
            return
        with self.lock:
            if rid in self._auto_fired:
                return
            self._auto_fired.add(rid)
            if any(j.mode == "smoke" for j in self.eval_history.get(rid, [])):
                return
            cur = self.evals.get(rid)
            if cur is not None and cur.state in EVAL_ACTIVE:
                return
        try:
            found = eval_view.discover(run.dir, [], self.eval_repo)["suite"]
        except Exception:
            found = []
        if any((d.get("provenance") or {}).get("smoke") for _p, d, _o in found):
            return
        try:
            self.eval_start(rid, {"smoke": True}, trigger="auto")
        except ApiError as e:
            self._auto_errors[rid] = e.message
        except Exception as e:                       # never break the listing
            self._auto_errors[rid] = f"{type(e).__name__}: {e}"

    def _details_link(self, run: Run) -> str:
        links = self._eval_links(run)
        return links.get("viewer_eval") or links["eval_page"]

    @staticmethod
    def _xv(m):
        return m.get("value") if isinstance(m, dict) and m.get("status") == "measured" else None

    @staticmethod
    def _pctfmt(v) -> str:
        return f"{round(float(v), 1):g}%"

    def grounding(self, run: Run, agg: dict | None) -> dict:
        """The automatic check's result as one plain-language line, plus the facts behind it."""
        why = self.eval_refusal(run)
        if why:
            return {"state": "unavailable", "reason": why, "summary": None}
        smokes = [j for j in self.eval_history.get(run.run_id, []) if j.mode == "smoke"]
        job = smokes[-1] if smokes else None
        base = {"eval_id": job.eval_id if job else None, "trigger": job.trigger if job else None,
                "details": self._details_link(run), "free": True}
        if job is not None and job.state in EVAL_ACTIVE:
            return {**base, "state": "running", "summary": "Checking grounding… (free)"}
        on_disk = [s for s in (agg or {}).get("suites") or [] if s.get("mode") == "smoke"]
        if job is None and not on_disk:
            err = self._auto_errors.get(run.run_id)
            if err:
                return {**base, "state": "unavailable", "reason": err,
                        "summary": f"⚠ The grounding check could not start — {err}"}
            return {**base, "state": "none", "summary": "Not checked yet"}
        if job is not None and job.state != "complete":
            reason = job.error or job.state
            return {**base, "state": "failed", "reason": reason,
                    "summary": f"⚠ The grounding check did not finish — {reason}"}
        exit_code = job.exit_code if job is not None else on_disk[0].get("exit_code")
        verdict = job.verdict if job is not None else on_disk[0].get("verdict")
        E = (agg or {}).get("exact") or {}
        c = E.get("conservation") or {}
        specs = E.get("spec_artifacts") or []
        spec = next((a for a in specs if a.get("artifact") == "bottled_spec"), specs[0] if specs else None)
        g = E.get("graph") or {}
        lines, files = self._xv(c.get("lines")), self._xv(c.get("files"))
        total = self._xv((spec or {}).get("citations_total"))
        invalid = self._xv((spec or {}).get("citations_invalid"))
        gpct = self._xv(g.get("parser_confirmed_edge_precision_pct")) if g.get("status") == "measured" else None
        ok_parts, problems = [], []
        if lines is not None:
            if c.get("ok") is False:
                problems.append("not every source line was read")
            else:
                ok_parts.append(f"{int(lines):,} lines read" + (f" in {int(files):,} files" if files is not None else ""))
        if total is not None:
            if invalid:
                problems.append(f"{int(invalid):,} of {int(total):,} citations do not point at real code")
            else:
                ok_parts.append(f"all {int(total):,} citations point at real code")
        if gpct is not None:
            if g.get("gate_violations"):
                problems.append(f"the graph differs from the code parser ({self._pctfmt(gpct)} confirmed)")
            else:
                ok_parts.append(f"graph matches the code parser ({self._pctfmt(gpct)})")
        if exit_code not in (0, None) and not problems:
            problems.append(f"a check did not pass ({verdict or f'exit {exit_code}'})")
        state = "warning" if problems else "passed"
        summary = (("⚠ Needs attention — " + " · ".join(problems + ok_parts)) if problems else
                   ("✓ Grounded — " + (" · ".join(ok_parts) or "checks passed")))
        return {**base, "state": state, "summary": summary, "problems": problems,
                "lines": lines, "files": files, "citations_total": total, "citations_invalid": invalid,
                "graph_parser_match_pct": gpct, "verdict": verdict, "exit_code": exit_code,
                "finished": job.finished if job else on_disk[0].get("started_at")}

    def _shipped_key(self, run: Run) -> dict | None:
        """A curated answer key shipped with the repo (AP / JC / eShopOnWeb suites) for this code."""
        sid = self._infer_suite(run)
        if not sid or sid == GENERIC_SUITE:
            return None
        kid = self.eval_suites[sid].get("answer_key")
        k = self.eval_keys.get(kid or "")
        if not k or not k.get("exists"):
            return None
        path = k["abs_path"]
        if path not in self._key_items_n:
            items, _why = eval_view.load_key_items(Path(path))
            self._key_items_n[path] = len(items) if items is not None else None
        return {"suite": sid, "answer_key": kid, "label": k.get("label"), "rules": self._key_items_n[path]}

    def auto_cap(self, planned) -> float:
        """The spending cap the guided flow sets: the estimate plus 25% (at least $0.50)."""
        import math
        cap = max(0.5, math.ceil(float(planned or 0) * 1.25 * 100) / 100)
        return round(min(cap, self.max_usd_ceiling), 2)

    def _job_result(self, job: EvalJob) -> dict | None:
        """The judged score of one launcher evaluation (its own suite output, bottled spec first)."""
        root = job.out_dir / "suite"
        if not root.is_dir():
            return None
        cands = []
        for p in root.rglob("score.json"):
            d = _read_json(p)
            if isinstance(d, dict) and d.get("method") == "llm":
                cands.append((0 if "bottled" in str(p.relative_to(root)) else 1, str(p), p, d))
        if not cands:
            return None
        _r, _s, p, d = sorted(cands, key=lambda t: (t[0], t[1]))[0]
        est = eval_view.score_summary(d, p, "bottled_spec", "launcher")["estimated"]
        caught = total = None
        kpath, _how = eval_view.resolve_key(d.get("key"), self.eval_repo, list(self.eval_keys.values()))
        items, _why = eval_view.load_key_items(kpath)
        if items is not None:
            kc = d.get("key_coverage") or {}
            flagged = [it for it in items if it.get("flags")]
            total = len(flagged)
            caught = sum(1 for it in flagged if kc.get(it.get("id")) in ("full", "partial"))
        return {"metric": est["metrics"]["recall_any_match_pct"], "traps_caught": caught,
                "traps_total": total, "judge_runs": est.get("judge_runs"), "source": str(p)}

    def _agg_result(self, agg: dict | None) -> dict | None:
        S = (agg or {}).get("estimated") or {}
        judged = [a for a in S.get("spec_artifacts") or [] if a.get("method") == "llm"]
        if not judged:
            return None
        a = next((x for x in judged if x.get("artifact") == "bottled_spec"), judged[0])
        caught = total = None
        t = S.get("traps") or {}
        if t.get("status") == "measured":
            tt = (t.get("totals") or {}).get(a["artifact"]) or {}
            caught = (tt.get("full") or 0) + (tt.get("partial") or 0)
            total = caught + (tt.get("missed") or 0)
        return {"metric": (a.get("metrics") or {}).get("recall_any_match_pct"), "traps_caught": caught,
                "traps_total": total, "judge_runs": a.get("judge_runs"), "source": a.get("source")}

    def _result_line(self, r: dict | None, run: Run, provisional) -> dict | None:
        m = (r or {}).get("metric") or {}
        if m.get("status") != "measured" or self._xv(m) is None:
            return None
        v = float(m["value"])
        rng, basis, width = None, None, None
        if m.get("ci95"):
            rng, basis = [float(m["ci95"][0]), float(m["ci95"][1])], "95% confidence interval"
            width = round(rng[1] - rng[0], 1)
        elif isinstance(m.get("across_runs"), dict) and m["across_runs"].get("min") is not None:
            rng, basis = [float(m["across_runs"]["min"]), float(m["across_runs"]["max"])], "across grading passes"
        elif isinstance(m.get("noise_band"), (int, float)):
            b = float(m["noise_band"])
            rng, basis = [max(0.0, v - b), min(100.0, v + b)], "measured grading noise"
        jr = r.get("judge_runs") if isinstance(r.get("judge_runs"), (int, float)) else None
        text = f"Scored: {self._pctfmt(v)} of known rules found"
        if rng:
            text += f" (range {round(rng[0], 1):g}–{round(rng[1], 1):g}%)"
        if r.get("traps_total"):
            text += f" · {r['traps_caught']} of {r['traps_total']} planted problems caught"
        return {"recall_pct": v, "range": rng, "range_basis": basis, "ci_width": width,
                "traps_caught": r.get("traps_caught"), "traps_total": r.get("traps_total"),
                "judge_runs": jr, "provisional": provisional or None, "summary": text,
                "double_check": bool(width is not None and width > 15 and (jr or 1) < 3),
                "details": self._details_link(run), "source": r.get("source")}

    def score_state(self, run: Run, agg: dict | None) -> dict:
        """Where the guided "Score this spec" flow stands, so the UI never recomputes it:
        key readiness (none | draft | approved | shipped), the latest plan, the latest scoring
        job and its result, and the step to show (key | cost | running | results)."""
        rid = run.run_id
        why = self.eval_refusal(run)
        ds = self.datasets.status(rid)
        counts = ds.get("counts") or {}
        ki = ds.get("key")
        dsinfo = {"state": ds.get("state"), "error": ds.get("error"),
                  **{k: counts.get(k) for k in ("total", "corroborated", "approved", "reviewed")}}
        shipped = self._shipped_key(run)
        if ki and not ki.get("provisional"):
            key = {"readiness": "approved", "rules": ki.get("approved"), "label": ki.get("label"),
                   "use": {"suite": "dataset", "answer_key": "dataset"}, "provisional": False,
                   "bulk_approved": ki.get("bulk_approved") or 0, "note": ki.get("note")}
        elif shipped:
            key = {"readiness": "shipped", "rules": shipped["rules"], "label": shipped["label"],
                   "use": {"suite": shipped["suite"], "answer_key": shipped["answer_key"]}, "provisional": False}
        elif ki:
            key = {"readiness": "draft", "rules": 0, "label": ki.get("label"),
                   "use": {"suite": "dataset", "answer_key": "dataset"}, "provisional": True}
        else:
            key = {"readiness": "none", "rules": 0, "label": None, "use": None, "provisional": False}
        key["building"] = ds.get("state") == "building"
        key["dataset"] = dsinfo
        hist = self.eval_history.get(rid, [])
        plans = [j for j in hist if j.mode == "plan"]
        fulls = [j for j in hist if j.mode == "full"]
        plan, full = (plans[-1] if plans else None), (fulls[-1] if fulls else None)
        plan_d = None
        if plan is not None:
            plan_d = {"eval_id": plan.eval_id, "state": plan.state, "planned_usd": plan.planned_usd,
                      "cap_usd": self.auto_cap(plan.planned_usd) if plan.planned_usd is not None else None,
                      "judge_runs": plan.judge_runs, "suite": plan.suite, "answer_key": plan.answer_key,
                      "provisional": plan.provisional, "key_label": plan.key_label, "error": plan.error,
                      "current": full is None or str(plan.created) > str(full.created)}
        scoring = None
        if full is not None:
            scoring = {"eval_id": full.eval_id, "state": full.state, "spent_usd": full.live_spend(),
                       "budget_usd": full.budget_usd, "planned_usd": full.planned_usd,
                       "judge_runs": full.judge_runs, "provisional": full.provisional,
                       "key_label": full.key_label, "error": full.error, "verdict": full.verdict}
        result = None
        if full is not None and full.state == "complete":
            result = self._result_line(self._job_result(full), run, full.key_label if full.provisional else None)
        if result is None and (full is None or full.state == "complete"):
            result = self._result_line(self._agg_result(agg), run, (agg or {}).get("provisional"))
        if result is not None:
            result["note"] = (agg or {}).get("key_note")
        cur = self.evals.get(rid)
        if scoring and scoring["state"] in EVAL_ACTIVE:
            step = "running"
        elif plan_d and plan_d["current"] and plan.state == "planned":
            step = "cost"
        elif result:
            step = "results"
        elif key["readiness"] in ("approved", "shipped"):
            step = "cost"
        else:
            step = "key"
        return {"can_score": why is None, "refusal": why, "key": key, "plan": plan_d,
                "scoring": scoring, "result": result, "step": step,
                "busy": (cur.mode if cur is not None and cur.state in EVAL_ACTIVE else None),
                "provisional": bool(key["provisional"] or (result or {}).get("provisional")),
                "judge_runs_double_check": 3}

    # test data sets -----------------------------------------------------------------------------
    def dataset_template(self) -> Path:
        """The suite a run's ad-hoc data-set suite is derived from (deterministic + keyed stages)."""
        return self.eval_repo / "eval" / "ci" / "suites" / "eshoponweb.yaml"

    def dataset_source(self, run: Run) -> str | None:
        """The run's own copy of its SOURCE (never its output): <run>/.source/<subdir> for a git run,
        the path for a path run, the recorded DISTILL_TARGET for an imported run. None = unknown."""
        src = run.request.get("source") or {}
        prov = _read_json(run.dir / "source_provenance.json") or {}
        if src.get("type") == "git":
            sub = src.get("subdir") or prov.get("subdir") or ""
            clone = run.dir / ".source" / sub
            if clone.is_dir():
                return str(clone)
            t = prov.get("target")
            return t if isinstance(t, str) and os.path.isabs(t) else None
        if run.imported:
            rj = _read_json(run.dir / "run.json") or {}
            t = (rj.get("env_snapshot") or {}).get("DISTILL_TARGET")
            if not t:
                t = ((_read_json(run.dir / "run_manifest.json") or {}).get("source") or {}).get("path")
            return t if isinstance(t, str) and t else None
        p = src.get("path")
        return p if isinstance(p, str) and p else None

    def dataset_start(self, run_id: str, body) -> tuple[int, dict]:
        run = self.get(run_id)
        try:
            opts = self.datasets.validate_build(body)
        except LD.DatasetError as e:
            raise ApiError(e.status, e.message, **e.extra)
        why = self.eval_refusal(run)
        if why:
            raise ApiError(409, why.replace("evaluation", "a test data set"), status=run.state)
        try:
            ds = self.datasets.start(run_id, run.dir, self.dataset_source(run), opts,
                                     corpus=f"dataset-{run_id}"[:64], env=self._eval_env())
        except LD.DatasetError as e:
            raise ApiError(e.status, e.message, **e.extra)
        return 202, self.datasets.status(run_id)

    def dataset_status(self, run_id: str) -> tuple[int, dict]:
        self.get(run_id)
        return 200, self.datasets.status(run_id)

    def dataset_items(self, run_id: str, query: dict) -> tuple[int, dict]:
        self.get(run_id)
        one = lambda k: (query.get(k) or [""])[0]
        try:
            page = int(one("page") or "1")
        except ValueError:
            raise ApiError(400, "page must be an integer", field="page")
        try:
            return 200, self.datasets.list_items(run_id, status=one("status"), source=one("source"),
                                                 q=one("q"), page=page)
        except LD.DatasetError as e:
            raise ApiError(e.status, e.message, **e.extra)

    def dataset_review(self, run_id: str, body) -> tuple[int, dict]:
        self.get(run_id)
        try:
            res = self.datasets.review(run_id, body)
        except LD.DatasetError as e:
            raise ApiError(e.status, e.message, **e.extra)
        return 200, {**res, "key": self.datasets.key_info(run_id)}


# ── HTTP ────────────────────────────────────────────────────────────────────────────────────
def _route_parts(path: str) -> list[str]:
    """Path segments, with the embedded viewer's per-run form folded in.

    A viewer generated with --api-base /api/runs/<id> fetches API_BASE + "/api/<route>", i.e.
    /api/runs/<id>/api/<route>; that is the same route as /api/runs/<id>/<route>. Only the
    segment list changes: every request has already passed _guard (Host, gate) and, for a POST,
    _origin_ok, and the run id is still looked up by Launcher.get."""
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 5 and parts[:2] == ["api", "runs"] and parts[3] == "api":
        parts = parts[:3] + parts[4:]
    return parts


class LauncherHandler(BaseHTTPRequestHandler):
    launcher: Launcher = None  # type: ignore[assignment]
    allowed_hosts: set[str] = set()
    cors_origins: set[str] = set()
    gate_code: str | None = None          # None ⇒ gate disabled (today's behavior)
    gate_origins: set[str] = set()        # https://<name> for each DISTILL_ALLOWED_HOSTS entry
    sessions: "SessionStore | None" = None
    server_version = "DistillLauncher/1"
    MAX_BODY = 64 * 1024

    def log_message(self, fmt, *args):  # quiet, but keep errors visible
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("launcher: " + (fmt % args) + "\n")

    # helpers
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return _bare_host(host) in self.allowed_hosts or is_loopback(_bare_host(host))

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        bare = _bare_host(host)                   # port/bracket-normalized, matching _host_ok
        allowed_self = {f"http://{host}"}         # existing behavior preserved
        if bare in self.allowed_hosts:            # only trusted fronting hosts get the https:// form
            allowed_self.add(f"https://{bare}")
        return origin in allowed_self or origin in self.cors_origins or origin in self.gate_origins

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin in self.cors_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, status: int, payload, ctype: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, e: ApiError) -> None:
        self._send(e.status, {"error": e.message, **e.extra})

    # ── Shared-access-code gate (inert unless gate_code is set) ──────────────────────────────
    def _authed(self) -> bool:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get("__Host-distill_session")
        token = morsel.value if morsel else ""
        return bool(self.sessions and self.sessions.valid(token))

    def path_is_healthz(self) -> bool:
        return self.command == "GET" and urlparse(self.path).path == "/healthz"

    def _wants_html(self) -> bool:
        # Known HTML routes are authoritative — they must get the challenge page even when the
        # client sends no Accept header (the test harness sends none; some fetch/browsers send
        # Accept: */* which does NOT contain "text/html").
        if urlparse(self.path).path in ("/", "/index.html"):
            return True
        return "text/html" in (self.headers.get("Accept") or "")

    def _gate(self) -> bool:
        if self.command == "OPTIONS":           # preflight is never gated
            return True
        if self.gate_code is None:              # gate disabled; today's behavior
            return True
        if self._authed():
            return True
        if self.command == "POST" and urlparse(self.path).path == "/api/auth":
            return True                         # let do_POST process the login (own screening)
        if self._wants_html():
            self._send(200, ACCESS_UI_FILE.read_bytes(), "text/html; charset=utf-8")
            return False
        self._send(401, {"error": "authentication required", "login": "/api/auth"})
        return False

    def _guard(self) -> bool:
        if self.path_is_healthz():          # GET /healthz only — fully exempt, BEFORE the host check
            return True
        if not self._host_ok():
            self._send(403, {"error": "Host header not allowed (loopback only)"})
            return False
        if not self._gate():                # no-op when gate_code is None OR method is OPTIONS
            return False
        return True

    def _viewer_body(self, route: str, raw: bytes) -> dict:
        """Parse + gate the body of a per-run viewer POST (chat/validate/dependencies/translate).

        Enforces application/json, a per-route field allowlist (unknown key → 400), and the
        existing credential-field rejection. The existing validate_request/credential_fields
        only run on POST /api/runs, so without this gate the new routes would get no screening.
        """
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        try:
            body = json.loads(raw or b"null")
        except json.JSONDecodeError:
            raise ApiError(400, "invalid JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "request body must be a JSON object")
        unknown = sorted(set(body) - VIEWER_ROUTE_FIELDS[route])
        if unknown:
            raise ApiError(400, f"unknown field(s) for {route}: {', '.join(unknown)}", fields=unknown)
        creds = credential_fields(body)
        if creds:
            raise ApiError(400, "credential fields are not accepted: " + ", ".join(sorted(creds)) +
                           ". The launcher uses its own process environment / AWS profile only.",
                           fields=sorted(creds))
        return body

    # verbs
    def do_OPTIONS(self):
        if not self._guard():
            return
        self.send_response(204 if self.headers.get("Origin") in self.cors_origins else 403)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        if u.path == "/healthz":                                   # NEW — first, unshadowed
            return self._send(200, {"status": "ok"})
        parts = _route_parts(u.path)
        L = self.launcher
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/config":
                self._send(200, L.config())
            elif parts == ["api", "runs"]:
                self._send(200, {"runs": L.list()})
            elif len(parts) == 3 and parts[:2] == ["api", "runs"]:
                self._send(200, L.detail(L.get(parts[2])))
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "log":
                q = parse_qs(u.query)
                try:
                    n = int((q.get("tail") or ["200"])[0])
                except ValueError:
                    raise ApiError(400, "tail must be an integer")
                self._send(*L.log(parts[2], n))
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "resume":
                self._send(*L.resume(parts[2]))
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "progress":
                run = L.get(parts[2])  # 404 if unknown run
                import progress_server  # deferred, matching the file's own deferred-import style
                self._send(200, progress_server.get_progress(str(run.dir)))
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "tpm":
                run = L.get(parts[2])  # 404 if unknown run
                import telemetry
                self._send(200, telemetry.aggregate_tpm(str(run.dir / "telemetry")))
            elif parts == ["api", "eval", "options"]:
                rid = (parse_qs(u.query).get("run") or [""])[0]
                self._send(200, L.eval_options(rid or None))
            elif len(parts) in (4, 5) and parts[:2] == ["api", "runs"] and parts[3] == "dataset" \
                    and (len(parts) == 4 or parts[4] == "status"):
                self._send(*L.dataset_status(parts[2]))
            elif len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3:] == ["dataset", "items"]:
                self._send(*L.dataset_items(parts[2], parse_qs(u.query)))
            elif parts == ["api", "eval", "compare"]:
                q = parse_qs(u.query)
                ids = [x.strip() for x in ",".join(q.get("runs") or []).split(",") if x.strip()]
                self._send(*L.eval_compare(ids))
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "eval":
                self._send(*L.eval_get(parts[2]))
            elif len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3:] == ["eval", "status"]:
                q = parse_qs(u.query)
                try:
                    n = int((q.get("tail") or ["40"])[0])
                except ValueError:
                    raise ApiError(400, "tail must be an integer")
                self._send(*L.eval_status(parts[2], n))
            elif len(parts) >= 4 and parts[0] == "runs" and parts[2] == "files":
                rel = unquote("/".join(parts[3:])).lstrip("/")
                if rel == EVAL_PAGE_REL:          # always the current page, for every viewer
                    return self._send(200, L.viewer_eval_page(parts[1]), "text/html; charset=utf-8")
                p = L.file_path(parts[1], "/".join(parts[3:]))
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                if p.suffix in (".md", ".log", ".tsv", ".txt"):
                    ctype = "text/plain; charset=utf-8"
                if rel == "viewer/index.html":
                    return self._send(200, L.viewer_index(p), ctype)
                self._send(200, p.read_bytes(), ctype)
            else:
                raise ApiError(404, "not found")
        except ApiError as e:
            self._error(e)
        except Exception as e:  # never drop the connection without a JSON answer
            sys.stderr.write(f"launcher: internal error on {self.command} {self.path}: {e!r}\n")
            self._send(500, {"error": f"internal error: {type(e).__name__}"})

    def do_POST(self):
        if not self._guard():
            return
        if not self._origin_ok():
            return self._send(403, {"error": "cross-origin request refused (see --cors-origin)"})
        u = urlparse(self.path)
        parts = _route_parts(u.path)
        L = self.launcher
        try:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "Content-Length must be an integer")
            if length < 0:                       # read(-1) would block until the client hangs up
                raise ApiError(400, "Content-Length must not be negative")
            if length > self.MAX_BODY:
                raise ApiError(413, "request body too large")
            raw = self.rfile.read(length) if length else b""
            if parts == ["api", "auth"]:
                # Shared-access-code login. Screened inline (NOT via credential_fields/
                # validate_request) so the code — which legitimately looks credential-ish — is
                # accepted on this one path only. The submitted value is never logged.
                if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                    raise ApiError(415, "Content-Type must be application/json")
                try:
                    body = json.loads(raw or b"null")
                except json.JSONDecodeError:
                    raise ApiError(400, "invalid JSON")
                if not isinstance(body, dict) or set(body) != {"code"} or not isinstance(body.get("code"), str):
                    raise ApiError(400, "body must be a JSON object with a single string field \"code\"")
                ok = self.sessions is not None and self.gate_code is not None and \
                    hmac.compare_digest(body["code"].encode(), self.gate_code.encode())
                if not ok:
                    return self._send(401, {"error": "incorrect access code"})
                token = self.sessions.issue()
                jar = http.cookies.SimpleCookie()
                jar["__Host-distill_session"] = token
                m = jar["__Host-distill_session"]
                m["path"] = "/"
                m["httponly"] = True
                m["secure"] = True
                m["samesite"] = "Lax"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                payload = json.dumps({"ok": True}).encode("utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Set-Cookie", m.OutputString())
                self._cors()
                self.end_headers()
                self.wfile.write(payload)
                return
            if parts == ["api", "runs"]:
                if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                    raise ApiError(415, "Content-Type must be application/json")
                try:
                    body = json.loads(raw or b"null")
                except json.JSONDecodeError:
                    raise ApiError(400, "invalid JSON")
                self._send(*L.create(body))
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "cancel":
                self._send(*L.cancel(parts[2]))
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "start":
                self._send(*L.start(parts[2]))
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "eval":
                if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                    raise ApiError(415, "Content-Type must be application/json")
                try:
                    body = json.loads(raw or b"null")
                except json.JSONDecodeError:
                    raise ApiError(400, "invalid JSON")
                self._send(*L.eval_start(parts[2], body))
            elif (len(parts) in (4, 5) and parts[:2] == ["api", "runs"] and parts[3] == "dataset"
                  and (len(parts) == 4 or parts[4] == "review")):
                if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                    raise ApiError(415, "Content-Type must be application/json")
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    raise ApiError(400, "invalid JSON")
                creds = credential_fields(body) if isinstance(body, dict) else []
                if creds:
                    raise ApiError(400, "credential fields are not accepted: " + ", ".join(sorted(creds)) +
                                   ". The launcher uses its own process environment / AWS profile only.",
                                   fields=sorted(creds))
                if len(parts) == 4:
                    self._send(*L.dataset_start(parts[2], body))
                else:
                    self._send(*L.dataset_review(parts[2], body))
            elif (len(parts) == 4 and parts[:2] == ["api", "runs"]
                  and parts[3] in VIEWER_ROUTE_FIELDS):
                # Per-run viewer routes: chat / validate / dependencies / translate. The route
                # set is disjoint from {cancel, start}, so this elif may sit anywhere in the
                # ladder without shadowing. _origin_ok() and MAX_BODY already applied above.
                route = parts[3]
                body = self._viewer_body(route, raw)
                run = L.get(parts[2])  # 404 if unknown run
                vector_db, _viewer_dir, source_root, repo_url = L._run_paths(run)
                if route != "translate" and not vector_db.is_file():
                    raise ApiError(404, "no vector store for this run yet "
                                        "(stills/shared_vectors.db is missing)")
                if route == "chat":
                    result = viewer_api.chat(str(vector_db), body.get("question", "").strip(),
                                             body.get("top_k", 15))
                elif route == "validate":
                    result = viewer_api.validate(str(vector_db), source_root, repo_url,
                                                 body.get("block", ""), body.get("title", ""),
                                                 body.get("evidence", ""))
                elif route == "dependencies":
                    result = viewer_api.dependencies(str(vector_db), body.get("title", ""),
                                                     body.get("still") or None)
                else:  # translate
                    result = viewer_api.translate(body.get("code", ""), body.get("language", ""))
                # 200 even on handled model/credential errors (error is in the body), mirroring
                # serve_viewer.py, so the browser's r.json() never throws.
                self._send(200, result)
            else:
                raise ApiError(404, "not found")
        except ApiError as e:
            self._error(e)
        except Exception as e:  # never drop the connection without a JSON answer
            sys.stderr.write(f"launcher: internal error on {self.command} {self.path}: {e!r}\n")
            self._send(500, {"error": f"internal error: {type(e).__name__}"})


def make_server(launcher: Launcher, host: str = "127.0.0.1", port: int = 8090,
                allow_non_loopback: bool = False, cors_origins: list[str] | None = None,
                _bind_checked: bool = False,
                *, access_code: str | None = None, allowed_hosts_extra: list[str] | None = None,
                session_ttl: int = 43200) -> ThreadingHTTPServer:
    if not _bind_checked:
        check_bind(host, allow_non_loopback)
    handler = type("BoundLauncherHandler", (LauncherHandler,), {
        "launcher": launcher,
        "allowed_hosts": ({host.lower(), f"[{host.lower()}]"} if allow_non_loopback else set())
                         | {h.lower() for h in (allowed_hosts_extra or [])},
        "cors_origins": set(cors_origins or []),
        "gate_code": access_code or None,
        "gate_origins": {f"https://{h.lower()}" for h in (allowed_hosts_extra or [])},
        "sessions": SessionStore(ttl=session_ttl),
    })
    server_cls = ThreadingHTTPServer
    if ":" in host.strip("[]"):              # an IPv6 literal such as ::1 (AF_INET cannot bind it)
        server_cls = type("ThreadingHTTPServerV6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
    srv = server_cls((host.strip("[]"), port), handler)
    srv.daemon_threads = True
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Local launcher API + form for run_distillation.sh")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1, loopback only)")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--allow-non-loopback", action="store_true",
                    help="permit a non-loopback --host (prints a warning; never expose publicly)")
    ap.add_argument("--max-concurrent", type=int, default=1, help="runs at once; more are queued (default 1)")
    ap.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR), help="per-run output dirs (default output/runs)")
    ap.add_argument("--script", default=str(DEFAULT_SCRIPT), help=argparse.SUPPRESS)
    ap.add_argument("--max-usd-ceiling", type=float, default=500.0,
                    help="largest max_usd a request may ask for (default 500)")
    ap.add_argument("--estimate-timeout", type=int, default=1800, help="seconds for the pre-run estimate")
    ap.add_argument("--cors-origin", action="append", default=[],
                    help="allow this browser origin (e.g. http://localhost:3000) to call the API")
    ap.add_argument("--session-ttl", type=positive_int, default=43200,
                    help="session lifetime in seconds (default 43200 = 12h)")
    ap.add_argument("--import-run", action="append", default=[], metavar="DIR",
                    help="register an existing completed output directory (e.g. output/ap-invoices-demo) "
                         "as a read-only run; repeatable. Nothing is written into it.")
    ap.add_argument("--eval-python", default=sys.executable,
                    help="Python that runs eval/ci/run_suite.py (needs PyYAML; default: this one)")
    ap.add_argument("--no-auto-check", action="store_true",
                    help="do not start the free grounding check (smoke suite) automatically when a "
                         "run completes or an imported run is first listed")
    a = ap.parse_args(argv)
    check_bind(a.host, a.allow_non_loopback)        # before any side effect
    try:
        launcher = Launcher(a.script, a.runs_dir, a.max_concurrent, estimate_timeout_s=a.estimate_timeout,
                            max_usd_ceiling=a.max_usd_ceiling, import_runs=a.import_run,
                            eval_cmd=[a.eval_python, str(REPO / "eval" / "ci" / "run_suite.py")],
                            auto_grounding=not a.no_auto_check)
    except ValueError as e:
        raise SystemExit(f"launcher: {e}")
    srv = make_server(launcher, a.host, a.port, a.allow_non_loopback, a.cors_origin, _bind_checked=True,
                      access_code=os.environ.get("DISTILL_ACCESS_CODE") or None,
                      allowed_hosts_extra=_parse_allowed_hosts(os.environ.get("DISTILL_ALLOWED_HOSTS")),
                      session_ttl=a.session_ttl)
    host = f"[{a.host}]" if ":" in a.host else a.host
    print(f"Distillation launcher on http://{host}:{srv.server_address[1]}/  (runs in {launcher.runs_dir})")
    print("  Synthetic or approved non-production code only. AWS credentials come from this "
          f"process's environment (AWS_PROFILE={os.environ.get('AWS_PROFILE') or '<unset>'}).", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
