# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deterministic, syntax-preserving defect injectors for a legacy corpus.

Each operator has two halves:

``sites(corpus)``
    Enumerate every applicable injection site. Site discovery is pattern-driven
    (no hard-coded line numbers), so the operators work on any VB.NET + T-SQL
    corpus, and the list is returned in a canonical order so that a given
    corpus always yields the same sites.

``build(site, corpus)``
    Produce a :class:`Mutation`: the line edits to apply, the ground-truth
    record (statement, locations, flags) and the structural profile delta the
    edit is allowed to cause. ``mutate.py`` rejects any mutation whose actual
    delta differs, which is what makes "syntax-preserving" an enforced property
    rather than a claim.

No LLM is involved anywhere in this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sanity import join_vb_continuations, lang_of, scan

# Categories and the detection flag each one plants. Flag names match the
# `flags:` vocabulary of the answer keys so existing scorers can consume them.
CATEGORIES = {
    "threshold_divergence": "inconsistency",
    "operator_relax": "logic_flaw",
    "client_check_removed": "db_only",
    "dead_code_orphan": "dead_code",
    "rule_to_comment": "comment_only",
    "silent_cap": "undocumented_feature",
    "date_off_by_one": "logic_flaw",
    "constant_alias_rename": "inconsistency",
}

UPPER_ID = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


# --------------------------------------------------------------------------- model
@dataclass
class Edit:
    """Replace lines [start, end] (1-based, inclusive) of ``file`` with ``new_lines``."""
    file: str
    start: int
    end: int
    new_lines: list[str]
    # Optional 0-based [a, b] slice of ``new_lines`` that the ground truth should cite,
    # for edits that rewrite a block but only part of it is the injected defect.
    loc_slice: tuple[int, int] | None = None

    @property
    def touched(self) -> set[int]:
        return set(range(self.start, self.end + 1))


@dataclass
class Site:
    category: str
    file: str
    line: int
    key: str                     # stable, human-readable site id
    payload: dict = field(default_factory=dict)

    def sort_key(self):
        return (self.category, self.file, self.line, self.key)


@dataclass
class Mutation:
    category: str
    site: Site
    edits: list[Edit]
    statement: str
    item_type: str = "rule"
    flags: list[str] = field(default_factory=list)
    evidence_class: str = "Verified"
    expected_delta: dict[str, dict[str, int]] = field(default_factory=dict)
    extra_locations: list[dict] = field(default_factory=list)
    before: str = ""
    after: str = ""
    detect_terms: list[str] = field(default_factory=list)


class Corpus:
    """Read-only view of a corpus tree: raw lines, comment-stripped code lines."""

    def __init__(self, root: Path, exts: tuple[str, ...] = (".vb", ".sql")):
        self.root = root
        self.files: dict[str, list[str]] = {}
        self.code: dict[str, list[str]] = {}
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            rel = p.relative_to(root).as_posix()
            text = p.read_text(errors="replace")
            self.files[rel] = text.split("\n")
            self.code[rel], _ = scan(text, lang_of(p))

    def vb(self) -> list[str]:
        return [f for f in self.files if f.lower().endswith(".vb")]

    def sql(self) -> list[str]:
        return [f for f in self.files if f.lower().endswith(".sql")]

    def logical(self, rel: str) -> list[str]:
        return join_vb_continuations(self.code[rel])

    def name_hits(self, name: str) -> list[tuple[str, int]]:
        """Every code-line occurrence of ``name`` as a whole word."""
        pat = re.compile(r"(?<![\w.])" + re.escape(name) + r"(?![\w])")
        return [(rel, i) for rel, lines in self.code.items()
                for i, l in enumerate(lines, 1) if pat.search(l)]


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _num_text(value: float, like: str) -> str:
    """Render ``value`` in the same textual style as the literal ``like``."""
    suffix = ""
    m = re.match(r"^([\d.]+)([DdRrFfSsLl!#@]?)$", like)
    if m:
        suffix = m.group(2)
    if float(value).is_integer() and "." not in like:
        return f"{int(value)}{suffix}"
    return f"{value:.2f}{suffix}"


