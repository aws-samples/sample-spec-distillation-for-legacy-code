#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""B4 spec-driven development: compatibility spec for a new Android field app.

Usage:
  python3 eval/benefits/fieldapp_eval.py --key FIELD-APP.yaml --source-root SRC \
      --adapter raw-code|spec|command [--spec spec.md] [--command "... {request}"] \
      --out OUT [--max-usd 1.0] [--request "custom request text"]

field-app.yaml: {items: [{id, category, requirement, locations: [{file, start_line, end_line}]}]}
(categories e.g. payload, required_field, server_validation, status, limit).
The shipped synthetic-corpus schema {requirements: [{id, kind, requirement, enforced_by, locations}]}
is accepted too: ``requirements`` is read as ``items`` and ``kind`` as ``category``. A key that loads
zero items is an error (exit 2), never an empty "ok".

The adapter is asked for a compatibility spec of the system's external API for a new field app
(payload, required fields, server-side validations, statuses, limits) and must return
{requirements: [{id, category, statement, citations[]}]}. A command adapter may instead return
{answer: "<markdown spec>"}; it is then segmented into requirements with eval/score.py.

A Bedrock judge (with the numbered source as evidence) marks every key item full / partial / missed /
contradicted, and every produced requirement matched / correct_extra / invented (cannot be confirmed
from the source) / wrong (contradicts the source or the key).
Metrics: recall (full, any), per-category recall, wrong requirements, invented requirements,
precision, citation validity. Judged metrics are computed over GRADED key items / requirements only:
if the judge did not run (budget) they are null and ``measurement.status`` is ``not_measured``; if it
omitted some ids those are ``ungraded`` (not "missed") and the status is ``partial``.
``--dry-run`` prints the cost plan and calls nothing. Outputs OUT/fieldapp.json and OUT/fieldapp.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (Budget, BudgetExceeded, KeyLoadError, Plan, Source, add_adapter_args, call_json,
                    citations_from, load_yaml, make_adapter, md_table, measurement, pct, score_mod,
                    validate_citation, write_outputs, write_plan)

DEFAULT_REQUEST = (
    "We are building a new Android field app that will submit records to this system through its "
    "external API (the entry point(s) other systems or devices call). Write a compatibility spec the "
    "mobile team can build against: the request payload and field formats/lengths, required fields, "
    "every server-side validation and the error it produces, the statuses a submitted record can "
    "get (returned and stored), and any limits. State only what the system actually does today.")

FIELDAPP_INSTR = """Write the compatibility spec requested above as a list of atomic, testable requirements.
Each requirement must cite the file and line ranges it comes from.
Return JSON only:
{"requirements": [{"id": "R1", "category": "payload|required_field|server_validation|status|limit|other",
  "statement": "...", "citations": [{"file": "X.vb", "start_line": 10, "end_line": 12}]}]}"""

JUDGE_SYSTEM = "You are a strict reviewer of API compatibility specs against legacy source code. Answer only with JSON."
JUDGE_PROMPT = """Expected requirements (answer key):
{key}

Source code (line-numbered) for checking claims:
{source}

Requirements produced by the tool:
{reqs}

1. For EVERY expected requirement give "coverage": "full" (the tool states its essential claim),
   "partial" (touches it but misses an essential part, e.g. a value or error code), "missed", or
   "contradicted" (the tool states something incompatible with it).
2. For EVERY tool requirement give "verdict": "matched" (expresses >= 1 expected item; list them in key_ids),
   "correct_extra" (not in the key but true of the source), "invented" (cannot be confirmed from the
   source - speculation, generic best practice presented as a system rule), or "wrong" (contradicts the
   source or the key). Add a <= 15 word "note" for invented/wrong.

Return JSON only:
{{"items": [{{"id": "FA-1", "coverage": "full"}}],
  "requirements": [{{"id": "R1", "verdict": "matched", "key_ids": ["FA-1"], "note": ""}}]}}
"""


def load_items(path: Path) -> list[dict]:
    d = load_yaml(path)
    if isinstance(d, dict):
        items = d.get("items") or d.get("requirements") or []
    else:
        items = d or []
    items = [it for it in items if isinstance(it, dict) and it.get("id")]
    if not items:
        raise KeyLoadError(path, "field-app items",
                           "expected {items: [...]} or {requirements: [...]}, each with id and requirement")
    for it in items:
        it.setdefault("category", it.get("kind") or "other")
        it.setdefault("requirement", it.get("statement", ""))
    return items


#: assumed size of the produced requirement list, for pricing the judge before it exists
PLAN_REQUIREMENTS = 45
PLAN_REQUIREMENT_CHARS = 420


