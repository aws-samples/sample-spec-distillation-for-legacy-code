# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Support code for ``eval/ci/run_suite.py``: config, provenance, budget, evaluators, baseline.

Every evaluator is treated as a **black-box CLI**. ``run_suite.py`` never imports
``eval/score.py``, ``eval/graph_eval.py`` or anything under ``eval/benefits/``; it
probes each one's ``--help`` at runtime and only passes flags that the tool
advertises. A tool that is missing, or that has dropped a flag the stage needs,
produces a recorded stage status instead of a traceback.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except Exception:                                              # pragma: no cover
    yaml = None

SUITE_SCHEMA_VERSION = 1
CONFIG_SCHEMA_VERSION = 1
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
DEFAULT_REGION = "us-east-1"
#: bedrock_util.DEFAULT_LOG: the per-machine running log (gitignored). The suite also gives
#: every stage its own log (EVAL_BEDROCK_LOG=<stage out>/bedrock_calls.jsonl) and watches this
#: one only as a backstop for a tool that ignores the variable.
SHARED_BEDROCK_LOG = "eval/logs/local/bedrock_calls.jsonl"
#: committed, frozen record of historical calls; never written, never charged
FROZEN_BEDROCK_LOG = "eval/logs/bedrock_calls.jsonl"
#: judge passes for score.py stages when neither the stage nor the suite pins ``judge_runs``.
#: score.py itself defaults to 3; CI suites default to 1 so a routine run costs one pass.
DEFAULT_SUITE_JUDGE_RUNS = 1

# Keys whose values change on every run; stripped by canonical() so two runs of the
# same suite can be compared for determinism.
VOLATILE_KEYS = {
    "started_at", "finished_at", "duration_s", "generated_at", "wall_time_s",
    "out_dir", "log", "baseline", "suite_json", "repo_root", "ts", "latency_s",
    "result_sha256",      # result files embed their own output paths / timestamps
}


# --------------------------------------------------------------------------- hashing
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes()) if p.is_file() else ""


def tree_hash(root: Path, exclude: tuple[str, ...] = ()) -> dict:
    """Content hash of a corpus: order-independent, excludes answer keys if asked."""
    h = hashlib.sha256()
    n = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(e == rel or e == p.name for e in exclude):
            continue
        h.update(rel.encode() + b"\0" + sha256_file(p).encode() + b"\n")
        n += 1
    return {"sha256": h.hexdigest(), "n_files": n}


# --------------------------------------------------------------------------- config
class ConfigError(Exception):
    pass


REQUIRED_TOP = ("suite", "schema_version", "corpora", "stages")


def interpolate(obj, vars_: dict):
    if isinstance(obj, str):
        def sub(m):
            k = m.group(1)
            if k not in vars_:
                raise ConfigError(f"unknown variable ${{{k}}}")
            return str(vars_[k])
        return re.sub(r"\$\{([A-Za-z0-9_]+)\}", sub, obj)
    if isinstance(obj, dict):
        return {k: interpolate(v, vars_) for k, v in obj.items()}
    if isinstance(obj, list):
        return [interpolate(v, vars_) for v in obj]
    return obj


