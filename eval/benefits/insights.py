#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""B1 modernization insight: does a tool surface duplicated logic, logic flaws, undocumented features
and inconsistencies -- and does it explicitly FLAG them as issues rather than just describe them?

Usage:
  # score a pipeline run's Issues & Insights report (<run>/issues/issues.md) -- the default artifact
  python3 eval/benefits/insights.py --run output/<run> --key KEY.yaml --source-root SRC --out OUT
  # score any other tool output (overrides --run)
  python3 eval/benefits/insights.py --spec SPEC.md|SPEC.json --key KEY.yaml --source-root SRC --out OUT
  # or generate the assessment first with an adapter (raw-code baseline, spec, or external command)
  python3 eval/benefits/insights.py --adapter raw-code --key KEY.yaml --source-root SRC --out OUT
  options: [--no-llm] [--max-usd 1.0] [--tool-name NAME] [--exclude GLOB] [--segment auto|lines]

Matching reuses eval/score.py unchanged (score.score(): segmentation, citation checks, LLM judge or
deterministic matcher). On top of that, every finding is classified for which issue categories it
explicitly flags (LLM classifier, or keyword rules with --no-llm). Key items carry flags
duplicated_logic, logic_flaw, undocumented_feature, inconsistency, dead_code (issue categories) and
comment_only, db_only (evidence-location categories). Per flag category we report:
- described recall: items matched by any finding (full / any degree), from score.py;
- flagged recall: items matched by a finding that explicitly flags the SAME category
  (and, leniently, ANY issue category);
- flag precision: of findings that flag the category, share matched to a key item carrying that
  flag (keyed) / matched to any key item or judged a true extra (true, LLM only).
Outputs OUT/insights.json, OUT/insights.md, and OUT/score.{json,md} from score.py.

Which artifact: B1 asks whether issues are FLAGGED, and the pipeline's flagging artifact is the
Issues & Insights report, so ``--run DIR`` scores ``DIR/issues/issues.md``. Scoring the business
spec here would only repeat the judge work of the suite's ``score-bottled`` stage at the same cost.

Unmeasured is never zero: if the budget stops the run before judging, every judged metric is null
and ``measurement.status`` is ``not_measured``. ``--dry-run`` prints the cost plan (the score.py judge
plan for this artifact plus the flag classifier) and calls nothing. A key with no items is an error.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from common import (Budget, BudgetExceeded, KeyLoadError, Plan, Source, call_json, make_adapter, md_table,
                    measurement, pct, score_mod, write_outputs, write_plan)

from judge.runner import JudgeBudgetExceeded  # noqa: E402  (eval/ is on sys.path via common)

#: the Issues & Insights report inside a pipeline run directory
ISSUES_REPORT = Path("issues") / "issues.md"

ISSUE_FLAGS = ["duplicated_logic", "logic_flaw", "undocumented_feature", "inconsistency", "dead_code"]
EVIDENCE_FLAGS = ["comment_only", "db_only"]

KEYWORDS = {
    "duplicated_logic": r"duplicat|copy of|copied|same logic|repeated|two places|re-?implement",
    "logic_flaw": r"\bbug\b|flaw|defect|off[- ]by[- ]one|incorrect(?:ly)?|\bwrong\b|error[- ]prone|boundary",
    "undocumented_feature": r"undocumented|hidden|back ?door|bypass|not documented|secret|magic",
    "inconsistency": r"inconsisten|mismatch|disagree|contradict|differ|discrepan|stale comment|conflict",
    "dead_code": r"dead code|never called|unused|unreachable|not referenced|never invoked",
}

GEN_INSTR = """Produce a modernization assessment of this system for a rewrite team.
List the business rules, and explicitly FLAG every issue a rewrite must decide on:
duplicated logic, logic flaws/bugs, undocumented features or behaviours, inconsistencies (code vs
comments, code vs database, between copies), and dead code. Cite file and line ranges for each.
Return JSON only:
{"findings": [{"statement": "...", "issue_type": "none|duplicated_logic|logic_flaw|undocumented_feature|inconsistency|dead_code",
  "citations": [{"file": "X.vb", "start_line": 10, "end_line": 20}]}]}"""

