#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""budget.py: the run budget guard (DISTILL_MAX_USD).

Three jobs:

1. **Estimate before any spend.** `estimate` turns the run plan (pages x stills, which stages are
   on, and what an earlier attempt into the same output directory already finished) into a USD
   estimate per stage, prints it, and writes `<output>/budget_estimate.json`. When the estimate
   (plus what this output directory has already spent) is over `DISTILL_MAX_USD` it refuses to
   start (exit 6, `run_status.json` = `over_estimate`) unless `DISTILL_ALLOW_OVER_ESTIMATE=1`.

2. **Gate before each LLM stage.** `gate --stage S` reads actual spend from `<output>/telemetry/`
   (telemetry.compute_cost: double-logged calls removed, cache tokens and per-model prices
   included) and stops the run cleanly (exit 6, `run_status.json` = `budget_exhausted`, with the
   spend, the stage reached and an exact resume command) when spend + the stage's estimate would
   exceed the cap.

3. **Guard inside the map and reduce loops.** `BudgetGuard` is used by jobs.py: before each map
   call it reserves a worst-case-ish per-call amount on top of actual spend plus what in-flight
   calls have reserved, and refuses the call when that would cross the cap.

The cap is cumulative over the output directory: telemetry/ accumulates across resumes, and
completed stages are reused through stage identity (they cost $0 on resume), so the resume command
carries a raised cap that covers what is left.

Per-unit costs come from telemetry history (`DISTILL_COST_HISTORY`: space-separated run dirs or
telemetry dirs of earlier runs) when given, otherwise from DEFAULT_UNIT_COSTS below.

CLI:
  budget.py estimate --output O --pages-dir P --stills "a b c"
  budget.py gate     --output O --stage S [--label "2.75 format"]
  budget.py stop     --output O --stage S --label L [--reason R]     (map/reduce guard tripped)
  budget.py remaining --output O                                     (USD left under the cap)
  budget.py spent    --output O
Exit codes: 0 ok, 6 over budget (refused or stopped), 2 bad arguments.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import shlex
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import telemetry  # noqa: E402

EXIT_BUDGET = 6

# ── Default per-unit costs (USD) ────────────────────────────────────────────────────────────
# Measured from telemetry of two complete default runs with us.anthropic.claude-sonnet-4-6 and
# 22,000-token pages (2026-10-05 AP sample, 3 pages, $17.21; 2026-10-06 JC module, 4 pages,
# $22.92). "page_still" units are multiplied by pages x stills, "page" units by pages, "run"
# units are fixed. Sonnet stages are scaled by the configured model's output price relative to
# Sonnet; code-graph-family stages run on DISTILL_CODE_GRAPH_MODEL (Haiku by default).
DEFAULT_UNIT_COSTS = {
    # stage: (usd, unit)
    "map": (0.170, "page_still"),          # AP $0.175, JC $0.165 per map call
    "reduce": (0.054, "page_still"),       # AP $1.76 / 33, JC $2.39 / 44
    "synthesis": (0.053, "page_still"),    # AP $1.93 / 33, JC $2.13 / 44
    "format": (0.165, "page_still"),       # AP $4.96 / 33, JC $7.74 / 44
    "code_graph": (0.050, "page"),         # Haiku, about $0.20 of the ~$0.28 unlabelled bucket
    "arbitrate": (0.050, "run"),
    "backmap_residue": (0.030, "run"),
    "issues": (0.500, "run"),              # AP $0.44, JC $0.50 (also capped by DISTILL_ISSUES_MAX_COST)
    "blend": (0.020, "run"),
    "bottling": (0.670, "page"),           # AP $2.07 / 3, JC $2.63 / 4
}
# Largest single map call seen in those runs ($0.29 AP, $0.31 JC): the in-loop map guard reserves
# this much per in-flight call so concurrent calls cannot jointly overshoot the cap.
DEFAULT_MAP_CALL_RESERVE = 0.32

