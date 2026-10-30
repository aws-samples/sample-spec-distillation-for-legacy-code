#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
"""Lean pipeline v1, layer 1b: deterministic post-processing of facts.json (no LLM).

Adds facts["v1"] with:
  units                 every code unit (VB method, SQL proc/function/view/trigger/script, report file)
  comparisons           VB + SQL comparisons normalised to (subject ids, op, reference ids, value)
  concept_index         concept id -> comparisons that use it (for counterpart / db-only checks)
  cross_layer_candidates the same concept/threshold compared with a different operator or value elsewhere
  duplicate_candidates  repeated logic: expression fingerprints, shared message literals, literal clusters,
                        same threshold comparison in several units
  status_domains        code columns -> values seen in CHECKs, literal SETs, comparisons, VB constants
  contracts             procs (signature, return codes, result sets, callers), reports, tables, I/O calls,
                        serialisation attributes, enums, DTO-like types, VB<->proc parameter bindings
and writes graph.json (symbol/table dependency graph for impact queries).

Everything here is lexical/structural and carries file + line, so the LLM layers can cite it.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from itertools import combinations
from pathlib import Path

STOP_IDS = set("""
tostring trim ucase lcase cdec cint cstr cdate cbyte clng cdbl isdbnull isnull isnothing nothing len ltrim rtrim
upper lower value item items rows row count length date today now getdate convert abs datediff dateadd day month
year format round math string compare equals true false null and or not is isnot me myclass mybase text cells
substring left right coalesce cast nullif exists select from where set top dbo sum min max avg case when then else
end in like between new object dt dr ds da cn cmd sql rs rc ret result results tmp temp i j k x y s n v r h d e
parameters addwithvalue add tables fill datatable datarow defaultview rowstate deleted inserted datarowstate
""".split())
OPS = {"Equals": "=", "NotEqualToBrackets": "<>", "NotEqualToExclamation": "<>", "LessThan": "<",
       "LessThanOrEqualTo": "<=", "GreaterThan": ">", "GreaterThanOrEqualTo": ">=", "In": "in", "NotIn": "notin",
       "NotLessThan": ">=", "NotGreaterThan": "<="}
FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "=", "<>": "<>", "in": "in", "notin": "notin"}
RELATIONAL = {"<", ">", "<=", ">="}
SQL_KEYWORDS = set("""select from where and or not null is in exists case when then else end as on join left right
inner outer group by order having distinct top insert update delete set values into declare begin if return
exec execute convert cast isnull len ltrim rtrim upper lower getdate dateadd datediff sum count max min avg abs
day month year varchar char int numeric smalldatetime datetime tinyint bit money decimal ms""".split())


# ============================================================================ CLR sections
# The structure tool emits VB.NET facts under "vb" and C# facts under "cs", with the SAME keys.
# Everything below is language-neutral, so the two are merged into one view here and a unit's id
# prefix is derived from its file extension. Consequence: duplicate-logic and cross-layer
# comparison candidates span VB, C# and T-SQL with no per-language branches, and a corpus with no
# .cs files produces byte-identical output to before (the merge is a no-op).
CLR_SECTIONS = ("vb", "cs")
CLR_KEYS = ("types", "methods", "members", "constants", "calls", "proc_refs", "inline_sql",
            "literals", "const_uses", "comparisons", "fingerprints", "column_refs",
            "unit_literals", "local_aliases", "attributes", "enums", "io_calls")


def unit_prefix(file: str | None) -> str:
    """The unit-id prefix for a CLR fact, from its file: `cs:` for C#, `vb:` otherwise."""
    return "cs" if str(file or "").lower().endswith(".cs") else "vb"


def clr(f: dict) -> dict:
    """VB + C# facts merged into one language-neutral view: {key: [facts from both sections]}."""
    return {k: list((f.get("vb") or {}).get(k) or []) + list((f.get("cs") or {}).get(k) or [])
            for k in CLR_KEYS}


def clr_uid(fact: dict, key: str = "from") -> str:
    """The unit id for a CLR fact (`vb:Class.Method` / `cs:Class.Method`)."""
    return f"{unit_prefix(fact.get('file'))}:{fact.get(key)}"


def norm_id(s: str) -> str:
    s = (s or "").strip().lstrip("@")
    s = re.sub(r"^(m_|g_)", "", s)
    return re.sub(r"[\W_]", "", s).lower()


def id_variants(s: str) -> set[str]:
    """Normalised identifier plus a de-Hungarian variant (sRole -> role, dtLines -> lines)."""
    out = set()
    n = norm_id(s)
    if n:
        out.add(n)
    m = re.match(r"^(?:m_|g_)?([a-z]{1,3})([A-Z]\w+)$", (s or "").lstrip("@"))
    if m and len(m.group(2)) >= 4:
        out.add(norm_id(m.group(2)))
    return {x for x in out if len(x) >= 3 and x not in STOP_IDS}


def specific(i: str) -> bool:
    return len(i) >= 4 and i not in STOP_IDS


def num(v):
    # Trailing type characters: VB (D R F @ # !) and C# literal suffixes (m/M decimal, u/U, d, f, l).
    # `5000m` in C# and `5000` in T-SQL are the same threshold; without this they never match.
    try:
        return float(str(v).strip().strip("'").rstrip("DdRrFf@#!MmUuLl"))
    except Exception:
        return None


def cite(file, a, b=None):
    b = b or a
    return f"{file}:L{a}" if a == b else f"{file}:L{a}-L{b}"


# ============================================================================ SQL operand parsing
SQL_LIT_RE = re.compile(r"^\s*\(?\s*(N?'(?P<s>[^']*)'|(?P<n>-?\d+(?:\.\d+)?))\s*\)?\s*$")
ISNULL_DEFAULT_RE = re.compile(r"(?:ISNULL|COALESCE)\s*\(\s*(?P<id>[@\w.]+)\s*,\s*(?:N?'(?P<s>[^']*)'|(?P<n>-?\d+(?:\.\d+)?))\s*\)", re.I)