# --------------------------------------------------------------------------- 1. threshold divergence
NUM_RE = re.compile(r"(?<![\w.$])(\d{3,}(?:\.\d+)?)([DdRrFfSsLl]?)(?![\w.])")


def _numeric_occurrences(c: Corpus) -> dict[str, list[tuple[str, int, int, str]]]:
    """value -> [(file, line, col, literal_text)] for integer-ish literals >= 100."""
    by_val: dict[str, list[tuple[str, int, int, str]]] = {}
    for rel, lines in c.code.items():
        for i, l in enumerate(lines, 1):
            for m in NUM_RE.finditer(l):
                try:
                    v = float(m.group(1))
                except ValueError:
                    continue
                # keep round, money-sized values: thresholds and limits, not divisors
                # (100 in `x / 100D`), column widths (255) or error codes (112).
                if v < 500 or v > 10 ** 9 or v % 50 != 0:
                    continue
                by_val.setdefault(f"{v:g}", []).append((rel, i, m.start(1), m.group(0)))
    return by_val


def sites_threshold_divergence(c: Corpus) -> list[Site]:
    out = []
    for val, occ in sorted(_numeric_occurrences(c).items(), key=lambda kv: float(kv[0])):
        files = sorted({o[0] for o in occ})
        if len(files) < 2:
            continue
        # mutate the occurrence in the client (VB) layer when there is one, so the
        # divergence is cross-layer; otherwise the first file alphabetically.
        vb = [o for o in occ if o[0].lower().endswith(".vb")]
        target = (vb or occ)[0]
        rel, line, col, lit = target
        out.append(Site("threshold_divergence", rel, line,
                        key=f"num:{val}@{rel}:{line}:{col}",
                        payload={"value": val, "col": col, "literal": lit,
                                 "others": [{"file": o[0], "start_line": o[1], "end_line": o[1]}
                                            for o in occ if o is not target]}))
    return out


def build_threshold_divergence(s: Site, c: Corpus) -> Mutation:
    raw = c.files[s.file][s.line - 1]
    lit = s.payload["literal"]
    old_v = float(s.payload["value"])
    new_v = old_v * 1.5
    new_lit = _num_text(new_v, lit)
    col = s.payload["col"]
    # replace exactly the one occurrence, located by column
    end = col + len(lit)
    new_raw = raw[:col] + new_lit + raw[end:]
    others = s.payload["others"]
    other_txt = ", ".join(f"{o['file']}:{o['start_line']}" for o in others[:4])
    stmt = (f"The threshold value {_num_text(old_v, lit)} was changed to {new_lit} at "
            f"{s.file}:{s.line} only. The same value is still used at {other_txt}, so the "
            f"layers now disagree about the same business threshold. A correct specification "
            f"must report the conflict instead of harmonising it to one value.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["inconsistency"], evidence_class="Confirmation Required",
        edits=[Edit(s.file, s.line, s.line, [new_raw])],
        extra_locations=others, before=raw.strip(), after=new_raw.strip(),
        detect_terms=["inconsisten", "conflict", "contradict", "disagree", "mismatch", "diverg",
                      "does not match", "differs from", "two different value", "out of sync",
                      "no longer match", "not in sync"])


# --------------------------------------------------------------------------- 2. operator relax
CMP_CTX = re.compile(r"(?<![\w.])(?:If|ElseIf|Return|While|Until|AndAlso|OrElse|And|Or)\b", re.I)


def sites_operator_relax(c: Corpus) -> list[Site]:
    out = []
    for rel in c.vb():
        for i, l in enumerate(c.code[rel], 1):
            if not CMP_CTX.search(l):
                continue
            for m in re.finditer(r"(>=|<=)", l):
                # only comparisons against something numeric / symbolic, not <> or >>
                rhs = l[m.end():m.end() + 40]
                if not re.match(r"\s*(?:[\w.(\"]|-\d)", rhs):
                    continue
                out.append(Site("operator_relax", rel, i,
                                key=f"op:{m.group(1)}@{rel}:{i}:{m.start()}",
                                payload={"col": m.start(), "op": m.group(1)}))
    return out