def plan(a) -> dict:
    budget = Budget(a.max_usd, a.out)
    src = Source(a.source_root, a.exclude)
    adapter = make_adapter(a, budget, src)
    items = load_items(a.key)
    pl = Plan()
    n = adapter.prompt_chars("fieldapp", {"id": "fieldapp", "request": a.request or DEFAULT_REQUEST},
                             FIELDAPP_INSTR)
    if n is not None:
        # the requirement list is long: price the generation at its max_tokens, not the guard's 3000
        pl.add("generate", n, getattr(adapter, "max_tokens", 6000))
    key_txt = "\n".join(f"{it['id']} [{it['category']}]: {' '.join(it['requirement'].split())}" for it in items)
    source, _ = src.numbered(a.max_source_chars)
    pl.add("judge", len(JUDGE_PROMPT) + len(JUDGE_SYSTEM) + len(key_txt) + len(source)
           + PLAN_REQUIREMENTS * PLAN_REQUIREMENT_CHARS, 40 * (len(items) + PLAN_REQUIREMENTS) + 200,
           note=f"assumes ~{PLAN_REQUIREMENTS} produced requirements")
    return pl.summary(key_items=len(items), adapter=adapter.name, context=adapter.context_summary(),
                      answers_with_unknown_cost=int(n is None))


def requirements_from(d: dict) -> list[dict]:
    reqs = d.get("requirements") or d.get("items")
    out = []
    if isinstance(reqs, list) and reqs:
        for i, r in enumerate(reqs, 1):
            if isinstance(r, str):
                r = {"statement": r}
            out.append({"id": f"R{i}", "category": r.get("category", "other"),
                        "statement": str(r.get("statement") or r.get("requirement") or r.get("text") or ""),
                        "citations": citations_from(r)})
        return out
    text = d.get("answer") or d.get("spec") or ""
    if text:  # free-text spec from an external command: segment like score.py
        for i, f in enumerate(score_mod.segment_markdown(str(text)), 1):
            out.append({"id": f"R{i}", "category": "other", "statement": f["text"],
                        "citations": [], "raw_segment": True})
    return out


def run(a) -> dict:
    budget = Budget(a.max_usd, a.out)
    src = Source(a.source_root, a.exclude)
    adapter = make_adapter(a, budget, src)
    items = load_items(a.key)
    reqs, gen, judged, error = [], {}, None, None
    key_txt = "\n".join(f"{it['id']} [{it['category']}]: {' '.join(it['requirement'].split())}" for it in items)
    source, _ = src.numbered(a.max_source_chars)
    try:
        # keep back enough for the judge call that grades the produced spec
        reserve = budget.estimate(len(JUDGE_PROMPT) + len(key_txt) + len(source)
                                  + PLAN_REQUIREMENTS * PLAN_REQUIREMENT_CHARS,
                                  40 * (len(items) + PLAN_REQUIREMENTS) + 200)
        gen = adapter.ask("fieldapp", {"id": "fieldapp", "request": a.request or DEFAULT_REQUEST},
                          FIELDAPP_INSTR, expected_out=3000, reserve=reserve)
        reqs = requirements_from(gen["data"] or {})
        if not gen["data"] and gen.get("raw_text"):
            reqs = requirements_from({"answer": gen["raw_text"]})
        for r in reqs:
            r["citations"] = [validate_citation(c, src) for c in r["citations"]]
        print(f"  produced {len(reqs)} requirements in {gen['latency_s']}s")
        if reqs:
            req_txt = "\n".join(
                f"{r['id']} [{r['category']}]: {r['statement'][:800]} "
                + " ".join(f"({c['file']}:{c.get('start_line')}-{c.get('end_line')})" for c in r["citations"])
                for r in reqs)
            judged, _ = call_json(budget, JUDGE_PROMPT.format(key=key_txt, source=source, reqs=req_txt),
                                  system=JUDGE_SYSTEM, max_tokens=6000,
                                  expected_out=40 * (len(items) + len(reqs)) + 200, tag="benefits:fieldapp:judge")
    except BudgetExceeded as e:
        print(f"STOPPED: {e}")
        error = str(e)
    judged = judged if isinstance(judged, dict) else {}
    cov = {str(x.get("id")): x.get("coverage") for x in judged.get("items", []) if isinstance(x, dict)}
    ver = {str(x.get("id")): x for x in judged.get("requirements", []) if isinstance(x, dict)}
    for it in items:
        # an id the judge did not return is UNGRADED, not "missed": absence of a grade is not a grade
        c = cov.get(str(it["id"]))
        it["coverage"] = c if c in COVERAGE else "ungraded"
    for r in reqs:
        v = ver.get(r["id"], {})
        verdict = v.get("verdict")
        r["verdict"] = verdict if verdict in VERDICTS else "ungraded"
        r["key_ids"], r["note"] = v.get("key_ids", []), v.get("note", "")
    return summarize(a, items, reqs, gen, budget, adapter, error)