def sql_operand(text: str, var_alias: dict) -> dict:
    text = text or ""
    m = SQL_LIT_RE.match(text)
    value = None
    if m:
        value = m.group("s") if m.group("s") is not None else str(num(m.group("n")))
    d = ISNULL_DEFAULT_RE.search(text)
    default = None
    if d:
        default = d.group("s") if d.group("s") is not None else str(num(d.group("n")))
    ids = set()
    for tok in re.findall(r"@?[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*", re.sub(r"'[^']*'", " ", text)):
        last = tok.split(".")[-1]
        if last.lower() in SQL_KEYWORDS:
            continue
        ids |= id_variants(last)
        if tok.startswith("@") and tok.lower() in var_alias:
            ids |= id_variants(var_alias[tok.lower()])
    return {"text": text, "ids": sorted(ids), "value": value, "default": default}


def vb_operand(o: dict, aliases: dict) -> dict:
    ids = set()
    for i in o.get("ids", []):
        ids |= id_variants(i)
        for a in aliases.get(i.lower(), []):
            ids |= id_variants(a)
    return {"text": o.get("text", ""), "ids": sorted(ids), "value": o.get("value"),
            "default": next((a["default"] for i in o.get("ids", []) for a in aliases.get("__d__" + i.lower(), [])), None)}


def fmt_val(v):
    if v is None:
        return None
    f = num(v)
    if f is not None and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", str(v)):
        return ("%f" % f).rstrip("0").rstrip(".")
    return str(v)


# ============================================================================ units
def build_units(f: dict) -> list[dict]:
    vb, sql = clr(f), f["sql"]
    forms = {t["name"] for t in vb["types"] if t.get("is_form")}
    units = []
    for m in vb["methods"]:
        p = unit_prefix(m["file"])
        units.append({"id": f"{p}:{m['id']}", "kind": f"{p}-" + m["kind"].lower(), "name": m["id"],
                      "file": m["file"],
                      "start": m["start"], "end": m["end"], "layer": "ui" if m["class"] in forms else "client",
                      "access": m["access"], "handles": m.get("handles") or []})
    for p in sql["procs"]:
        units.append({"id": f"sql:{p['name']}", "kind": p.get("kind", "procedure"), "name": p["name"], "file": p["file"],
                      "start": p["start"], "end": p["end"], "layer": "db"})
    for t in sql["triggers"]:
        units.append({"id": f"trg:{t['name']}", "kind": "trigger", "name": t["name"], "file": t["file"],
                      "start": t["start"], "end": t["end"], "layer": "db", "table": t["table"]})
    for s in sql["scripts"]:
        units.append({"id": f"script:{s['file']}", "kind": "script", "name": s["file"], "file": s["file"],
                      "start": s["start"], "end": s["end"], "layer": "db"})
    for x in f["files"]:
        if x["language"] == "other":
            units.append({"id": f"doc:{x['file']}", "kind": "report-or-doc", "name": x["file"], "file": x["file"],
                          "start": 1, "end": x["end"], "layer": "report"})
    for t in sql["tables"]:
        units.append({"id": f"ddl:{t['name']}", "kind": "table-ddl", "name": t["name"], "file": t["file"],
                      "start": t["start"], "end": t["end"], "layer": "db"})
    return units


def sql_units(f):
    """(unit id, unit dict) for every SQL body incl. VB inline SQL (attributed to its VB method)."""
    for p in f["sql"]["procs"]:
        yield f"sql:{p['name']}", p, "db"
    for t in f["sql"]["triggers"]:
        yield f"trg:{t['name']}", t, "db"
    for s in f["sql"]["scripts"]:
        yield f"script:{s['file']}", s, "db"
    for s in clr(f)["inline_sql"]:
        yield clr_uid(s), s, "client-sql"


def resolve_columns(f: dict):
    """Fill table for unqualified SQL column refs using DDL columns of the tables the unit touches."""
    ddl = {t["name"].lower(): {c["name"].lower() for c in t["columns"]} for t in f["sql"]["tables"]}
    canon = {t["name"].lower(): t["name"] for t in f["sql"]["tables"]}
    for uid, u, _ in sql_units(f):
        touched = [x["table"] for x in u.get("reads", []) + u.get("writes", [])]
        if u.get("table"):
            touched.append(u["table"])
        tline = defaultdict(list)
        for x in u.get("reads", []) + u.get("writes", []):
            tline[x["table"].lower()] += x["lines"]
        expanded = []
        for c in u.get("columns", []):
            t = c.get("table")
            if t and t.lower() in ("inserted", "deleted") and u.get("table"):
                expanded.append(dict(c, table=u["table"]))
                continue
            if t or c.get("resolved_by_line"):
                expanded.append(c)
                continue
            for ln in c["lines"]:
                cands = [x for x in dict.fromkeys(z.lower() for z in touched) if c["column"].lower() in ddl.get(x, set())]
                e = dict(c, lines=[ln], resolved_by_line=True)
                e.pop("candidates", None)
                if cands:
                    if len(cands) > 1:
                        cands.sort(key=lambda x: min(abs(ln - l) for l in tline.get(x, [10 ** 6])))
                        e["ambiguous"] = [canon.get(x, x) for x in cands]
                    e["table"] = canon.get(cands[0], cands[0])
                expanded.append(e)
        merged = {}
        for e in expanded:
            k = (e.get("table"), e["column"], e["mode"])
            if k in merged:
                merged[k]["lines"] = sorted(set(merged[k]["lines"]) | set(e["lines"]))
            else:
                merged[k] = e
        u["columns"] = list(merged.values())