def build_operator_relax(s: Site, c: Corpus) -> Mutation:
    raw = c.files[s.file][s.line - 1]
    op, col = s.payload["op"], s.payload["col"]
    new_op = ">" if op == ">=" else "<"
    new_raw = raw[:col] + new_op + raw[col + 2:]
    stmt = (f"{s.file}:{s.line} now uses `{new_op}` where it used `{op}`. The boundary value "
            f"itself is no longer included, so a record exactly on the boundary takes the other "
            f"branch than it did before, and than the other layer still does. A correct "
            f"specification must state which comparison each layer uses.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["logic_flaw", "inconsistency"], evidence_class="Confirmation Required",
        edits=[Edit(s.file, s.line, s.line, [new_raw])],
        before=raw.strip(), after=new_raw.strip(),
        detect_terms=["off-by-one", "off by one", "boundary mismatch", "inclusive", "exclusive",
                      "strictly greater", "strictly less", "excludes the boundary",
                      "inconsisten", "does not match", "differs from", "disagree"])


# --------------------------------------------------------------------------- guards (3 and 5)
GUARD_RE = re.compile(
    r"^(?P<ind>\s*)If\s+(?P<cond>.+?)\s+Then\s+(?P<act>Return\b.*|Exit\s+(?:Sub|Function)\b.*)$", re.I)


def _guard_sites(c: Corpus, category: str) -> list[Site]:
    out = []
    for rel in c.vb():
        code = c.code[rel]
        for i, l in enumerate(code, 1):
            if l.rstrip().endswith("_"):
                continue                      # continued line: skip, keep edits single-line
            m = GUARD_RE.match(l)
            if not m:
                continue
            cond = m.group("cond")
            if not re.search(r"(<=|>=|<>|<|>|=)", cond):
                continue                      # not a comparison: likely a UI/state flag
            if not (re.search(r"\d", cond) or UPPER_ID.search(cond)):
                continue                      # not a business threshold / coded value
            out.append(Site(category, rel, i, key=f"guard:{category}@{rel}:{i}",
                            payload={"cond": cond.strip(), "act": m.group("act").strip(),
                                     "indent": m.group("ind")}))
    return out


def sites_client_check_removed(c: Corpus) -> list[Site]:
    return _guard_sites(c, "client_check_removed")


def build_client_check_removed(s: Site, c: Corpus) -> Mutation:
    raw = c.files[s.file][s.line - 1]
    cond = s.payload["cond"]
    stmt = (f"The client-side validation `If {cond}` was deleted from {s.file} (it was at line "
            f"{s.line} of the original). Nothing in the client layer enforces it any more; if the "
            f"rule survives at all it survives only in the database layer. A correct specification "
            f"must say the rule is database-only and that the UI will let the case through.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["db_only"], evidence_class="Inferred",
        edits=[Edit(s.file, s.line, s.line, [])],
        before=raw.strip(), after="(line deleted)",
        detect_terms=["only in the database", "database only", "db-only", "db only",
                      "not enforced in the ui", "not validated", "stored procedure only",
                      "server-side only", "no client", "missing validation", "unenforced",
                      "only the database enforces", "silently allowed"])


def sites_rule_to_comment(c: Corpus) -> list[Site]:
    return _guard_sites(c, "rule_to_comment")