STAGE_ORDER = ["map", "reduce", "synthesis", "format", "code_graph", "arbitrate",
               "backmap_residue", "issues", "blend", "bottling"]
SONNET_STAGES = {"map", "reduce", "synthesis", "format", "issues", "blend", "bottling", "arbitrate"}
# .stage_identity/<stamp>.id written by run_distillation.sh when the stage completed
STAGE_STAMP = {"synthesis": "synthesis", "format": "format", "code_graph": "code_graph",
               "arbitrate": "arbitrate", "backmap_residue": "concept_symbols", "issues": "issues",
               "blend": "blend", "bottling": "bottling"}
STAGE_LABEL = {"map": "2 map", "reduce": "2 reduce", "synthesis": "2.6 concept synthesis",
               "format": "2.75 format", "code_graph": "2.8 code graph",
               "arbitrate": "2.9 graph arbitration", "backmap_residue": "2.95 back-map residue",
               "issues": "2.98 issues & insights", "blend": "4 master blend",
               "bottling": "5 bottling"}

# Environment carried into the resume command (identity-relevant knobs and credentials context;
# never secrets). DISTILL_MAX_USD is replaced with the suggested cap.
RESUME_ENV_PREFIXES = ("DISTILL_", "SKIP_")
RESUME_ENV_KEYS = ("AWS_PROFILE", "AWS_DEFAULT_REGION", "AWS_REGION", "AWS_SHARED_CREDENTIALS_FILE",
                   "AWS_CONFIG_FILE", "BEDROCK_MODEL_ID",
                   "ORCHESTRATION_STRATEGY", "PLATE_STRATEGY", "PYTHONUNBUFFERED")
RESUME_ENV_DROP = {"DISTILL_TELEMETRY_DIR", "DISTILL_MAX_USD", "DISTILL_ALLOW_OVER_ESTIMATE",
                   "DISTILL_ESTIMATE_ONLY"}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _float_env(name: str) -> float | None:
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    try:
        f = float(v)
    except ValueError:
        raise SystemExit(f"budget: {name}={v!r} is not a number")
    if f <= 0:
        raise SystemExit(f"budget: {name} must be > 0 (got {v})")
    return f


def max_usd() -> float | None:
    return _float_env("DISTILL_MAX_USD")


def warn_usd() -> float | None:
    return _float_env("DISTILL_WARN_USD")


def model_scale(model_id: str) -> float:
    """Output-price ratio of model_id to Sonnet (the default table was measured on Sonnet)."""
    return telemetry.pricing_for(model_id)["output_per_1m"] / telemetry.PRICING["output_per_1m"]


# ── Spend ───────────────────────────────────────────────────────────────────────────────────
def telemetry_dir(output: str | Path) -> Path:
    return Path(output) / "telemetry"


def spent_usd(output: str | Path) -> float:
    """Actual spend recorded under <output>/telemetry (fixed telemetry.py cost logic)."""
    return float(telemetry.compute_cost(str(telemetry_dir(output)))["total_cost_usd"])


class SpendLedger:
    """Incremental reader of a telemetry dir: re-reads only appended bytes, so the in-loop guard
    can check spend before every call without re-parsing the whole history each time."""

    def __init__(self, tel_dir: str | Path):
        self.dir = Path(tel_dir)
        self._offsets: dict[str, int] = {}
        self._records: list[dict] = []
        self._lock = threading.Lock()

    def spent(self) -> float:
        with self._lock:
            if self.dir.is_dir():
                for f in sorted(self.dir.glob("*.jsonl")):
                    off = self._offsets.get(f.name, 0)
                    try:
                        size = f.stat().st_size
                    except OSError:
                        continue
                    if size <= off:
                        continue
                    with open(f, "rb") as fh:
                        fh.seek(off)
                        chunk = fh.read()
                    # only consume complete lines; a half-written trailing line is read next time
                    end = chunk.rfind(b"\n") + 1
                    for ln in chunk[:end].decode("utf-8", "replace").splitlines():
                        if ln.strip():
                            try:
                                self._records.append(json.loads(ln))
                            except json.JSONDecodeError:
                                pass
                    self._offsets[f.name] = off + end
            recs = telemetry.dedupe_records(list(self._records))
            return sum(telemetry.record_cost(r) for r in recs)