CLS_SYSTEM = "You classify findings from a code-analysis report. Answer only with JSON."
CLS_PROMPT = """For each finding, list the issue categories it EXPLICITLY flags as a problem, risk or
anomaly to be addressed. Merely describing behaviour does not count (e.g. "total uses 1.08" is not a
flag; "the two total calculations disagree" flags inconsistency). Categories:
duplicated_logic (says logic is duplicated/copied), logic_flaw (says there is a bug/flaw/off-by-one/
wrong result), undocumented_feature (says a behaviour is undocumented/hidden/a bypass),
inconsistency (says two sources disagree: code vs comment, code vs DB, copy vs copy),
dead_code (says code is unused/unreachable/never called).

Findings:
{findings}

Return JSON only: {{"flags": [{{"finding_id": "F1", "flags": ["inconsistency"]}}]}}
"""


#: judged summary metrics reported as null when the run stops before judging
NULL_SUMMARY_KEYS = ("findings", "issue_items", "issue_described_any_pct", "issue_flagged_same_pct",
                     "issue_flagged_any_pct", "findings_flagging_issues", "flag_precision_keyed_pct",
                     "flag_precision_true_pct", "overall_recall_full_pct", "overall_recall_any_pct",
                     "incorrect_findings")


def generate_spec(a, budget: Budget, src: Source) -> tuple[Path, dict]:
    adapter = make_adapter(a, budget, src)
    r = adapter.ask("insights", {"id": "insights", "request": "modernization assessment"}, GEN_INSTR,
                    expected_out=4000)
    d = r["data"] or {}
    lines = [f"# Modernization assessment ({a.tool_name or adapter.name})", ""]
    fs = d.get("findings") or d.get("items")
    if isinstance(fs, list) and fs:
        for f in fs:
            if isinstance(f, str):
                lines.append(f"- {f}")
                continue
            cits = []
            for c in f.get("citations") or []:
                if isinstance(c, dict) and c.get("file"):
                    a_, b_ = c.get("start_line"), c.get("end_line", c.get("start_line"))
                    cits.append(f"{c['file']}:L{a_}-L{b_}" if a_ else c["file"])
                elif isinstance(c, str):
                    cits.append(c)
            it = f.get("issue_type") or "none"
            tag = f"[ISSUE: {it}] " if it and it != "none" else ""
            lines.append(f"- {tag}{f.get('statement', '')} ({'; '.join(cits)})")
    else:
        lines.append(str(d.get("answer") or r.get("raw_text") or ""))
    spec = a.out / "insights_spec.md"
    a.out.mkdir(parents=True, exist_ok=True)
    spec.write_text("\n".join(lines) + "\n")
    gen = {k: r[k] for k in ("tokens_in", "tokens_out", "cost_usd", "latency_s", "error")}
    gen["adapter"] = adapter.name
    return spec, gen


def classify_keywords(findings: list[dict]) -> dict:
    out = {}
    for f in findings:
        t = f["text"].lower()
        out[f["id"]] = [k for k, rx in KEYWORDS.items() if re.search(rx, t)]
        m = re.search(r"\[issue:\s*(\w+)\]", t)
        if m and m.group(1) in ISSUE_FLAGS and m.group(1) not in out[f["id"]]:
            out[f["id"]].append(m.group(1))
    return out


def classify_llm(budget: Budget, findings: list[dict], batch: int = 60) -> dict:
    out = {}
    for i in range(0, len(findings), batch):
        chunk = findings[i:i + batch]
        txt = "\n".join(f"{f['id']}: {' '.join(f['text'].split())[:600]}" for f in chunk)
        data, _ = call_json(budget, CLS_PROMPT.format(findings=txt), system=CLS_SYSTEM, max_tokens=4000,
                            expected_out=25 * len(chunk) + 100, tag="benefits:insights:classify")
        for x in (data or {}).get("flags", []):
            out[str(x.get("finding_id"))] = [c for c in x.get("flags", []) if c in ISSUE_FLAGS]
    for f in findings:
        out.setdefault(f["id"], [])
    return out


