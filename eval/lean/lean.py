#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
"""Lean spec-distillation pipeline v1 (layers 1-5).

Layer 1  (deterministic) eval/lean/structure: Roslyn (VB.NET) + ScriptDom (T-SQL) in Docker -> facts.json.
Layer 1b (deterministic) facts_post.py: duplicate-logic / cross-layer comparison candidates, status domains,
         external contracts, db-check counterparts -> facts.json["v1"]; dependency graph -> graph.json.
Layer 2  (LLM, 1 call per file): line-numbered file + neighbour slice of the facts + pinned constant
         definitions + the layer-1b candidates that touch the file.
Layer 3  (LLM, 1 call): system-level pass over a compact facts summary -> architecture, persistence,
         lifecycles, external interfaces (+ glossary.md for business-language questions).
Layer 5  (LLM, 1 call): consolidation over all findings + candidates -> merge duplicates and emit
         "Duplicated logic", "Inconsistencies", "Possible logic flaws", "Undocumented behaviours",
         "External contract".

Output (<out>/): spec.md (eval/score.py layout: table rows with file:Lx-Ly citations), findings.json,
glossary.md, modernization.md, facts.json, graph.json, tokens.jsonl, generation_usage.json, contexts/.

Usage:
  python3 eval/lean/lean.py --corpus <dir> --out <run dir> [--include '*.vb' --include '*.sql' ...]
      [--exclude ANSWER_KEY.yaml --exclude README.md] [--skip-structure] [--dry-run] [--only <rel>]
      [--reuse] (reuse existing per-file answers in contexts/, no new layer-2 calls for them)
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from bedrock_util import MODEL_ID, converse, extract_json, summarize_log  # noqa: E402
import facts_post  # noqa: E402
from specfmt import CitationIndex, parse_findings, render_findings, EVIDENCE  # noqa: E402

IMAGE = "specdistill-lean-structure"
DEFAULT_EXCLUDES = ["ANSWER_KEY.yaml", "README.md"]
SNIPPET_CAP = 45        # max lines per neighbour snippet
CONTEXT_LINE_BUDGET = 320  # max neighbour snippet lines per file (constant definitions are outside this budget)
BUDGET_USD = 5.0        # generation cap for one run (stops before a call once 80% is spent)


# ============================================================================ layer 1
def stage_corpus(corpus: Path, dest: Path, excludes: list[str], includes: list[str] | None) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    for p in sorted(corpus.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(corpus)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if any(fnmatch.fnmatch(p.name, g) or fnmatch.fnmatch(str(rel), g) for g in excludes):
            continue
        if includes and not any(fnmatch.fnmatch(p.name, g) or fnmatch.fnmatch(str(rel), g) for g in includes):
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest / rel)


def run_structure(src: Path, facts_path: Path) -> dict:
    subprocess.run(["docker", "build", "-q", "-t", IMAGE, str(HERE / "structure")], check=True,
                   stdout=subprocess.DEVNULL)
    tar = subprocess.Popen(["tar", "-C", str(src), "-cf", "-", "."], stdout=subprocess.PIPE)
    with open(facts_path, "w") as out:
        subprocess.run(["docker", "run", "--rm", "-i", IMAGE], stdin=tar.stdout, stdout=out, check=True)
    tar.wait()
    facts = json.loads(facts_path.read_text())
    facts = facts_post.enrich(facts, src)                       # layer 1b
    facts_path.write_text(json.dumps(facts, indent=1))
    graph = facts_post.build_graph(facts, facts["v1"])
    (facts_path.parent / "graph.json").write_text(json.dumps(graph, indent=1))
    return facts


# ============================================================================ neighbour context (v0 + fixes)
class Corpus:
    def __init__(self, src: Path, facts: dict):
        self.src, self.f = src, facts
        self.lines = {x["file"]: (src / x["file"]).read_text(errors="replace").splitlines() for x in facts["files"]}
        vb, sql, d = facts["vb"], facts["sql"], facts["derived"]
        self.methods = {m["id"]: m for m in vb["methods"]}
        self.procs = {p["name"].lower(): p for p in sql["procs"]}
        self.triggers = sql["triggers"]
        self.tables = {t["name"].lower(): t for t in sql["tables"]}
        self.table_index = {t["table"].lower(): t for t in d["table_index"]}
        self.consts = {c["name"]: c for c in vb["constants"]}

    def cite(self, file, a, b=None):
        b = b or a
        return f"{file}:L{a}" if a == b else f"{file}:L{a}-L{b}"

    def numbered(self, file, ranges, cap=SNIPPET_CAP):
        """Line-numbered text for merged ranges, capped; gaps shown as '...'."""
        ls = self.lines[file]
        want = set()
        for a, b in ranges:
            want.update(range(max(1, a), min(len(ls), b) + 1))
        out, prev, n = [], None, 0
        for i in sorted(want):
            if n >= cap:
                out.append("     ... (truncated)")
                break
            if prev is not None and i != prev + 1:
                out.append("     ...")
            out.append(f"{i:4d} | {ls[i - 1]}")
            prev, n = i, n + 1
        return "\n".join(out), n

    # ---- unit helpers
    def unit_key_lines(self, u):
        """Lines in a SQL unit worth showing when the unit is too long to include whole."""
        ks = []
        for k in ("literal_sets", "literal_assigns", "raises", "returns", "exec_calls"):
            ks += [x["line"] for x in u.get(k, [])]
        ks += [x["line"] for x in u.get("comparisons", []) if x.get("has_literal") or "Thr" in x["text"] or "Amt" in x["text"]]
        return sorted(set(ks))

    def leading_comment(self, file, start):
        ls, a = self.lines[file], start - 1
        while a - 1 >= 1 and (ls[a - 2].lstrip().startswith(("--", "'")) or not ls[a - 2].strip()) and start - a < 25:
            a -= 1
        return a

    def snippet_for_sql_unit(self, u, focus_lines=()):
        file, s, e = u["file"], u["start"], u["end"]
        if e - s + 1 <= SNIPPET_CAP:
            return self.numbered(file, [(self.leading_comment(file, s), e)], cap=SNIPPET_CAP + 20)
        a = self.leading_comment(file, s)
        ranges = [(a, min(s + 3, a + 22))]
        for ln in list(focus_lines) + self.unit_key_lines(u):
            ranges.append((ln - 1, ln + 1))
        return self.numbered(file, ranges)

    def snippet_for_method(self, m):
        return self.numbered(m["file"], [(m["start"], m["end"])], cap=35)


def sql_unit_summary(u, kind):
    def tl(xs):
        return ", ".join(f"{x['table']}" + (f" {x['op']}" if "op" in x else "") + f" (L{','.join(map(str, x['lines'][:4]))})"
                         for x in xs) or "-"
    parts = [f"{kind} {u.get('name', u['file'])} [{u['file']}:L{u['start']}-L{u['end']}]"]
    if kind == "trigger":
        parts.append(f"ON {u['table']} {u['timing']} {'/'.join(u['events'])}")
    if u.get("params"):
        parts.append("params: " + ", ".join(p["name"] + ("=" + p["default"] if p.get("default") else "") +
                                            (" OUTPUT" if p.get("output") else "") for p in u["params"]))
    parts.append("reads: " + tl(u.get("reads", [])))
    parts.append("writes: " + tl(u.get("writes", [])))
    if u.get("exec_calls"):
        parts.append("calls procs: " + ", ".join(f"{x['proc']} (L{x['line']})" for x in u["exec_calls"]))
    if u.get("literal_sets"):
        parts.append("literal SETs: " + "; ".join(f"L{x['line']} {x['text']}" for x in u["literal_sets"][:12]))
    return "\n  ".join(parts)


def build_context_v0(c: Corpus, rel: str) -> str:
    f, d = c.f, c.f["derived"]
    vb, sql = f["vb"], f["sql"]
    own_methods = [m for m in vb["methods"] if m["file"] == rel]
    own_ids = {m["id"] for m in own_methods}
    own_procs = [p for p in sql["procs"] if p["file"] == rel]
    own_trigs = [t for t in sql["triggers"] if t["file"] == rel]
    own_tables = [t for t in sql["tables"] if t["file"] == rel]
    own_scripts = [s for s in sql["scripts"] if s["file"] == rel]
    own_inline = [s for s in vb["inline_sql"] if s["file"] == rel]

    facts_lines: list[str] = []   # compact structural facts (no snippets)
    snippets: list[tuple[int, str, str]] = []  # (priority, header, body)
    seen_snip: set = set()

    def add_snip(prio, key, header, body_n):
        if key in seen_snip:
            return
        seen_snip.add(key)
        snippets.append((prio, header, body_n))

    # ---------------- this file's own structure
    own = []
    if own_methods:
        own.append("methods: " + ", ".join(f"{m['id']} L{m['start']}-L{m['end']}" + (" [handles " + ", ".join(m['handles']) + "]" if m["handles"] else "")
                                         for m in own_methods))
    for u in own_procs:
        own.append(sql_unit_summary(u, "proc"))
    for u in own_trigs:
        own.append(sql_unit_summary(u, "trigger"))
    for u in own_scripts:
        own.append(sql_unit_summary(u, "script") + (f"\n  crystal params: {', '.join(u['crystal_params'])}" if u.get("crystal_params") else ""))
    for t in own_tables:
        own.append(f"table {t['name']} L{t['start']}-L{t['end']}" + (", checks: " + "; ".join(f"L{x['line']} {x['text']}" for x in t["checks"]) if t["checks"] else ""))
    for s in own_inline:
        own.append(f"inline SQL in {s['from']} L{s['start']}-L{s['end']}: reads {', '.join(x['table'] for x in s.get('reads', [])) or '-'}; "
                   f"writes {', '.join(x['table'] + ' ' + x['op'] for x in s.get('writes', [])) or '-'}"
                   + ("; conditional fragments at " + ", ".join(f"L{x['start']}" for x in s["appended_fragments"] if x["conditional"]) if any(x["conditional"] for x in s["appended_fragments"]) else ""))
    for m in d["vb_methods_without_callers"]:
        if m["file"] == rel:
            own.append(f"NO CALLERS in corpus: {m['method']} L{m['start']}-L{m['end']} ({m['note']}; not an event handler)")
    for r in vb["proc_refs"]:
        if r["file"] == rel and not r["defined_in_corpus"]:
            own.append(f"PROC NOT IN CORPUS: {r['from']} references {r['proc']} at L{r['line']} (no CREATE PROCEDURE for it in any .sql file)")
    for cc in d["commented_out_code"]:
        if cc["file"] == rel:
            own.append(f"COMMENTED-OUT CODE: L{cc['start']}-L{cc['end']}")
    if own:
        facts_lines.append("## Parser facts for this file\n" + "\n".join("- " + x for x in own))

    nb = []
    # ---------------- VB call graph (cross-file)
    for call in vb["calls"]:
        if call["file"] == rel and call["callee_file"] != rel:
            m = c.methods[call["to"]]
            nb.append(f"{call['from']} (L{call['line']}) calls {m['id']} [{c.cite(m['file'], m['start'], m['end'])}]")
            ro_getter = any(x["from"] == m["id"] and not x.get("writes") for x in vb["inline_sql"])
            add_snip(4 if ro_getter else 2, ("m", m["id"]), f"callee {m['id']}", c.snippet_for_method(m))
        elif call["callee_file"] == rel and call["file"] != rel:
            nb.append(f"called by {call['from']} at {c.cite(call['file'], call['line'])}: `{call['text']}` -> {call['to']}")
            caller = c.methods.get(call["from"])
            if caller and caller["end"] - caller["start"] <= 40:
                add_snip(3, ("m", caller["id"]), f"caller {caller['id']}", c.snippet_for_method(caller))
            else:
                add_snip(3, ("l", call["file"], call["line"]), f"call site in {call['from']}",
                         c.numbered(call["file"], [(call["line"] - 3, call["line"] + 4)]))

    # ---------------- VB -> stored procs, including procs reached through cross-file callees
    my_procrefs = [r for r in vb["proc_refs"] if r["file"] == rel]
    callee_ids = {x["to"] for x in vb["calls"] if x["file"] == rel}
    hop2 = [r for r in vb["proc_refs"] if r["from"] in callee_ids and r["file"] != rel]
    for r, prio in [(r, 1) for r in my_procrefs] + [(r, 1.5) for r in hop2]:
        p = c.procs.get(r["proc"].lower())
        via = "" if r in my_procrefs else f" (via {r['from']})"
        if p:
            nb.append(f"{r['from']} calls stored proc {p['name']}{via}\n  " + sql_unit_summary(p, "proc"))
            add_snip(prio, ("p", p["name"]), f"proc {p['name']}", c.snippet_for_sql_unit(p))
    # procs defined here: VB callers, SQL callers/callees
    for p in own_procs:
        for pi in d["proc_index"]:
            if pi["proc"].lower() == p["name"].lower():
                for vc in pi["vb_callers"]:
                    nb.append(f"{p['name']} is called from VB {vc['from']} at {c.cite(vc['file'], vc['line'])}")
                    m = c.methods.get(vc["from"])
                    if m:
                        add_snip(1, ("m", m["id"]), f"VB caller {m['id']}", c.snippet_for_method(m))
                        # callers of the wrapper (forms / BL)
                        for up in vb["calls"]:
                            if up["to"] == m["id"]:
                                nb.append(f"  ...which is called by {up['from']} at {c.cite(up['file'], up['line'])}")
                                add_snip(2, ("l", up["file"], up["line"]), f"call site in {up['from']}",
                                         c.numbered(up["file"], [(up["line"] - 4, up["line"] + 8)]))
                for sc in pi["sql_callers"]:
                    nb.append(f"{p['name']} is called by proc {sc['from']} at {c.cite(sc['file'], sc['line'])}")
                    add_snip(2, ("l", sc["file"], sc["line"]), f"call site in {sc['from']}",
                             c.numbered(sc["file"], [(sc["line"] - 6, sc["line"] + 3)]))
        for x in p["exec_calls"]:
            q = c.procs.get(x["proc"].lower())
            if q:
                nb.append(f"{p['name']} calls proc {q['name']} at L{x['line']}\n  " + sql_unit_summary(q, "proc"))
                add_snip(2, ("p", q["name"]), f"proc {q['name']}", c.snippet_for_sql_unit(q))
            else:
                nb.append(f"{p['name']} calls proc {x['proc']} at L{x['line']} which is NOT in the corpus")

    # ---------------- tables touched -> triggers, DDL, other writers
    touched = {}
    for u in own_procs + own_trigs + own_scripts + own_inline:
        for x in u.get("reads", []):
            touched.setdefault(x["table"].lower(), set()).add("read")
        for x in u.get("writes", []):
            touched.setdefault(x["table"].lower(), set()).add("write")
    for t in own_tables:
        touched.setdefault(t["name"].lower(), set()).add("ddl")
    for t in own_trigs:
        touched.setdefault(t["table"].lower(), set()).update({"write", "guard"})
    # writes done on this file's behalf by cross-file callees (e.g. form -> DAL inline UPDATE)
    for s in vb["inline_sql"]:
        if s["from"] in callee_ids and s["file"] != rel:
            for x in s.get("writes", []):
                touched.setdefault(x["table"].lower(), set()).add("write")
                nb.append(f"callee {s['from']} writes {x['table']} ({x['op']}) [{c.cite(s['file'], s['start'], s['end'])}]")
    for tn, how in sorted(touched.items()):
        ti = c.table_index.get(tn)
        if not ti:
            continue
        if ti["ddl"] and "ddl" not in how:
            t = c.tables[tn]
            nb.append(f"table {t['name']} defined at {c.cite(t['file'], t['start'], t['end'])}"
                      + (" checks: " + "; ".join(f"L{x['line']} {x['text']}" for x in t["checks"]) if t["checks"] else ""))
            if "write" in how:
                add_snip(4, ("t", tn), f"DDL {t['name']}", c.numbered(t["file"], [(t["start"], t["end"])], cap=30))
        elif not ti["ddl"] and "ddl" not in how:
            nb.append(f"table {ti['table']}: no CREATE TABLE in corpus")
        for tr in ti["triggers"]:
            if tr["file"] != rel:
                nb.append(f"table {ti['table']} has trigger {tr['trigger']} ({'/'.join(tr['events'])}) [{c.cite(tr['file'], tr['start'], tr['end'])}]")
                trg = next(x for x in c.triggers if x["name"] == tr["trigger"])
                if "write" in how or "ddl" in how:
                    add_snip(2, ("p", trg["name"]), f"trigger {trg['name']}", c.snippet_for_sql_unit(trg))
        others_w = [w for w in ti["written_by"] if w["file"] != rel]
        if "guard" in how:
            units = {u.get("name") or u.get("from"): u for u in sql["procs"] + vb["inline_sql"]}
            for w in others_w:
                u = units.get(w["unit"])
                for ls in (u or {}).get("literal_sets", []):
                    nb.append(f"  {w['unit']} sets `{ls['text']}` [{c.cite(u['file'], ls['line'])}]")
        if others_w and ("write" in how or "ddl" in how):
            nb.append(f"other writers of {ti['table']}: " + "; ".join(
                f"{w['unit']} {w['op']} [{w['file']}:L{w['lines'][0]}]" for w in others_w[:10]))
        if "ddl" in how:
            rd = [r for r in ti["read_by"] if r["file"] != rel]
            if rd:
                nb.append(f"readers of {ti['table']}: " + "; ".join(f"{r['unit']} [{r['file']}:L{r['lines'][0]}]" for r in rd[:10]))
    # seed rows defined here / used elsewhere
    for sr in sql["seed_rows"]:
        if sr["file"] != rel and sr["table"].lower() in touched:
            nb.append(f"seed row {sr['table']}({', '.join(sr['columns'])}) = ({', '.join(sr['values'])}) at {c.cite(sr['file'], sr['start'])}")

    # ---------------- constants used here that live elsewhere, and uses of constants defined here
    used = {}
    for u in vb["const_uses"]:
        if u["file"] == rel and u["const_file"] != rel:
            used.setdefault(u["const"], []).append(u["line"])
    for name, lines in sorted(used.items()):
        k = c.consts[name]
        nb.append(f"uses const {name} = {k['value']} [{c.cite(k['file'], k['start'])}] at L{', L'.join(map(str, lines[:6]))}")
    own_consts = {k["name"] for k in vb["constants"] if k["file"] == rel}
    if own_consts:
        by = {}
        for u in vb["const_uses"]:
            if u["const"] in own_consts and u["file"] != rel:
                by.setdefault(u["const"], []).append(f"{u['file']}:L{u['line']}" + (f" ({u['comparison']})" if u["comparison"] else ""))
        for name, locs in sorted(by.items()):
            nb.append(f"const {name} used at {', '.join(locs[:6])}")
    # v1 fix: constant definitions referenced by this file are pinned (never dropped by the snippet budget),
    # grouped per defining file, with the comment line(s) directly above each constant group.
    pinned = []
    by_file = {}
    for n in used:
        k = c.consts[n]
        by_file.setdefault(k["file"], []).append(k)
    for kf, ks in sorted(by_file.items()):
        rng = []
        for k in ks:
            a = k["start"]
            ls = c.lines[kf]
            # walk up through the constant group to its heading comment (at most 10 lines)
            while a - 1 >= 1 and k["start"] - a < 10 and (ls[a - 2].lstrip().startswith("'") or
                                                            re.search(r"\bConst\b", ls[a - 2])):
                a -= 1
            rng.append((a, k["end"]))
        body, _ = c.numbered(kf, rng, cap=400)
        pinned.append(f"### {kf}\n```\n{body}\n```")

    # ---------------- cross-layer matches (names and numeric values)
    for x in d["cross_layer_name_matches"]:
        sides = [x["file"]] + [m["file"] for m in x["sql_matches"]]
        if rel in sides or any(u["file"] == rel and u["const"] == x["vb_const"] for u in vb["const_uses"]):
            nb.append(f"CROSS-LAYER NAME MATCH: VB const {x['vb_const']} = {x['value']} [{c.cite(x['file'], x['start'])}] ~ "
                      + ", ".join(f"{m['kind']} {m['name']} [{m['file']}:L{m['line']}]" for m in x["sql_matches"]))
            for m in x["sql_matches"]:
                for pr in sql["procs"]:
                    if True:
                        hits = [cmp for cmp in pr["comparisons"] if m["name"].split(".")[-1].lstrip("@").lower() in cmp["text"].lower()]
                        for cmp in hits[:3]:
                            nb.append(f"  compared in {pr['name']} at {c.cite(pr['file'], cmp['line'])}: `{cmp['text']}`")
                            add_snip(1, ("l", pr["file"], cmp["line"]), f"comparison in {pr['name']}",
                                     c.numbered(pr["file"], [(cmp["line"] - 3, cmp["line"] + 3)]))
            for u in vb["const_uses"]:
                if u["const"] == x["vb_const"] and u["comparison"]:
                    nb.append(f"  VB compares {x['vb_const']} at {c.cite(u['file'], u['line'])}: `{u['text']}`")
    for v in d["shared_numeric_values"]:
        vb_here = [w for w in v["vb"] if w["file"] == rel]
        sql_here = [w for w in v["sql"] if w["file"] == rel]
        if float(v["value"]) < 100:
            continue
        if vb_here or sql_here:
            other = v["sql"] if vb_here else v["vb"]
            nb.append(f"SAME NUMBER {v['value']} in both layers: here " +
                      "; ".join(f"L{w['line']} `{w['text']}`" for w in (vb_here or sql_here)[:2]) + " / elsewhere " +
                      "; ".join(f"{w['file']}:L{w['line']} `{w['text']}`" for w in other[:3]))

    # ---------------- comments that point across files
    for m in d["comment_mentions"]:
        if m["file"] == rel:
            nb.append(f"comment at L{m['start']}-L{m['end']} names {m['kind']} {m['mentions']} (defined in {m['defined_in']})")
            name = m["mentions"]
            if m["kind"] == "proc" and c.procs.get(name.lower()):
                p = c.procs[name.lower()]
                add_snip(1, ("p", p["name"]), f"proc {p['name']}", c.snippet_for_sql_unit(p))
            elif m["kind"] == "trigger":
                trg = next(x for x in c.triggers if x["name"] == name)
                add_snip(1, ("p", trg["name"]), f"trigger {trg['name']}", c.snippet_for_sql_unit(trg))
            elif m["kind"] == "vb-method":
                for mm in vb["methods"]:
                    if mm["name"] == name and mm["file"] == m["defined_in"]:
                        add_snip(3, ("m", mm["id"]), f"method {mm['id']}", c.snippet_for_method(mm))
        elif m["defined_in"] == rel:
            cm = next((x for x in f["comments"] if x["file"] == m["file"] and x["start"] == m["start"]), None)
            if cm:
                nb.append(f"comment in {c.cite(m['file'], m['start'], m['end'])} names {m['mentions']} (defined here): \"{cm['text'][:300]}\"")

    # ---------------- partial classes / files with no parser coverage
    my_types = {t["name"] for t in vb["types"] if t["file"] == rel}
    for t in vb["types"]:
        if t["name"] in my_types and t["file"] != rel:
            nb.append(f"same class {t['name']} also declared in {t['file']} (L{t['start']}-L{t['end']})")
    lang = next(x["language"] for x in f["files"] if x["file"] == rel)
    if lang == "other":
        text = "\n".join(c.lines[rel]).lower()
        stems = {Path(x["file"]).stem.lower(): x["file"] for x in f["files"] if x["file"] != rel and x["language"] != "other"}
        for stem, file in stems.items():
            if len(stem) >= 3 and re.search(r"(?<![\w])" + re.escape(stem) + r"(?![\w])", text + " " + rel.lower()):
                nb.append(f"this file mentions {file}")
                for s in sql["scripts"]:
                    if s["file"] == file:
                        nb.append("  " + sql_unit_summary(s, "script"))
                        add_snip(2, ("f", file), f"{file}", c.numbered(file, [(1, len(c.lines[file]))], cap=60))
        for tn in c.table_index:
            if re.search(r"(?<![\w])" + re.escape(tn) + r"(?![\w])", text):
                nb.append(f"this file mentions table {c.table_index[tn]['table']}")

    if nb:
        # dedupe, keep order
        seen, nb2 = set(), []
        for x in nb:
            if x not in seen:
                seen.add(x)
                nb2.append(x)
        facts_lines.append("## Neighbours (from the parser; cite their lines as file:Lx-Ly if you rely on them)\n" +
                           "\n".join("- " + x for x in nb2[:80]))

    # ---------------- snippets within budget, by priority
    used_lines, blocks = 0, []
    for prio, header, (body, n) in sorted(snippets, key=lambda s: s[0]):
        if used_lines + n > CONTEXT_LINE_BUDGET:
            continue
        used_lines += n
        blocks.append(f"### {header}\n```\n{body}\n```")
    if pinned:
        facts_lines.append("## Constant definitions used by this file (complete; cite them as file:Lx)\n" + "\n".join(pinned))
    if blocks:
        facts_lines.append("## Neighbour snippets (line-numbered, excerpted)\n" + "\n".join(blocks))
    return "\n\n".join(facts_lines)


# ============================================================================ layer-1b block for a file
def in_file(cite: str, rel: str) -> bool:
    return cite.startswith(rel + ":")


def build_context(c: Corpus, rel: str) -> str:
    base = build_context_v0(c, rel)
    v1 = c.f["v1"]
    out = []
    for g in v1["cross_layer_candidates"]:
        if any(in_file(m["cite"], rel) for m in g["members"]):
            out.append(f"- SAME CONCEPT COMPARED DIFFERENTLY ({g['concept']}; {g['differs']}): " +
                       "; ".join(f"{m['cite']} `{m['text']}`" for m in g["members"]))
    for g in v1["duplicate_candidates"]:
        if not any(in_file(m["cite"], rel) for m in g["members"]):
            continue
        what = {"same-expression": "same expression", "same-shape": "same expression shape",
                "same-message": "same message text", "shared-literals": "shared literal values",
                "same-threshold-check": "same threshold check"}[g["kind"]]
        detail = g.get("example") or g.get("text") or ", ".join(map(str, g.get("values", []))) or g.get("concept", "")
        out.append(f"- REPEATED LOGIC ({what}: `{str(detail)[:120]}`) at " +
                   "; ".join(m["cite"] + (f" `{m['text']}`" if m.get("text") else "") for m in g["members"][:8]))
    checks = [d for d in v1["db_checks"] if in_file(d["cite"], rel)]
    for d in checks[:45]:
        cp = "; ".join(f"{x['cite']} `{x['text'][:70]}`" for x in d["client_counterparts"][:3]) or "NONE found in client code"
        out.append(f"- DB CHECK {d['cite']} `{d['text'][:90]}` -> client-side counterpart: {cp}")
    client = [x for x in v1["comparisons"] if x["file"] == rel and x["layer"] in ("client", "client-sql") and x["subject"]]
    n, seen_txt = 0, set()
    for x in client:
        if (x["line"], x["text"]) in seen_txt or set(x["subject"]) & set(x["ref"]):
            continue
        seen_txt.add((x["line"], x["text"]))
        cps = facts_post.counterparts(v1["comparisons"], x, {"db"})
        if cps and n < 30:
            n += 1
            out.append(f"- CLIENT CHECK {x['cite']} `{x['text'][:80]}` -> DB counterpart: " +
                       "; ".join(f"{o['cite']} `{o['text'][:70]}`" for o in cps[:3]))
    for d in v1["status_domains"]:
        ws = [w for vs in d["values"].values() for w in vs]
        if any(in_file(w["cite"], rel) for w in ws):
            out.append(f"- CODE VALUES of {d['column']}: " + ", ".join(
                f"'{v}' ({len(vs)}x, e.g. {vs[0]['cite']})" for v, vs in list(d["values"].items())[:12]))
    k = v1["contracts"]
    for p in k["procs"]:
        if p["cite"].startswith(rel + ":"):
            rc = "; ".join(f"{code}: " + (next((x["message"] for x in xs if x["message"]), "") or "") +
                           f" (L{xs[0]['line']})" for code, xs in p["return_codes"].items())
            out.append(f"- CONTRACT {p['kind']} {p['name']}: params " +
                       ", ".join(x["name"] + (" OUTPUT" if x["output"] else "") + ("=" + x["default"] if x["default"] else "") for x in p["params"]) +
                       (f"; return codes {rc}" if rc else "") +
                       (f"; result sets at L{', L'.join(str(r['line']) for r in p['result_sets'])}" if p["result_sets"] else "") +
                       ("; ENTRY POINT (no caller in corpus)" if p["entry_point"] else ""))
    for b in k["param_bindings"]:
        if in_file(b["cite"], rel) or any(p["name"] == b["proc"] and p["cite"].startswith(rel + ":") for p in k["procs"]):
            issues = (f" missing (no default): {b['missing_without_default']}" if b["missing_without_default"] else "") + \
                     (f" not declared by proc: {b['not_declared']}" if b["not_declared"] else "")
            out.append(f"- PARAM BINDING {b['vb_method']} -> {b['proc']} at {b['cite']}: sends {', '.join(b['sent'])}"
                       + (issues or " (matches the proc signature)"))
    for x in k["io_calls"]:
        if x["file"] == rel:
            out.append(f"- EXTERNAL I/O ({x['kind']}) {x['api']} at {x['file']}:L{x['line']}")
    for x in k["attributes"]:
        if x["file"] == rel:
            out.append(f"- ATTRIBUTE <{x['name']}{x['args'] or ''}> on {x['target']} at {x['file']}:L{x['line']}")
    for x in k["enums"]:
        if x["file"] == rel:
            out.append(f"- ENUM {x['name']} at {x['file']}:L{x['start']}-L{x['end']}: " +
                       ", ".join(m["name"] + ("=" + m["value"] if m["value"] else "") for m in x["members"]))
    for r in k["reports"]:
        if r["file"] == rel or rel in r.get("references", []):
            out.append(f"- REPORT {r['file']}: params {', '.join(r.get('params', [])) or '-'}" +
                       (f"; fields {', '.join(r['fields'][:25])}" if r.get("fields") else "") +
                       (f"; data source {', '.join(r['references'])}" if r.get("references") else ""))
    if out:
        base += ("\n\n## Layer-1 candidates touching this file (deterministic; confirm against the code before "
                 "reporting, cite every location)\n" + "\n".join(out[:120]))
    return base


# ============================================================================ layer 2
SYSTEM = """You are extracting a behavioural specification from a legacy VB.NET WinForms + SQL Server
application, one source file at a time. You are given the file (line-numbered) and a parser-built
context about its neighbours: callers, callees, stored procedures and tables it touches, triggers on
those tables, constants it uses, and deterministic candidates (repeated logic, the same concept compared
differently in another layer, database checks with or without a client-side counterpart, code values,
interface contracts). The context is reliable structure (from Roslyn/ScriptDom), but snippets are excerpts
and candidates are only hints: report one only if the shown code confirms it.