COVERAGE = ("full", "partial", "missed", "contradicted")
VERDICTS = ("matched", "correct_extra", "invented", "wrong")


def summarize(a, items, reqs, gen, budget, adapter, error) -> dict:
    c = [it["coverage"] for it in items if it["coverage"] != "ungraded"]
    v = [r["verdict"] for r in reqs if r["verdict"] != "ungraded"]
    n_graded_items = len(c)
    reason = None
    if n_graded_items < len(items):
        reason = (f"{n_graded_items} of {len(items)} key items graded, {len(v)} of {len(reqs)} produced "
                  f"requirements graded" + (f"; {error}" if error else ""))
    meas = measurement(len(items), n_graded_items, what="key items", reason=reason)
    cits = [x for r in reqs for x in r["citations"]]
    cats = {}
    for it in items:
        if it["coverage"] != "ungraded":
            cats.setdefault(it["category"], []).append(it["coverage"])
    s = {
        "measurement_status": meas["status"],
        "key_items": len(items), "key_items_graded": n_graded_items,
        "requirements_produced": len(reqs), "requirements_graded": len(v),
        # judged metrics: over GRADED items / requirements only; null when none were graded
        "recall_full_pct": pct(c.count("full"), len(c)),
        "recall_any_pct": pct(c.count("full") + c.count("partial"), len(c)),
        "key_items_contradicted": c.count("contradicted") if c else None,
        "wrong_requirements": v.count("wrong") if v else None,
        "invented_requirements": v.count("invented") if v else None,
        "precision_keyed_pct": pct(v.count("matched"), len(v)),
        "precision_correct_pct": pct(v.count("matched") + v.count("correct_extra"), len(v)),
        "citation_validity_pct": pct(sum(1 for x in cits if x["valid"]), len(cits)),
        "requirements_without_citation": sum(1 for r in reqs if not r["citations"]),
    }
    for cat, cs in sorted(cats.items()):
        s[f"recall_{cat}_pct"] = pct(cs.count("full") + 0.5 * cs.count("partial"), len(cs))
    s.update({"answer_tokens_in": gen.get("tokens_in", 0), "answer_tokens_out": gen.get("tokens_out", 0),
              "answer_cost_usd": gen.get("cost_usd", 0), "latency_s": gen.get("latency_s"),
              "errors": int(bool(gen.get("error") or error))})
    return {"benefit": "B4 field-app spec", "script": "fieldapp_eval", "tool": a.tool_name or adapter.name,
            "adapter": adapter.name, "key": str(a.key), "source_root": str(a.source_root),
            "context_truncated": getattr(adapter, "truncated", None), "request": a.request or DEFAULT_REQUEST,
            "context": adapter.context_summary(), "generation_context": gen.get("context"),
            "measurement": meas, "summary": s, "budget": budget.summary(), "items": items, "requirements": reqs,
            "generation_error": gen.get("error") or error}


def to_markdown(r: dict) -> str:
    L = [f"# B4 field-app compatibility spec: {r['tool']}", "", f"- Adapter: `{r['adapter']}`, key: `{r['key']}`",
         f"- Measurement: **{r['measurement']['status']}**"
         + (f" ({r['measurement']['reason']})" if r['measurement'].get('reason') else ""),
         f"- Spend: ${r['budget']['cost_usd']:.4f} of ${r['budget']['max_usd']:.2f}"
         + (f" -- **{r['budget']['stopped']}**" if r['budget']['stopped'] else ""), ""]
    L += md_table(list(r["summary"].items()), ("Metric", "Value"))
    L += ["", "## Key items", ""]
    L += md_table([(i["id"], i["category"], i["coverage"], i["requirement"][:120]) for i in r["items"]],
                  ("Item", "Category", "Coverage", "Requirement"))
    L += ["", "## Produced requirements", ""]
    L += md_table([(q["id"], q["category"], q["verdict"], ", ".join(q["key_ids"]), q["note"], q["statement"][:140])
                   for q in r["requirements"]], ("Req", "Category", "Verdict", "Key ids", "Note", "Statement"))
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, type=Path, help="field-app.yaml")
    ap.add_argument("--request", help="override the field-app request text")
    add_adapter_args(ap)
    a = ap.parse_args()
    if a.dry_run:
        write_plan(a.out, "fieldapp", plan(a), "fieldapp_eval")
        return
    r = run(a)
    write_outputs(a.out, "fieldapp", r, to_markdown(r))
    print(json.dumps({"summary": r["summary"], "budget": r["budget"]}, indent=2))


if __name__ == "__main__":
    main()
