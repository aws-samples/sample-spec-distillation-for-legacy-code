#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""B2/B5 onboarding + product-team Q&A evaluation.

Usage:
  python3 eval/benefits/qa_eval.py --qa QA.yaml --source-root SRC --adapter raw-code|spec|command \
      [--spec spec.md] [--command "python3 eval/lean/ask.py --run-dir X --question {question}"] \
      --out OUT [--max-usd 1.0] [--tool-name NAME] [--limit N]

qa.yaml: {questions: [{id, audience: engineer|product|trick, question, reference_answer,
          locations: [{file, start_line, end_line}]}]} (a bare list is also accepted).

For each question the adapter returns {answer, citations[], evidence_class[, cannot_determine]}.
A Bedrock judge then rates (batched, from the reference answer, reference snippets and the cited
snippets): correctness (correct/partial/wrong), citation support (supported/partial/unsupported/none),
trick handling (abstained_correctly/guessed), and for product questions a plain-language score
(1-5, 5 = no code jargon). Deterministic: citation validity and overlap with reference lines.

Questions are answered and graded in batches of --judge-batch (default 6): a batch is graded as
soon as it is answered, and money for grading it is reserved before each answer, so running out
of budget leaves graded results for the batches already done instead of answers nobody graded.

Unmeasured is never zero. Judged metrics (correct_pct, score_pct, per-audience, trick, plain
language, citation support) are computed over GRADED questions only; with none graded they are
null and ``measurement.status`` is ``not_measured``; with some graded it is ``partial`` and the
reason says how many. ``--dry-run`` prints the cost plan and calls nothing.
Outputs OUT/qa.json and OUT/qa.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (Budget, BudgetExceeded, KeyLoadError, Plan, Source, add_adapter_args, call_json,
                    citations_from, fmt, load_yaml, loc_overlap, make_adapter, md_table, measurement, pct,
                    validate_citation, write_outputs, write_plan)

QA_INSTR = """Answer the QUESTION using only the material above.
- Cite the exact file and line ranges that support the answer.
- If the answer cannot be determined from the material, or the material is inconsistent, say so
  explicitly (and set cannot_determine true) instead of guessing.
- If ASKED_BY is a product manager, answer in plain business language without code identifiers.
Return JSON only:
{"answer": "...", "citations": [{"file": "X.vb", "start_line": 10, "end_line": 20}],
 "evidence_class": "code|db|comment|report|config|inferred|none", "cannot_determine": false}"""

JUDGE_SYSTEM = ("You are a strict grader of answers about a legacy codebase. You compare each answer to a "
                "reference answer and to the source lines it cites. Answer only with JSON.")

JUDGE_PROMPT = """Grade each answer below.

For each item you get: the question, its audience, the reference answer, the reference source lines,
the tool's answer, and the source lines the tool cited (or a note that the citation is invalid).

Rate:
- "correctness": "correct" (states the reference's essential facts, nothing materially false),
  "partial" (some essential facts missing or blurred, nothing materially false), or
  "wrong" (contradicts the reference or the source, or misses the essential point).
- "citation_support": "supported" (cited lines exist and support the answer's key claims),
  "partial" (support some claims), "unsupported" (cited lines do not support it / invalid), "none" (no citations).
- "trick_handling" (only when audience is "trick"; else "n/a"): "abstained_correctly" if the answer
  says the question cannot be answered as asked / the code is inconsistent and explains why, in line
  with the reference; "guessed" if it gives a single confident answer instead.
- "plain_language" (only when audience is "product"; else null): 1-5, 5 = fully plain business
  language with no code identifiers, function/table/variable names, or programming jargon; 1 = mostly code talk.
  "jargon": list of code terms used (product only).
- "note": <= 25 words justifying the correctness rating.

Items:
{items}

Return JSON only:
{{"grades": [{{"id": "Q1", "correctness": "correct", "citation_support": "supported", "trick_handling": "n/a",
  "plain_language": null, "jargon": [], "note": ""}}]}}
"""