Report what THIS file implements or decides. Use the neighbour context to judge where a rule is really
enforced and whether layers agree; cite neighbour lines too when a finding depends on them. Do not
re-describe the neighbours' own logic except where it changes the meaning of a finding here.
Never invent behaviour that is not in the given lines. Prefer precise, checkable statements with the
actual values, codes and messages. State facts, not recommendations."""

PROMPT = """File: {name}

{context}

## The file
```
{body}
```

Extract the business rules, states and transitions, workflows and exceptions implemented in `{name}`.

Output markdown with exactly these sections, each a table with columns
`| ID | Statement | Citations | Evidence | Flags |`:
## Business Rules
## States and Transitions
## Workflows
## Exceptions and Error Handling
then a short `## Open Questions` bullet list (each bullet with citations) for things a person must confirm.

Rules for the table cells:
- Statement: one self-contained sentence (or two) naming the entity, condition, value and outcome.
  When a value comes from a constant, give the constant name and its value.
- Citations: every item cites `{base}:L<start>-L<end>` (or `:L<n>`); add neighbour citations when the
  finding relies on them. Every citation MUST start with the file name including its extension
  (e.g. `name.vb:L10-L12`, `name.sql:L5`); never write a bare `L10`, a procedure/method name instead of
  a file, or a file name without its extension. Cite only lines you were shown.