class BudgetGuard:
    """Thread-safe reservation guard used inside the map and reduce loops.

    try_reserve(amount) succeeds only when actual spend + amounts reserved by in-flight calls +
    amount stays within the cap. The caller releases the reservation once the call has finished
    and its telemetry record has been written (from then on it counts as actual spend)."""

    def __init__(self, cap_usd: float, tel_dir: str | Path, call_reserve_usd: float,
                 warn_at_usd: float | None = None, log=print):
        self.cap = cap_usd
        self.call_reserve = call_reserve_usd
        self.warn_at = warn_at_usd
        self.ledger = SpendLedger(tel_dir)
        self.reserved = 0.0
        self.exhausted = False
        self.last_spent = 0.0
        self._warned = False
        self._lock = threading.Lock()
        self._log = log

    def try_reserve(self, amount: float | None = None) -> bool:
        amount = self.call_reserve if amount is None else amount
        with self._lock:
            spent = self.ledger.spent()
            self.last_spent = spent
            if self.warn_at is not None and not self._warned and spent >= self.warn_at:
                self._warned = True
                self._log(f"[budget] WARNING spend ${spent:.2f} reached DISTILL_WARN_USD ${self.warn_at:.2f} "
                          f"(cap ${self.cap:.2f})")
            if spent + self.reserved + amount > self.cap:
                if not self.exhausted:
                    self._log(f"[budget] cap reached: spent ${spent:.2f} + in-flight ${self.reserved:.2f} "
                              f"+ next ${amount:.2f} > DISTILL_MAX_USD ${self.cap:.2f}; no new calls")
                self.exhausted = True
                return False
            self.reserved += amount
            return True

    def release(self, amount: float | None = None) -> None:
        amount = self.call_reserve if amount is None else amount
        with self._lock:
            self.reserved = max(0.0, self.reserved - amount)


def guard_from_env(tel_dir: str | Path | None = None, model_id: str = "", log=print) -> BudgetGuard | None:
    """A BudgetGuard when DISTILL_MAX_USD is set, else None (no guard, no overhead)."""
    cap = max_usd()
    if cap is None:
        return None
    tel = tel_dir or os.environ.get("DISTILL_TELEMETRY_DIR", "")
    if not tel:
        return None
    reserve = _float_env("DISTILL_MAP_CALL_RESERVE_USD")
    if reserve is None:
        reserve = DEFAULT_MAP_CALL_RESERVE * model_scale(model_id or os.environ.get("BEDROCK_MODEL_ID", ""))
        hist = history_units(_history_dirs())
        if hist.get("map_call_max"):
            reserve = max(reserve, hist["map_call_max"])
    return BudgetGuard(cap, tel, reserve, warn_usd(), log=log)


# ── Unit costs from telemetry history ───────────────────────────────────────────────────────
def _history_dirs() -> list[str]:
    return [d for d in os.environ.get("DISTILL_COST_HISTORY", "").split() if d]


def _category(rec: dict) -> str:
    phase, still = rec.get("phase") or "", rec.get("still") or ""
    if phase == "map":
        return "map"
    if phase == "reduce":
        return "reduce"
    if phase == "synthesize":
        return "synthesis"
    if phase.startswith("format"):
        return "format"
    if phase == "issues":
        return "issues"
    if still == "bottling":
        return "bottling"
    return "code_graph"  # unlabelled bucket: code graph, arbitration, back-map residue, blend