def load_questions(path: Path) -> list[dict]:
    d = load_yaml(path)
    qs = (d.get("questions") or d.get("items") or []) if isinstance(d, dict) else (d or [])
    qs = [q for q in qs if isinstance(q, dict) and q.get("question")]
    if not qs:
        raise KeyLoadError(path, "questions", "expected {questions: [{id, question, reference_answer, ...}]}")
    for q in qs:
        q.setdefault("audience", "engineer")
        q.setdefault("locations", [])
        q.setdefault("reference_answer", "")
    return qs


def snippets(src: Source, locs: list[dict], max_lines=30) -> str:
    out = []
    for c in locs[:6]:
        if c.get("valid") is False:
            out.append(f"[{c.get('file')}:{c.get('start_line')}-{c.get('end_line')}] INVALID: {c.get('reason')}")
            continue
        if c.get("start_line") is None:
            out.append(f"[{c.get('file')}] (file only, no lines)")
            continue
        s = src.snippet(c["file"], c["start_line"], c.get("end_line") or c["start_line"], max_lines=max_lines)
        out.append(f"[{c['file']}:{c['start_line']}-{c.get('end_line')}]\n{s}")
    return "\n".join(out) or "(none)"


#: assumed answer / cited-snippet sizes for pricing a judge batch before the answers exist
PLAN_ANSWER_CHARS = 2500
PLAN_CITED_SNIPPET_CHARS = 2500
GRADES = ("correct", "partial", "wrong")


def judge_prompt(src: Source, chunk: list[dict]) -> str:
    parts = []
    for r in chunk:
        parts.append(
            f"### {r['id']} (audience: {r['audience']})\nQUESTION: {r['question']}\n"
            f"REFERENCE ANSWER: {r['reference_answer']}\nREFERENCE LINES:\n{snippets(src, r['reference_locations'])}\n"
            f"TOOL ANSWER: {r['answer'][:3000] or '(no answer)'}\n"
            f"TOOL CITED LINES:\n{snippets(src, r['citations'])}\n")
    return JUDGE_PROMPT.format(items="\n".join(parts))


def judge_batch_chars(src: Source, qs: list[dict]) -> int:
    """Prompt size of grading these questions, before their answers exist (assumed sizes)."""
    chars = len(JUDGE_PROMPT) + len(JUDGE_SYSTEM)
    for q in qs:
        chars += (len(q["question"]) + len(str(q["reference_answer"])) + 120
                  + len(snippets(src, [dict(l) for l in q["locations"]]))
                  + PLAN_ANSWER_CHARS + PLAN_CITED_SNIPPET_CHARS)
    return chars


def judge_batch_estimate(budget: Budget, src: Source, qs: list[dict]) -> float:
    return budget.estimate(judge_batch_chars(src, qs), 250 * len(qs))


def judge(budget: Budget, src: Source, rows: list[dict], batch: int = 6) -> dict:
    grades = {}
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        data, _ = call_json(budget, judge_prompt(src, chunk), system=JUDGE_SYSTEM, max_tokens=3000,
                            expected_out=250 * len(chunk), tag="benefits:qa:judge")
        for g in (data or {}).get("grades", []):
            grades[str(g.get("id"))] = g
    return grades


def payload_for(q: dict) -> dict:
    payload = {"id": q["id"], "question": q["question"]}
    if q["audience"] == "product":
        payload["asked_by"] = "a product manager (non-technical)"
    return payload


def plan(a) -> dict:
    budget = Budget(a.max_usd, a.out)
    src = Source(a.source_root, a.exclude)
    adapter = make_adapter(a, budget, src)
    qs = load_questions(a.qa)[: a.limit or None]
    pl = Plan()
    unknown = 0
    for q in qs:
        n = adapter.prompt_chars("qa", payload_for(q), QA_INSTR)
        if n is None:
            unknown += 1
        else:
            pl.add("answer", n, 600)
    for i in range(0, len(qs), a.judge_batch):
        chunk = qs[i:i + a.judge_batch]
        pl.add("judge", judge_batch_chars(src, chunk), 250 * len(chunk))
    return pl.summary(questions=len(qs), adapter=adapter.name, context=adapter.context_summary(),
                      answers_with_unknown_cost=unknown,
                      note=("external command: answer cost is reported by the tool, not estimable"
                            if unknown else ""))