- Evidence: one of
  Verified (executable code in the cited lines does exactly this),
  Inferred (follows from code + names/structure but not stated outright),
  Unknown (depends on code, data or configuration not in the corpus),
  Confirmation Required (layers disagree, rule exists only in a comment, code looks unreachable or removed,
  or enforcement is only in the UI or only in the DB, so a person must confirm the intended behaviour).
- Flags: zero or more of
  `inconsistent-values` (the same rule appears with a different value or operator in another layer: say both values and both locations),
  `db-only` (enforced only in a stored proc/trigger/constraint. Use it ONLY when the layer-1 list says the DB
     check has NO client-side counterpart AND none of the client code shown checks the same thing; if a
     counterpart exists, use `duplicated-across-layers` or `inconsistent-values` instead),
  `ui-only` (enforced only in the client, nothing in the DB/procs enforces it),
  `duplicated-across-layers` (same rule hardcoded in more than one layer, latent drift risk, even if values agree today),
  `duplicated-logic` (the same logic is implemented again elsewhere in the same layer: cite every copy),
  `dead-code` (no callers, calls a proc that is not in the corpus, or otherwise unreachable),
  `comment-only` (the rule exists only in a comment; no code enforces it),
  `removed` (commented-out or documented-as-removed behaviour),
  `external` (depends on code not in the corpus),
  `undocumented` (behaviour no comment, message or documentation in the shown code describes, that a re-implementer would likely miss),
  `logic-flaw` (the code looks wrong: contradictory or unreachable conditions, boundary off by one, ignored result, missing check on one path),
  `contract` (an interface other layers or systems depend on: procedure signature, return codes, result columns, code values, report parameters, payload fields).
  Use `-` when none apply. Explain each flag in the Statement or after the flag.