def history_units(dirs: list[str]) -> dict:
    """Per-unit USD from earlier runs' telemetry: {stage: usd per unit, 'map_call_max': usd,
    'runs': n}. A dir counts only when it has map records (so pages x stills is known)."""
    sums: dict[str, float] = {}
    units = {"page_still": 0, "page": 0, "run": 0}
    map_max = 0.0
    runs = 0
    for d in dirs:
        p = Path(d)
        tel = p / "telemetry" if (p / "telemetry").is_dir() else p
        recs = telemetry.load_records(str(tel))
        maps = [r for r in recs if r.get("phase") == "map"]
        pages = len({r.get("page_id") for r in maps if r.get("page_id")})
        stills = len({r.get("still") for r in maps if r.get("still")})
        if not pages or not stills:
            continue
        runs += 1
        units["page_still"] += pages * stills
        units["page"] += pages
        units["run"] += 1
        for r in recs:
            sums[_category(r)] = sums.get(_category(r), 0.0) + telemetry.record_cost(r)
        map_max = max(map_max, max(telemetry.record_cost(r) for r in maps))
    if not runs:
        return {}
    out: dict = {"runs": runs, "map_call_max": round(map_max, 4)}
    for stage, (_usd, unit) in DEFAULT_UNIT_COSTS.items():
        if stage in ("arbitrate", "backmap_residue", "blend"):
            continue  # measured together in the unlabelled code_graph bucket
        n = units[unit]
        if stage in sums and n:
            out[stage] = sums[stage] / n
    if "code_graph" in sums:
        out["code_graph"] = sums["code_graph"] / units["page"]   # whole unlabelled bucket, per page
        out["arbitrate"] = out["backmap_residue"] = out["blend"] = 0.0
    return out


def unit_costs(model_id: str = "") -> tuple[dict, str]:
    """({stage: (usd, unit)}, source description)."""
    scale = model_scale(model_id or os.environ.get("BEDROCK_MODEL_ID", ""))
    table = {s: ((u * scale if s in SONNET_STAGES else u), unit)
             for s, (u, unit) in DEFAULT_UNIT_COSTS.items()}
    hist = history_units(_history_dirs())
    if not hist:
        src = "default table (AP + JC reference runs, Sonnet 4.6, 22K-token pages)"
        return table, src + (f", scaled x{scale:.2f} for {model_id}" if scale != 1 else "")
    for s, (_u, unit) in table.items():
        if s in hist:
            table[s] = (hist[s], unit)
    return table, f"telemetry history ({hist['runs']} run(s) from DISTILL_COST_HISTORY)"


# ── Plan ────────────────────────────────────────────────────────────────────────────────────
def enabled_stages() -> dict[str, bool]:
    e = os.environ.get
    cg = e("DISTILL_CODE_GRAPH", "1") == "1"
    return {
        "map": True, "reduce": True,
        "synthesis": e("SKIP_SYNTHESIS", "0") != "1",
        "format": True,
        "code_graph": cg, "arbitrate": cg,
        "backmap_residue": e("DISTILL_SKIP_BACKMAP_RESIDUE", "0") != "1",
        "issues": e("DISTILL_ISSUES", "1") == "1",
        "blend": True,
        "bottling": e("SKIP_BOTTLING", "0") != "1",
    }


def plan_from_disk(output: str | Path, pages_dir: str | Path, stills: list[str]) -> dict:
    """Count what is left to do. Map: (page, still) pairs without a non-empty extract. Reduce:
    stills without a raw_concat. Downstream stages count as done only when map and reduce have
    nothing left AND the stage has a stamp (a re-mapped still changes every downstream identity).
    The runtime gates use real telemetry, so this is only the pre-run estimate."""
    out = Path(output)
    pages = sorted(p.name for p in Path(pages_dir).glob("page_*") if (p / "concat.txt").is_file())
    map_todo = 0
    reduce_todo = 0
    for s in stills:
        for pg in pages:
            f = out / "stills" / s / pg / f"{s}_extract.md"
            if not (f.is_file() and f.stat().st_size > 0):
                map_todo += 1
        rc = out / "stills" / s / f"{s}_raw_concat.md"
        if not (rc.is_file() and rc.stat().st_size > 0):
            reduce_todo += 1
    upstream_clean = map_todo == 0 and reduce_todo == 0
    done = {st: upstream_clean and (out / ".stage_identity" / f"{stamp}.id").is_file()
            for st, stamp in STAGE_STAMP.items()}
    return {"pages": len(pages), "stills": list(stills), "map_todo": map_todo,
            "reduce_todo_stills": reduce_todo, "downstream_done": done,
            "enabled": enabled_stages()}