def run(a) -> dict:
    budget = Budget(a.max_usd, a.out)
    src = Source(a.source_root, a.exclude)
    adapter = make_adapter(a, budget, src)
    qs = load_questions(a.qa)[: a.limit or None]
    rows, grades, stopped = [], {}, None
    try:
        for i in range(0, len(qs), a.judge_batch):
            batch_q = qs[i:i + a.judge_batch]
            batch_rows = []
            for j, q in enumerate(batch_q):
                # keep back enough to grade this batch, including the answers still to come
                reserve = judge_batch_estimate(budget, src, batch_q)
                r = adapter.ask("qa", payload_for(q), QA_INSTR, expected_out=600, reserve=reserve)
                d = r["data"] or {}
                cits = [validate_citation(c, src) for c in citations_from(d)]
                ref = [dict(l) for l in q["locations"]]
                row = {
                    "id": q["id"], "audience": q["audience"], "question": q["question"],
                    "reference_answer": q["reference_answer"], "reference_locations": ref,
                    "answer": str(d.get("answer", "")), "evidence_class": d.get("evidence_class"),
                    "cannot_determine": d.get("cannot_determine"), "citations": cits,
                    "citation_overlaps_reference": any(loc_overlap(c, l, src) for c in cits if c["valid"] for l in ref)
                    if ref else None,
                    "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"], "cost_usd": r["cost_usd"],
                    "latency_s": r["latency_s"], "error": r["error"], "context": r.get("context")}
                rows.append(row)
                batch_rows.append(row)
                print(f"  {q['id']}: answered in {r['latency_s']}s ({r['tokens_in']}/{r['tokens_out']} tok)")
            grades.update(judge(budget, src, batch_rows, batch=len(batch_rows)))
    except BudgetExceeded as e:
        print(f"STOPPED: {e}")
        stopped = str(e)
        # the reserve normally covers it, but grade whatever was answered and is still ungraded
        pending = [r for r in rows if r["id"] not in grades]
        if pending:
            try:
                grades.update(judge(budget, src, pending, batch=a.judge_batch))
            except BudgetExceeded as e2:
                print(f"STOPPED (grading): {e2}")
    for r in rows:
        g = grades.get(r["id"])
        if g is not None and g.get("correctness") in GRADES:
            r["grade"], r["graded"] = g, True
        else:
            r["grade"], r["graded"] = {"correctness": "ungraded"}, False
    return summarize(a, rows, budget, adapter, len(qs), stopped)