Keep it to findings that matter for a re-implementation; no preamble.
"""


# ============================================================================ layer 3 (system pass)
def system_summary(c: Corpus) -> str:
    f, v1 = c.f, c.f["v1"]
    vb, sql, d = f["vb"], f["sql"], f["derived"]
    L = ["## Files"]
    for x in f["files"]:
        L.append(f"- {x['file']} ({x['language']}, {x['end']} lines)")
    L.append("\n## Client types (VB)")
    meth_by_cls = defaultdict(list)
    for m in vb["methods"]:
        meth_by_cls[m["class"]].append(m)
    for t in vb["types"]:
        ms = meth_by_cls.get(t["name"], [])
        hs = [m["name"] for m in ms if m["handles"]]
        L.append(f"- {t['kind']} {t['name']}{' (form)' if t.get('is_form') else ''} [{t['file']}:L{t['start']}-L{t['end']}]: "
                 f"methods {', '.join(m['name'] for m in ms if not m['handles'])[:400]}" + (f"; event handlers {', '.join(hs)[:300]}" if hs else ""))
    L.append("\n## Call flow (class level; count of call sites)")
    flow = defaultdict(int)
    for x in vb["calls"]:
        a, b = x["from"].split(".")[0], x["to"].split(".")[0]
        if a != b:
            flow[(a, b)] += 1
    for (a, b), n in sorted(flow.items()):
        L.append(f"- {a} -> {b} ({n})")
    for r in vb["proc_refs"]:
        L.append(f"- {r['from']} -> proc {r['proc']} [{r['file']}:L{r['line']}]" + ("" if r["defined_in_corpus"] else " (NOT IN CORPUS)"))
    for s in vb["inline_sql"]:
        w = ", ".join(x["table"] + " " + x["op"] for x in s.get("writes", []))
        r = ", ".join(x["table"] for x in s.get("reads", []))
        L.append(f"- {s['from']} inline SQL [{s['file']}:L{s['start']}-L{s['end']}]: reads {r or '-'}; writes {w or '-'}")
    L.append("\n## Stored procedures / functions / views")
    for p in v1["contracts"]["procs"]:
        u = next(x for x in sql["procs"] if x["name"] == p["name"])
        rc = "; ".join(f"{k}" + (f" '{next((y['message'] for y in v if y['message']), '')}'" if any(y["message"] for y in v) else "")
                       for k, v in p["return_codes"].items())
        L.append(f"- {p['kind']} {p['name']} [{p['cite']}] ({', '.join(x['name'] + (' OUTPUT' if x['output'] else '') for x in p['params'])}): "
                 f"reads {', '.join(x['table'] for x in u.get('reads', [])) or '-'}; writes {', '.join(x['table'] + ' ' + x['op'] for x in u.get('writes', [])) or '-'}"
                 + (f"; calls {', '.join(x['proc'] for x in u.get('exec_calls', []))}" if u.get("exec_calls") else "")
                 + (f"; return codes {rc}" if rc else "")
                 + (f"; raises {'; '.join(r['message'][:60] for r in p['raises'])}" if p["raises"] else "")
                 + (f"; result set columns {', '.join(p['result_sets'][0]['columns'][:12])}" if p["result_sets"] else "")
                 + f"; callers VB {len(p['vb_callers'])}, SQL {len(p['sql_callers'])}")
    L.append("\n## Triggers")
    for t in sql["triggers"]:
        L.append(f"- {t['name']} ON {t['table']} {t['timing']} {'/'.join(t['events'])} [{t['file']}:L{t['start']}-L{t['end']}]: "
                 f"writes {', '.join(x['table'] + ' ' + x['op'] for x in t.get('writes', [])) or '-'}; raises {len(t.get('raises', []))}")
    L.append("\n## Tables")
    ti = {x["table"].lower(): x for x in d["table_index"]}
    for t in sql["tables"]:
        x = ti.get(t["name"].lower(), {})
        L.append(f"- {t['name']} [{t['file']}:L{t['start']}-L{t['end']}] columns: " +
                 ", ".join(cc["name"] + (f"={cc['default']}" if cc.get("default") else "") for cc in t["columns"])[:600] +
                 (f"; checks: {'; '.join(ch['text'][:80] for ch in t['checks'])}" if t["checks"] else "") +
                 f"; written by {', '.join(sorted({w['unit'] for w in x.get('written_by', [])}))[:200] or '-'}"
                 f"; read by {', '.join(sorted({w['unit'] for w in x.get('read_by', [])}))[:200] or '-'}")
    for sr in sql["seed_rows"][:30]:
        L.append(f"- seed {sr['table']}({', '.join(sr['columns'])}) = ({', '.join(sr['values'])}) [{sr['file']}:L{sr['start']}]")
    missing = [t["table"] for t in d["table_index"] if not t["ddl"]]
    if missing:
        L.append(f"- tables used but not defined in the corpus: {', '.join(missing)}")
    L.append("\n## Code values (column -> value: where)")
    for dm in v1["status_domains"][:20]:
        L.append(f"- {dm['column']}: " + "; ".join(f"'{v}' " + ", ".join(w['cite'] + f" ({w['how']})" for w in ws[:3])
                                               for v, ws in list(dm["values"].items())[:10]))
    L.append("\n## Literal status writes (SET col = 'x') and the guards in the same unit")
    comps = v1["comparisons"]
    for uid, u, layer in facts_post.sql_units(f):
        for s_ in u.get("literal_sets", []):
            guards = [x for x in comps if x["unit"] == uid and norm(s_["column"]) in x["subject"] and x["line"] < s_["line"]][-3:]
            L.append(f"- {uid} sets {s_.get('table') or '?'}.{s_['column']} = '{s_['value']}' [{u['file']}:L{s_['line']}]"
                     + (" after " + "; ".join(f"`{g['text'][:60]}` L{g['line']}" for g in guards) if guards else ""))
    L.append("\n## Interfaces")
    k = v1["contracts"]
    for r in k["reports"]:
        L.append(f"- report {r['file']} [{r['cite']}]: params {', '.join(r.get('params', [])) or '-'}"
                 + (f"; fields {', '.join(r.get('fields', [])[:20])}" if r.get("fields") else "")
                 + (f"; data source {', '.join(r.get('references', []))}" if r.get("references") else ""))
    for x in k["io_calls"][:30]:
        L.append(f"- I/O {x['kind']} {x['api']} in {x['from']} [{x['file']}:L{x['line']}]")
    for x in k["attributes"][:20]:
        L.append(f"- attribute <{x['name']}> on {x['target']} [{x['file']}:L{x['line']}]")
    for x in k["enums"][:20]:
        L.append(f"- enum {x['name']} [{x['file']}:L{x['start']}-L{x['end']}]")
    for b in k["param_bindings"]:
        L.append(f"- binding {b['vb_method']} -> {b['proc']} [{b['cite']}]" +
                 (f" MISSING {b['missing_without_default']}" if b["missing_without_default"] else "") +
                 (f" UNDECLARED {b['not_declared']}" if b["not_declared"] else ""))
    L.append(f"- entry points (procs with no caller in corpus): {', '.join(p['name'] for p in k['procs'] if p['entry_point']) or '-'}")
    L.append(f"- missing procs (called, not defined): {', '.join(d['missing_procs']) or '-'}")
    L.append("\n## Cross-layer candidates")
    for g in v1["cross_layer_candidates"][:15]:
        L.append(f"- {g['concept']}: {g['differs']}")
    L.append("\n## Header comment of each file (trimmed)")
    for x in f["files"]:
        cm = next((cc for cc in f["comments"] if cc["file"] == x["file"]), None)
        if cm:
            L.append(f"- {x['file']}:L{cm['start']}-L{cm['end']}: " + re.sub(r"\s+", " ", cm["text"])[:400])
    return "\n".join(L)


def norm(s):
    return facts_post.norm_id(s)


SYSTEM3 = """You are writing the system-level part of a behavioural specification of a legacy application
from deterministic parser facts (Roslyn for VB.NET, ScriptDom for T-SQL). Every fact carries a
file:line location. State only what the facts support; no recommendations, no speculation."""

PROMPT3 = """Parser facts for the whole system:

