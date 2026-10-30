#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""B3 regression / impact analysis evaluation.

Usage:
  python3 eval/benefits/impact_eval.py --impact IMPACT.yaml --source-root SRC \
      --adapter raw-code|spec|command [--spec spec.md] \
      [--command "python3 eval/lean/impact.py --run-dir X --change {change}"] \
      [--key KEY.yaml] [--no-llm] --out OUT [--max-usd 1.0]

impact.yaml: {changes: [{id, change, affected_locations: [{file, start_line, end_line[, duplicate_group]}],
              affected_rules: [rule ids or statements], affected_reports: [names], affected_contracts: [names]}]}
Locations sharing a duplicate_group are copies of the same logic; "all copies found" requires every one.

The shipped synthetic-corpus schema is accepted too, and normalised on load (load_changes):
  {scenarios: [{id, change, affected_locations: {must_change: [{file, start_line, end_line, why}],
                must_review: [...]}, affected_rules, affected_reports, affected_external_contract: "Yes. ..."}]}
  - bucketed locations are flattened, each keeping ``bucket`` (must_change / must_review) and
    ``why`` -> ``reason``; ``location_recall_must_change_pct`` is reported alongside the overall recall;
  - ``affected_external_contract`` (prose) becomes ``affected_contracts`` = the interface identifiers it
    names (camelCase fields such as requiresExecApproval, CODE_LIKE values such as JOB_CLOSED). The prose
    is kept as ``contract_note``; a change whose note names no identifier has no contract to match.
A key that loads zero changes is an error (exit 2), never an empty "ok".

The adapter must return {affected_locations: [{file, start_line, end_line, reason}], affected_rules: [str],
affected_reports: [str], affected_contracts: [str]}.