def build_rule_to_comment(s: Site, c: Corpus) -> Mutation:
    raw = c.files[s.file][s.line - 1]
    ind = s.payload["indent"]
    cond, act = s.payload["cond"], s.payload["act"]
    new_lines = [
        f"{ind}' Business rule, documentation only - not enforced anywhere in code:",
        f"{ind}'   If {cond} Then {act}",
    ]
    stmt = (f"The rule `If {cond} Then {act}` exists in {s.file} only as a comment; the executable "
            f"statement was removed. Nothing enforces it in code or in the database. A correct "
            f"specification must mark it as prose-only and needing business confirmation.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["comment_only"], evidence_class="Confirmation Required",
        edits=[Edit(s.file, s.line, s.line, new_lines)],
        before=raw.strip(), after=new_lines[1].strip(),
        detect_terms=["comment only", "comment-only", "only in a comment", "not enforced",
                      "documentation only", "no code enforces", "manual convention",
                      "prose only", "stated only in a comment"])


# --------------------------------------------------------------------------- 4. dead code orphan
FUNC_DECL = re.compile(
    r"^(?P<ind>\s*)(?P<mods>(?:Public|Private|Protected|Friend|Shared|Overloads|Overrides)"
    r"(?:\s+(?:Public|Private|Protected|Friend|Shared|Overloads|Overrides))*)\s+"
    r"Function\s+(?P<name>\w+)\s*\((?P<args>[^)]*)\)(?P<tail>.*)$", re.I)


def _functions(c: Corpus, rel: str) -> list[dict]:
    """Balanced Public/Private Function blocks in one VB file."""
    code, out = c.code[rel], []
    for i, l in enumerate(code, 1):
        m = FUNC_DECL.match(l)
        if not m:
            continue
        end = None
        for j in range(i + 1, min(len(code), i + 60) + 1):
            if re.match(r"^\s*End\s+Function\s*$", code[j - 1], re.I):
                end = j
                break
            if FUNC_DECL.match(code[j - 1]):
                break
        if end:
            out.append({"start": i, "end": end, "name": m.group("name"), "indent": m.group("ind")})
    return out


def sites_dead_code_orphan(c: Corpus) -> list[Site]:
    out = []
    existing = {n for rel in c.vb() for n in (f["name"] for f in _functions(c, rel))}
    for rel in c.vb():
        for f in _functions(c, rel):
            if f["end"] - f["start"] < 3 or f["end"] - f["start"] > 30:
                continue
            clone = f["name"] + "Legacy"
            if clone in existing:
                continue
            out.append(Site("dead_code_orphan", rel, f["start"],
                            key=f"func:{f['name']}@{rel}:{f['start']}",
                            payload={"name": f["name"], "clone": clone,
                                     "start": f["start"], "end": f["end"],
                                     "indent": f["indent"]}))
    return out


def build_dead_code_orphan(s: Site, c: Corpus) -> Mutation:
    lines = c.files[s.file]
    body = lines[s.payload["start"] - 1: s.payload["end"]]
    name, clone = s.payload["name"], s.payload["clone"]
    pat = re.compile(r"(?<![\w.])" + re.escape(name) + r"(?![\w])")
    copy = [pat.sub(clone, b) for b in body]
    ind = s.payload["indent"]
    new_block = lines[s.payload["start"] - 1: s.payload["end"]] + ["", f"{ind}' ---"] + copy
    stmt = (f"`{clone}` in {s.file} is never called from anywhere in the corpus: it is dead code. "
            f"It is a verbatim copy of `{name}`, so the two will drift apart the first time the "
            f"live one is changed. A correct specification must report it as unreachable / "
            f"duplicated logic rather than describe it as a second business rule.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["dead_code", "duplicated_logic"], evidence_class="Inferred",
        edits=[Edit(s.file, s.payload["start"], s.payload["end"], new_block,
                    loc_slice=(len(body) + 2, len(new_block) - 1))],
        expected_delta={s.file: {"vb_open_function": 1, "vb_close_function": 1}},
        before=f"(no {clone})", after=f"copy of {name} named {clone}",
        detect_terms=["dead code", "unused", "no caller", "never called", "unreachable",
                      "orphan", "duplicate of", "duplicated logic", "redundant copy",
                      "not referenced", "identical cop"])