# ============================================================================ comparisons / concepts
def build_comparisons(f: dict) -> list[dict]:
    vb, sql = clr(f), f["sql"]
    out = []
    # VB/C# local aliases per method
    al = defaultdict(lambda: defaultdict(list))
    for a in vb.get("local_aliases", []):
        al[a["from"]][a["name"].lower()] += a["aliases"]
        if a.get("default"):
            al[a["from"]]["__d__" + a["name"].lower()].append(a)
    consts = {c["name"]: c for c in vb["constants"]}
    for c in vb.get("comparisons", []):
        aliases = al.get(c["from"], {})
        L, R = vb_operand(c["left"], aliases), vb_operand(c["right"], aliases)
        out.append(dict(layer="client", file=c["file"], line=c["line"], unit=clr_uid(c), op=c["op"],
                        L=L, R=R, text=c["text"], context=c["context"],
                        consts=c["left"].get("consts", []) + c["right"].get("consts", [])))
    for uid, u, layer in sql_units(f):
        va = {}
        for x in u.get("var_from_column", []):
            if x.get("column"):
                va[x["variable"].lower()] = x["column"]
        for p in u.get("params", []):
            va.setdefault(p["name"].lower(), p["name"])
        for c in u.get("comparisons", []):
            if "left" not in c:
                continue
            L, R = sql_operand(c["left"], va), sql_operand(c["right"], va)
            if c.get("values"):
                R["value"] = ",".join(sorted(str(v) for v in c["values"]))
            out.append(dict(layer="db" if layer == "db" else "client-sql", file=u["file"], line=c["line"], unit=uid,
                            op=OPS.get(c["op"], c["op"]), L=L, R=R, text=c["text"], context="sql", consts=[]))
    for t in sql["tables"]:
        for ch in t["checks"]:
            m = re.match(r"\s*\(?\s*\[?(\w+)\]?\s*(>=|<=|<>|!=|=|>|<|IN)\s*(.+?)\)?\s*$", ch["text"], re.I)
            if not m:
                continue
            op = {"!=": "<>"}.get(m.group(2).upper(), m.group(2).lower() if m.group(2).upper() == "IN" else m.group(2))
            R = sql_operand(m.group(3), {})
            if op == "in":
                R["value"] = ",".join(sorted(re.findall(r"'([^']*)'", m.group(3))))
            out.append(dict(layer="db", file=t["file"], line=ch["line"], unit=f"ddl:{t['name']}", op=op,
                            L={"text": m.group(1), "ids": sorted(id_variants(m.group(1))), "value": None, "default": None},
                            R=R, text=f"CHECK {ch['text']}", context="check", consts=[]))
    # orientation: literal / constant / threshold on the right
    for c in out:
        if c["L"].get("value") is not None and c["R"].get("value") is None:
            c["L"], c["R"] = c["R"], c["L"]
            c["op"] = FLIP.get(c["op"], c["op"])
        c["subject"] = [i for i in c["L"]["ids"] if specific(i)]
        c["ref"] = [i for i in c["R"]["ids"] if specific(i)]
        v = c["R"].get("value")
        c["value"] = fmt_val(v if v is not None else c["R"].get("default"))
        c["value_source"] = "literal" if v is not None else ("default" if c["R"].get("default") is not None else None)
        c["cite"] = cite(c["file"], c["line"])
        for k in consts:
            pass
    return out


def concept_index(comps: list[dict]) -> dict:
    idx = defaultdict(list)
    for n, c in enumerate(comps):
        for i in set(c["subject"]) | set(c["ref"]):
            idx[i].append(n)
    return idx


BOUNDARY = {">=": "upper", "<": "upper", ">": "lower", "<=": "lower"}


def concept_keys(c) -> set:
    """Relational comparisons only. Key = (subject id, reference id) or (subject id, literal value)."""
    if c["op"] not in RELATIONAL:
        return set()
    subj = set(c["subject"]) - set(c["ref"])   # drop self-joins (h.Vendor <> d.Vendor)
    if not subj or set(c["subject"]) & set(c["ref"]):
        return set()
    if c["ref"]:
        return {(s, "ref:" + r) for s in subj for r in c["ref"]}
    if num(c["value"]) is not None:
        return {(s, "val:" + c["value"]) for s in subj}
    return set()