def stage_estimates(plan: dict, table: dict) -> dict[str, float]:
    """Full (from-scratch) USD estimate for each stage under this plan."""
    pages, n_stills = plan["pages"], len(plan["stills"])
    mult = {"page_still": pages * n_stills, "page": pages, "run": 1}
    return {s: (table[s][0] * mult[table[s][1]] if plan["enabled"].get(s, True) else 0.0)
            for s in STAGE_ORDER}


def remaining_estimates(plan: dict, table: dict) -> dict[str, float]:
    """USD still to spend per stage, given what is already on disk."""
    full = stage_estimates(plan, table)
    pages = plan["pages"]
    rem = dict(full)
    rem["map"] = table["map"][0] * plan["map_todo"]
    rem["reduce"] = table["reduce"][0] * pages * plan["reduce_todo_stills"]
    for s, done in plan["downstream_done"].items():
        if done:
            rem[s] = 0.0
    return rem


# ── run_status + resume command ─────────────────────────────────────────────────────────────
def suggested_cap(spent: float, remaining: float) -> float:
    """Spend so far + what is left + 15% headroom, rounded up to the next $0.50."""
    return math.ceil((spent + remaining * 1.15) * 2) / 2


def resume_command(new_cap: float, script: str | None = None) -> str:
    script = script or str(HERE.parent.parent / "run_distillation.sh")
    env = {}
    for k, v in sorted(os.environ.items()):
        if k in RESUME_ENV_DROP:
            continue
        if k.startswith(RESUME_ENV_PREFIXES) or k in RESUME_ENV_KEYS:
            env[k] = v
    if env.get("DISTILL_GIT_URL"):
        # git input: pin the resume to the commit this run analysed (a moved branch would otherwise
        # invalidate everything), keep the originally requested ref for provenance, and never carry
        # DISTILL_TARGET (mutually exclusive with DISTILL_GIT_URL).
        env.pop("DISTILL_TARGET", None)
        commit = env.pop("DISTILL_GIT_COMMIT", "")
        if commit:
            env["DISTILL_GIT_REF_REQUESTED"] = env.get("DISTILL_GIT_REF_REQUESTED") or env.get("DISTILL_GIT_REF") or "HEAD"
            env["DISTILL_GIT_REF"] = commit
    for k in ("DISTILL_TARGET", "DISTILL_OUTPUT"):
        if env.get(k):
            p = Path(env[k])
            env[k] = str(p if p.is_absolute() else (Path(script).resolve().parent / p).resolve())
    env["DISTILL_MAX_USD"] = f"{new_cap:.2f}"
    parts = [f"{k}={shlex.quote(v)}" for k, v in env.items()]
    return " ".join(parts + ["bash", shlex.quote(str(Path(script).resolve()))])