def analyse(report: dict, items: list[dict], flags_by_f: dict, method: str) -> dict:
    cov = report["key_coverage"]
    by_key: dict[str, list[dict]] = {}
    for f in report["findings"]:
        f["flags_raised"] = flags_by_f.get(f["id"], [])
        for m in f["judgement"]["matches"]:
            by_key.setdefault(m["key_id"], []).append(f)
    item_flags = {k["id"]: set(k.get("flags") or []) for k in items}
    per = {}
    for cat in ISSUE_FLAGS + EVIDENCE_FLAGS:
        ks = [k for k in items if cat in item_flags[k["id"]]]
        raised = [f for f in report["findings"] if cat in f["flags_raised"]]
        row = {"items": len(ks),
               "described_full": sum(1 for k in ks if cov[k["id"]] == "full"),
               "described_any": sum(1 for k in ks if cov[k["id"]] != "missed")}
        if cat in ISSUE_FLAGS:
            row["flagged_same"] = sum(1 for k in ks if any(cat in f["flags_raised"] for f in by_key.get(k["id"], [])))
            row["flagged_any_issue"] = sum(1 for k in ks if any(f["flags_raised"] for f in by_key.get(k["id"], [])))
            row["findings_flagging"] = len(raised)
            row["flag_keyed_tp"] = sum(1 for f in raised
                                       if any(cat in item_flags.get(m["key_id"], ()) for m in f["judgement"]["matches"]))
            row["flag_true_tp"] = sum(1 for f in raised if f["judgement"]["matches"]
                                      or f["judgement"]["verdict"] == "correct_extra") if method == "llm" else None
        else:  # evidence-location categories: was the item found, and flagged as any issue?
            row["flagged_same"] = None
            row["flagged_any_issue"] = sum(1 for k in ks if any(f["flags_raised"] for f in by_key.get(k["id"], [])))
            row["findings_flagging"] = row["flag_keyed_tp"] = row["flag_true_tp"] = None
        n = row["items"]
        row.update({
            "described_recall_full_pct": pct(row["described_full"], n),
            "described_recall_any_pct": pct(row["described_any"], n),
            "flagged_recall_pct": pct(row["flagged_same"], n) if row["flagged_same"] is not None else None,
            "flagged_any_issue_recall_pct": pct(row["flagged_any_issue"], n),
            "flag_precision_keyed_pct": pct(row["flag_keyed_tp"], row["findings_flagging"])
            if row["findings_flagging"] else None,
            "flag_precision_true_pct": pct(row["flag_true_tp"], row["findings_flagging"])
            if row["findings_flagging"] and row["flag_true_tp"] is not None else None,
        })
        per[cat] = row
    issue_items = [k for k in items if item_flags[k["id"]] & set(ISSUE_FLAGS)]
    flagged_findings = [f for f in report["findings"] if f["flags_raised"]]
    summary = {
        "key_items": len(items), "findings": len(report["findings"]),
        "issue_items": len(issue_items),
        "issue_described_any_pct": pct(sum(1 for k in issue_items if cov[k["id"]] != "missed"), len(issue_items)),
        "issue_flagged_same_pct": pct(sum(1 for k in issue_items if any(
            item_flags[k["id"]] & set(f["flags_raised"]) for f in by_key.get(k["id"], []))), len(issue_items)),
        "issue_flagged_any_pct": pct(sum(1 for k in issue_items if any(
            f["flags_raised"] for f in by_key.get(k["id"], []))), len(issue_items)),
        "findings_flagging_issues": len(flagged_findings),
        "flag_precision_keyed_pct": pct(sum(1 for f in flagged_findings if any(
            item_flags.get(m["key_id"], set()) & set(f["flags_raised"]) for m in f["judgement"]["matches"])),
            len(flagged_findings)),
        "flag_precision_true_pct": pct(sum(1 for f in flagged_findings if f["judgement"]["matches"]
                                           or f["judgement"]["verdict"] == "correct_extra"), len(flagged_findings))
        if method == "llm" else None,
        "overall_recall_full_pct": report["recall"]["strict_full_pct"],
        "overall_recall_any_pct": report["recall"]["any_match_pct"],
        "incorrect_findings": report["precision"]["incorrect"],
    }
    for cat in ISSUE_FLAGS + EVIDENCE_FLAGS:
        if per[cat]["items"]:
            summary[f"{cat}_described_pct"] = per[cat]["described_recall_any_pct"]
            if cat in ISSUE_FLAGS:
                summary[f"{cat}_flagged_pct"] = per[cat]["flagged_recall_pct"]
    return {"summary": summary, "per_flag": per,
            "items": [{"id": k["id"], "flags": sorted(item_flags[k["id"]]), "coverage": cov[k["id"]],
                       "flagged_as": sorted({c for f in by_key.get(k["id"], []) for c in f["flags_raised"]}),
                       "statement": " ".join(k["statement"].split())} for k in items]}