# --------------------------------------------------------------------------- 6. silent cap
RET_RE = re.compile(r"^(?P<ind>\s*)Return\s+(?P<expr>.+?)\s*$", re.I)


def sites_silent_cap(c: Corpus) -> list[Site]:
    out = []
    for rel in c.vb():
        code = c.code[rel]
        for f in _functions(c, rel):
            decl = code[f["start"] - 1]
            if not re.search(r"\bAs\s+Decimal\s*$", decl, re.I):
                continue
            for i in range(f["start"] + 1, f["end"]):
                m = RET_RE.match(code[i - 1])
                if not m:
                    continue
                expr = m.group("expr")
                if not re.search(r"[*/+]", expr) or expr.rstrip().endswith("_"):
                    continue
                out.append(Site("silent_cap", rel, i, key=f"ret@{rel}:{i}",
                                payload={"func": f["name"], "expr": expr,
                                         "indent": m.group("ind")}))
    return out


def build_silent_cap(s: Site, c: Corpus) -> Mutation:
    raw = c.files[s.file][s.line - 1]
    expr, ind, func = s.payload["expr"], s.payload["indent"], s.payload["func"]
    cap = "5000D"
    new_raw = f"{ind}Return Math.Min({expr}, {cap})"
    stmt = (f"`{func}` in {s.file} silently caps its result at {cap.rstrip('D')}. Nothing in the "
            f"code, the comments, the screens or the database records that a cap exists, and no "
            f"message is shown when it bites. A correct specification must surface the cap as an "
            f"undocumented limit.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["undocumented_feature"], evidence_class="Verified",
        edits=[Edit(s.file, s.line, s.line, [new_raw])],
        before=raw.strip(), after=new_raw.strip(),
        detect_terms=["undocumented", "silently", "silent cap", "math.min", "clamp",
                      "hard cap", "capped at", "caps the", "upper bound of", "not documented",
                      "no message", "without warning"])


# --------------------------------------------------------------------------- 7. date off-by-one
DATE_WORD = re.compile(r"(?:date|dte|dt|day|month|mth|period|asof|due|aging)", re.I)


def sites_date_off_by_one(c: Corpus) -> list[Site]:
    out = []
    for rel in c.sql():
        for i, l in enumerate(c.code[rel], 1):
            for m in re.finditer(r"(>=|<=)", l):
                window = l[max(0, m.start() - 70): m.end() + 70]
                if not DATE_WORD.search(window):
                    continue
                out.append(Site("date_off_by_one", rel, i,
                                key=f"date:{m.group(1)}@{rel}:{i}:{m.start()}",
                                payload={"col": m.start(), "op": m.group(1)}))
    return out


def build_date_off_by_one(s: Site, c: Corpus) -> Mutation:
    raw = c.files[s.file][s.line - 1]
    op, col = s.payload["op"], s.payload["col"]
    new_op = ">" if op == ">=" else "<"
    new_raw = raw[:col] + new_op + raw[col + 2:]
    stmt = (f"The date boundary at {s.file}:{s.line} is off by one: `{op}` became `{new_op}`, so "
            f"rows falling exactly on the boundary date are now excluded. Reports and selections "
            f"built on this predicate silently lose one day of data. A correct specification must "
            f"state the boundary and that it is exclusive.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["logic_flaw"], evidence_class="Verified",
        edits=[Edit(s.file, s.line, s.line, [new_raw])],
        before=raw.strip(), after=new_raw.strip(),
        detect_terms=["off-by-one", "off by one", "excludes the boundary", "exclusive",
                      "exactly on", "one day", "strictly less", "boundary mismatch",
                      "loses a day", "misses rows"])


# --------------------------------------------------------------------------- 8. constant alias
CONST_RE = re.compile(
    r"^(?P<ind>\s*)(?P<mods>Public|Private|Friend|Protected)\s+Const\s+(?P<name>\w+)\s+"
    r"As\s+(?P<type>[\w.]+)\s*=\s*(?P<value>[^']+?)\s*$", re.I)