{summary}

Write markdown with exactly these sections. The first four are tables with columns
`| ID | Statement | Citations | Evidence | Flags |` (IDs ARC-nn, PER-nn, LC-nn, IF-nn):
## Architecture
  layers, main components and how a user action flows through them (form -> business class -> data
  access -> procedure/trigger -> table), where validation lives, shared modules.
## Persistence
  the main tables and what each holds, keys, constraints/defaults that encode rules, which units write
  each table, tables used but not defined in the corpus.
## Lifecycles
  each status/code column: its values and meanings, which unit moves a record from which value to which
  (cite the write and its guard), and where the allowed transitions are enforced in each layer.
## External Interfaces
  everything another system or layer depends on: procedure signatures, return codes and messages,
  result sets, report parameters and fields, files/HTTP/serialised payloads, code values exposed to callers.
Then:
## Glossary
  a table `| Term | Meaning | Code identifiers | Citations |` mapping business terms a product manager would
  use (entities, statuses, roles, documents, amounts) to the identifiers, codes and tables in the code.

Rules: one checkable fact per row with the actual names/values; Citations are `name.ext:Lx-Ly` taken from
the facts (file name with extension, never a bare line); Evidence is Verified / Inferred / Unknown /
Confirmation Required; Flags `-` or any of `contract`, `undocumented`, `inconsistent-values`,
`duplicated-across-layers`, `dead-code`, `external`. No preamble.
"""


# ============================================================================ layer 5 (consolidation)
SYSTEM5 = """You consolidate a behavioural specification extracted file-by-file from a legacy application.
You see every per-file finding (with ids and citations) and deterministic parser candidates. You
de-duplicate and you surface cross-cutting issues a modernisation team needs. Everything you state must
be supported by the findings or candidates you were given; answer with JSON only."""

PROMPT5 = """Per-file findings (id [file / section] statement {{citations}} evidence flags doc):
{findings}

