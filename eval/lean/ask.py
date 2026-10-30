#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
"""Ask a business or technical question about a distilled system (lean pipeline v1 run directory).

Retrieval only, no raw full-code dump: BM25 over spec findings (findings.json) with glossary expansion of
business terms, plus graph.json neighbours of the code units those findings cite. The model may ask for
specific cited source lines once ("on demand"); --snippets includes the top findings' cited lines up front.

Answer: JSON {answer, citations, evidence, cannot_determine, findings}, printed as markdown by default.

Usage:
  python3 eval/lean/ask.py --run eval/runs/lean-ap "Who can approve a 30,000 invoice?"
      [--snippets] [--json] [--k 20]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from bedrock_util import converse, extract_json  # noqa: E402
from kb import KB  # noqa: E402
from specfmt import EVIDENCE  # noqa: E402

SYSTEM = """You answer questions about a legacy application using ONLY the specification findings,
glossary entries, dependency facts and source excerpts you are given. Every claim must be backed by
the citations provided (file:Lx-Ly). If the material does not answer the question, say so and set
cannot_determine. Answer with JSON only."""

PROMPT = """Question: {q}

Audience: {audience}. {audience_rule}

Glossary (business term -> code):
{glossary}

Specification findings (id [section / file] statement {{citations}} (evidence; flags)):
{findings}

Dependency facts (from the parser graph):
{graph}
{snippets}
Return JSON:
{{"answer": "...", "citations": ["name.ext:Lx-Ly", ...], "evidence": "Verified|Inferred|Unknown|Confirmation Required",
  "cannot_determine": false, "findings": ["F12", ...], "caveats": "short, optional"}}
{need_code_rule}
Rules: cite only citations that appear above; "Verified" only if the cited findings are Verified and directly
answer the question; "Confirmation Required" if sources disagree or the behaviour is only in a comment;
"Unknown" if it depends on data/config/code outside the corpus. If the findings do not cover the question,
answer "Cannot determine from the distilled specification" plus what is known, and set cannot_determine true.
"""

NEED_CODE = """If (and only if) you need to read specific source lines to answer, return instead
{"need_code": ["name.ext:Lx-Ly", ...]} with at most 6 ranges taken from the citations above."""


def answer(kb: KB, q: str, k: int = 20, snippets: bool = False, audience: str | None = None,
           log: Path | None = None) -> dict:
    hits = kb.search(q, k=k)
    finds = [f for _, f in hits]
    gl = kb.glossary_hits(q)
    # graph context: units cited by the top findings + nodes matching the question words
    nids = []
    for f in finds[:8]:
        for c in f["citations"][:2]:
            u = kb.units_at(*c)
            if u:
                nids.append(u[0]["id"])
    nids += [n["id"] for n in kb.match_nodes(q, limit=8) if n["kind"] not in ("column", "file")]
    nids = list(dict.fromkeys(nids))[:14]
    if audience is None:
        tech = any(ch in q for ch in "_.()@") or any(w in q.lower() for w in ("proc", "table", "trigger", "method",
                                                                                  "class", "sql", "column", "function"))
        audience = "technical" if tech else "business"
    rule = ("Answer in plain business language (no identifiers unless needed; explain codes), 2-6 sentences."
            if audience == "business" else "Answer precisely with identifiers, values and locations.")
    snip = ""
    if snippets:
        snip = "\nSource excerpts:\n" + "\n".join(
            f"--- {kb.ci.fmt(*c)}\n{kb.snippet(*c, cap=30)}" for f in finds[:5] for c in f["citations"][:1])
    def build(sn, allow_need):
        return PROMPT.format(q=q, audience=audience, audience_rule=rule,
                             glossary="\n".join(f"- {g['term']}: {g['meaning']} ({g['identifiers']}) {g['citations']}" for g in gl) or "-",
                             findings="\n".join(kb.finding_line(f) for f in finds) or "-",
                             graph="\n".join("- " + kb.node_summary(n) for n in nids) or "-",
                             snippets=sn, need_code_rule=NEED_CODE if allow_need else "")
    usage = []
    r = converse(build(snip, not snippets), system=SYSTEM, max_tokens=2500, tag="ask")
    usage.append(r)
    data = _json(r["text"])
    if data.get("need_code") and not snippets:
        ranges = []
        for x in data["need_code"][:6]:
            ranges += kb.ci.extract(x)
        snip = "\nSource excerpts (requested):\n" + "\n".join(f"--- {kb.ci.fmt(*c)}\n{kb.snippet(*c)}" for c in ranges[:6])
        r = converse(build(snip, False), system=SYSTEM, max_tokens=2500, tag="ask")
        usage.append(r)
        data = _json(r["text"])
        data["code_requested"] = [kb.ci.fmt(*c) for c in ranges[:6]]
    allowed = {kb.ci.fmt(*c) for f in finds for c in f["citations"]} | \
              {kb.ci.fmt(*c) for x in data.get("code_requested", []) for c in kb.ci.extract(x)}
    cits = []
    for x in data.get("citations", []):
        for c in kb.ci.extract(x):
            s = kb.ci.fmt(*c)
            if s:
                cits.append(s)
    data["citations"] = list(dict.fromkeys(cits))
    data["unsupported_citations"] = [c for c in data["citations"] if c not in allowed]
    if data.get("evidence") not in EVIDENCE:
        data["evidence"] = "Unknown"
    data["audience"] = audience
    data["cost_usd"] = round(sum(u["cost_usd"] for u in usage), 4)
    if log:
        with open(log, "a") as fh:
            for u in usage:
                fh.write(json.dumps({"tag": "ask", "input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"],
                                     "cost_usd": u["cost_usd"], "stop_reason": u["stop_reason"]}) + "\n")
    return data


def _json(text: str) -> dict:
    try:
        d = extract_json(text)
        return d if isinstance(d, dict) else {"answer": text}
    except ValueError:
        return {"answer": text.strip(), "evidence": "Unknown", "citations": [], "cannot_determine": False}


def to_md(q: str, d: dict) -> str:
    L = [f"**Q:** {q}", "", d.get("answer", ""), ""]
    if d.get("cannot_determine"):
        L.append("_Cannot determine fully from the distilled specification._")
    L.append(f"**Evidence:** {d.get('evidence')}  ")
    L.append("**Citations:** " + ("; ".join(f"`{c}`" for c in d.get("citations", [])) or "-") + "  ")
    if d.get("findings"):
        L.append("**Spec findings:** " + ", ".join(d["findings"]) + "  ")
    if d.get("caveats"):
        L.append(f"**Caveats:** {d['caveats']}  ")
    if d.get("unsupported_citations"):
        L.append(f"**Warning:** citations not in the retrieved material: {', '.join(d['unsupported_citations'])}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("question")
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--snippets", action="store_true", help="include the top findings' cited source lines up front")
    ap.add_argument("--audience", choices=["business", "technical"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    kb = KB(a.run)
    d = answer(kb, a.question, a.k, a.snippets, a.audience, log=a.run / "query_tokens.jsonl")
    print(json.dumps(d, indent=2) if a.json else to_md(a.question, d))


if __name__ == "__main__":
    main()