def load_config(path: Path, overrides: dict[str, str] | None = None) -> dict:
    if yaml is None:                                           # pragma: no cover
        raise ConfigError("PyYAML is required to read suite configs")
    if not path.is_file():
        raise ConfigError(f"suite config not found: {path}")
    try:
        cfg = yaml.safe_load(path.read_text())
    except Exception as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    missing = [k for k in REQUIRED_TOP if k not in cfg]
    if missing:
        raise ConfigError(f"{path}: missing required field(s): {', '.join(missing)}")
    if cfg["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise ConfigError(f"{path}: schema_version {cfg['schema_version']} is not supported "
                          f"(this run_suite.py reads {CONFIG_SCHEMA_VERSION})")
    vars_ = dict(cfg.get("vars") or {})
    vars_.update(overrides or {})
    # vars may refer to other vars (e.g. insights_spec: ${run}/issues/issues.md); resolve them
    # to a fixed point so that --var run=... also moves everything derived from ${run}
    for _ in range(10):
        nxt = {k: interpolate(v, vars_) for k, v in vars_.items()}
        if nxt == vars_:
            break
        vars_ = nxt
    else:
        raise ConfigError(f"{path}: vars refer to each other in a cycle")
    cfg = interpolate({k: v for k, v in cfg.items() if k != "vars"}, vars_)
    cfg["vars"] = vars_
    if not isinstance(cfg["corpora"], dict) or not cfg["corpora"]:
        raise ConfigError(f"{path}: 'corpora' must be a non-empty mapping")
    for name, c in cfg["corpora"].items():
        for k in ("source_root", "key"):
            if k not in c:
                raise ConfigError(f"{path}: corpus '{name}' is missing '{k}'")
    if not isinstance(cfg["stages"], list) or not cfg["stages"]:
        raise ConfigError(f"{path}: 'stages' must be a non-empty list")
    seen = set()
    for st in cfg["stages"]:
        for k in ("id", "evaluator"):
            if k not in st:
                raise ConfigError(f"{path}: every stage needs '{k}' (offender: {st})")
        if st["id"] in seen:
            raise ConfigError(f"{path}: duplicate stage id '{st['id']}'")
        seen.add(st["id"])
        if st.get("corpus") and st["corpus"] not in cfg["corpora"]:
            raise ConfigError(f"{path}: stage '{st['id']}' names unknown corpus '{st['corpus']}'")
    for t in cfg.get("thresholds") or []:
        for k in ("stage", "metric"):
            if k not in t:
                raise ConfigError(f"{path}: every threshold needs '{k}' (offender: {t})")
        if t["stage"] not in seen:
            raise ConfigError(f"{path}: threshold names unknown stage '{t['stage']}'")
        if not any(k in t for k in ("min", "max", "equals")):
            raise ConfigError(f"{path}: threshold {t['stage']}/{t['metric']} needs min, max or equals")
    cfg.setdefault("budget_usd", 1.0)
    cfg.setdefault("seeds", {})
    jr = cfg.setdefault("judge_runs", DEFAULT_SUITE_JUDGE_RUNS)
    for st in [cfg] + list(cfg["stages"]):
        v = st.get("judge_runs", jr)
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ConfigError(f"{path}: judge_runs must be a positive integer (got {v!r})")
    return cfg