Deterministic candidates from the parser:
{candidates}

Return JSON:
{{"merges": [{{"keep": "F12", "drop": ["F40"], "statement": "optional merged statement"}}],
 "duplicated_logic": [ITEM], "inconsistencies": [ITEM], "logic_flaws": [ITEM],
 "undocumented": [ITEM], "external_contract": [ITEM]}}
where ITEM = {{"statement": "...", "citations": ["name.ext:Lx-Ly", ...], "evidence": "Verified|Inferred|Unknown|Confirmation Required",
              "findings": ["F3", "F45"], "modernization": "one short sentence for the modernisation team"}}

Rules:
- merges: only findings that state the SAME rule (same condition, same value, same outcome) so one is
  redundant, typically the same check described from two files. Keep the most specific one; give a merged
  "statement" only if the dropped one adds a detail (value, message, location) the kept one lacks. Do NOT
  merge findings that differ in condition, value, layer behaviour or outcome; do not merge across different
  rules of the same feature. When in doubt, do not merge.
- duplicated_logic: the same logic implemented in more than one place (same layer or different layers),
  e.g. a threshold, validation, code list, calculation or message repeated; name every copy and say
  whether the copies agree today.
- inconsistencies: places that implement the same concept differently (different value, operator,
  boundary, code list, rounding, message, or a check present on one path and missing on another).
- logic_flaws: behaviour that looks wrong or fragile (contradictions, unreachable or dead branches,
  boundary errors, ignored results, a guard that can be bypassed, data that can become inconsistent).
  State the behaviour as fact; evidence is usually Confirmation Required.
- undocumented: behaviour a reader of the comments/messages/documentation would not expect (findings
  marked doc:none, hidden side effects, implicit limits, defaults, behaviour only in the DB).
- external_contract: what other systems or layers depend on and must stay compatible: procedure
  signatures, return codes and their meaning, result columns, code values, report parameters/fields,
  serialised payloads/DTOs/HTTP calls (if any).