def summarize(a, rows, budget, adapter, n_total, stopped=None) -> dict:
    graded = [r for r in rows if r.get("graded")]

    def sub(aud=None):
        rs = [r for r in graded if aud is None or r["audience"] == aud]
        g = [r["grade"].get("correctness") for r in rs]
        return rs, g
    rs, g = sub()
    all_cits = [c for r in rows for c in r["citations"]]
    supp = [r["grade"].get("citation_support") for r in graded if r["citations"]]
    trick = [r for r in graded if r["audience"] == "trick"]
    prod = [r for r in graded if r["audience"] == "product"
            and isinstance(r["grade"].get("plain_language"), (int, float))]
    with_ref = [r for r in rows if r["citation_overlaps_reference"] is not None]
    reason = None
    if len(graded) < n_total:
        reason = (f"{len(graded)} of {n_total} questions graded ({len(rows)} answered)"
                  + (f"; {stopped}" if stopped else ""))
    meas = measurement(n_total, len(graded), what="questions", reason=reason)
    s = {
        "measurement_status": meas["status"],
        "questions": len(rows), "questions_total": n_total,
        "questions_answered": len(rows), "questions_graded": len(graded),
        # judged metrics: over GRADED questions only; null when none were graded
        "correct_pct": pct(g.count("correct"), len(g)),
        "score_pct": pct(g.count("correct") + 0.5 * g.count("partial"), len(g)),
        "wrong": g.count("wrong") if graded else None,
    }
    for aud in ("engineer", "product", "trick"):
        _, ga = sub(aud)
        s[f"{aud}_correct_pct"] = pct(ga.count("correct"), len(ga))
    s.update({
        "trick_abstain_pct": pct(sum(1 for r in trick if r["grade"].get("trick_handling") == "abstained_correctly"),
                                 len(trick)),
        "plain_language_avg": round(sum(r["grade"]["plain_language"] for r in prod) / len(prod), 2) if prod else None,
        "answers_with_citations_pct": pct(sum(1 for r in rows if r["citations"]), len(rows)),
        "citation_validity_pct": pct(sum(1 for c in all_cits if c["valid"]), len(all_cits)),
        "citation_supported_pct": pct(supp.count("supported"), len(supp)),
        "citation_hits_reference_pct": pct(sum(1 for r in with_ref if r["citation_overlaps_reference"]), len(with_ref)),
        # deterministic metrics: over ANSWERED questions
        "answer_tokens_in": sum(r["tokens_in"] for r in rows),
        "answer_tokens_out": sum(r["tokens_out"] for r in rows),
        "answer_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "latency_avg_s": round(sum(r["latency_s"] for r in rows) / len(rows), 2) if rows else None,
        "errors": sum(1 for r in rows if r["error"]),
    })
    return {"benefit": "B2/B5 Q&A", "script": "qa_eval", "tool": a.tool_name or adapter.name,
            "adapter": adapter.name, "qa": str(a.qa), "source_root": str(a.source_root),
            "context_truncated": getattr(adapter, "truncated", None),
            "context": adapter.context_summary(),
            "measurement": meas, "summary": s, "budget": budget.summary(), "questions": rows}


def to_markdown(r: dict) -> str:
    s = r["summary"]
    m = r["measurement"]
    L = [f"# B2/B5 Q&A: {r['tool']}", "", f"- Adapter: `{r['adapter']}`, QA set: `{r['qa']}`",
         f"- Measurement: **{m['status']}**" + (f" ({m['reason']})" if m.get("reason") else ""),
         f"- Context: {fmt((r.get('context') or {}).get('mode'))}"
         f" (whole spec not shown: {fmt(r['context_truncated'])})",
         f"- Spend (all Bedrock calls incl. judge): ${r['budget']['cost_usd']:.4f} of ${r['budget']['max_usd']:.2f}"
         + (f" -- **{r['budget']['stopped']}**" if r['budget']['stopped'] else ""), ""]
    L += md_table(list(s.items()), ("Metric", "Value"))
    L += ["", "## Per question", ""]
    L += md_table([(q["id"], q["audience"], q["grade"].get("correctness"), q["grade"].get("citation_support"),
                    q["grade"].get("trick_handling"), q["grade"].get("plain_language"), len(q["citations"]),
                    q["citation_overlaps_reference"], q["latency_s"], q["grade"].get("note", ""))
                   for q in r["questions"]],
                  ("Q", "Audience", "Correctness", "Citation support", "Trick", "Plain lang", "Cits",
                   "Hits ref lines", "Latency s", "Judge note"))
    L += ["", "## Answers", ""]
    for q in r["questions"]:
        L.append(f"- **{q['id']}** {q['question']}\n  > {q['answer'][:500].replace(chr(10), ' ')}")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qa", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=0, help="only the first N questions")
    ap.add_argument("--judge-batch", type=int, default=6,
                    help="questions answered then graded together (default 6)")
    add_adapter_args(ap)
    a = ap.parse_args()
    if a.dry_run:
        write_plan(a.out, "qa", plan(a), "qa_eval")
        return
    r = run(a)
    write_outputs(a.out, "qa", r, to_markdown(r))
    print(json.dumps({"summary": r["summary"], "budget": r["budget"]}, indent=2))


if __name__ == "__main__":
    main()