def cross_layer_candidates(comps: list[dict]) -> tuple[list[dict], list[dict]]:
    """Groups of relational comparisons on the same concept + threshold in different units.
    Inconsistent when the boundary differs (>= vs >, < vs <=) or the threshold value differs."""
    keyed = [(c, concept_keys(c)) for c in comps]
    keyed = [(c, k) for c, k in keyed if k]
    parent = list(range(len(keyed)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    linked = set()
    for a, b in combinations(range(len(keyed)), 2):
        (x, kx), (y, ky) = keyed[a], keyed[b]
        if x["unit"] == y["unit"] or not (kx & ky):
            continue
        parent[find(a)] = find(b)
        linked |= {a, b}
    groups = defaultdict(list)
    for i in linked:
        groups[find(i)].append(i)
    inc, con = [], []
    for g in groups.values():
        members = [keyed[i][0] for i in sorted(g, key=lambda i: (keyed[i][0]["file"], keyed[i][0]["line"]))]
        bounds = {BOUNDARY[m["op"]] for m in members}
        vals = {m["value"] for m in members if m["value"] is not None}
        keys = set.intersection(*[keyed[i][1] for i in g]) or set.union(*[keyed[i][1] for i in g])
        concept = sorted({k[0] for k in keys})[:2], sorted({k[1].split(":", 1)[1] for k in keys})[:3]
        item = {"concept": "/".join(concept[0]) + " vs " + "/".join(concept[1]),
                "layers": sorted({m["layer"] for m in members}),
                "members": [{"cite": m["cite"], "unit": m["unit"], "layer": m["layer"], "op": m["op"], "value": m["value"],
                             "value_source": m["value_source"], "text": m["text"][:140]} for m in members]}
        diffs = []
        if len(bounds) > 1:
            diffs.append("boundary differs: " + ", ".join(f"{m['op']} at {m['cite']}" for m in members))
        if len(vals) > 1:
            diffs.append("threshold value differs: " + " vs ".join(sorted(vals)))
        if diffs:
            item["differs"] = "; ".join(diffs)
            inc.append(item)
        else:
            con.append(item)
    return inc, con


# ============================================================================ duplicates
def literal_sets(f: dict) -> dict:
    """unit id -> {file, values:{(kind,value): [lines]}}."""
    out = defaultdict(lambda: {"file": None, "values": defaultdict(list)})
    for l in clr(f).get("unit_literals", []):
        if not l["from"]:
            continue
        u = out[clr_uid(l)]
        u["file"] = l["file"]
        u["values"][(l["kind"], l["value"])].append(l["line"])
    for uid, u, layer in sql_units(f):
        if layer != "db":
            continue
        o = out[uid]
        o["file"] = u["file"]
        for s in u.get("string_literals", []):
            o["values"][("string", s["value"])] += s["lines"]
    for n in f["sql"]["numeric_literals"]:
        uid = None
        for p in f["sql"]["procs"]:
            if p["name"] == n["unit"]:
                uid = f"sql:{p['name']}"
        for t in f["sql"]["triggers"]:
            if t["name"] == n["unit"]:
                uid = f"trg:{t['name']}"
        ctx = n.get("context") or ""
        if uid and re.search(r"(char|binary|numeric|decimal)\s*\(\s*[\d,\s]*\b" + re.escape(str(n["value"])) + r"\b", ctx, re.I):
            continue
        if uid and re.search(r"CONVERT\s*\(.*,\s*" + re.escape(str(n["value"])) + r"\s*\)", ctx, re.I):
            continue
        if uid:
            v = fmt_val(n["value"])
            out[uid]["values"][("number", v)].append(n["line"])
    return out


def doc_literals(src: Path, f: dict) -> dict:
    """Quoted strings in non-code files (report definitions, docs) per file."""
    out = {}
    for x in f["files"]:
        if x["language"] != "other":
            continue
        vals = defaultdict(list)
        for i, line in enumerate((src / x["file"]).read_text(errors="replace").splitlines(), 1):
            for m in re.finditer(r'"([^"\n]{1,80})"|\'([^\'\n]{1,80})\'', line):
                vals[("string", m.group(1) or m.group(2))].append(i)
        out[f"doc:{x['file']}"] = {"file": x["file"], "values": vals}
    return out


SALIENT_SKIP = re.compile(r"^(\s*|[#0,.$%]+|MM/yy|MM/dd/yyyy|yyyy.*|N|Y|SYSTEM|@\w+|\W+)$")


def salient(kind, v, columns=frozenset()) -> bool:
    if kind == "string" and str(v).lower() in columns:
        return False
    if kind == "number":
        f_ = num(v)
        return f_ is not None and abs(f_) not in (0, 1, 2, 100) and f_ != -1
    v = str(v)
    return not SALIENT_SKIP.match(v) and len(v.strip()) >= 1


def duplicate_candidates(f: dict, src: Path, consistent_groups: list[dict]) -> list[dict]:
    out = []
    # 1. expression / statement fingerprints repeated in different units
    fps = []
    for x in clr(f).get("fingerprints", []):
        # `lang` buckets the fingerprints: a VB expression and a C# expression are compared within
        # their own language (token shapes differ), while shared literals/messages cross languages.
        fps.append({"lang": unit_prefix(x["file"]), "unit": clr_uid(x), "file": x["file"],
                    "start": x["start"], "end": x["end"],
                    "kind": x["kind"], "k1": x["k1"], "k2": x["k2"], "ntok": x["ntok"], "text": x["text"]})
    for uid, u, layer in sql_units(f):
        for x in u.get("fingerprints", []):
            fps.append({"lang": "sql", "unit": uid, "file": u["file"], "start": x["line"], "end": x["end"],
                        "kind": x["kind"], "k1": x["k1"], "k2": x["k2"], "ntok": x["ntok"], "text": x["text"]})
    seen_spans = set()
    for key in ("k1", "k2"):
        by = defaultdict(list)
        for x in fps:
            by[(x["lang"], x[key])].append(x)
        for (lang, k), xs in by.items():
            units = {x["unit"] for x in xs}
            if len(units) < 2:
                continue
            ntok = xs[0]["ntok"]
            has_lit = bool(re.search(r"'[^']*'|\"[^\"]*\"|#|\b\d{2,}\b", k))
            if ntok < (8 if has_lit else 14):
                continue
            if key == "k2" and ntok < 12:
                continue
            # one per unit, prefer largest span; skip if this exact span set was already reported via k1
            per_unit = {}
            for x in xs:
                per_unit.setdefault(x["unit"], x)
            sig = frozenset((x["file"], x["start"]) for x in per_unit.values())
            if sig in seen_spans:
                continue
            seen_spans.add(sig)
            out.append({"kind": "same-expression" if key == "k1" else "same-shape", "lang": lang,
                        "fragment_kind": xs[0]["kind"], "ntok": ntok, "example": xs[0]["text"][:160],
                        "members": [{"cite": cite(x["file"], x["start"], x["end"]), "unit": x["unit"]}
                                    for x in sorted(per_unit.values(), key=lambda x: (x["file"], x["start"]))]})
    # drop fingerprint groups whose every member is inside a bigger reported group's members
    out.sort(key=lambda g: -g["ntok"] * len(g["members"]))
    kept = []
    for g in out:
        spans = [(m["cite"].split(":L")[0], *map(int, m["cite"].split(":L")[1].replace("L", "").split("-") * 1)) for m in g["members"]]
        dominated = False
        for h in kept:
            if len(h["members"]) < len(g["members"]):
                continue
            hs = [(m["cite"].split(":L")[0], *map(int, m["cite"].split(":L")[1].replace("L", "").split("-"))) for m in h["members"]]
            if all(any(a[0] == b[0] and b[1] <= a[1] and a[-1] <= b[-1] for b in hs) for a in spans):
                dominated = True
                break
        if not dominated:
            kept.append(g)
    out = kept[:60]

    # 2. shared message / literal clusters across units in different files (incl. report definitions)
    ls = literal_sets(f)
    ls.update(doc_literals(src, f))
    columns = frozenset(c["name"].lower() for t in f["sql"]["tables"] for c in t["columns"]) | \
        frozenset(p["name"].lstrip("@").lower() for x in f["sql"]["procs"] for p in x["params"])
    sal = {u: {k: v for k, v in d["values"].items() if salient(*k, columns)} for u, d in ls.items()}
    msgs = defaultdict(list)
    for u, vals in sal.items():
        for (kind, v), lines in vals.items():
            if kind == "string" and len(v.strip()) >= 15:
                msgs[re.sub(r"\W+", " ", v).strip().lower()].append((u, ls[u]["file"], lines[0], v))
    for k, xs in msgs.items():
        files = {x[1] for x in xs}
        if len(files) >= 2:
            out.append({"kind": "same-message", "text": xs[0][3][:120],
                        "members": [{"cite": cite(x[1], x[2]), "unit": x[0]} for x in xs]})
    us = [u for u in sal if len(sal[u]) >= 3]
    clusters = []
    for a, b in combinations(us, 2):
        if ls[a]["file"] == ls[b]["file"]:
            continue
        A, B = set(sal[a]), set(sal[b])
        inter = A & B
        if len(inter) < 3:
            continue
        jac = len(inter) / len(A | B)
        if jac < 0.2:
            continue
        clusters.append({"kind": "shared-literals", "jaccard": round(jac, 2),
                         "values": sorted(str(v) for _, v in inter)[:16],
                         "members": [{"cite": cite(ls[u]["file"], min(l for k in inter for l in sal[u][k]),
                                                   max(l for k in inter for l in sal[u][k])), "unit": u} for u in (a, b)]})
    clusters.sort(key=lambda c: -c["jaccard"])
    out += clusters[:40]
    # 3. the same threshold comparison (same op + value / reference) in several units
    for g in consistent_groups:
        out.append({"kind": "same-threshold-check", "concept": g["concept"], "layers": g["layers"],
                    "members": [{"cite": m["cite"], "unit": m["unit"], "text": f"{m['op']} {m['value']}: {m['text'][:100]}"}
                                for m in g["members"]]})
    return out


# ============================================================================ status domains
def status_domains(f: dict, comps: list[dict], src: Path | None = None) -> list[dict]:
    """Code-like columns (short literal values) with every value seen and where."""
    src_lines = {x["file"]: (src / x["file"]).read_text(errors="replace").splitlines() for x in f["files"]} if src else {}
    dom = defaultdict(lambda: defaultdict(list))  # col -> value -> [where]
    tables = {t["name"].lower(): t for t in f["sql"]["tables"]}
    col_table = defaultdict(set)
    for t in f["sql"]["tables"]:
        for c in t["columns"]:
            col_table[c["name"].lower()].add(t["name"])
    for uid, u, layer in sql_units(f):
        for s in u.get("literal_sets", []):
            key = f"{s.get('table') or '?'}.{s['column']}"
            dom[key][s["value"]].append({"cite": cite(u["file"], s["line"]), "how": "SET", "unit": uid})
    # (unit, column) -> {line: table} from resolved SQL column refs
    where = defaultdict(dict)
    for uid, u, layer in sql_units(f):
        for x in u.get("columns", []):
            if x.get("table"):
                for ln in x["lines"]:
                    where[(uid, x["column"].lower())][ln] = x["table"]
    var_col = {}
    for uid, u, layer in sql_units(f):
        for x in u.get("var_from_column", []):
            if x.get("column"):
                var_col[(uid, norm_id(x["variable"]))] = x["column"].lower()
    for c in comps:
        if c["op"] not in ("=", "<>", "in", "notin") or c["R"].get("value") is None:
            continue
        vals = c["R"]["value"].split(",") if c["op"] in ("in", "notin") else [c["value"]]
        if any(len(v) > 6 for v in vals):
            continue
        subj = [i for i in c["subject"]]
        cols = [i for i in subj if i in col_table] or subj[:1]
        for col in cols[:1]:
            t = sorted(col_table.get(col, {"?"}))
            colname = var_col.get((c["unit"], col), col)
            hit = where.get((c["unit"], colname), {})
            if hit:
                ln = min(hit, key=lambda l: abs(l - c["line"]))
                t = [hit[ln]]
                col = colname
            key = f"{t[0] if len(t) == 1 else '|'.join(t)}.{col}"
            for v in vals:
                if v == "":
                    continue
                dom[key][v].append({"cite": c["cite"], "how": "CHECK" if c["context"] == "check" else f"compare {c['op']}",
                                    "unit": c["unit"]})
    for sr in f["sql"]["seed_rows"]:
        for col, val in zip(sr["columns"], sr["values"]):
            m = re.fullmatch(r"N?'([^']{1,6})'", val.strip())
            if m and len(sr["columns"]) > 1 and col == sr["columns"][0]:
                dom[f"{sr['table']}.{col}"][m.group(1)].append({"cite": cite(sr["file"], sr["start"]), "how": "SEED", "unit": f"ddl:{sr['table']}"})
    # VB constants whose group comment names Table.Column
    cm = [x for x in f["comments"] if x["language"] in ("vb.net", "c#")]
    for k in clr(f)["constants"]:
        near = [x for x in cm if x["file"] == k["file"] and x["end"] < k["start"] and k["start"] - x["end"] <= 12]
        near = max(near, key=lambda x: x["end"], default=None)
        if near:
            between = (src_lines.get(k["file"]) or [])[near["end"]:k["start"] - 1]
            if not all(re.search(r"\bConst\b", l) for l in between):
                near = None
        if not near or not k.get("value"):
            continue
        m = re.search(r"\(?(\w+)\.(\w+)\)?", near["text"])
        if m and m.group(1).lower() in tables:
            dom[f"{tables[m.group(1).lower()]['name']}.{norm_id(m.group(2))}"][fmt_val(k["value"].strip('"')) if not k["value"].startswith('"') else k["value"].strip('"')].append(
                {"cite": cite(k["file"], k["start"]), "how": f"VB const {k['name']}", "unit": f"const:{k['name']}"})
    out = []
    for col, vals in dom.items():
        vals = {v: ws for v, ws in vals.items() if str(v).upper() != "NULL"}
        if len(vals) < 2 or col.startswith("?."):
            continue
        if not any(w["how"] in ("SET", "CHECK", "SEED") or w["how"].startswith("VB const") for ws in vals.values() for w in ws):
            continue
        out.append({"column": col, "values": {v: ws[:8] for v, ws in sorted(vals.items())}})
    merged = {}
    plain = {d["column"].lower() for d in out if "|" not in d["column"]}
    for d in out:
        k = d["column"].lower()
        if "|" in k:
            ts, col = k.rsplit(".", 1)
            k = next((f"{t}.{col}" for t in ts.split("|") if f"{t}.{col}" in plain), k)
        if k in merged:
            for v, ws in d["values"].items():
                merged[k]["values"].setdefault(v, []).extend(ws)
        else:
            merged[k] = d
    return sorted(merged.values(), key=lambda d: -len(d["values"]))


# ============================================================================ contracts
def contracts(f: dict, src: Path) -> dict:
    vb, sql, d = clr(f), f["sql"], f["derived"]
    pidx = {p["proc"].lower(): p for p in d["proc_index"]}
    procs = []
    for p in sql["procs"]:
        pi = pidx.get(p["name"].lower(), {})
        rc_vars = {x["variable"] for x in p.get("literal_assigns", []) if re.search(r"rcode|ret|result|status|err", x["variable"], re.I)
                   and re.fullmatch(r"-?\d+", str(x["value"]))}
        codes = defaultdict(list)
        msgs_by_line = {x["line"]: x["value"] for x in p.get("literal_assigns", []) if not re.fullmatch(r"-?\d+", str(x["value"]))}
        for x in p.get("literal_assigns", []):
            if x["variable"] in rc_vars:
                codes[str(x["value"])].append({"line": x["line"], "message": msgs_by_line.get(x["line"])})
        for r in p.get("returns", []):
            if re.fullmatch(r"-?\d+", r["value"]):
                codes[r["value"]].append({"line": r["line"], "message": None})
        procs.append({
            "name": p["name"], "kind": p.get("kind"), "cite": cite(p["file"], p["start"], p["end"]),
            "params": [{"name": x["name"], "type": x["type"], "default": x["default"], "output": x["output"], "line": x["line"]} for x in p["params"]],
            "return_codes": {k: v[:6] for k, v in sorted(codes.items(), key=lambda kv: num(kv[0]) or 0)},
            "raises": [{"line": r["line"], "message": r["message"]} for r in p.get("raises", [])],
            "result_sets": p.get("result_sets", []),
            "vb_callers": pi.get("vb_callers", []), "cs_callers": pi.get("cs_callers", []),
            "sql_callers": pi.get("sql_callers", []),
            "entry_point": not pi.get("vb_callers") and not pi.get("cs_callers")
                           and not pi.get("sql_callers"),
        })
    # VB/C# -> proc parameter binding check (AddWithValue("@x") / Parameters.Add near a proc ref)
    bindings = []
    refs_by_method = defaultdict(list)
    for r in vb["proc_refs"]:
        refs_by_method[r["from"]].append(r)
    params_by_method = defaultdict(list)
    for c in vb.get("column_refs", []):
        if c["how"] == "sql-param":
            params_by_method[c["from"]].append(c)
    for m, rs in refs_by_method.items():
        for r in rs:
            p = next((x for x in sql["procs"] if x["name"].lower() == r["proc"].lower()), None)
            if not p:
                continue
            sent = {c["name"].lower() for c in params_by_method.get(m, [])}
            declared = {x["name"].lstrip("@").lower(): x for x in p["params"]}
            missing = [x["name"] for k, x in declared.items() if k not in sent and not x["default"] and sent]
            extra = sorted(k for k in sent if k not in declared)
            bindings.append({"vb_method": m, "cite": cite(r["file"], r["line"]), "proc": p["name"],
                             "sent": sorted(sent), "declared": [x["name"] for x in p["params"]],
                             "missing_without_default": missing, "not_declared": extra})
    reports = []
    for s in sql["scripts"]:
        if s.get("crystal_params") or s.get("result_sets"):
            reports.append({"file": s["file"], "cite": cite(s["file"], s["start"], s["end"]),
                            "params": s.get("crystal_params", []), "result_sets": s.get("result_sets", []),
                            "reads": [x["table"] for x in s.get("reads", [])]})
    for x in f["files"]:
        if x["language"] != "other":
            continue
        text = (src / x["file"]).read_text(errors="replace")
        params = sorted(set(re.findall(r"\{\?(\w+)\}", text)))
        fields = sorted(set(re.findall(r"\{\w+\.(\w+)\}", text)))
        refs = sorted({y["file"] for y in f["files"] if y["file"] != x["file"] and
                       re.search(r"(?<![\w])" + re.escape(Path(y["file"]).name) + r"(?![\w])", text.replace("\\", "/"))})
        reports.append({"file": x["file"], "cite": cite(x["file"], 1, x["end"]), "params": params, "fields": fields,
                        "references": refs})
    forms = {t["name"] for t in vb["types"] if t.get("is_form")}
    methods_by_class = defaultdict(list)
    for m in vb["methods"]:
        methods_by_class[m["class"]].append(m)
    dto = [{"type": t["name"], "cite": cite(t["file"], t["start"], t["end"]),
            "members": [m["name"] for m in vb["members"] if m["owner"] == t["name"]][:30]}
           for t in vb["types"] if t["kind"] == "class" and not t.get("is_form") and
           all(m["kind"] in ("Property", "Constructor") for m in methods_by_class.get(t["name"], []))]
    # VB spells the modifier "Public", C# "public" — compare case-insensitively.
    public_api = [{"method": m["id"], "cite": cite(m["file"], m["start"], m["end"])}
                  for m in vb["methods"] if (m["access"] or "").lower() == "public"
                  and m["class"] not in forms and m["kind"] != "Constructor"]
    return {
        "procs": procs, "param_bindings": bindings, "reports": reports,
        "tables": [{"name": t["name"], "cite": cite(t["file"], t["start"], t["end"]),
                    "columns": [c["name"] for c in t["columns"]],
                    "checks": [{"line": c["line"], "text": c["text"]} for c in t["checks"]],
                    "keys": [k["text"][:120] for k in t["keys"]]} for t in sql["tables"]],
        "io_calls": vb.get("io_calls", []), "attributes": [a for a in vb.get("attributes", []) if not re.match(r"^(System\.)?(Diagnostics\.)?DebuggerStepThrough|Global\.Microsoft\.VisualBasic\.CompilerServices", a["name"])],
        "enums": vb.get("enums", []), "dto_candidates": dto, "public_api": public_api,
    }


# ============================================================================ db-only verification
def counterparts(comps: list[dict], c: dict, other_layers: set[str]) -> list[dict]:
    """Comparisons in other layers on the same subject that also share the value or the reference."""
    out = []
    for o in comps:
        if o["layer"] not in other_layers or o is c:
            continue
        if not set(o["subject"]) & set(c["subject"]) or set(o["subject"]) & set(o["ref"]):
            continue
        same_ref = bool(set(o["ref"]) & set(c["ref"]))
        same_val = o["value"] is not None and c["value"] is not None and \
            (o["value"] == c["value"] or set(str(o["value"]).split(",")) & set(str(c["value"]).split(",")))
        if same_ref or same_val:
            out.append(o)
    return out


def db_checks(f: dict, comps: list[dict]) -> list[dict]:
    """SQL comparisons in procs/triggers/CHECKs, each with client-side counterparts (empty = candidate db-only)."""
    out = []
    cols = {norm_id(c["name"]) for t in f["sql"]["tables"] for c in t["columns"]}
    for c in comps:
        if c["layer"] != "db" or not c["subject"]:
            continue
        if set(c["subject"]) & set(c["ref"]):          # key join / parameter match (APCo = @APCo)
            continue
        if not (set(c["subject"]) | set(c["ref"])) & cols:  # control-flow variables only (@rcode, @@ROWCOUNT)
            continue
        cps = counterparts(comps, c, {"client", "client-sql"})
        out.append({"cite": c["cite"], "unit": c["unit"], "text": c["text"][:140],
                    "client_counterparts": [{"cite": o["cite"], "text": o["text"][:120]} for o in cps[:5]]})
    return out


# ============================================================================ graph
def build_graph(f: dict, v1: dict) -> dict:
    # `vb` is the MERGED CLR view (VB.NET + C#); a unit's id prefix comes from its file, so a C#
    # method is `cs:Class.Method` and its type `cstype:Class`, next to the VB `vb:` / `vbtype:` ids.
    vb, sql, d = clr(f), f["sql"], f["derived"]
    nodes, edges = {}, []

    def node(i, kind, name, file=None, start=None, end=None, **kw):
        if i not in nodes:
            nodes[i] = {"id": i, "kind": kind, "name": name, "file": file, "start": start, "end": end, **kw}
        return i

    def edge(a, b, kind, file=None, line=None, **kw):
        edges.append({"src": a, "dst": b, "kind": kind, "file": file, "line": line, **kw})

    for x in f["files"]:
        node(f"file:{x['file']}", "file", x["file"], x["file"], 1, x["end"], language=x["language"])
    for u in v1["units"]:
        node(u["id"], u["kind"], u["name"], u["file"], u["start"], u["end"], layer=u["layer"],
             **({"handles": u["handles"]} if u.get("handles") else {}))
        edge(f"file:{u['file']}", u["id"], "contains", u["file"], u["start"])
    for t in vb["types"]:
        p_ = unit_prefix(t["file"])
        node(f"{p_}type:{t['name']}", f"{p_}-type", t["name"], t["file"], t["start"], t["end"],
             is_form=t.get("is_form"))
    for m in vb["methods"]:
        p_ = unit_prefix(m["file"])
        edge(f"{p_}type:{m['class']}", f"{p_}:{m['id']}", "member", m["file"], m["start"])
    tbl_names = {}
    for t in sql["tables"]:
        node(f"table:{t['name']}", "table", t["name"], t["file"], t["start"], t["end"])
        tbl_names[t["name"].lower()] = t["name"]
        edge(f"ddl:{t['name']}", f"table:{t['name']}", "defines", t["file"], t["start"])
        for c in t["columns"]:
            cid = node(f"col:{t['name']}.{c['name']}", "column", f"{t['name']}.{c['name']}", t["file"], c["line"], c["line"])
            edge(f"table:{t['name']}", cid, "has_column", t["file"], c["line"])

    def tnode(name):
        canon = tbl_names.get(name.lower(), name)
        return node(f"table:{canon}", "table", canon)

    def cnode(table, col):
        canon = tbl_names.get((table or "").lower(), table)
        return node(f"col:{canon}.{col}", "column", f"{canon}.{col}")

    for k in vb["constants"]:
        node(f"const:{k['name']}", "constant", k["name"], k["file"], k["start"], k["end"], value=k["value"])
    for c in vb["calls"]:
        edge(clr_uid(c), f"{unit_prefix(c.get('callee_file') or c['file'])}:{c['to']}",
             "calls", c["file"], c["line"])
    for r in vb["proc_refs"]:
        dst = f"sql:{r['proc']}" if r["defined_in_corpus"] else node(f"sql:{r['proc']}", "missing-proc", r["proc"])
        if r["defined_in_corpus"]:
            p = next((p for p in sql["procs"] if p["name"].lower() == r["proc"].lower()), None)
            dst = f"sql:{p['name']}" if p else dst
        # a proc name in a field/const initialiser has no enclosing method: attribute the edge to
        # the FILE rather than inventing a `vb:None` / `cs:None` unit.
        edge(clr_uid(r) if r.get("from") else f"file:{r['file']}", dst, "exec", r["file"], r["line"])
    for uid, u, layer in sql_units(f):
        for x in u.get("exec_calls", []):
            p = next((p for p in sql["procs"] if p["name"].lower() == x["proc"].lower()), None)
            dst = f"sql:{p['name']}" if p else node(f"sql:{x['proc']}", "missing-proc", x["proc"])
            edge(uid, dst, "exec", u["file"], x["line"])
        for x in u.get("reads", []):
            edge(uid, tnode(x["table"]), "reads", u["file"], x["lines"][0])
        for x in u.get("writes", []):
            edge(uid, tnode(x["table"]), "writes", u["file"], x["lines"][0], op=x["op"])
        for c in u.get("columns", []):
            if not c.get("table") or c["table"].lower() in ("inserted", "deleted") or c["table"].startswith("#"):
                continue
            edge(uid, cnode(c["table"], c["column"]), "writes_col" if c["mode"] == "write" else "reads_col",
                 u["file"], c["lines"][0], **({"ambiguous": c["ambiguous"]} if c.get("ambiguous") else {}))
    for t in sql["triggers"]:
        edge(tnode(t["table"]), f"trg:{t['name']}", "fires", t["file"], t["start"], events=t["events"])
    for u in vb["const_uses"]:
        edge(clr_uid(u), f"const:{u['const']}", "uses_const", u["file"], u["line"])
    col_table = defaultdict(list)
    for t in sql["tables"]:
        for c in t["columns"]:
            col_table[c["name"].lower()].append(t["name"])
    for c in vb.get("column_refs", []):
        if c["how"] == "sql-param" or not c["from"]:
            continue
        ts = col_table.get(c["name"].lower(), [])
        for t in ts[:6]:
            edge(clr_uid(c), cnode(t, c["name"]), "writes_col" if c["how"] == "write" else "reads_col",
                 c["file"], c["line"], via="row-indexer", ambiguous=len(ts) > 1)
    for h in d["vb_event_handlers"]:
        for ev in h["handles"]:
            ctl = ev.split(".")[0]
            n = node(f"event:{h['method'].split('.')[0]}.{ev}", "ui-event", ev, h["file"], h["start"], h["start"])
            edge(n, f"vb:{h['method']}", "handled_by", h["file"], h["start"])
    for m in d["comment_mentions"]:
        target = {"proc": f"sql:{m['mentions']}", "trigger": f"trg:{m['mentions']}", "table": f"table:{m['mentions']}",
                  "vb-const": f"const:{m['mentions']}", "vb-type": f"vbtype:{m['mentions']}",
                  "cs-const": f"const:{m['mentions']}", "cs-type": f"cstype:{m['mentions']}"}.get(m["kind"])
        if m["kind"] in ("vb-method", "cs-method"):
            target = next((f"{unit_prefix(x['file'])}:{x['id']}" for x in vb["methods"]
                           if x["name"] == m["mentions"] and x["file"] == m["defined_in"]), None)
        if m["kind"] == "column":
            target = f"col:{m['mentions']}"
        if target:
            edge(f"file:{m['file']}", target, "comment_mentions", m["file"], m["start"])
    for r in v1["contracts"]["reports"]:
        for ref in r.get("references", []):
            edge(f"doc:{r['file']}" if f"doc:{r['file']}" in nodes else f"file:{r['file']}", f"file:{ref}", "report_source", r["file"], 1)
    # concept nodes: identifiers compared in more than one unit
    used = defaultdict(set)
    for c in v1["comparisons"]:
        for i in set(c["subject"]) | set(c["ref"]):
            used[i].add(c["unit"])
    for c in v1["comparisons"]:
        for i in set(c["subject"]) | set(c["ref"]):
            if len(used[i]) >= 2:
                node(f"concept:{i}", "concept", i)
                edge(c["unit"], f"concept:{i}", "compares", c["file"], c["line"], op=c["op"], value=c["value"])
    for x in d["cross_layer_name_matches"]:
        for m in x["sql_matches"]:
            if m["kind"] == "column":
                t, col = m["name"].split(".", 1)
                edge(f"const:{x['vb_const']}", cnode(t, col), "mirrors", x["file"], x["start"])
            else:
                proc = m["name"].split(".")[0]
                edge(f"const:{x['vb_const']}", f"sql:{proc}", "mirrors", x["file"], x["start"], param=m["name"])
    # drop edges to unknown endpoints
    for e in edges:
        for k in ("src", "dst"):
            if e[k] not in nodes:
                kind = e[k].split(":")[0]
                nodes[e[k]] = {"id": e[k], "kind": kind, "name": e[k].split(":", 1)[1], "file": None, "start": None, "end": None}
    seen, uniq = set(), []
    for e in edges:
        k = (e["src"], e["dst"], e["kind"], e["file"], e["line"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return {"schema": "lean-graph/v1", "nodes": sorted(nodes.values(), key=lambda n: n["id"]), "edges": uniq,
            "edge_kinds": {
                "calls": "VB method -> VB method", "exec": "caller -> stored proc", "reads/writes": "unit -> table",
                "reads_col/writes_col": "unit -> column", "fires": "table -> trigger (on write)",
                "uses_const": "VB method -> constant", "compares": "unit -> concept (identifier compared)",
                "mirrors": "VB constant -> SQL column/param with the same name", "contains": "file -> unit",
                "member": "VB type -> method", "has_column": "table -> column", "defines": "DDL -> table",
                "handled_by": "UI event -> handler", "comment_mentions": "file (comment) -> symbol",
                "report_source": "report definition -> command SQL file"}}


# ============================================================================ entry point
def enrich(facts: dict, src: Path) -> dict:
    resolve_columns(facts)
    v1 = {"units": build_units(facts)}
    comps = build_comparisons(facts)
    v1["comparisons"] = comps
    inc, con = cross_layer_candidates(comps)
    v1["cross_layer_candidates"] = inc
    v1["duplicate_candidates"] = duplicate_candidates(facts, src, con)
    v1["status_domains"] = status_domains(facts, comps, src)
    v1["contracts"] = contracts(facts, src)
    v1["db_checks"] = db_checks(facts, comps)
    idx = concept_index(comps)
    v1["concept_index"] = {k: [comps[i]["cite"] for i in v] for k, v in sorted(idx.items()) if len({comps[i]["unit"] for i in v}) >= 2}
    facts["v1"] = v1
    return facts


def main():
    import argparse
    ap = argparse.ArgumentParser(description="layer 1b: enrich facts.json and write graph.json")
    ap.add_argument("--run", required=True, type=Path, help="run dir with facts.json and source/")
    a = ap.parse_args()
    facts = json.loads((a.run / "facts.json").read_text())
    facts = enrich(facts, a.run / "source")
    (a.run / "facts.json").write_text(json.dumps(facts, indent=1))
    g = build_graph(facts, facts["v1"])
    (a.run / "graph.json").write_text(json.dumps(g, indent=1))
    v = facts["v1"]
    print(json.dumps({"cross_layer_candidates": len(v["cross_layer_candidates"]),
                      "duplicate_candidates": len(v["duplicate_candidates"]),
                      "status_domains": len(v["status_domains"]), "graph_nodes": len(g["nodes"]),
                      "graph_edges": len(g["edges"])}))


if __name__ == "__main__":
    main()