# --------------------------------------------------------------------------- provenance
def git_info(repo: Path) -> dict:
    def run(*a):
        try:
            r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                               text=True, timeout=20)
            return r.stdout.strip()
        except Exception:
            return ""
    return {"commit": run("rev-parse", "HEAD"),
            "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(run("status", "--porcelain"))}


def bedrock_region(repo: Path) -> tuple[str, str]:
    """The region eval/bedrock_util.py actually sends calls to, and where that came from.

    Read from the module itself (``REGION``), not from AWS_DEFAULT_REGION / AWS_REGION:
    bedrock_util pins its client's region, so the shell's default region says nothing
    about where the calls went (on the JC host it said us-west-2; the calls went to
    us-east-1). bedrock_util imports boto3 lazily, so this needs no AWS SDK.
    """
    import importlib.util
    path = repo / "eval" / "bedrock_util.py"
    try:
        spec = importlib.util.spec_from_file_location("_suite_bedrock_util_probe", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)                           # type: ignore[union-attr]
        return str(mod.REGION), f"{path.relative_to(repo).as_posix()} REGION"
    except Exception as exc:                                   # pragma: no cover
        return DEFAULT_REGION, f"default (could not read {path.name}: {exc})"


def provenance(repo: Path, cfg_path: Path, cfg: dict, smoke: bool) -> dict:
    corpora = {}
    for name, c in cfg["corpora"].items():
        root = repo / c["source_root"]
        key = repo / c["key"]
        corpora[name] = {
            "source_root": c["source_root"],
            "exists": root.is_dir(),
            "content": tree_hash(root, tuple(c.get("exclude") or ())) if root.is_dir()
                       else {"sha256": "", "n_files": 0},
            "excluded_from_hash": list(c.get("exclude") or []),
            "key": c["key"], "key_exists": key.is_file(), "key_sha256": sha256_file(key),
        }
    region, region_source = bedrock_region(repo)
    return {
        "suite": cfg["suite"],
        "suite_config": {"path": str(cfg_path), "sha256": sha256_file(cfg_path),
                         "schema_version": cfg["schema_version"]},
        "git": git_info(repo),
        "model_id": os.environ.get("EVAL_MODEL_ID", DEFAULT_MODEL_ID),
        "region": region,
        "region_source": region_source,
        # recorded for diagnosis only: NOT the region the evaluators call
        "shell_default_region": os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION"),
        "judge_runs_default": cfg.get("judge_runs", DEFAULT_SUITE_JUDGE_RUNS),
        "corpora": corpora,
        "seeds": dict(cfg.get("seeds") or {}),
        "vars": dict(cfg.get("vars") or {}),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "smoke": smoke,
        "suite_schema_version": SUITE_SCHEMA_VERSION,
    }


# --------------------------------------------------------------------------- evaluators
@dataclass
class Evaluator:
    name: str
    script: str                       # repo-relative path
    result: str                       # result file written under the stage out dir
    required: tuple[str, ...] = ()    # flags the stage cannot run without
    maps: tuple[tuple[str, str], ...] = ()      # (stage key, CLI flag) for scalars
    repeats: tuple[tuple[str, str], ...] = ()   # (stage key, CLI flag) for repeatable lists
    positional: tuple[str, ...] = ()            # stage keys passed as bare arguments
    out_flag: str = "--out-dir"
    out_value: str = ""                         # file under the out dir, "" = the dir itself
    smoke_flags: tuple[str, ...] = ()
    uses_llm: bool = True
    budget_flag: str | None = None     # flag that caps the tool's own spend
    # flags that make the tool print/write a cost plan without calling Bedrock, and the
    # dotted path of the planned spend inside its result file
    plan_flags: tuple[str, ...] = ()
    plan_cost_key: str = ""


EVALUATORS: dict[str, Evaluator] = {
    "score": Evaluator(
        "score", "eval/score.py", "score.json",
        required=("--spec", "--key", "--source-root"),
        maps=(("spec", "--spec"), ("key", "--key"), ("source_root", "--source-root"),
              ("gen_log", "--gen-log"), ("segment", "--segment"),
              ("judge_runs", "--judge-runs"), ("cache_dir", "--cache-dir")),
        repeats=(("exclude", "--exclude"),),
        out_flag="--out-dir", smoke_flags=("--no-llm",), uses_llm=True,
        budget_flag="--max-judge-cost-usd",
        plan_flags=("--dry-run-judge",), plan_cost_key="judge_plan.est_cost_usd"),
    "graph_eval": Evaluator(
        "graph_eval", "eval/graph_eval.py", "graph_eval.json",
        # graph_eval.py takes the run directory as --run (there is no --run-dir), and it
        # is deterministic unless --llm is given, so it has no smoke flag to add.
        required=("--run",),
        maps=(("run_dir", "--run"), ("source_root", "--source-root"), ("key", "--key")),
        repeats=(("exclude", "--exclude"),),
        out_flag="--out-dir", smoke_flags=(), uses_llm=False),
    # B1 insights: by default the stage names the run's Issues & Insights report as `spec`
    # (${run}/issues/issues.md, see the suites' insights_spec var); `run_dir` maps to --run.
    "benefits_insights": Evaluator(
        "benefits_insights", "eval/benefits/insights.py", "insights.json",
        required=("--key", "--source-root", "--out"),
        maps=(("spec", "--spec"), ("run_dir", "--run"), ("key", "--key"),
              ("source_root", "--source-root"), ("adapter", "--adapter"),
              ("tool_name", "--tool-name"), ("judge_runs", "--judge-runs")),
        repeats=(("exclude", "--exclude"),),
        out_flag="--out", smoke_flags=("--no-llm",), uses_llm=True, budget_flag="--max-usd",
        plan_flags=("--dry-run",), plan_cost_key="plan.est_cost_usd"),
    "benefits_qa": Evaluator(
        "benefits_qa", "eval/benefits/qa_eval.py", "qa.json",
        required=("--qa", "--source-root", "--out"),
        maps=(("qa", "--qa"), ("spec", "--spec"), ("source_root", "--source-root"),
              ("adapter", "--adapter"), ("tool_name", "--tool-name")),
        repeats=(("exclude", "--exclude"),),
        out_flag="--out", smoke_flags=(), uses_llm=True, budget_flag="--max-usd",
        plan_flags=("--dry-run",), plan_cost_key="plan.est_cost_usd"),
    # impact_eval --no-llm only drops the rule JUDGE; the answerer (raw-code / spec adapter) is
    # still an LLM call, so it has no deterministic mode and is skipped in --smoke.
    "benefits_impact": Evaluator(
        "benefits_impact", "eval/benefits/impact_eval.py", "impact.json",
        required=("--impact", "--source-root", "--out"),
        maps=(("impact", "--impact"), ("key", "--key"), ("spec", "--spec"),
              ("source_root", "--source-root"), ("adapter", "--adapter"),
              ("tool_name", "--tool-name")),
        repeats=(("exclude", "--exclude"),),
        out_flag="--out", smoke_flags=(), uses_llm=True, budget_flag="--max-usd",
        plan_flags=("--dry-run",), plan_cost_key="plan.est_cost_usd"),
    "benefits_fieldapp": Evaluator(
        "benefits_fieldapp", "eval/benefits/fieldapp_eval.py", "fieldapp.json",
        required=("--key", "--source-root", "--out"),
        maps=(("key", "--key"), ("spec", "--spec"), ("source_root", "--source-root"),
              ("adapter", "--adapter"), ("tool_name", "--tool-name")),
        repeats=(("exclude", "--exclude"),),
        out_flag="--out", smoke_flags=(), uses_llm=True, budget_flag="--max-usd",
        plan_flags=("--dry-run",), plan_cost_key="plan.est_cost_usd"),
    "mutate": Evaluator(
        "mutate", "eval/mutate/mutate.py", "mutate_manifest.json",
        required=("--corpus", "--out"),
        # `corpus` is reserved for the corpora-table reference, so the path key is
        # `corpus_path`.
        maps=(("corpus_path", "--corpus"), ("mutations", "--mutations"), ("seed", "--seed"),
              ("categories", "--categories"), ("avoid_key_margin", "--avoid-key-margin"),
              ("avoid_key_scope", "--avoid-key-scope")),
        repeats=(("exclude", "--exclude"), ("avoid_key", "--avoid-key")),
        out_flag="--out", smoke_flags=(), uses_llm=False),
    "mutation_detect": Evaluator(
        "mutation_detect", "eval/mutate/score_mutations.py", "mutation_score.json",
        required=("--mutations", "--output"),
        maps=(("mutations", "--mutations"), ("output", "--output"), ("artifact", "--artifact"),
              ("source_root", "--source-root"), ("control", "--control"),
              ("base_key", "--base-key"), ("line_slack", "--line-slack")),
        out_flag="--out", smoke_flags=(), uses_llm=False),
    "sanity": Evaluator(
        "sanity", "eval/mutate/sanity.py", "sanity.json", required=("--json",),
        maps=(), positional=("path",), out_flag="--json", out_value="sanity.json",
        smoke_flags=(), uses_llm=False),
}


_HELP_CACHE: dict[str, str] = {}


def probe_flags(repo: Path, script: str) -> tuple[bool, set[str], str]:
    """Discover a tool's flags from ``--help`` at runtime. Returns (exists, flags, raw help)."""
    path = repo / script
    if not path.is_file():
        return False, set(), ""
    if script in _HELP_CACHE:
        raw = _HELP_CACHE[script]
    else:
        try:
            r = subprocess.run([sys.executable, str(path), "--help"], capture_output=True,
                               text=True, timeout=180, cwd=str(repo))
            raw = (r.stdout or "") + (r.stderr or "")
        except Exception as exc:
            raw = f"__probe_failed__ {exc}"
        _HELP_CACHE[script] = raw
    return True, set(re.findall(r"--[a-z0-9][a-z0-9-]*", raw)), raw


def build_argv(repo: Path, ev: Evaluator, stage: dict, out_dir: Path, flags: set[str],
               smoke: bool, budget_left: float | None,
               plan: bool = False) -> tuple[list[str], list[str]]:
    """Assemble the command line, skipping anything the tool does not advertise.

    Returns (argv, notes). ``notes`` records every flag that was dropped, so a suite
    report always says which requested settings the tool could not accept.
    ``plan=True`` appends the evaluator's plan flags (cost plan, no Bedrock calls).
    """
    argv = [sys.executable, str(repo / ev.script)]
    notes: list[str] = []

    def add(flag: str, value=None):
        if flag and flag not in flags:
            notes.append(f"dropped {flag}: not advertised by {ev.script} --help")
            return
        argv.append(flag)
        if value is not None:
            argv.append(str(value))

    for key, flag in ev.maps:
        if key in stage and stage[key] is not None:
            add(flag, stage[key])
    for key, flag in ev.repeats:
        for v in stage.get(key) or []:
            add(flag, v)
    for key in ev.positional:
        if stage.get(key) is None:
            notes.append(f"missing required stage key '{key}' for {ev.script}")
            continue
        argv.append(str(stage[key]))
    if ev.out_flag:
        add(ev.out_flag, out_dir / ev.out_value if ev.out_value else out_dir)
    extra = dict(stage.get("args") or {})
    if smoke:
        extra.update(stage.get("smoke_args") or {})
        for f in ev.smoke_flags:
            extra.setdefault(f.lstrip("-"), True)
    for k, v in sorted(extra.items()):
        flag = k if k.startswith("--") else "--" + k.replace("_", "-")
        if v is True:
            add(flag)
        elif v is False or v is None:
            continue
        else:
            add(flag, v)
    if plan:
        for f in ev.plan_flags:
            add(f)
    elif ev.budget_flag and budget_left is not None and ev.budget_flag in flags:
        argv += [ev.budget_flag, f"{max(0.0, budget_left):.4f}"]
    return argv, notes


# --------------------------------------------------------------------------- estimates
# Fallback cost table, used only when an evaluator cannot produce its own plan (no plan
# flag, or the plan run failed). Every row is a MEASURED cost from a real run, not a guess,
# and is the per-judge-pass figure: the estimate multiplies it by the stage's judge_runs.
#
#   score.py, auto segmentation   AP demo bottled spec, 438 KB: $3.36/pass (35 calls)
#                                 JC bottled spec, 556 KB:       $5.39/pass (64 calls, plan)
#                                 -> $0.97 per 100 KB of spec per pass (the worse of the two)
#   score.py, requirements        AP demo bottled spec: $4.79 for 3 passes = $1.60/pass
#                                 -> $0.37 per 100 KB per pass
#   qa_eval.py, spec adapter      JC: $2.43 for 16 answers at a 150K-char context = $0.152
#                                 per question, + ~$0.05 per question to grade
#   impact_eval.py                JC: $1.71 for 9 calls (8 answers + 1) = $0.19 per change,
#                                 + ~$0.03 per change to judge rules
#   fieldapp_eval.py              JC: $0.64 and $0.69 per run (generate + judge)
#   insights.py                   score.py rate over the artifact (1 pass) + 10% for the
#                                 flag classifier
# The 2026-10-06 suites estimated $3.30 for the whole JC suite against ~$16 per spec artifact
# at score.py's default 3 passes; this table and the plan route replace those numbers.
CALIBRATED_COSTS: dict[str, dict] = {
    "score": {"usd_per_100kb_per_pass": {"auto": 0.97, "lines": 0.97, "requirements": 0.37},
              "basis": "AP 438 KB $3.36/pass auto, $1.60/pass requirements; JC 556 KB $5.39/pass"},
    "benefits_insights": {"usd_per_100kb_per_pass": 0.97 * 1.10,
                          "basis": "score.py auto rate over the artifact + 10% flag classifier"},
    "benefits_qa": {"usd_per_item": 0.20, "basis": "JC: $2.43 / 16 answers + grading"},
    "benefits_impact": {"usd_per_item": 0.22, "basis": "JC: $1.71 / 9 calls + rule judge"},
    "benefits_fieldapp": {"usd_per_run": 0.69, "basis": "JC: $0.64 and $0.69 per run"},
}


def _count_items(path: Path, keys: tuple[str, ...]) -> int | None:
    if yaml is None or not path.is_file():
        return None
    try:
        d = yaml.safe_load(path.read_text())
    except Exception:
        return None
    if isinstance(d, list):
        return len(d)
    if isinstance(d, dict):
        for k in keys:
            if isinstance(d.get(k), list):
                return len(d[k])
    return None


def calibrated_estimate(ename: str, stage: dict, repo: Path) -> tuple[float | None, str]:
    """Per-evaluator fallback estimate from CALIBRATED_COSTS. (usd, basis) or (None, why)."""
    row = CALIBRATED_COSTS.get(ename)
    if not row:
        return None, f"no calibrated cost for evaluator '{ename}'"
    runs = int(stage.get("judge_runs") or 1)

    def rp(v):
        q = Path(str(v))
        return q if q.is_absolute() else repo / q

    if "usd_per_100kb_per_pass" in row:
        art = stage.get("spec")
        if not art and stage.get("run_dir"):
            art = str(Path(str(stage["run_dir"])) / "issues" / "issues.md")
        if not art or not rp(art).is_file():
            return None, "artifact not found for a size-based estimate"
        rate = row["usd_per_100kb_per_pass"]
        if isinstance(rate, dict):
            rate = rate.get(str(stage.get("segment") or "auto"), max(rate.values()))
        kb = rp(art).stat().st_size / 1e5
        return round(rate * kb * runs, 4), (f"calibrated: ${rate:.2f}/100KB/pass x {kb:.2f} x "
                                            f"{runs} pass(es) ({row['basis']})")
    if "usd_per_item" in row:
        src = stage.get("qa") or stage.get("impact")
        n = _count_items(rp(src), ("questions", "changes", "scenarios", "items")) if src else None
        if not n:
            return None, "could not count the items to price"
        return round(row["usd_per_item"] * n, 4), (f"calibrated: ${row['usd_per_item']:.2f} x {n} "
                                                    f"items ({row['basis']})")
    return round(row["usd_per_run"], 4), f"calibrated: ${row['usd_per_run']:.2f}/run ({row['basis']})"


def dotted(data, path: str):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def measurement_of(data: dict) -> tuple[str | None, str | None]:
    """An evaluator's self-reported measurement status (measured / partial / not_measured)."""
    if not isinstance(data, dict):
        return None, None
    m = data.get("measurement")
    if isinstance(m, dict) and m.get("status"):
        return str(m["status"]), m.get("reason")
    st = (data.get("summary") or {}).get("measurement_status") if isinstance(data.get("summary"), dict) else None
    return (str(st), None) if st else (None, None)


# --------------------------------------------------------------------------- budget
@dataclass
class Budget:
    cap_usd: float
    logs: list[Path] = field(default_factory=list)
    _seen: set[tuple] = field(default_factory=set)
    spent: float = 0.0
    calls: int = 0
    planned: float = 0.0     # --plan: estimated spend of the stages planned so far

    def _read(self, p: Path) -> list[dict]:
        if not p.is_file():
            return []
        out = []
        for line in p.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def prime(self) -> None:
        """Mark everything already in the logs as pre-existing, so other sessions'
        concurrent calls are never charged to this suite."""
        for p in self.logs:
            for r in self._read(p):
                self._seen.add(self._key(r))

    @staticmethod
    def _key(r: dict) -> tuple:
        return (r.get("ts"), r.get("tag"), r.get("input_tokens"), r.get("output_tokens"),
                r.get("cost_usd"))

    def collect(self, extra_logs: list[Path] | None = None) -> dict:
        """Charge every unseen record in the known logs. Returns this call's delta."""
        delta, n = 0.0, 0
        for p in list(self.logs) + list(extra_logs or []):
            for r in self._read(p):
                k = self._key(r)
                if k in self._seen:
                    continue
                self._seen.add(k)
                c = r.get("cost_usd")
                if c is None and (r.get("input_tokens") or r.get("output_tokens")):
                    c = (r.get("input_tokens", 0) / 1e6 * 3.0
                         + r.get("output_tokens", 0) / 1e6 * 15.0)
                delta += float(c or 0.0)
                n += 1
        self.spent = round(self.spent + delta, 6)
        self.calls += n
        return {"stage_usd": round(delta, 6), "stage_calls": n,
                "cumulative_usd": self.spent, "cumulative_calls": self.calls}

    @property
    def left(self) -> float:
        return round(self.cap_usd - self.spent - self.planned, 6)

    def reserve(self, usd: float) -> None:
        """--plan: count a planned stage's estimate as if it had been spent."""
        self.planned = round(self.planned + float(usd), 6)

    def allows(self, est_usd: float) -> tuple[bool, str]:
        committed = self.spent + self.planned
        if committed + est_usd > self.cap_usd + 1e-9:
            return False, (f"estimated ${est_usd:.4f} would take spend to "
                           f"${committed + est_usd:.4f} over the ${self.cap_usd:.2f} cap")
        return True, ""


# --------------------------------------------------------------------------- metrics
def flatten(obj, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        if obj and all(isinstance(x, (int, float, str)) for x in obj):
            out[prefix] = obj
    else:
        out[prefix] = obj
    return out


def pick(metrics: dict, path: str):
    return metrics.get(path)


# --------------------------------------------------------------------------- tolerances
# Default regression tolerance per metric family, used when the evaluator reports no
# confidence interval. Tolerances are absolute: percentage points, or counts.
#
# They are MEASURED, not guessed. Every pair of score.json files in eval/runs/ that
# re-judged the *same* artifact with the *same* segmentation was compared
# (6 independent pairs across the AP, JC-lean and COBOL corpora):
#
#   metric                                pairs   max |delta|   mean |delta|
#   recall.strict_full_pct                    6          10.5           5.2
#   recall.partial_credit_pct                 6           5.2           2.6
#   recall.any_match_pct                      4           0.0           0.0
#   recall.core_full_pct                      1           7.1           7.1
#   precision.keyed_pct                       6           3.2           1.2
#   precision.correct_pct                     6           2.1           0.8
#   precision.incorrect (count)               6           4             1.3
#   precision.unsupported (count)             6          12             5.3
#   citations.validity_pct                    3           0.0           0.0
#   citations.accuracy_pct                    6           1.7           0.7
#   citations.findings_without_citation       5           0             0
#
# Each tolerance below is set at or just above the observed maximum, so a flagged
# regression means "larger than any movement we have ever seen from re-judging alone".
# Rows marked `exact` come from deterministic code (file/line arithmetic, parsers,
# mutation bookkeeping) and are given a tolerance of 0 or near 0.
#
# Longest prefix wins: more specific rows must come before their family.
DEFAULT_TOLERANCES: list[tuple[str, float, str, str]] = [
    # (metric name, prefix or suffix; tolerance; direction; basis)
    ("citations.validity_pct", 0.5, "higher_better", "exact"),
    ("citations.findings_without_citation", 0.0, "lower_better", "exact"),
    ("citations.invalid", 0.0, "lower_better", "exact"),
    ("citations.accuracy_pct", 2.0, "higher_better", "judged"),
    ("recall.any_match_pct", 1.0, "higher_better", "judged"),
    ("recall.core_any_pct", 1.0, "higher_better", "judged"),
    ("recall.partial_credit_pct", 6.0, "higher_better", "judged"),
    ("recall.strict_full_pct", 11.0, "higher_better", "judged"),
    ("recall.core_full_pct", 11.0, "higher_better", "judged"),
    ("recall.", 11.0, "higher_better", "judged"),
    ("precision.incorrect", 4.0, "lower_better", "judged"),
    ("precision.unsupported", 12.0, "lower_better", "judged"),
    ("precision.keyed_pct", 4.0, "higher_better", "judged"),
    ("precision.correct_pct", 3.0, "higher_better", "judged"),
    ("precision.", 4.0, "higher_better", "judged"),
    ("precision.findings_matched", None, "informational", "exact"),
    ("precision.correct_extra", None, "informational", "exact"),
    ("recall_by.", None, "informational", "judged"),
    ("overall.detection_rate_pct", 0.0, "higher_better", "exact"),
    ("overall.detection_rate_cited_pct", 0.0, "higher_better", "exact"),
    ("false_positives.", 0.0, "lower_better", "exact"),
    ("n_problems", 0.0, "lower_better", "exact"),
    ("sanity_problem_count", 0.0, "lower_better", "exact"),
    ("rejected_count", None, "informational", "exact"),
    ("missed", 0.0, "lower_better", "exact"),
    ("applied", 0.0, "higher_better", "exact"),
    ("by_category.", 0.0, "higher_better", "exact"),
    ("counts.", None, "informational", "exact"),
]


def tolerance_for(metric: str) -> tuple[float | None, str, str]:
    """Tolerance, direction and basis for one metric.

    Anything not named in DEFAULT_TOLERANCES is regression-gated only if it is a
    percentage (``*_pct``); raw counts and identifiers are informational, because a
    count moving by a few units usually reflects segmentation rather than quality.
    """
    for pat, tol, direction, basis in DEFAULT_TOLERANCES:
        if metric == pat or metric.startswith(pat) or metric.endswith(pat):
            return tol, direction, basis
    if metric.endswith("_pct"):
        return 11.0, "higher_better", "judged (unlisted percentage, widest measured band)"
    return None, "informational", "unlisted count or identifier"


def metric_ci(metrics: dict, metric: str) -> tuple[float, float] | None:
    """Use the evaluator's own confidence interval when it publishes one."""
    for lo, hi in ((f"{metric}_ci_low", f"{metric}_ci_high"),
                   (f"{metric}.ci_low", f"{metric}.ci_high"),
                   (f"{metric}_ci.0", f"{metric}_ci.1")):
        a, b = metrics.get(lo), metrics.get(hi)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return float(a), float(b)
    ci = metrics.get(f"{metric}_ci")
    if isinstance(ci, list) and len(ci) == 2 and all(isinstance(x, (int, float)) for x in ci):
        return float(ci[0]), float(ci[1])
    return None


def compare_to_baseline(current: dict, baseline: dict) -> dict:
    """Flag metrics that moved beyond tolerance. Never raises on an older baseline."""
    bver = (baseline.get("provenance") or {}).get("suite_schema_version",
                                                 baseline.get("suite_schema_version"))
    if bver != SUITE_SCHEMA_VERSION:
        return {"compared": False,
                "reason": f"baseline suite_schema_version {bver!r} != {SUITE_SCHEMA_VERSION}; "
                          f"metric names may not line up, comparison skipped",
                "regressions": [], "improvements": [], "checked": 0}
    bstages = {s["id"]: s for s in baseline.get("stages", [])}
    regressions, improvements, checked = [], [], 0
    for st in current.get("stages", []):
        b = bstages.get(st["id"])
        if not b:
            continue
        cm, bm = st.get("metrics") or {}, b.get("metrics") or {}
        for metric, cv in sorted(cm.items()):
            bv = bm.get(metric)
            if not isinstance(cv, (int, float)) or not isinstance(bv, (int, float)):
                continue
            if isinstance(cv, bool) or isinstance(bv, bool):
                continue
            tol, direction, basis = tolerance_for(metric)
            if direction == "informational" or tol is None:
                continue
            ci = metric_ci(bm, metric)
            checked += 1
            if ci:
                lo, hi = ci
                bad = cv < lo if direction == "higher_better" else cv > hi
                band = f"baseline CI [{lo:g}, {hi:g}] (reported by the evaluator)"
            else:
                bad = (cv < bv - tol) if direction == "higher_better" else (cv > bv + tol)
                band = f"baseline {bv:g} +-{tol:g} ({basis} tolerance)"
            rec = {"stage": st["id"], "metric": metric, "baseline": bv, "current": cv,
                   "delta": round(cv - bv, 6), "direction": direction, "band": band}
            if bad:
                regressions.append(rec)
            elif (cv > bv + (tol or 0) and direction == "higher_better") or \
                 (cv < bv - (tol or 0) and direction == "lower_better"):
                improvements.append(rec)
    return {"compared": True, "reason": "", "regressions": regressions,
            "improvements": improvements, "checked": checked}


# --------------------------------------------------------------------------- determinism
def canonical(report: dict) -> dict:
    """Strip volatile fields so two runs of the same suite can be compared byte for byte.

    Two things vary between otherwise identical runs: wall-clock values, and the output
    directory (which appears inside commands, artifact paths and some result files). The
    first is dropped by name, the second is rewritten to the literal ``<OUT>``. What is
    left is the part a regression gate cares about: statuses, metrics and verdicts.
    """
    out_dir = str(report.get("out_dir") or "")

    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in sorted(o.items()) if k not in VOLATILE_KEYS}
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, str) and out_dir and out_dir in o:
            return o.replace(out_dir, "<OUT>")
        return o
    return walk(report)