def resolve_artifact(a) -> Path | None:
    """--spec wins; else --run DIR -> DIR/issues/issues.md (must exist); else None (generate)."""
    if a.spec:
        return a.spec
    if a.run:
        p = Path(a.run) / ISSUES_REPORT
        if not p.is_file():
            raise SystemExit(f"--run {a.run}: no Issues & Insights report at {p}; pass --spec to score "
                             f"another artifact")
        return p
    return None


def load_key_checked(path: Path):
    items = score_mod.load_key(path)["items"]
    if not items:
        raise KeyLoadError(path, "answer-key items", "expected {items: [...]}")
    return items


def classify_calls(n_findings: int, avg_chars: int, batch: int = 60) -> list[tuple[int, int]]:
    """(prompt chars, expected output tokens) of each flag-classifier call."""
    out = []
    for i in range(0, n_findings, batch):
        n = min(batch, n_findings - i)
        out.append((len(CLS_PROMPT) + len(CLS_SYSTEM) + n * (min(avg_chars, 600) + 8), 25 * n + 100))
    return out


def classify_estimate(budget: Budget, n_findings: int, avg_chars: int, batch: int = 60) -> float:
    return sum(budget.estimate(c, o) for c, o in classify_calls(n_findings, avg_chars, batch))


def plan(a) -> dict:
    budget = Budget(a.max_usd, a.out)
    spec = resolve_artifact(a)
    load_key_checked(a.key)
    if spec is None:
        return Plan().summary(note="--adapter generation: assessment cost depends on the adapter; "
                                   "the scoring cost cannot be planned before it exists",
                              est_unknown=True)
    pl = Plan()
    if a.no_llm:
        return pl.summary(artifact=str(spec), note="--no-llm: no Bedrock calls")
    rep_, _ = score_mod.score(spec, a.key, a.source_root, True, None, None, a.exclude, a.segment,
                              a.max_source_chars, "benefits:insights:judge", None,
                              judge_runs=a.judge_runs, dry_run_judge=True,
                              progress=lambda *x, **k: None)
    jp = rep_["judge_plan"]
    pl.calls.append({"what": "score_judge", "input_tokens": jp["est_input_tokens"],
                     "output_tokens": jp["est_output_tokens"], "cost_usd": jp["est_cost_usd"],
                     "note": f"{jp['calls']} judge calls, {jp.get('expected_cache_hits', 0)} expected "
                             f"from cache"})
    n_f = rep_["segmentation"]["findings"]
    avg = (rep_["segmentation"]["finding_chars"]["total"] // n_f) if n_f else 0
    for chars, out_tok in classify_calls(n_f, avg):
        pl.add("flag_classifier", chars, out_tok)
    return pl.summary(artifact=str(spec), findings=n_f, judge_plan=jp)


def to_markdown(r: dict) -> str:
    m = r.get("measurement") or {}
    L = [f"# B1 modernization insight: {r['tool']}", "", f"- Spec: `{r['spec']}`, key: `{r['key']}`",
         f"- Measurement: **{m.get('status')}**" + (f" ({m['reason']})" if m.get("reason") else ""),
         f"- Matcher: {r['method']}, flag classifier: {r['flag_classifier']}",
         f"- Spend: ${r['budget']['cost_usd']:.4f} of ${r['budget']['max_usd']:.2f}"
         + (f" -- **{r['budget']['stopped']}**" if r['budget']['stopped'] else ""), ""]
    L += md_table(list(r["summary"].items()), ("Metric", "Value"))
    L += ["", "## Per flag category", ""]
    L += md_table([(c, v["items"], v["described_recall_any_pct"], v["described_recall_full_pct"],
                    v["flagged_recall_pct"], v["flagged_any_issue_recall_pct"], v["findings_flagging"],
                    v["flag_precision_keyed_pct"], v["flag_precision_true_pct"])
                   for c, v in r["per_flag"].items() if v["items"] or v["findings_flagging"]],
                  ("Flag", "Items", "Described any %", "Described full %", "Flagged (same) %",
                   "Flagged (any issue) %", "Findings flagging", "Flag precision keyed %", "Flag precision true %"))
    L += ["", "## Key items", ""]
    L += md_table([(i["id"], ", ".join(i["flags"]), i["coverage"], ", ".join(i["flagged_as"]) or "-", i["statement"][:110])
                   for i in r["items"]], ("Item", "Key flags", "Coverage", "Flagged by tool as", "Statement"))
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path,
                    help="pipeline run directory: score its Issues & Insights report, "
                         "RUN/issues/issues.md (the default artifact for B1)")
    ap.add_argument("--spec", type=Path, help="tool output to score (overrides --run); omit both to "
                                              "generate one with --adapter")
    ap.add_argument("--adapter", choices=["raw-code", "spec", "command"],
                    help="generate the assessment first (raw-code baseline / from --gen-spec / external command)")
    ap.add_argument("--gen-spec", type=Path, help="spec.md used as context by --adapter spec")
    ap.add_argument("--command", help="command template for --adapter command (placeholders: {request}, {task})")
    ap.add_argument("--command-timeout", type=int, default=900)
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--source-root", required=True, type=Path)
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--segment", choices=["auto", "lines"], default="auto")
    ap.add_argument("--no-llm", action="store_true", help="deterministic matcher + keyword flag classifier")
    ap.add_argument("--max-source-chars", type=int, default=150000)
    ap.add_argument("--max-spec-chars", type=int, default=150000)
    ap.add_argument("--max-usd", type=float, default=1.0)
    ap.add_argument("--judge-runs", type=int, default=1,
                    help="score.py judge passes over the artifact (default 1)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print and write the cost plan (plan.est_cost_usd); no Bedrock calls")
    ap.add_argument("--tool-name")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    if not a.spec and not a.run and not a.adapter:
        raise SystemExit("give --run, --spec or --adapter")
    if a.dry_run:
        write_plan(a.out, "insights", plan(a), "insights")
        return
    a.spec = resolve_artifact(a)
    items_checked = load_key_checked(a.key)
    budget = Budget(a.max_usd, a.out)
    src = Source(a.source_root, a.exclude)
    gen = None
    stopped = None
    try:
        spec = a.spec
        if not spec:
            a.spec = a.gen_spec  # make_adapter reads a.spec for the spec adapter
            spec, gen = generate_spec(a, budget, src)
            print(f"  generated {spec} in {gen['latency_s']}s ({gen['tokens_in']}/{gen['tokens_out']} tok)")
        use_llm = not a.no_llm
        if use_llm:  # pre-check the score.py judge plan (cache hits are free) + the classifier
            rep_, _ = score_mod.score(spec, a.key, a.source_root, True, None, None, a.exclude, a.segment,
                                      a.max_source_chars, "benefits:insights:judge", None,
                                      judge_runs=a.judge_runs, dry_run_judge=True,
                                      progress=lambda *x, **k: None)
            jp = rep_["judge_plan"]
            n_f = rep_["segmentation"]["findings"]
            avg = (rep_["segmentation"]["finding_chars"]["total"] // n_f) if n_f else 0
            reserve = classify_estimate(budget, n_f, avg)
            if budget.spent + jp["est_cost_usd"] + reserve > budget.max_usd:
                budget.stopped = (f"budget guard: score.py judge estimated ${jp['est_cost_usd']:.4f} "
                                  f"+ classifier ${reserve:.4f}, spent ${budget.spent:.4f}, "
                                  f"cap ${budget.max_usd:g}")
                raise BudgetExceeded(budget.stopped)
        report, items = score_mod.score(spec, a.key, a.source_root, use_llm, None, None, a.exclude, a.segment,
                                        a.max_source_chars, "benefits:insights:judge", None,
                                        judge_runs=a.judge_runs,
                                        max_judge_cost_usd=max(0.0, budget.max_usd - budget.spent)
                                        if use_llm else None)
        if report.get("judge_usage"):
            u = report["judge_usage"]
            budget.add(u["input_tokens"], u["output_tokens"], u["cost_usd"])
        (a.out / "score.json").write_text(json.dumps(report, indent=2))
        (a.out / "score.md").write_text(score_mod.to_markdown(report, items))
        flags = classify_llm(budget, report["findings"]) if use_llm else classify_keywords(report["findings"])
    except (BudgetExceeded, JudgeBudgetExceeded) as e:
        print(f"STOPPED: {e}")
        stopped = str(e)
        meas = measurement(len(items_checked), 0, what="key items", reason=f"not judged: {stopped}")
        # every judged metric is null (not 0): nothing was graded
        summary = {"measurement_status": meas["status"], "key_items": len(items_checked),
                   **{k: None for k in NULL_SUMMARY_KEYS}}
        r = {"benefit": "B1 modernization insight", "script": "insights", "tool": a.tool_name or a.adapter or "spec",
             "spec": str(a.spec), "key": str(a.key), "measurement": meas,
             "summary": summary, "budget": budget.summary(), "error": stopped}
        write_outputs(a.out, "insights", r, f"# B1 modernization insight\n\n"
                      f"Measurement: **not_measured**. STOPPED: {stopped}\n")
        return
    res = analyse(report, items, flags, report["method"])
    meas = measurement(len(items), len(items), what="key items")
    res["summary"] = {"measurement_status": meas["status"], **res["summary"]}
    r = {"benefit": "B1 modernization insight", "script": "insights", "measurement": meas,
         "tool": a.tool_name or (a.adapter if gen else Path(spec).parent.name),
         "spec": str(spec), "key": str(a.key), "method": report["method"],
         "flag_classifier": "llm" if not a.no_llm else "keywords", "generation": gen,
         "summary": dict(res["summary"], **({"answer_tokens_in": gen["tokens_in"], "answer_tokens_out": gen["tokens_out"],
                                            "answer_cost_usd": gen["cost_usd"], "latency_s": gen["latency_s"]}
                                           if gen else {})),
         "per_flag": res["per_flag"], "items": res["items"], "budget": budget.summary(),
         "findings": [{"id": f["id"], "text": f["text"], "flags_raised": f["flags_raised"],
                       "matches": f["judgement"]["matches"], "verdict": f["judgement"]["verdict"]}
                      for f in report["findings"]]}
    write_outputs(a.out, "insights", r, to_markdown(r))
    print(json.dumps({"summary": r["summary"], "budget": r["budget"]}, indent=2))


if __name__ == "__main__":
    main()