Metrics per change and overall:
- location recall / precision at file+line-range granularity (overlap), and at file granularity;
- rule recall: LLM judge decides which expected rules the answer covers (--no-llm: rule counts as
  found when a predicted location overlaps the rule's key locations; needs --key);
- report / contract recall: case-insensitive name match against the tool's lists or answer text;
- all-duplicate-copies-found rate.
Outputs OUT/impact.json and OUT/impact.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import re

from common import (Budget, BudgetExceeded, KeyLoadError, Plan, Source, add_adapter_args, call_json,
                    citations_from, load_yaml, loc_overlap, make_adapter, md_table, measurement, pct,
                    same_file, score_mod, validate_citation, write_outputs, write_plan)

IMPACT_INSTR = """The CHANGE above is proposed for this system. Perform a regression / impact analysis.
List EVERY source location that would have to change or be re-tested (including every duplicated copy
of the same logic, database objects and reports), the business rules affected, any reports affected,
and any external contracts (APIs, stored procedures, file formats, integrations) whose behaviour changes.
Return JSON only:
{"affected_locations": [{"file": "X.vb", "start_line": 10, "end_line": 20, "reason": "..."}],
 "affected_rules": ["plain statement of each affected business rule"],
 "affected_reports": ["report name"], "affected_contracts": ["API / procedure / interface name"]}"""

JUDGE_SYSTEM = "You are a strict grader of software impact analyses. Answer only with JSON."
JUDGE_PROMPT = """For each change below, decide which EXPECTED business rules the tool's analysis covers.
A rule is covered if the analysis names the rule or clearly identifies the logic it governs as affected
(its listed rules or location reasons). Do not credit topic-only mentions.

{items}

Return JSON only: {{"changes": [{{"id": "I-1", "covered": ["R-1"], "note": "<= 20 words"}}]}}
"""


#: interface identifiers inside a prose contract note: camelCase fields, CODE_LIKE values
CONTRACT_ID_RE = re.compile(r"\b(?:[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][A-Z0-9]*_[A-Z0-9_]+)\b")


def contract_identifiers(note: str) -> list[str]:
    seen, out = set(), []
    for m in CONTRACT_ID_RE.findall(note or ""):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _flatten_locations(locs) -> list[dict]:
    """[{...}] stays; {bucket: [{...}]} becomes [{..., bucket}] in bucket order."""
    if isinstance(locs, dict):
        out = []
        for bucket, items in locs.items():
            for l in items or []:
                out.append(dict(l, bucket=bucket))
        locs = out
    out = []
    for l in locs or []:
        if not isinstance(l, dict):
            continue
        l = dict(l)
        if "reason" not in l and "why" in l:
            l["reason"] = l.pop("why")
        out.append(l)
    return out


def load_changes(path: Path) -> list[dict]:
    d = load_yaml(path)
    if isinstance(d, dict):
        cs = d.get("changes") or d.get("scenarios") or d.get("items") or []
    else:
        cs = d or []
    cs = [c for c in cs if isinstance(c, dict) and c.get("change")]
    if not cs:
        raise KeyLoadError(path, "changes",
                           "expected {changes: [...]} or {scenarios: [...]}, each with id and change")
    for c in cs:
        c["affected_locations"] = _flatten_locations(c.get("affected_locations"))
        for k in ("affected_rules", "affected_reports"):
            c[k] = c.get(k) or []
        if not c.get("affected_contracts") and c.get("affected_external_contract"):
            note = " ".join(str(c["affected_external_contract"]).split())
            c["contract_note"] = note
            c["affected_contracts"] = contract_identifiers(note)
        c["affected_contracts"] = c.get("affected_contracts") or []
    return cs


def name_hit(name, pred_names: list[str], text: str) -> bool:
    # YAML reads a bare report number (e.g. `615`) as an int, so coerce before matching.
    n = str(name).lower().strip()
    return any(n in p.lower() or p.lower() in n for p in pred_names if len(p) > 2) or n in text.lower()


def score_change(c: dict, d: dict, src: Source, key_items: dict) -> dict:
    preds = [validate_citation(p, src) for p in citations_from(d, "affected_locations")]
    reasons = [str(x.get("reason", "")) for x in d.get("affected_locations") or [] if isinstance(x, dict)]
    exp = c["affected_locations"]
    hit_exp = [any(loc_overlap(p, e, src) for p in preds if p["valid"]) for e in exp]
    must = [h for e, h in zip(exp, hit_exp) if e.get("bucket") == "must_change"]
    hit_pred = [any(loc_overlap(p, e, src) for e in exp) for p in preds]
    exp_files = {src.resolve(e["file"]) or e["file"] for e in exp}
    pred_files = {p["resolved"] for p in preds if p["resolved"]}
    groups = {}
    for e, h in zip(exp, hit_exp):
        if e.get("duplicate_group"):
            groups.setdefault(e["duplicate_group"], []).append(h)
    rules_pred = [str(x) for x in d.get("affected_rules") or []]
    text = json.dumps(d)
    # deterministic rule hit: a predicted location overlaps the rule's key locations
    rule_loc_hit = {}
    for rid in c["affected_rules"]:
        k = key_items.get(rid)
        if k and k.get("locations"):
            rule_loc_hit[rid] = any(loc_overlap(p, l, src) for p in preds if p["valid"] for l in k["locations"])
    return {
        "predicted_locations": preds, "location_reasons": reasons, "predicted_rules": rules_pred,
        "predicted_reports": d.get("affected_reports") or [], "predicted_contracts": d.get("affected_contracts") or [],
        "expected_hit": [{"loc": e, "hit": h} for e, h in zip(exp, hit_exp)],
        "loc_tp": sum(hit_exp), "loc_n_expected": len(exp),
        "must_change_tp": sum(must), "must_change_n": len(must),
        "loc_pred_tp": sum(hit_pred), "loc_n_pred": len(preds),
        "file_tp": len(exp_files & pred_files), "file_n_expected": len(exp_files), "file_n_pred": len(pred_files),
        "invalid_locations": sum(1 for p in preds if not p["valid"]),
        "duplicate_groups": {g: all(v) for g, v in groups.items()},
        "all_copies_found": all(all(v) for v in groups.values()) if groups else None,
        "rule_loc_hit": rule_loc_hit,
        "reports_hit": {n: name_hit(n, [str(x) for x in d.get("affected_reports") or []], text)
                        for n in c["affected_reports"]},
        "contracts_hit": {n: name_hit(n, [str(x) for x in d.get("affected_contracts") or []], text)
                          for n in c["affected_contracts"]},
    }


def judge_rules(budget: Budget, rows: list[dict], key_items: dict) -> dict:
    parts = []
    for r in rows:
        if not r["affected_rules"]:
            continue
        exp = "\n".join(f"  {rid}: {' '.join(key_items[rid]['statement'].split()) if rid in key_items else rid}"
                        for rid in r["affected_rules"])
        reasons = "\n".join(f"  - {x}" for x in r["location_reasons"][:30])
        rules = "\n".join(f"  - {x}" for x in r["predicted_rules"][:30])
        parts.append(f"### {r['id']}\nCHANGE: {r['change']}\nEXPECTED RULES:\n{exp}\n"
                     f"TOOL AFFECTED RULES:\n{rules or '  (none)'}\nTOOL LOCATION REASONS:\n{reasons or '  (none)'}\n")
    if not parts:
        return {}
    data, _ = call_json(budget, JUDGE_PROMPT.format(items="\n".join(parts)), system=JUDGE_SYSTEM,
                        max_tokens=3000, expected_out=120 * len(parts), tag="benefits:impact:judge")
    return {str(c.get("id")): c for c in (data or {}).get("changes", [])}


#: measured on the JC run: 27,906 output tokens over 9 impact answers (~3,100 each)
ANSWER_OUT_TOKENS = 3000


def load_key_items(path: Path | None) -> dict:
    key_items = {k["id"]: k for k in score_mod.load_key(path)["items"]} if path else {}
    for k in key_items.values():
        if "locations" not in k and k.get("citations"):
            k["locations"] = [dict(zip(("file", "start_line", "end_line"), score_mod.parse_key_cit(c)))
                              for c in k["citations"]]
    return key_items


def judge_prompt_estimate_chars(changes: list[dict], key_items: dict) -> int:
    """Rule-judge prompt size before the answers exist (answers assumed ~3000 chars each)."""
    n = len(JUDGE_PROMPT) + len(JUDGE_SYSTEM)
    for c in changes:
        if not c["affected_rules"]:
            continue
        n += len(c["change"]) + 3000 + sum(
            len(" ".join(key_items[r]["statement"].split())) if r in key_items else len(str(r))
            for r in c["affected_rules"])
    return n


def plan(a) -> dict:
    budget = Budget(a.max_usd, a.out)
    src = Source(a.source_root, a.exclude)
    key_items = load_key_items(a.key)
    adapter = make_adapter(a, budget, src)
    changes = load_changes(a.impact)[: a.limit or None]
    pl = Plan()
    unknown = 0
    for c in changes:
        n = adapter.prompt_chars("impact", {"id": c["id"], "change": c["change"]}, IMPACT_INSTR)
        if n is None:
            unknown += 1
        else:
            pl.add("answer", n, ANSWER_OUT_TOKENS)
    if not a.no_llm and any(c["affected_rules"] for c in changes):
        pl.add("rule_judge", judge_prompt_estimate_chars(changes, key_items),
               120 * sum(1 for c in changes if c["affected_rules"]))
    return pl.summary(changes=len(changes), adapter=adapter.name, context=adapter.context_summary(),
                      answers_with_unknown_cost=unknown)


def run(a) -> dict:
    budget = Budget(a.max_usd, a.out)
    src = Source(a.source_root, a.exclude)
    key_items = load_key_items(a.key)
    adapter = make_adapter(a, budget, src)
    changes = load_changes(a.impact)[: a.limit or None]
    rows, judged, judge_ran, stopped = [], {}, False, None
    try:
        for c in changes:
            # keep back enough for the one rule-judge call that grades every answer
            reserve = 0.0 if a.no_llm else budget.estimate(
                judge_prompt_estimate_chars(changes, key_items), 120 * len(changes))
            r = adapter.ask("impact", {"id": c["id"], "change": c["change"]}, IMPACT_INSTR,
                            expected_out=ANSWER_OUT_TOKENS, reserve=reserve)
            row = dict(c, **score_change(c, r["data"] or {}, src, key_items),
                       tokens_in=r["tokens_in"], tokens_out=r["tokens_out"], cost_usd=r["cost_usd"],
                       latency_s=r["latency_s"], error=r["error"], context=r.get("context"))
            rows.append(row)
            print(f"  {c['id']}: {row['loc_tp']}/{row['loc_n_expected']} expected locations hit, "
                  f"{row['loc_n_pred']} predicted ({r['latency_s']}s)")
    except BudgetExceeded as e:
        print(f"STOPPED: {e}")
        stopped = str(e)
    if rows and not a.no_llm:
        try:
            judged = judge_rules(budget, rows, key_items)
            judge_ran = True
        except BudgetExceeded as e:
            print(f"STOPPED (rule judge): {e}")
            stopped = (stopped + "; " if stopped else "") + str(e)
    for r in rows:
        j = judged.get(r["id"])
        if a.no_llm:
            # deterministic: a rule counts as found when a predicted location overlaps its key
            # locations; only rules that HAVE key locations can be assessed this way
            r["rules_assessed"] = sorted(r["rule_loc_hit"])
            r["rules_covered"] = [rid for rid, h in r["rule_loc_hit"].items() if h]
            r["rule_method"] = "location" if r["rule_loc_hit"] else "none"
        elif j is not None:
            r["rules_assessed"] = list(r["affected_rules"])
            r["rules_covered"] = [x for x in j.get("covered", []) if x in r["affected_rules"]]
            r["rule_method"] = "llm"
            r["rule_note"] = j.get("note", "")
        else:
            # the judge did not grade this change: its rule recall is NOT measured (never 0)
            r["rules_assessed"] = []
            r["rules_covered"] = []
            r["rule_method"] = "ungraded" if r["affected_rules"] else "none"
        # graded = everything this change asks for was assessed (a change with no rules, or a
        # --no-llm run with no --key to resolve rules, has nothing more to assess)
        r["rules_graded"] = (bool(r["rules_assessed"]) or not r["affected_rules"]
                             or (a.no_llm and not key_items))
    return summarize(a, rows, budget, adapter, len(changes), stopped, judge_ran)


def summarize(a, rows, budget, adapter, n_total, stopped=None, judge_ran=None) -> dict:
    S = lambda k: sum(r[k] for r in rows)  # noqa: E731
    dup = [r["all_copies_found"] for r in rows if r["all_copies_found"] is not None]
    groups = [v for r in rows for v in r["duplicate_groups"].values()]
    n_rules = sum(len(r.get("rules_assessed") or []) for r in rows)
    rep = [h for r in rows for h in r["reports_hit"].values()]
    con = [h for r in rows for h in r["contracts_hit"].values()]
    rl = [h for r in rows for h in r["rule_loc_hit"].values()]
    graded = sum(1 for r in rows if r.get("rules_graded"))
    reason = None
    if graded < n_total:
        reason = (f"{len(rows)} of {n_total} changes answered, rule coverage graded for {graded}"
                  + (f"; {stopped}" if stopped else ""))
    meas = measurement(n_total, graded, what="changes", reason=reason)
    s = {
        "measurement_status": meas["status"],
        "changes": len(rows), "changes_total": n_total,
        "location_recall_pct": pct(S("loc_tp"), S("loc_n_expected")),
        "location_recall_must_change_pct": pct(S("must_change_tp"), S("must_change_n")),
        "location_precision_pct": pct(S("loc_pred_tp"), S("loc_n_pred")),
        "file_recall_pct": pct(S("file_tp"), S("file_n_expected")),
        "file_precision_pct": pct(S("file_tp"), S("file_n_pred")),
        # judged (or, with --no-llm, location-based): over GRADED changes only; null if none
        "rule_recall_pct": pct(sum(len(r["rules_covered"]) for r in rows), n_rules),
        "rule_recall_by_location_pct": pct(sum(rl), len(rl)),
        "report_recall_pct": pct(sum(rep), len(rep)),
        "contract_recall_pct": pct(sum(con), len(con)),
        "all_copies_found_pct": pct(sum(dup), len(dup)),
        "duplicate_groups_complete_pct": pct(sum(groups), len(groups)),
        # counts over answered changes; null (not 0) when nothing was answered
        "invalid_locations": S("invalid_locations") if rows else None,
        "predicted_locations": S("loc_n_pred") if rows else None,
        "answer_tokens_in": S("tokens_in"), "answer_tokens_out": S("tokens_out"),
        "answer_cost_usd": round(S("cost_usd"), 4),
        "latency_avg_s": round(S("latency_s") / len(rows), 2) if rows else None,
        "errors": sum(1 for r in rows if r["error"]),
    }
    return {"benefit": "B3 impact analysis", "script": "impact_eval", "tool": a.tool_name or adapter.name,
            "adapter": adapter.name, "impact": str(a.impact), "source_root": str(a.source_root),
            "rule_method": "location" if a.no_llm else "llm",
            "context_truncated": getattr(adapter, "truncated", None),
            "context": adapter.context_summary(),
            "measurement": meas, "summary": s, "budget": budget.summary(), "changes": rows}


def to_markdown(r: dict) -> str:
    L = [f"# B3 impact analysis: {r['tool']}", "", f"- Adapter: `{r['adapter']}`, set: `{r['impact']}`",
         f"- Rule matching: {r['rule_method']}",
         f"- Measurement: **{r['measurement']['status']}**"
         + (f" ({r['measurement']['reason']})" if r['measurement'].get('reason') else ""),
         f"- Spend: ${r['budget']['cost_usd']:.4f} of ${r['budget']['max_usd']:.2f}"
         + (f" -- **{r['budget']['stopped']}**" if r['budget']['stopped'] else ""), ""]
    L += md_table(list(r["summary"].items()), ("Metric", "Value"))
    L += ["", "## Per change", ""]
    L += md_table([(c["id"], f"{c['loc_tp']}/{c['loc_n_expected']}", f"{c['loc_pred_tp']}/{c['loc_n_pred']}",
                    f"{c['file_tp']}/{c['file_n_expected']}",
                    f"{len(c['rules_covered'])}/{len(c['affected_rules'])}", c["all_copies_found"],
                    ", ".join(k for k, v in c["contracts_hit"].items() if not v) or "-", c["latency_s"])
                   for c in r["changes"]],
                  ("Change", "Expected locs hit", "Predicted locs correct", "Files hit", "Rules",
                   "All copies", "Missed contracts", "Latency s"))
    L += ["", "## Missed expected locations", ""]
    for c in r["changes"]:
        for e in c["expected_hit"]:
            if not e["hit"]:
                l = e["loc"]
                L.append(f"- {c['id']}: `{l['file']}:{l['start_line']}-{l.get('end_line')}`"
                         + (f" (copy group {l['duplicate_group']})" if l.get("duplicate_group") else ""))
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--impact", required=True, type=Path)
    ap.add_argument("--key", type=Path, help="answer key, to resolve rule ids to statements/locations")
    ap.add_argument("--no-llm", action="store_true", help="no rule judge; rules matched by key locations")
    ap.add_argument("--limit", type=int, default=0)
    add_adapter_args(ap)
    a = ap.parse_args()
    if a.dry_run:
        write_plan(a.out, "impact", plan(a), "impact_eval")
        return
    r = run(a)
    write_outputs(a.out, "impact", r, to_markdown(r))
    print(json.dumps({"summary": r["summary"], "budget": r["budget"]}, indent=2))


if __name__ == "__main__":
    main()
