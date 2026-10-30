#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""facts_block.py: render the per-page "Structural facts" block appended to map prompts.

Input: an enriched facts.json (structure tool + facts_post.enrich) and the units a page carries
(path + original line range, from the page's units.jsonl ledger). Output: a compact, line-cited
text block about the files on that page: calls, VB -> stored-proc calls, proc/table reads and
writes, triggers, constants with their values, cross-layer comparison candidates and
duplicate-logic candidates.

The block is bounded: it never exceeds `max_chars`. Sections are filled in a fixed priority order
and items that do not fit are dropped, with a count of how many were left out. Nothing here is
corpus-specific; every line is derived from parser output and carries file + line.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from facts_post import clr, unit_prefix  # the merged VB.NET + C# view; one definition, see facts_post

HEADER = (
    "STRUCTURAL FACTS FOR FILES ON THIS PAGE (deterministic parser output: Roslyn for VB.NET and "
    "C#, ScriptDom for T-SQL).\n"
    "Treat these as authoritative STRUCTURE: who calls whom, which procedures and tables are "
    "read or written, constant values and line numbers. They say nothing about business intent. "
    "Lines marked CANDIDATE are hints only: report one only if the source on this page confirms "
    "it. Cite the source lines (file:Lx-Ly), not this block. Locations on other pages are given "
    "so you can name cross-file relationships; do not invent their content."
)

# Section order = priority order (highest-value, lowest-volume first).
SECTIONS = (
    ("cross_layer", "Cross-layer candidates: same concept compared differently, same name (CANDIDATE)"),
    ("constants", "Constants (value, definition, uses)"),
    ("data", "Stored procedures, tables and triggers"),
    ("duplicates", "Repeated logic (CANDIDATE)"),
    ("calls", "Cross-file calls"),
)


# ── page units ────────────────────────────────────────────────────────────────────────────
def page_units(page_dir: str | Path) -> dict[str, list[tuple[int, int]]]:
    """{rel_path: [(line_start, line_end), ...]} for every emitted unit on the page."""
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    p = Path(page_dir) / "units.jsonl"
    if not p.is_file():
        return {}
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r.get("status") != "emitted":
            continue
        out[r["path"]].append((int(r["line_start"]), int(r["line_end"])))
    return dict(out)


class _Page:
    def __init__(self, units: dict[str, list[tuple[int, int]]]):
        self.units = units

    def has(self, file: str | None, a: int | None = None, b: int | None = None) -> bool:
        """True when [a, b] overlaps a unit of `file` on this page (whole file when a is None)."""
        if not file or file not in self.units:
            return False
        if a is None:
            return True
        b = a if b is None else b
        return any(not (b < ls or a > le) for ls, le in self.units[file])

    def cite_on(self, cite: str) -> bool:
        file, _, rng = cite.rpartition(":")
        if not file:
            return False
        try:
            parts = [int(x.lstrip("L")) for x in rng.split("-")]
        except ValueError:
            return self.has(file)
        return self.has(file, parts[0], parts[-1])


# ── formatting helpers ────────────────────────────────────────────────────────────────────
class _Names:
    """Display a file by basename when it is unique in the corpus, else by its relative path."""

    def __init__(self, files: list[str]):
        counts: dict[str, int] = defaultdict(int)
        for f in files:
            counts[Path(f).name] += 1
        self.short = {f: (Path(f).name if counts[Path(f).name] == 1 else f) for f in files}

    def f(self, file: str) -> str:
        return self.short.get(file, file)

    def cite(self, file: str, a, b=None) -> str:
        b = a if b is None else b
        return f"{self.f(file)}:L{a}" if a == b else f"{self.f(file)}:L{a}-L{b}"

    def recite(self, cite: str) -> str:
        """Shorten a 'path:Lx-Ly' cite produced by facts_post."""
        file, _, rng = cite.rpartition(":")
        return f"{self.f(file)}:{rng}" if file else cite


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _lines(xs) -> str:
    ls = sorted({int(x) for x in xs})
    return ",".join(f"L{x}" for x in ls[:6]) + ("..." if len(ls) > 6 else "")


def _tables(xs, with_op=False) -> str:
    out = []
    for x in xs:
        op = f" {x['op']}" if with_op and x.get("op") else ""
        out.append(f"{x['table']}{op} ({_lines(x.get('lines', []))})")
    return "; ".join(out)


# ── section builders: each returns a list of item strings ─────────────────────────────────
def _cross_layer(f: dict, pg: _Page, nm: _Names) -> list[str]:
    out = []
    for g in f.get("v1", {}).get("cross_layer_candidates", []):
        if not any(pg.cite_on(m["cite"]) for m in g["members"]):
            continue
        out.append(f"{g['concept']} ({_clip(g.get('differs', ''), 160).replace(chr(10), ' ')}): " + "; ".join(
            f"{nm.recite(m['cite'])} `{_clip(m['text'], 70)}`" for m in g["members"][:6]))
    for x in f.get("derived", {}).get("cross_layer_name_matches", []):
        sides = [(x["file"], x["start"])] + [(m["file"], m.get("line")) for m in x["sql_matches"]]
        if not any(pg.has(fl, ln) for fl, ln in sides):
            continue
        out.append(f"VB const {x['vb_const']} = {x['value']} [{nm.cite(x['file'], x['start'])}] has the same name as "
                   + ", ".join(f"{m['kind']} {m['name']} [{nm.cite(m['file'], m['line'])}]" for m in x["sql_matches"][:4]))
    return out


def _constants(f: dict, pg: _Page, nm: _Names) -> list[str]:
    vb = clr(f)
    consts = {c["name"]: c for c in vb.get("constants", [])}
    uses_by: dict[str, list[dict]] = defaultdict(list)
    for u in vb.get("const_uses", []):
        uses_by[u["const"]].append(u)
    out = []
    for name in sorted(consts):
        c = consts[name]
        defined_here = pg.has(c["file"], c["start"], c["end"])
        uses = uses_by.get(name, [])
        used_here = [u for u in uses if pg.has(u["file"], u["line"])]
        if not defined_here and not used_here:
            continue
        shown = used_here if not defined_here else (used_here + [u for u in uses if u not in used_here])
        use_txt = ", ".join(nm.cite(u["file"], u["line"]) + (f" ({u['comparison']})" if u.get("comparison") else "")
                            for u in shown[:5]) + (f" +{len(shown) - 5} more" if len(shown) > 5 else "")
        out.append(f"{name} = {_clip(c.get('value', '?'), 40)} [{nm.cite(c['file'], c['start'])}]"
                   + (f"; used at {use_txt}" if use_txt else "; no uses in corpus"))
    return out


def _data(f: dict, pg: _Page, nm: _Names) -> list[str]:
    vb, sql, d = clr(f), f.get("sql", {}), f.get("derived", {})
    procs = {p["name"].lower(): p for p in sql.get("procs", [])}
    tindex = {t["table"].lower(): t for t in d.get("table_index", [])}
    out = []
    # VB -> stored procedures
    for r in vb.get("proc_refs", []):
        if not pg.has(r["file"], r["line"]):
            continue
        p = procs.get(r["proc"].lower())
        if p:
            out.append(f"{r['from']} [{nm.cite(r['file'], r['line'])}] calls proc {p['name']} "
                       f"[{nm.cite(p['file'], p['start'], p['end'])}]")
        else:
            out.append(f"{r['from']} [{nm.cite(r['file'], r['line'])}] calls proc {r['proc']}, which is NOT defined in the corpus")
    # inline SQL in VB
    for s in vb.get("inline_sql", []):
        if not pg.has(s["file"], s["start"], s["end"]) or not (s.get("reads") or s.get("writes")):
            continue
        parts = []
        if s.get("reads"):
            parts.append("reads " + _tables(s["reads"]))
        if s.get("writes"):
            parts.append("writes " + _tables(s["writes"], with_op=True))
        out.append(f"inline SQL in {s['from']} [{nm.cite(s['file'], s['start'], s['end'])}]: " + "; ".join(parts))
    # SQL units defined on this page
    units = [("proc", u) for u in sql.get("procs", [])] + [("trigger", u) for u in sql.get("triggers", [])] + \
            [("script", u) for u in sql.get("scripts", [])]
    for kind, u in units:
        if not pg.has(u["file"], u["start"], u["end"]):
            continue
        label = f"{kind} {u.get('name') or nm.f(u['file'])} [{nm.cite(u['file'], u['start'], u['end'])}]"
        if kind == "trigger":
            label += f" ON {u['table']} {'/'.join(u.get('events', []))}"
        parts = [label]
        if u.get("reads"):
            parts.append("reads " + _tables(u["reads"]))
        if u.get("writes"):
            parts.append("writes " + _tables(u["writes"], with_op=True))
        for x in u.get("exec_calls", [])[:6]:
            q = procs.get(x["proc"].lower())
            parts.append(f"calls proc {x['proc']} at L{x['line']}" +
                         (f" [{nm.cite(q['file'], q['start'], q['end'])}]" if q else " (NOT in corpus)"))
        if u.get("raises"):
            parts.append("raises at " + _lines(x["line"] for x in u["raises"]))
        if u.get("returns"):
            parts.append("returns " + ", ".join(f"{x['value']} (L{x['line']})" for x in u["returns"][:6]))
        # VB callers of this proc (often on another page)
        if kind == "proc":
            for pi in d.get("proc_index", []):
                if pi["proc"].lower() != u["name"].lower():
                    continue
                callers = (pi.get("vb_callers") or []) + (pi.get("cs_callers") or [])
                if callers:
                    parts.append("called from client code " + ", ".join(
                        f"{c['from']} [{nm.cite(c['file'], c['line'])}]" for c in callers[:4]))
        # triggers fired by tables this unit writes
        for w in u.get("writes", []):
            for tr in tindex.get(w["table"].lower(), {}).get("triggers", []):
                if tr.get("trigger") != u.get("name"):
                    parts.append(f"writing {w['table']} fires trigger {tr['trigger']} "
                                 f"({'/'.join(tr.get('events', []))}) [{nm.cite(tr['file'], tr['start'], tr['end'])}]")
        out.append("; ".join(parts))
    # tables defined on this page: who reads / writes them
    for t in sql.get("tables", []):
        if not pg.has(t["file"], t["start"], t["end"]):
            continue
        ti = tindex.get(t["name"].lower(), {})
        w = sorted({x["unit"] for x in ti.get("written_by", [])})
        r = sorted({x["unit"] for x in ti.get("read_by", [])})
        trg = [x["trigger"] for x in ti.get("triggers", [])]
        out.append(f"table {t['name']} [{nm.cite(t['file'], t['start'], t['end'])}]"
                   + (f"; written by {', '.join(w[:6])}{'...' if len(w) > 6 else ''}" if w else "")
                   + (f"; read by {', '.join(r[:6])}{'...' if len(r) > 6 else ''}" if r else "")
                   + (f"; triggers {', '.join(trg)}" if trg else ""))
    return out


def _duplicates(f: dict, pg: _Page, nm: _Names) -> list[str]:
    what = {"same-expression": "same expression", "same-shape": "same expression shape",
            "same-message": "same message text", "shared-literals": "shared literal values",
            "same-threshold-check": "same threshold check"}
    out = []
    for g in f.get("v1", {}).get("duplicate_candidates", []):
        if not any(pg.cite_on(m["cite"]) for m in g["members"]):
            continue
        detail = g.get("example") or g.get("text") or ", ".join(map(str, g.get("values", []))) or g.get("concept", "")
        out.append(f"{what.get(g['kind'], g['kind'])} `{_clip(detail, 90)}` at "
                   + "; ".join(nm.recite(m["cite"]) for m in g["members"][:6])
                   + (f" +{len(g['members']) - 6} more" if len(g["members"]) > 6 else ""))
    return out


def _calls(f: dict, pg: _Page, nm: _Names) -> list[str]:
    vb, d = clr(f), f.get("derived", {})
    methods = {m["id"]: m for m in vb.get("methods", [])}
    out_by: dict[str, list[str]] = defaultdict(list)
    in_by: dict[str, list[str]] = defaultdict(list)
    for c in vb.get("calls", []):
        callee = methods.get(c["to"])
        if not callee:
            continue  # unresolved (framework / outside the corpus)
        if pg.has(c["file"], c["line"]):
            # same-file calls whose callee is also on this page are visible in the source itself
            if callee["file"] == c["file"] and pg.has(callee["file"], callee["start"], callee["end"]):
                continue
            out_by[c["from"]].append(f"{c['to']} [{nm.cite(callee['file'], callee['start'], callee['end'])}] @L{c['line']}")
        elif pg.has(callee["file"], callee["start"], callee["end"]):
            in_by[c["to"]].append(f"{c['from']} [{nm.cite(c['file'], c['line'])}]")
    out = []
    for frm in sorted(out_by):
        out.append(f"{frm} calls " + ", ".join(dict.fromkeys(out_by[frm])))
    for to in sorted(in_by):
        out.append(f"{to} is called by " + ", ".join(dict.fromkeys(in_by[to])))
    for m in (d.get("vb_methods_without_callers") or []) + (d.get("cs_methods_without_callers") or []):
        if pg.has(m["file"], m["start"], m["end"]):
            out.append(f"{m['method']} [{nm.cite(m['file'], m['start'], m['end'])}] has no caller in the corpus")
    return out


_BUILDERS = {"cross_layer": _cross_layer, "constants": _constants, "data": _data,
             "duplicates": _duplicates, "calls": _calls}


def render_page_block(facts: dict, units: dict[str, list[tuple[int, int]]], max_chars: int) -> str:
    """The bounded facts block for one page ('' when nothing applies or the bound is too small)."""
    if not units or max_chars <= len(HEADER) + 40:
        return ""
    pg = _Page(units)
    nm = _Names([x["file"] for x in facts.get("files", [])])
    out = [HEADER]
    used = len(HEADER)
    any_item = False
    for key, title in SECTIONS:
        items = _BUILDERS[key](facts, pg, nm)
        if not items:
            continue
        head = f"\n## {title}"
        if used + len(head) + 40 > max_chars:
            break
        sec, dropped = [head], 0
        sec_len = len(head)
        for it in items:
            line = "\n- " + it
            # keep room for a final "omitted" note
            if used + sec_len + len(line) + 40 > max_chars:
                dropped += 1
                continue
            sec.append(line)
            sec_len += len(line)
        if len(sec) == 1:
            continue
        if dropped:
            sec.append(f"\n- ({dropped} more omitted for length)")
            sec_len += len(sec[-1])
        out.extend(sec)
        used += sec_len
        any_item = True
    if not any_item:
        return ""
    text = "".join(out)
    return text[:max_chars]
