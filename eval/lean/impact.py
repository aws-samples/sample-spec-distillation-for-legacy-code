#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
"""Change-impact analysis over a lean pipeline v1 run (graph.json + findings.json).

1. One LLM call maps the change description to seed nodes of graph.json (chosen from a shortlist
   built by retrieval; --seed skips the LLM).
2. Deterministic graph walk from the seeds:
   - upstream: whoever calls / executes / reads / writes / compares / handles the changed thing,
     transitively up to UI events (edge kinds calls, exec, uses_const, reads(_col), writes(_col),
     compares, handled_by, defines);
   - side effects: triggers fired by tables the affected units write (fires), constants mirrored by
     columns (mirrors);
   - downstream data consumers: units that read a table/column written by a changed unit (one hop);
   - same-concept checks: other units that compare the same identifier (keep-consistent list).
3. Maps affected units to spec findings (rules to re-test), reports and external contracts.

Usage:
  python3 eval/lean/impact.py --run eval/runs/lean-ap "Raise the approval threshold to 15000"
      [--seed const:APPR_THRESHOLD --seed col:APCoParm.ApprThreshold] [--json] [--depth 6]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from bedrock_util import converse, extract_json  # noqa: E402
from kb import KB  # noqa: E402

UPSTREAM = {"calls", "exec", "uses_const", "reads", "reads_col", "writes", "writes_col", "compares",
            "handled_by", "defines"}
FORWARD = {"fires", "mirrors"}
SHORTLIST_KINDS = {"constant", "table", "procedure", "function", "view", "trigger", "script", "report-or-doc",
                   "vb-function", "vb-sub", "vb-property", "missing-proc", "concept", "table-ddl"}

SYSTEM = """You map a proposed change to the code symbols it directly modifies, choosing ONLY from the
listed graph node ids. Answer with JSON only."""

PROMPT = """Proposed change: {change}

Relevant specification findings:
{findings}

Candidate graph nodes (id: description):
{nodes}