def write_status(output: str | Path, status: str, **fields) -> dict:
    data = {"status": status, "failed_stages": [], **fields, "finished": _now()}
    Path(output).mkdir(parents=True, exist_ok=True)
    (Path(output) / "run_status.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def _load_estimate(output: str | Path) -> dict | None:
    p = Path(output) / "budget_estimate.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _remaining_from(est: dict | None, stage: str) -> float:
    """Estimated USD for `stage` and every stage after it (from the saved pre-run estimate)."""
    if not est:
        return 0.0
    rem = est.get("remaining_usd_by_stage", {})
    idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 0
    return sum(float(rem.get(s, 0.0)) for s in STAGE_ORDER[idx:])


def stop(output: str | Path, stage: str, reason: str, next_usd: float | None = None,
         log=print) -> dict:
    """Mark the run budget_exhausted and print the stop banner with the exact resume command."""
    cap = max_usd()
    spent = spent_usd(output)
    est = _load_estimate(output)
    remaining = _remaining_from(est, stage)
    if stage == "map" and est:
        # the map may have finished part of its work: re-count it from disk
        plan = plan_from_disk(output, est.get("pages_dir", ""), est["plan"]["stills"])
        table = {k: tuple(v) for k, v in est["unit_costs"].items()}
        rem = remaining_estimates(plan, table)
        remaining = sum(rem[s] for s in STAGE_ORDER)
    new_cap = suggested_cap(spent, remaining)
    cmd = resume_command(new_cap)
    label = STAGE_LABEL.get(stage, stage)
    data = write_status(output, "budget_exhausted", stage_reached=label, stage_key=stage,
                        reason=reason, spent_usd=round(spent, 4), max_usd=cap,
                        next_step_estimate_usd=None if next_usd is None else round(next_usd, 4),
                        remaining_estimate_usd=round(remaining, 2),
                        suggested_max_usd=new_cap, resume_command=cmd)
    bar = "═" * 63
    log(bar)
    log(f"  BUDGET EXHAUSTED at {label}: spent ${spent:.2f} of DISTILL_MAX_USD ${cap:.2f}")
    log(f"  {reason}")
    log(f"  Completed stages are kept; a resume reuses them at $0 (stage identity).")
    log(f"  Estimated cost to finish: ${remaining:.2f}. Resume with:")
    log(f"    {cmd}")
    log(f"  run_status.json = budget_exhausted. This output is NOT a deliverable.")
    log(bar)
    return data


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def cmd_estimate(a) -> int:
    stills = [s for s in a.stills.replace(",", " ").split() if s]
    model = os.environ.get("BEDROCK_MODEL_ID", "")
    table, source = unit_costs(model)
    plan = plan_from_disk(a.output, a.pages_dir, stills)
    full = stage_estimates(plan, table)
    rem = remaining_estimates(plan, table)
    spent = spent_usd(a.output)
    remaining = sum(rem.values())
    projected = spent + remaining
    cap, warn = max_usd(), warn_usd()
    est = {"generated": _now(), "pages_dir": str(a.pages_dir), "plan": plan, "model_id": model,
           "unit_costs": {k: list(v) for k, v in table.items()}, "unit_cost_source": source,
           "full_usd_by_stage": {k: round(v, 4) for k, v in full.items()},
           "remaining_usd_by_stage": {k: round(v, 4) for k, v in rem.items()},
           "spent_usd": round(spent, 4), "remaining_usd": round(remaining, 2),
           "projected_total_usd": round(projected, 2), "max_usd": cap, "warn_usd": warn}
    Path(a.output).mkdir(parents=True, exist_ok=True)
    (Path(a.output) / "budget_estimate.json").write_text(json.dumps(est, indent=2), encoding="utf-8")

    print(f"  Cost estimate: {plan['pages']} page(s) x {len(stills)} still(s); unit costs from {source}")
    for s in STAGE_ORDER:
        tag = "" if rem[s] == full[s] else ("  (done, reused)" if rem[s] == 0 and full[s] else "  (partly done)")
        if not plan["enabled"].get(s, True):
            tag = "  (disabled)"
        print(f"    {STAGE_LABEL[s]:<26} ${rem[s]:7.2f}{tag}")
    print(f"    {'already spent (telemetry/)':<26} ${spent:7.2f}")
    print(f"    {'projected total':<26} ${projected:7.2f}"
          + (f"   cap DISTILL_MAX_USD ${cap:.2f}" if cap else "   (no cap: set DISTILL_MAX_USD)"))
    if warn is not None and projected > warn:
        print(f"  WARNING projected total ${projected:.2f} is over DISTILL_WARN_USD ${warn:.2f}")
    if cap is None or projected <= cap:
        return 0
    if os.environ.get("DISTILL_ALLOW_OVER_ESTIMATE", "0") == "1":
        print(f"  WARNING projected total ${projected:.2f} is over the cap ${cap:.2f}; starting anyway "
              f"(DISTILL_ALLOW_OVER_ESTIMATE=1). The run stops cleanly when actual spend reaches the cap.")
        return 0
    new_cap = suggested_cap(spent, remaining)
    cmd = resume_command(new_cap)
    write_status(a.output, "over_estimate", stage_reached="pre-run estimate", spent_usd=round(spent, 4),
                 max_usd=cap, projected_total_usd=round(projected, 2), suggested_max_usd=new_cap,
                 resume_command=cmd)
    print(f"  REFUSING TO START: projected ${projected:.2f} > DISTILL_MAX_USD ${cap:.2f} (no model call made).")
    print(f"  Raise the cap, or set DISTILL_ALLOW_OVER_ESTIMATE=1 to start anyway and stop at the cap:")
    print(f"    {cmd}")
    return EXIT_BUDGET


def cmd_gate(a) -> int:
    cap = max_usd()
    if cap is None:
        return 0
    est = _load_estimate(a.output)
    table = {k: tuple(v) for k, v in (est or {}).get("unit_costs", {}).items()} or unit_costs()[0]
    full = (est or {}).get("full_usd_by_stage") or {}
    nxt = float(full.get(a.stage, 0.0)) if full else 0.0
    if not full and est is None:
        # no pre-run estimate (e.g. a stage run standalone): use the table with a 1-page guess
        nxt = table.get(a.stage, (0.0, "run"))[0]
    spent = spent_usd(a.output)
    warn = warn_usd()
    if warn is not None and spent >= warn:
        print(f"  [budget] WARNING spend ${spent:.2f} is at or over DISTILL_WARN_USD ${warn:.2f} (cap ${cap:.2f})")
    if spent + nxt > cap:
        stop(a.output, a.stage, f"Next stage estimate ${nxt:.2f} + spent ${spent:.2f} > cap ${cap:.2f}; "
                                f"stopped before starting it.", next_usd=nxt)
        return EXIT_BUDGET
    print(f"  [budget] {STAGE_LABEL.get(a.stage, a.stage)}: spent ${spent:.2f} + estimate ${nxt:.2f} "
          f"<= cap ${cap:.2f}")
    return 0


def cmd_stop(a) -> int:
    if max_usd() is None:
        print("budget: stop called without DISTILL_MAX_USD", file=sys.stderr)
    stop(a.output, a.stage, a.reason or "The in-loop guard refused the next call.")
    return EXIT_BUDGET


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run budget guard (DISTILL_MAX_USD)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("estimate")
    e.add_argument("--output", required=True)
    e.add_argument("--pages-dir", required=True)
    e.add_argument("--stills", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--output", required=True)
    g.add_argument("--stage", required=True, choices=STAGE_ORDER)
    s = sub.add_parser("stop")
    s.add_argument("--output", required=True)
    s.add_argument("--stage", required=True, choices=STAGE_ORDER)
    s.add_argument("--reason", default="")
    r = sub.add_parser("remaining")
    r.add_argument("--output", required=True)
    sp = sub.add_parser("spent")
    sp.add_argument("--output", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "estimate":
        return cmd_estimate(a)
    if a.cmd == "gate":
        return cmd_gate(a)
    if a.cmd == "stop":
        return cmd_stop(a)
    if a.cmd == "remaining":
        cap = max_usd()
        print("inf" if cap is None else f"{max(0.0, cap - spent_usd(a.output)):.4f}")
        return 0
    print(f"{spent_usd(a.output):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