- Each ITEM: a factual statement with the actual values, citations to EVERY location involved (file name
  with extension and lines), and the ids of the findings it is based on. 5-15 items per list where the
  material supports it; fewer if not. No recommendations inside "statement" (put them in "modernization").
"""


def doc_status(c: Corpus, f: dict) -> str:
    """'comment' if a comment block overlaps or sits just above any cited range, else 'none'."""
    for file, a, b in f["citations"]:
        for cm in c.f["comments"]:
            if cm["file"] == file and cm["start"] <= b and cm["end"] >= a - 3:
                return "comment"
    return "none"


def candidates_text(v1: dict) -> str:
    L = []
    for g in v1["cross_layer_candidates"]:
        L.append(f"- CROSS-LAYER {g['concept']}: {g['differs']} | " + "; ".join(f"{m['cite']} `{m['text'][:80]}`" for m in g["members"]))
    for g in v1["duplicate_candidates"][:45]:
        detail = g.get("example") or g.get("text") or ", ".join(map(str, g.get("values", []))) or g.get("concept", "")
        L.append(f"- REPEATED {g['kind']} `{str(detail)[:100]}`: " + "; ".join(m["cite"] for m in g["members"][:8]))
    nocp = [d for d in v1["db_checks"] if not d["client_counterparts"]]
    L.append(f"- DB checks with no client-side counterpart: " + "; ".join(f"{d['cite']} `{d['text'][:50]}`" for d in nocp[:40]))
    k = v1["contracts"]
    for p in k["procs"]:
        L.append(f"- PROC {p['name']} [{p['cite']}] params {', '.join(x['name'] for x in p['params'])}; return codes {', '.join(p['return_codes'])}"
                 + ("; entry point" if p["entry_point"] else ""))
    for b in k["param_bindings"]:
        if b["missing_without_default"] or b["not_declared"]:
            L.append(f"- BINDING MISMATCH {b['vb_method']} -> {b['proc']} [{b['cite']}] missing {b['missing_without_default']} undeclared {b['not_declared']}")
    for r in k["reports"]:
        L.append(f"- REPORT {r['file']} params {', '.join(r.get('params', []))}")
    for x in k["io_calls"][:20]:
        L.append(f"- I/O {x['kind']} {x['api']} [{x['file']}:L{x['line']}]")
    for dm in v1["status_domains"][:12]:
        L.append(f"- CODES {dm['column']}: " + ", ".join(f"'{v}' ({len(ws)}x)" for v, ws in dm["values"].items()))
    return "\n".join(L)


# ============================================================================ helpers
class Budget:
    def __init__(self, log: Path, cap: float):
        self.log, self.cap = log, cap

    def spent(self):
        return summarize_log(self.log)["cost_usd"] if self.log.exists() else 0.0

    def call(self, prompt, system, tag, max_tokens):
        s = self.spent()
        if s > self.cap * 0.8:
            raise SystemExit(f"stopping: ${s:.2f} spent, near the ${self.cap} cap")
        t0 = time.time()
        r = converse(prompt, system=system, max_tokens=max_tokens, tag=tag)
        with open(self.log, "a") as fh:
            fh.write(json.dumps({"tag": tag, "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                                 "cost_usd": r["cost_usd"], "stop_reason": r["stop_reason"]}) + "\n")
        print(f"{tag}: in={r['input_tokens']} out={r['output_tokens']} ${r['cost_usd']:.4f} "
              f"({time.time() - t0:.1f}s, stop={r['stop_reason']})", flush=True)
        return r


def verify_db_only(c: Corpus, fs: list[dict], ci: CitationIndex) -> int:
    """Drop `db-only` when a cited DB comparison has a client-side counterpart (same subject + value/ref)."""
    comps = c.f["v1"]["comparisons"]
    n = 0
    for f in fs:
        if "db-only" not in f["flags"]:
            continue
        cited = [x for x in comps if x["layer"] == "db" and x["subject"] and
                 any(x["file"] == file and a <= x["line"] <= b for file, a, b in f["citations"])]
        cps = []
        for x in cited:
            cps += facts_post.counterparts(comps, x, {"client", "client-sql"})
        # the same message text raised by the client near a cited DB line also counts as a counterpart
        for g in c.f["v1"]["duplicate_candidates"]:
            if g["kind"] != "same-message":
                continue
            locs = [(m["cite"].rsplit(":L", 1)[0], int(m["cite"].rsplit(":L", 1)[1].split("-")[0]), m["unit"]) for m in g["members"]]
            near_db = any(file == lf and a - 10 <= ln <= b + 10 for file, a, b in f["citations"] for lf, ln, u in locs
                          if not u.startswith("vb:"))
            if near_db:
                cps += [{"cite": f"{lf}:L{ln}"} for lf, ln, u in locs if u.startswith("vb:")]
        if cps:
            n += 1
            f["flags"] = [x for x in f["flags"] if x != "db-only"]
            loc = ", ".join(sorted({o["cite"] for o in cps})[:3])
            f["flag_note"] = re.sub(r"`?db-only`?", "~~db-only~~", f.get("flag_note") or "") + \
                f" [layer-1 check: client-side counterpart at {loc}, so not db-only]"
            f["citations"] = list(dict.fromkeys(f["citations"] + ci.extract(loc)))
    return n


def section_items(md: str, ci: CitationIndex, origin: str) -> tuple[list[dict], str]:
    """Split layer-3 markdown into findings (tables) and the glossary section text."""
    parts = re.split(r"(?m)^##\s+Glossary\s*$", md, maxsplit=1)
    fs = parse_findings(parts[0], None, ci, origin=origin)
    return fs, (parts[1].strip() if len(parts) > 1 else "")


SEC5 = [("duplicated_logic", "Duplicated logic", "DUP"), ("inconsistencies", "Inconsistencies", "INC"),
        ("logic_flaws", "Possible logic flaws", "FLAW"), ("undocumented", "Undocumented behaviours", "UND"),
        ("external_contract", "External contract", "EXT")]
SEC3 = ["Architecture", "Persistence", "Lifecycles", "External Interfaces"]


# ============================================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--exclude", action="append", default=None)
    ap.add_argument("--include", action="append", default=None, help="only stage files matching these globs")
    ap.add_argument("--skip-structure", action="store_true", help="reuse <out>/facts.json and <out>/source")
    ap.add_argument("--dry-run", action="store_true", help="build contexts only, no Bedrock calls")
    ap.add_argument("--only", action="append", help="limit layer 2 to these relative paths")
    ap.add_argument("--reuse", action="store_true", help="reuse existing contexts/*.out.md (and layer 3/5 answers)")
    ap.add_argument("--budget", type=float, default=BUDGET_USD)
    a = ap.parse_args()
    excludes = a.exclude or DEFAULT_EXCLUDES
    a.out.mkdir(parents=True, exist_ok=True)
    src, facts_path = a.out / "source", a.out / "facts.json"

    if not a.skip_structure:
        stage_corpus(a.corpus, src, excludes, a.include)
        t0 = time.time()
        facts = run_structure(src, facts_path)
        v = facts["v1"]
        print(f"layer 1: {time.time() - t0:.1f}s counts={json.dumps(facts['derived']['counts'])}")
        print(f"layer 1b: cross_layer={len(v['cross_layer_candidates'])} duplicates={len(v['duplicate_candidates'])} "
              f"status_domains={len(v['status_domains'])}")
    else:
        facts = json.loads(facts_path.read_text())
        if "v1" not in facts:
            facts = facts_post.enrich(facts, src)
            facts_path.write_text(json.dumps(facts, indent=1))
            (a.out / "graph.json").write_text(json.dumps(facts_post.build_graph(facts, facts["v1"]), indent=1))
    c = Corpus(src, facts)
    files = [x["file"] for x in facts["files"]]
    ci = CitationIndex(src, files)

    ctx_dir = a.out / "contexts"
    ctx_dir.mkdir(exist_ok=True)
    log = a.out / "tokens.jsonl"
    if not a.dry_run and log.exists() and not a.only and not a.reuse:
        log.unlink()
    bud = Budget(log, a.budget)

    # ---------------- layer 2
    l2_files = [f for f in files if not a.only or f in a.only]
    for rel in l2_files:
        context = build_context(c, rel)
        body = "\n".join(f"{i:4d} | {l}" for i, l in enumerate(c.lines[rel], 1))
        prompt = PROMPT.format(name=rel, base=rel.split("/")[-1], context=context, body=body)
        stem = rel.replace("/", "__")
        (ctx_dir / (stem + ".md")).write_text(prompt)
        if a.dry_run:
            print(f"{rel}: prompt {len(prompt)} chars (~{len(prompt) // 4} tokens)")
            continue
        outp = ctx_dir / (stem + ".out.md")
        if a.reuse and outp.exists():
            continue
        r = bud.call(prompt, SYSTEM, f"lean1:{rel}", 10000)
        outp.write_text(r["text"])
    if a.dry_run:
        summ = system_summary(c)
        (ctx_dir / "_system.md").write_text(PROMPT3.format(summary=summ))
        print(f"layer 3 prompt ~{len(summ) // 4} tokens")
        return
    if a.only:
        return

    findings = []
    for rel in files:
        p = ctx_dir / (rel.replace("/", "__") + ".out.md")
        if p.exists():
            findings += parse_findings(p.read_text(), rel, ci)
    for i, f in enumerate(findings, 1):
        f["id"] = f"F{i}"
        f["doc"] = doc_status(c, f)
    n_db = verify_db_only(c, findings, ci)
    print(f"layer 2: {len(findings)} findings; db-only flags removed by layer-1 check: {n_db}")

    # ---------------- layer 3
    p3 = ctx_dir / "_system.out.md"
    summ = system_summary(c)
    (ctx_dir / "_system.md").write_text(PROMPT3.format(summary=summ))
    if not (a.reuse and p3.exists()):
        r = bud.call(PROMPT3.format(summary=summ), SYSTEM3, "lean1:system", 12000)
        p3.write_text(r["text"])
    sys_findings, glossary = section_items(p3.read_text(), ci, "layer3")
    for i, f in enumerate(sys_findings, 1):
        f["id"] = f"S{i}"

    # ---------------- layer 5
    p5 = ctx_dir / "_consolidate.out.json"
    ftxt = "\n".join(
        f"{f['id']} [{f['file'].split('/')[-1]} / {f['section']}] {re.sub(r'\s+', ' ', f['statement'])[:280]} "
        f"{{{'; '.join(ci.fmt(*x) or '' for x in f['citations'][:4])}}} {f['evidence']} "
        f"{','.join(f['flags']) or '-'} doc:{f['doc']}" for f in findings)
    prompt5 = PROMPT5.format(findings=ftxt, candidates=candidates_text(facts["v1"]))
    (ctx_dir / "_consolidate.md").write_text(prompt5)
    if not (a.reuse and p5.exists()):
        r = bud.call(prompt5, SYSTEM5, "lean1:consolidate", 20000)
        p5.write_text(r["text"])
    try:
        cons = extract_json(p5.read_text())
    except ValueError:
        print("WARNING: consolidation answer is not JSON; no merges / cross-cutting sections")
        cons = {}
    byid = {f["id"]: f for f in findings}
    dropped, n_merge = set(), 0
    for m in cons.get("merges", []) or []:
        keep = byid.get(m.get("keep"))
        drops = [byid[x] for x in m.get("drop", []) if x in byid and x != m.get("keep") and x not in dropped]
        if not keep or keep["id"] in dropped or not drops:
            continue
        for d in drops:
            dropped.add(d["id"])
            keep["citations"] = list(dict.fromkeys(keep["citations"] + d["citations"]))
            keep["flags"] = list(dict.fromkeys(keep["flags"] + d["flags"]))
            keep.setdefault("merged_from", []).append(d["id"])
        if m.get("statement") and len(m["statement"]) >= 0.6 * len(keep["statement"]):
            keep["statement"] = m["statement"]
        n_merge += 1
    final = [f for f in findings if f["id"] not in dropped]
    cross = []
    for key, title, pre in SEC5:
        for j, it in enumerate(cons.get(key, []) or [], 1):
            cits = ci.extract(" ; ".join(it.get("citations", [])))
            for fid in it.get("findings", []) or []:
                if fid in byid and byid[fid]["citations"] and not any(x[0] == byid[fid]["citations"][0][0] for x in cits):
                    cits.append(byid[fid]["citations"][0])
            ev = next((e for e in EVIDENCE if e.lower() == str(it.get("evidence", "")).lower()), "Inferred")
            stmt = re.sub(r"\s*\((?:see |findings? )?F\d+(?:\s*[,/;]\s*F\d+)*\)", "", it.get("statement", ""))
            stmt = re.sub(r"\s*\b(?:findings?|per) F\d+(?:\s*[,/;]\s*F\d+)*", "", stmt)
            cross.append({"id": f"{pre}-{j:02d}", "local_id": f"{pre}-{j:02d}", "section": title, "file": None,
                          "statement": stmt, "citations": list(dict.fromkeys(cits)), "evidence": ev,
                          "flags": [], "flag_note": "", "origin": "layer5", "based_on": it.get("findings", []),
                          "modernization": it.get("modernization", "")})
    print(f"layer 5: merged {n_merge} groups ({len(dropped)} findings dropped); cross-cutting items {len(cross)}")

    # ---------------- assemble
    out = [f"# Lean spec v1: {a.corpus.name}", ""]
    out.append("# System overview")
    for sec in SEC3:
        items = [f for f in sys_findings if f["section"].lower().startswith(sec.lower())]
        if items:
            out += ["", f"## {sec}", "", render_findings(items, ci)]
    out += ["", "# Cross-cutting findings"]
    for key, title, pre in SEC5:
        items = [f for f in cross if f["section"] == title]
        out += ["", f"## {title}", "", render_findings(items, ci) if items else "_none found_"]
    for rel in files:
        fs = [f for f in final if f["file"] == rel]
        if not fs:
            continue
        out += ["", "---", "", f"# File: {rel}"]
        for sec in dict.fromkeys(f["section"] for f in fs):
            items = [f for f in fs if f["section"] == sec and not f.get("bullet")]
            bullets = [f for f in fs if f["section"] == sec and f.get("bullet")]
            out += ["", f"## {sec}", ""]
            if items:
                out.append(render_findings(items, ci))
            for b in bullets:
                out.append("- " + ci.normalize_text(b["statement"], rel))
    (a.out / "spec.md").write_text("\n".join(out) + "\n")
    allf = sys_findings + cross + final
    for f in allf:
        f["citations"] = [list(x) for x in f["citations"]]
    (a.out / "findings.json").write_text(json.dumps({"schema": "lean-findings/v1", "findings": allf,
                                                      "dropped": sorted(dropped)}, indent=1))
    (a.out / "glossary.md").write_text("# Glossary\n\n" + ci.normalize_text(glossary) + "\n")
    mod = ["# Modernisation notes (from layer 5; recommendations, not part of spec.md)", ""]
    for f in cross:
        if f.get("modernization"):
            mod.append(f"- **{f['id']}** ({f['section']}): {f['modernization']} "
                       f"[{'; '.join(ci.fmt(*x) or '' for x in f['citations'][:4])}]")
    (a.out / "modernization.md").write_text("\n".join(mod) + "\n")
    tot = summarize_log(log)
    (a.out / "generation_usage.json").write_text(json.dumps(tot, indent=2))
    print("total:", tot)


if __name__ == "__main__":
    main()