Return JSON {{"seeds": [{{"id": "<node id from the list>", "why": "..."}}], "interpretation": "one sentence"}}.
Pick the nodes whose definition or data would be edited to make the change (constants, columns, tables,
procedures, methods, concepts). Include every place where the same rule is hard-coded (all copies),
since all must change together. 1-12 seeds."""


def shortlist(kb: KB, change: str, limit: int = 220) -> list[str]:
    ids = []
    for _, f in kb.search(change, k=25):
        for c in f["citations"][:3]:
            for u in kb.units_at(*c)[:1]:
                ids.append(u["id"])
    ids += [n["id"] for n in kb.match_nodes(change, limit=60)]
    ids += [n["id"] for n in kb.graph["nodes"] if n["kind"] in SHORTLIST_KINDS]
    out = list(dict.fromkeys(i for i in ids if i in kb.nodes))
    return out[:limit]


def choose_seeds(kb: KB, change: str, log: Path | None) -> tuple[list[dict], str, float]:
    cands = shortlist(kb, change)
    finds = [f for _, f in kb.search(change, k=15)]
    prompt = PROMPT.format(change=change, findings="\n".join(kb.finding_line(f, 250) for f in finds) or "-",
                           nodes="\n".join(f"{i}: {kb.node_summary(i)[:220]}" for i in cands))
    r = converse(prompt, system=SYSTEM, max_tokens=2000, tag="impact")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps({"tag": "impact", "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                                 "cost_usd": r["cost_usd"], "stop_reason": r["stop_reason"]}) + "\n")
    try:
        d = extract_json(r["text"])
    except ValueError:
        d = {}
    seeds = [s for s in d.get("seeds", []) if s.get("id") in kb.nodes]
    return seeds, d.get("interpretation", ""), r["cost_usd"]


def walk(kb: KB, seeds: list[str], depth: int = 6) -> dict[str, dict]:
    """node id -> {tier, depth, via: (edge kind, from node)}."""
    aff = {s: {"tier": "changed", "depth": 0, "via": None} for s in seeds}
    q = deque((s, 0) for s in seeds)
    while q:
        nid, d = q.popleft()
        if d >= depth:
            continue
        nxt = []
        for e in kb.in_e.get(nid, []):
            if e["kind"] in UPSTREAM:
                nxt.append((e["src"], e["kind"], "upstream", e))
        for e in kb.out_e.get(nid, []):
            if e["kind"] in FORWARD:
                nxt.append((e["dst"], e["kind"], "side-effect", e))
        for e in kb.in_e.get(nid, []):
            if e["kind"] == "mirrors":
                nxt.append((e["src"], e["kind"], "side-effect", e))
        # a changed table/column: its readers are upstream already; a column seed also pulls its table's triggers
        if kb.nodes.get(nid, {}).get("kind") == "column":
            t = "table:" + kb.nodes[nid]["name"].split(".")[0]
            if t in kb.nodes:
                for e in kb.out_e.get(t, []):
                    if e["kind"] == "fires":
                        nxt.append((e["dst"], "fires", "side-effect", e))
        for m, kind, tier, e in nxt:
            if m in aff:
                continue
            # concept edges are 'same-concept' only (not transitive through other units' callers)
            if kind == "compares" and kb.nodes.get(nid, {}).get("kind") != "concept":
                continue
            aff[m] = {"tier": "same-concept" if kind == "compares" else tier, "depth": d + 1, "via": (kind, nid),
                      "touch": (e.get("file"), e.get("line")) if tier == "upstream" else None}
            if kind != "compares":
                q.append((m, d + 1))
    # same-concept checks: other units comparing an identifier the seeds define or compare
    import re as _re
    concepts = set()
    for s in seeds:
        n = kb.nodes.get(s, {})
        # a unit seed only contributes narrow concepts (compared in <= 4 units); key columns etc. are too generic
        concepts |= {e["dst"] for e in kb.out_e.get(s, []) if e["kind"] == "compares" and
                     len({x["src"] for x in kb.in_e.get(e["dst"], []) if x["kind"] == "compares"}) <= 4}
        if n.get("kind") in ("constant", "column", "concept"):
            cid = "concept:" + _re.sub(r"[\W_]", "", n["name"].split(".")[-1].lstrip("@")).lower()
            if cid in kb.nodes:
                concepts.add(cid)
    for cid in concepts:
        for e in kb.in_e.get(cid, []):
            if e["kind"] == "compares" and e["src"] not in aff:
                aff[e["src"]] = {"tier": "same-concept", "depth": 1, "via": (f"compares {cid.split(':', 1)[1]}", cid),
                                 "touch": (e.get("file"), e.get("line"))}
    # downstream data consumers: readers of the exact columns the seeds (or their direct callers) write
    for nid, info in list(aff.items()):
        if info["tier"] not in ("changed", "upstream") or info["depth"] > 1:
            continue
        for e in kb.out_e.get(nid, []):
            if e["kind"] != "writes_col" or e.get("ambiguous"):
                continue
            for r in kb.in_e.get(e["dst"], []):
                if r["kind"] == "reads_col" and r["src"] not in aff and not r.get("ambiguous"):
                    aff[r["src"]] = {"tier": "data-consumer", "depth": info["depth"] + 1,
                                     "via": (f"reads {e['dst']}", nid), "touch": (r.get("file"), r.get("line"))}
    for nid, info in list(aff.items()):
        if info["tier"] not in ("changed", "upstream") or info["depth"] > 1:
            continue
        for e in kb.out_e.get(nid, []):
            if e["kind"] == "writes":
                for f_ in kb.out_e.get(e["dst"], []):
                    if f_["kind"] == "fires" and f_["dst"] not in aff:
                        aff[f_["dst"]] = {"tier": "side-effect", "depth": info["depth"] + 1, "via": ("fires", e["dst"]),
                                          "touch": None}
    return aff


def report(kb: KB, change: str, seeds: list[dict], interp: str, aff: dict) -> dict:
    v1 = kb.facts["v1"]
    units = {n: i for n, i in aff.items() if kb.nodes.get(n, {}).get("file") and kb.nodes[n].get("start")
             and kb.nodes[n]["kind"] not in ("file", "column", "table", "vb-type")}
    order = {"changed": 0, "upstream": 1, "side-effect": 2, "data-consumer": 3, "same-concept": 4}
    rules, seen = [], set()
    seed_names = {kb.nodes.get(x["id"], {}).get("name", x["id"]).split(".")[-1].lower() for x in seeds}

    def short(nid):
        return kb.nodes.get(nid, {}).get("name", nid).split(".")[-1].split("/")[-1]

    def hits_for(nid, info):
        n = kb.nodes.get(nid, {})
        out = []
        if info["tier"] == "changed" and n.get("file") and n.get("start"):
            file, a, b = n["file"], n["start"], n["end"]
        elif info.get("touch") and info["touch"][0]:
            file, a, b = info["touch"][0], info["touch"][1], info["touch"][1]
        elif n.get("file") and n.get("start") and n["kind"] in ("trigger", "script", "report-or-doc"):
            file, a, b = n["file"], n["start"], n["end"]
        else:
            return out
        names = seed_names
        for f in kb.findings:
            st = f["statement"].lower()
            for c in f["citations"]:
                if c[0] != file or c[1] > b or c[2] < a:
                    continue
                # a wide citation (whole table / class / proc) only counts if the finding names the thing
                if c[2] - c[1] > 8 and not any(nm in st for nm in names):
                    continue
                out.append(f)
                break
        return out

    for nid, info in sorted(aff.items(), key=lambda kv: (order[kv[1]["tier"]], kv[1]["depth"])):
        for f in hits_for(nid, info):
            if f["id"] in seen:
                continue
            seen.add(f["id"])
            rules.append({"id": f["id"], "tier": info["tier"], "unit": nid, "section": f.get("section"),
                          "statement": f["statement"][:300], "citations": [kb.ci.fmt(*c) for c in f["citations"][:4]],
                          "evidence": f["evidence"]})
    tables = {n.split(":", 1)[1] for n in aff if n.startswith("table:")} | \
             {n.split(":", 1)[1].split(".")[0] for n in aff if n.startswith("col:")}
    cols = {n.split(":", 1)[1].lower() for n in aff if n.startswith("col:")}
    procs = {kb.nodes[n]["name"] for n in aff if kb.nodes.get(n, {}).get("kind") in ("procedure", "function", "view")}
    k = v1["contracts"]
    contracts = []
    for p in k["procs"]:
        if p["name"] in procs:
            contracts.append({"kind": "procedure", "name": p["name"], "cite": p["cite"],
                              "params": [x["name"] for x in p["params"]], "return_codes": list(p["return_codes"])})
    for b in k["param_bindings"]:
        if b["proc"] in procs:
            contracts.append({"kind": "param-binding", "name": f"{b['vb_method']} -> {b['proc']}", "cite": b["cite"]})
    for t in k["tables"]:
        if t["name"] in tables:
            contracts.append({"kind": "table", "name": t["name"], "cite": t["cite"]})
    for d in v1["status_domains"]:
        if d["column"].lower() in cols or any(d["column"].lower().startswith(t.lower() + ".") for t in tables) and \
                any(c.endswith("." + d["column"].split(".")[-1].lower()) for c in cols):
            contracts.append({"kind": "code-values", "name": d["column"], "values": list(d["values"])})
    for x in k["io_calls"]:
        if any(kb.nodes.get(n, {}).get("name") == x["from"] for n in units):
            contracts.append({"kind": "io", "name": f"{x['kind']} {x['api']}", "cite": f"{x['file']}:L{x['line']}"})
    reports = []
    for r in k["reports"]:
        reads = set(r.get("reads", []))
        hit = (f"script:{r['file']}" in aff or f"doc:{r['file']}" in aff or reads & tables or
               any(f"script:{ref}" in aff for ref in r.get("references", [])) or
               any(f.lower() in cols for f in []))
        if not hit and r.get("references"):
            hit = any(set(x.get("reads", [])) & tables for x in k["reports"] if x["file"] in r["references"])
        if hit:
            reports.append({"file": r["file"], "cite": r["cite"], "params": r.get("params", [])})
            contracts.append({"kind": "report", "name": r["file"], "cite": r["cite"]})
    ui = [n for n in aff if n.startswith("event:")]
    def path(n):
        p, cur = [], n
        while cur in aff and aff[cur]["via"] and len(p) < 8:
            kind, prev = aff[cur]["via"]
            p.append(f"{kind}")
            cur = prev
        return " <- ".join(reversed(p))
    return {
        "change": change, "interpretation": interp,
        "seeds": seeds,
        "affected": [{"id": n, "tier": i["tier"], "depth": i["depth"], "path": path(n),
                      "location": kb.ci.fmt(kb.nodes[n]["file"], kb.nodes[n]["start"], kb.nodes[n]["end"])
                      if kb.nodes.get(n, {}).get("file") and kb.nodes[n].get("start") else None}
                     for n, i in sorted(aff.items(), key=lambda kv: (order[kv[1]["tier"]], kv[1]["depth"], kv[0]))
                     if not n.startswith(("file:", "concept:"))],
        "ui_entry_points": ui,
        "rules_to_retest": rules,
        "reports": reports,
        "contracts": contracts,
        "summary": {t: sum(1 for i in aff.values() if i["tier"] == t) for t in order},
    }


def to_md(r: dict) -> str:
    L = [f"# Impact: {r['change']}", ""]
    if r["interpretation"]:
        L += [f"_Interpretation: {r['interpretation']}_", ""]
    L.append("## Seeds (directly changed)")
    for s in r["seeds"]:
        L.append(f"- `{s['id']}`: {s.get('why', '')}")
    L += ["", "## Affected code (deterministic graph walk)", "", "| Node | Tier | Location | Path |", "|---|---|---|---|"]
    for a in r["affected"]:
        if a["id"].startswith(("col:", "table:")) and a["tier"] != "changed":
            continue
        L.append(f"| `{a['id']}` | {a['tier']} | {('`' + a['location'] + '`') if a['location'] else '-'} | {a['path'] or '-'} |")
    L += ["", f"## UI entry points to exercise ({len(r['ui_entry_points'])})"] + [f"- `{u}`" for u in r["ui_entry_points"]]
    L += ["", f"## Rules to re-test ({len(r['rules_to_retest'])})", "", "| Finding | Tier | Statement | Citations |", "|---|---|---|---|"]
    for x in r["rules_to_retest"]:
        L.append(f"| {x['id']} | {x['tier']} | {x['statement'].replace('|', '/')[:220]} | {'; '.join('`' + c + '`' for c in x['citations'] if c)} |")
    L += ["", "## Reports"] + ([f"- `{x['file']}` params {', '.join(x['params']) or '-'}" for x in r["reports"]] or ["- none"])
    L += ["", "## External contracts touched"] + ([f"- {c['kind']}: {c['name']}" + (f" `{c['cite']}`" if c.get("cite") else "") +
                                                   (f" values {c['values']}" if c.get("values") else "") for c in r["contracts"]] or ["- none"])
    L += ["", f"Summary: {json.dumps(r['summary'])}"]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("change")
    ap.add_argument("--seed", action="append", help="graph node id(s); skips the LLM seed mapping")
    ap.add_argument("--depth", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    kb = KB(a.run)
    if a.seed:
        bad = [s for s in a.seed if s not in kb.nodes]
        if bad:
            raise SystemExit(f"unknown node ids: {bad}")
        seeds, interp = [{"id": s, "why": "given"} for s in a.seed], ""
    else:
        seeds, interp, _ = choose_seeds(kb, a.change, a.run / "query_tokens.jsonl")
    if not seeds:
        raise SystemExit("could not map the change to any graph node; pass --seed <node id>")
    aff = walk(kb, [s["id"] for s in seeds], a.depth)
    r = report(kb, a.change, seeds, interp, aff)
    print(json.dumps(r, indent=2) if a.json else to_md(r))


if __name__ == "__main__":
    main()