def sites_constant_alias_rename(c: Corpus) -> list[Site]:
    out = []
    for rel in c.vb():
        for i, l in enumerate(c.code[rel], 1):
            m = CONST_RE.match(l)
            if not m:
                continue
            name = m.group("name")
            hits = [h for h in c.name_hits(name) if not (h[0] == rel and h[1] == i)]
            if not hits:
                continue                       # unused constant: nothing to re-point
            alias = name + "_ALT"
            if c.name_hits(alias):
                continue
            use = hits[0]
            out.append(Site("constant_alias_rename", rel, i,
                            key=f"const:{name}@{rel}:{i}",
                            payload={"name": name, "alias": alias, "type": m.group("type"),
                                     "value": m.group("value").strip(), "mods": m.group("mods"),
                                     "indent": m.group("ind"),
                                     "use_file": use[0], "use_line": use[1],
                                     "n_uses": len(hits)}))
    return out


def build_constant_alias_rename(s: Site, c: Corpus) -> Mutation:
    p = s.payload
    decl_raw = c.files[s.file][s.line - 1]
    alias_decl = f"{p['indent']}{p['mods']} Const {p['alias']} As {p['type']} = {p['value']}"
    edits = [Edit(s.file, s.line, s.line, [decl_raw, alias_decl])]
    use_raw = c.files[p["use_file"]][p["use_line"] - 1]
    new_use = re.sub(r"(?<![\w.])" + re.escape(p["name"]) + r"(?![\w])", p["alias"], use_raw, count=1)
    if p["use_file"] == s.file and p["use_line"] == s.line:
        raise ValueError("alias use site collides with the declaration")
    edits.append(Edit(p["use_file"], p["use_line"], p["use_line"], [new_use]))
    stmt = (f"`{p['alias']}` was added to {s.file} with the same value ({p['value']}) as "
            f"`{p['name']}`, and {p['use_file']}:{p['use_line']} was switched to the new name while "
            f"the remaining {p['n_uses'] - 1} use site(s) still use `{p['name']}`. One business "
            f"concept now has two names and two declarations that must be kept in step. A correct "
            f"specification must report the duplicate definition, not two separate rules.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["inconsistency", "duplicated_logic"], evidence_class="Verified",
        edits=edits,
        extra_locations=[{"file": p["use_file"], "start_line": p["use_line"],
                          "end_line": p["use_line"]}],
        before=f"{p['name']} = {p['value']} (single declaration)",
        after=f"{p['name']} and {p['alias']} both = {p['value']}",
        detect_terms=["duplicate declaration", "duplicated", "two names", "alias",
                      "same value", "redundant", "inconsisten", "kept in step", "drift",
                      "synonym", "two constants"])


# --------------------------------------------------------------------------- registry
OPERATORS = {
    "threshold_divergence": (sites_threshold_divergence, build_threshold_divergence),
    "operator_relax": (sites_operator_relax, build_operator_relax),
    "client_check_removed": (sites_client_check_removed, build_client_check_removed),
    "dead_code_orphan": (sites_dead_code_orphan, build_dead_code_orphan),
    "rule_to_comment": (sites_rule_to_comment, build_rule_to_comment),
    "silent_cap": (sites_silent_cap, build_silent_cap),
    "date_off_by_one": (sites_date_off_by_one, build_date_off_by_one),
    "constant_alias_rename": (sites_constant_alias_rename, build_constant_alias_rename),
}


def all_sites(c: Corpus, categories: list[str] | None = None) -> list[Site]:
    """Every applicable site, in a canonical order."""
    cats = categories or sorted(OPERATORS)
    out: list[Site] = []
    for cat in cats:
        if cat not in OPERATORS:
            raise KeyError(f"unknown mutation category: {cat}")
        out += OPERATORS[cat][0](c)
    return sorted(out, key=lambda s: s.sort_key())
