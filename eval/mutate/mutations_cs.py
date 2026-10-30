# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""C#-aware defect injectors. Same contract and category names as ``mutations.py``.

The eight categories mean the same thing for C# as for VB.NET + T-SQL, so detection rates
are comparable across corpora; only site discovery and the edit itself are C#-specific:

==========================  ==================================================================
``threshold_divergence``    a numeric threshold that appears in 2+ files (``HasMaxLength(256)``,
                            ``const int ImageMaximumBytes = 512000``) is changed in one file only
``operator_relax``          ``>=`` / ``<=`` in a decision becomes ``>`` / ``<``
``client_check_removed``    a guard clause (``Guard.Against.X(...)`` or ``if (..) throw ..``)
                            is deleted where the same member is still guarded elsewhere (another
                            guard, a DataAnnotation, or EF Core config), so the rule is now
                            enforced *only* there
``dead_code_orphan``        an uncalled copy of a method (``<Name>Legacy``) is added
``rule_to_comment``         a guard clause is replaced by a ``//`` comment stating it
``silent_cap``              a numeric ``return`` / ``=>`` result is wrapped in ``Math.Min(.., 5000)``
``date_off_by_one``         a date or collection boundary moves by one: ``>=`` -> ``>`` next to a
                            date word, ``< x.Count`` -> ``<= x.Count``, or ``.Take(n)`` ->
                            ``.Take(n + 1)``
``constant_alias_rename``   ``NAME_ALT`` is declared next to ``const NAME`` with the same value and
                            one use site is switched to it
==========================  ==================================================================

Sites come from the comment- and string-blanked text (``cs_scan.scan_cs``), never from fixed
line numbers. Test projects, EF migrations, ``obj/``/``bin/`` and generated files are never
mutated: a defect injected there is not a business-logic defect.
"""
from __future__ import annotations

import re
from pathlib import Path

from cs_scan import is_method_head
from mutations import CATEGORIES, Corpus, Edit, Mutation, Site

SKIP_DIR = re.compile(r"(^|/)(tests?|[^/]*\.?tests?|migrations|obj|bin|wwwroot|node_modules)(/|$)",
                      re.I)
SKIP_FILE = re.compile(r"(\.designer\.cs|\.g\.cs|\.g\.i\.cs|AssemblyInfo\.cs|GlobalUsings\.cs)$", re.I)


class CsCorpus(Corpus):
    """Read-only view of the C# files of a tree that may be mutated."""

    def __init__(self, root: Path):
        super().__init__(root, exts=(".cs",))
        for rel in [r for r in self.files if SKIP_DIR.search(r) or SKIP_FILE.search(r)]:
            del self.files[rel]
            del self.code[rel]

    def cs(self) -> list[str]:
        return sorted(self.files)

    def word_hits(self, name: str) -> list[tuple[str, int]]:
        """Code-line occurrences of ``name`` as a whole identifier (qualified uses included)."""
        pat = re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])")
        return [(rel, i) for rel, lines in sorted(self.code.items())
                for i, l in enumerate(lines, 1) if pat.search(l)]


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


# --------------------------------------------------------------------------- method blocks
def methods(c: CsCorpus, rel: str) -> list[dict]:
    """Block-bodied methods of one file: {start, end, name, indent, head}. Brace-matched on the
    blanked text, so braces in strings and comments are ignored."""
    code, out = c.code[rel], []
    i = 0
    while i < len(code):
        line = code[i]
        if not is_method_head(line):
            i += 1
            continue
        name = re.search(r"([A-Za-z_]\w*)\s*(?:<[^()]*>)?\s*\(", line).group(1)
        # find the body's opening brace before any ';' or '=>'
        j, opened, depth, end = i, False, 0, None
        while j < len(code) and j < i + 80:
            seg = code[j]
            for ch_i, ch in enumerate(seg):
                if not opened:
                    if ch == ";" or seg[ch_i:ch_i + 2] == "=>":
                        j = len(code)          # abstract / expression-bodied: not a block method
                        break
                    if ch == "{":
                        opened, depth = True, 1
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            if end is not None or j >= len(code):
                break
            j += 1
        if end is not None:
            out.append({"start": i + 1, "end": end + 1, "name": name, "indent": _indent(line),
                        "head": line.strip()})
            i = end + 1
        else:
            i += 1
    return out


# --------------------------------------------------------------------------- 1. threshold divergence
CS_NUM = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)([mMdDfFuUlL]{0,2})(?![\w.])")
THRESHOLD_CTX = re.compile(
    r"HasMaxLength|MaxLength|MinLength|StringLength|MinimumLength|Range\s*\(|HasPrecision|"
    r"\bconst\b|\breadonly\b|[<>]=?|==|!=|\.Take\s*\(|PageSize|PerPage|Limit|Maximum|Minimum|"
    r"OutOfRange|Retry|retry", re.I)


def _cs_numeric_occurrences(c: CsCorpus) -> dict[str, list[tuple[str, int, int, str]]]:
    by_val: dict[str, list[tuple[str, int, int, str]]] = {}
    for rel in c.cs():
        for i, l in enumerate(c.code[rel], 1):
            if not THRESHOLD_CTX.search(l) or re.match(r"^\s*(using|namespace|#)", l):
                continue
            for m in CS_NUM.finditer(l):
                try:
                    v = float(m.group(1))
                except ValueError:
                    continue
                if v < 5 or v > 10 ** 9 or not v.is_integer():
                    continue
                by_val.setdefault(f"{v:g}", []).append((rel, i, m.start(1), m.group(0)))
    return by_val


def sites_threshold_divergence(c: CsCorpus) -> list[Site]:
    out = []
    for val, occ in sorted(_cs_numeric_occurrences(c).items(), key=lambda kv: float(kv[0])):
        files = sorted({o[0] for o in occ})
        if len(files) < 2:
            continue
        target = sorted(occ)[0]
        rel, line, col, lit = target
        out.append(Site("threshold_divergence", rel, line, key=f"num:{val}@{rel}:{line}:{col}",
                        payload={"value": val, "col": col, "literal": lit,
                                 "others": [{"file": o[0], "start_line": o[1], "end_line": o[1]}
                                            for o in sorted(occ) if o is not target]}))
    return out


def _cs_num_text(value: float, like: str) -> str:
    m = re.match(r"^([\d.]+)([A-Za-z]*)$", like)
    suffix = m.group(2) if m else ""
    if float(value).is_integer() and "." not in like:
        return f"{int(value)}{suffix}"
    return f"{value:.2f}{suffix}"


def build_threshold_divergence(s: Site, c: CsCorpus) -> Mutation:
    raw = c.files[s.file][s.line - 1]
    lit, col = s.payload["literal"], s.payload["col"]
    old_v = float(s.payload["value"])
    new_lit = _cs_num_text(round(old_v * 1.5), lit)
    new_raw = raw[:col] + new_lit + raw[col + len(lit):]
    others = s.payload["others"]
    other_txt = ", ".join(f"{o['file']}:{o['start_line']}" for o in others[:4])
    stmt = (f"The threshold value {lit} was changed to {new_lit} at {s.file}:{s.line} only. The "
            f"same value is still used at {other_txt}, so the two places now disagree about the "
            f"same limit. A correct specification must report the conflict instead of "
            f"harmonising it to one value.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["inconsistency"], evidence_class="Confirmation Required",
        edits=[Edit(s.file, s.line, s.line, [new_raw])],
        extra_locations=others, before=raw.strip(), after=new_raw.strip(),
        detect_terms=["inconsisten", "conflict", "contradict", "disagree", "mismatch", "diverg",
                      "does not match", "differs from", "two different value", "out of sync",
                      "no longer match", "not in sync"])


# --------------------------------------------------------------------------- 2. operator relax
CS_DECISION = re.compile(r"(?<![\w.])(?:if|while|return|for|when|case)\b|&&|\|\||\?\s|=>")


def _cmp_sites(c: CsCorpus, category: str, want_ctx) -> list[Site]:
    out = []
    for rel in c.cs():
        for i, l in enumerate(c.code[rel], 1):
            for m in re.finditer(r"(?<![<>=!])(>=|<=)(?!=)", l):
                if not want_ctx(l, m):
                    continue
                rhs = l[m.end():m.end() + 40]
                if not re.match(r"\s*(?:[\w.(@]|-\d)", rhs):
                    continue
                out.append(Site(category, rel, i, key=f"op:{m.group(1)}@{rel}:{i}:{m.start()}",
                                payload={"col": m.start(), "op": m.group(1)}))
    return out


DATE_WORD = re.compile(r"(?:date|time|day|month|year|expir|utc|period|due|aging|deadline)", re.I)


def sites_operator_relax(c: CsCorpus) -> list[Site]:
    def ctx(l, m):
        window = l[max(0, m.start() - 70): m.end() + 70]
        return bool(CS_DECISION.search(l)) and not DATE_WORD.search(window)
    return _cmp_sites(c, "operator_relax", ctx)


def build_operator_relax(s: Site, c: CsCorpus) -> Mutation:
    raw = c.files[s.file][s.line - 1]
    op, col = s.payload["op"], s.payload["col"]
    new_op = ">" if op == ">=" else "<"
    new_raw = raw[:col] + new_op + raw[col + 2:]
    stmt = (f"{s.file}:{s.line} now uses `{new_op}` where it used `{op}`. The boundary value "
            f"itself is no longer included, so a value exactly on the boundary takes the other "
            f"branch than it did before. A correct specification must state the exact comparison.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["logic_flaw"], evidence_class="Confirmation Required",
        edits=[Edit(s.file, s.line, s.line, [new_raw])],
        before=raw.strip(), after=new_raw.strip(),
        detect_terms=["off-by-one", "off by one", "boundary mismatch", "inclusive", "exclusive",
                      "strictly greater", "strictly less", "excludes the boundary",
                      "inconsisten", "does not match", "differs from", "disagree"])


# --------------------------------------------------------------------------- guards (3 and 5)
GUARD_LINE = re.compile(r"^(?P<ind>\s*)Guard\.Against\.(?P<kind>\w+)\s*\((?P<args>.*)\)\s*;\s*$")
IF_THROW_LINE = re.compile(r"^(?P<ind>\s*)if\s*\((?P<cond>.+)\)\s*throw\s+new\s+(?P<exc>[\w.]+)\s*\(.*\)\s*;\s*$")


def _guard_member(raw: str) -> str | None:
    m = re.search(r"nameof\(\s*([\w.?]+)\s*\)", raw)
    if m:
        return m.group(1).split(".")[-1].rstrip("?")
    m = GUARD_LINE.match(raw)
    if m:
        first = m.group("args").split(",")[0].strip()
        return re.split(r"[.?]", first)[-1] or None
    m = IF_THROW_LINE.match(raw)
    if m:
        ids = re.findall(r"[A-Za-z_]\w*", m.group("cond"))
        ids = [x for x in ids if x not in ("null", "is", "not", "string", "IsNullOrEmpty",
                                          "IsNullOrWhiteSpace", "Any", "Count", "Length")]
        return ids[-1] if ids else None
    return None


def _guard_lines(c: CsCorpus) -> list[tuple[str, int, str, str]]:
    """(file, line, member, kind) for single-line guard statements."""
    out = []
    for rel in c.cs():
        for i, l in enumerate(c.code[rel], 1):
            raw = c.files[rel][i - 1]
            if GUARD_LINE.match(l):
                kind = GUARD_LINE.match(l).group("kind")
            elif IF_THROW_LINE.match(l):
                kind = "if-throw"
            else:
                continue
            mem = _guard_member(raw)
            if mem:
                out.append((rel, i, mem, kind))
    return out


def _elsewhere(c: CsCorpus, rel: str, line: int, member: str,
               guards: list[tuple[str, int, str, str]]) -> list[dict]:
    """Other places that still enforce a rule on ``member``: another guard on the same member,
    or EF Core / DataAnnotations configuration of a property with that name."""
    locs = [{"file": f, "start_line": n, "end_line": n} for f, n, m, _ in guards
            if m.lower() == member.lower() and (f, n) != (rel, line)]
    prop = member[:1].upper() + member[1:]
    cfg = re.compile(r"\.Property\(\s*\w+\s*=>\s*\w+\." + re.escape(prop) + r"\b")
    for f in c.cs():
        for n, l in enumerate(c.code[f], 1):
            if cfg.search(l):
                locs.append({"file": f, "start_line": n, "end_line": n})
    return locs


def _guard_sites(c: CsCorpus, category: str, need_elsewhere: bool) -> list[Site]:
    guards = _guard_lines(c)
    out = []
    for rel, i, mem, kind in guards:
        other = _elsewhere(c, rel, i, mem, guards)
        if need_elsewhere and not other:
            continue
        raw = c.files[rel][i - 1]
        out.append(Site(category, rel, i, key=f"guard:{category}@{rel}:{i}",
                        payload={"member": mem, "kind": kind, "text": raw.strip(),
                                 "indent": _indent(raw), "elsewhere": other[:6]}))
    return out


def sites_client_check_removed(c: CsCorpus) -> list[Site]:
    return _guard_sites(c, "client_check_removed", need_elsewhere=True)


def build_client_check_removed(s: Site, c: CsCorpus) -> Mutation:
    p = s.payload
    other = p["elsewhere"]
    db = [o for o in other if "Config" in o["file"] or "Data/" in o["file"]]
    where = ", ".join(f"{o['file']}:{o['start_line']}" for o in other[:4])
    stmt = (f"The guard `{p['text']}` was deleted from {s.file} (it was at line {s.line} of the "
            f"original). The rule on `{p['member']}` is now enforced only elsewhere ({where}); "
            f"this code path accepts the value without checking it. A correct specification must "
            f"say which paths enforce the rule and which no longer do.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["db_only"] if db and len(db) == len(other) else ["inconsistency"],
        evidence_class="Inferred",
        edits=[Edit(s.file, s.line, s.line, [])], extra_locations=other[:4],
        before=p["text"], after="(line deleted)",
        detect_terms=["only in the database", "database only", "db-only", "db only",
                      "not validated", "no longer validated", "missing validation", "missing guard",
                      "unenforced", "not enforced", "only enforced", "enforced only",
                      "silently allowed", "bypass", "inconsisten"])


def sites_rule_to_comment(c: CsCorpus) -> list[Site]:
    return _guard_sites(c, "rule_to_comment", need_elsewhere=False)


def build_rule_to_comment(s: Site, c: CsCorpus) -> Mutation:
    p = s.payload
    ind = p["indent"]
    new_lines = [f"{ind}// Business rule, documentation only - not enforced anywhere in code:",
                 f"{ind}//   {p['text']}"]
    stmt = (f"The rule `{p['text']}` exists in {s.file} only as a comment; the executable "
            f"statement was removed. Nothing enforces it on this path. A correct specification "
            f"must mark it as prose-only and needing business confirmation.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["comment_only"], evidence_class="Confirmation Required",
        edits=[Edit(s.file, s.line, s.line, new_lines)],
        before=p["text"], after=new_lines[1].strip(),
        detect_terms=["comment only", "comment-only", "only in a comment", "not enforced",
                      "documentation only", "no code enforces", "manual convention",
                      "prose only", "stated only in a comment"])


# --------------------------------------------------------------------------- 4. dead code orphan
def sites_dead_code_orphan(c: CsCorpus) -> list[Site]:
    out = []
    all_methods = {rel: methods(c, rel) for rel in c.cs()}
    existing = {m["name"] for ms in all_methods.values() for m in ms}
    for rel in c.cs():
        cls = set(re.findall(r"(?:class|record|struct)\s+([A-Za-z_]\w*)", "\n".join(c.code[rel])))
        for m in all_methods[rel]:
            if m["end"] - m["start"] < 3 or m["end"] - m["start"] > 30:
                continue
            if m["name"] in cls or re.search(r"\b(override|extern|abstract|partial)\b", m["head"]):
                continue                     # constructors / overrides cannot be cloned by name
            if re.search(r"^\s*\[", c.code[rel][m["start"] - 2] if m["start"] >= 2 else ""):
                continue                     # attributed (routes, tests): a clone changes behaviour
            clone = m["name"] + "Legacy"
            if clone in existing:
                continue
            out.append(Site("dead_code_orphan", rel, m["start"],
                            key=f"func:{m['name']}@{rel}:{m['start']}",
                            payload={**m, "clone": clone}))
    return out


def build_dead_code_orphan(s: Site, c: CsCorpus) -> Mutation:
    lines = c.files[s.file]
    p = s.payload
    body = lines[p["start"] - 1: p["end"]]
    name, clone = p["name"], p["clone"]
    head_pat = re.compile(r"(?<![\w.])" + re.escape(name) + r"(?=\s*(?:<[^()]*>)?\s*\()")
    copy = list(body)
    copy[0] = head_pat.sub(clone, copy[0], count=1)
    if copy[0] == body[0]:
        raise ValueError("could not rename the method head")
    new_block = body + ["", f"{p['indent']}// ---"] + copy
    stmt = (f"`{clone}` in {s.file} is never called from anywhere in the code base: it is dead "
            f"code. It is a verbatim copy of `{name}`, so the two will drift apart the first time "
            f"the live one is changed. A correct specification must report it as unreachable / "
            f"duplicated logic rather than describe it as a second business rule.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["dead_code", "duplicated_logic"], evidence_class="Inferred",
        edits=[Edit(s.file, p["start"], p["end"], new_block,
                    loc_slice=(len(body) + 2, len(new_block) - 1))],
        expected_delta={s.file: {"cs_method_decl": 1}},
        before=f"(no {clone})", after=f"copy of {name} named {clone}",
        detect_terms=["dead code", "unused", "no caller", "never called", "unreachable",
                      "orphan", "duplicate of", "duplicated logic", "redundant copy",
                      "not referenced", "identical cop"])


# --------------------------------------------------------------------------- 6. silent cap
NUM_TYPES = {"decimal": "5000m", "int": "5000", "long": "5000L", "double": "5000d"}
RET = re.compile(r"^(?P<ind>\s*)return\s+(?P<expr>.+?)\s*;\s*$")
EXPR_BODY = re.compile(r"^(?P<pre>\s*(?:public|internal|protected|private)[^=;{]*?\b(?P<type>decimal|"
                       r"int|long|double)\??\s+(?P<name>\w+)(?:\s*\([^)]*\))?\s*=>\s*)(?P<expr>.+?)"
                       r"\s*;\s*$")


def sites_silent_cap(c: CsCorpus) -> list[Site]:
    out = []
    for rel in c.cs():
        code = c.code[rel]
        for i, l in enumerate(code, 1):
            m = EXPR_BODY.match(l)
            if m and re.search(r"[*/+\-]|Sum\(|Count\(", m.group("expr")):
                out.append(Site("silent_cap", rel, i, key=f"ret@{rel}:{i}",
                                payload={"func": m.group("name"), "type": m.group("type"),
                                         "form": "expr"}))
        for meth in methods(c, rel):
            mt = re.search(r"\b(decimal|int|long|double)\??\s+" + re.escape(meth["name"]) + r"\s*\(",
                           meth["head"])
            if not mt:
                continue
            for i in range(meth["start"] + 1, meth["end"]):
                m = RET.match(code[i - 1])
                if m and re.search(r"[*/+\-]|Sum\(|Count\(", m.group("expr")):
                    out.append(Site("silent_cap", rel, i, key=f"ret@{rel}:{i}",
                                    payload={"func": meth["name"], "type": mt.group(1),
                                             "form": "return"}))
    return out


def build_silent_cap(s: Site, c: CsCorpus) -> Mutation:
    raw = c.files[s.file][s.line - 1]
    p = s.payload
    cap = NUM_TYPES[p["type"]]
    if p["form"] == "expr":
        m = EXPR_BODY.match(raw)
        new_raw = f"{m.group('pre')}Math.Min({m.group('expr')}, {cap});"
    else:
        m = RET.match(raw)
        new_raw = f"{m.group('ind')}return Math.Min({m.group('expr')}, {cap});"
    stmt = (f"`{p['func']}` in {s.file} silently caps its result at 5000. Nothing in the code, "
            f"the comments, the UI or the database records that a cap exists, and no message is "
            f"shown when it bites. A correct specification must surface the cap as an "
            f"undocumented limit.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["undocumented_feature"], evidence_class="Verified",
        edits=[Edit(s.file, s.line, s.line, [new_raw])],
        before=raw.strip(), after=new_raw.strip(),
        detect_terms=["undocumented", "silently", "silent cap", "math.min", "clamp",
                      "hard cap", "capped at", "caps the", "upper bound of", "not documented",
                      "no message", "without warning"])


# --------------------------------------------------------------------------- 7. date / collection off-by-one
COLL = re.compile(r"\.(?:Count|Length)\b|\.Count\(\)")
TAKE = re.compile(r"\.Take\s*\(\s*(?P<arg>[^()]+?)\s*\)")


def sites_date_off_by_one(c: CsCorpus) -> list[Site]:
    def ctx(l, m):
        window = l[max(0, m.start() - 70): m.end() + 70]
        return bool(DATE_WORD.search(window))
    out = _cmp_sites(c, "date_off_by_one", ctx)
    for rel in c.cs():
        for i, l in enumerate(c.code[rel], 1):
            for m in re.finditer(r"(?<![<>=!-])(<|>)(?![<>=])", l):
                rhs, lhs = l[m.end():m.end() + 40], l[max(0, m.start() - 40):m.start()]
                if COLL.search(rhs.split(")")[0] if m.group(1) == "<" else lhs) and \
                        re.search(r"(?<![\w.])(?:if|while|for|return)\b|&&|\|\|", l):
                    out.append(Site("date_off_by_one", rel, i,
                                    key=f"coll:{m.group(1)}@{rel}:{i}:{m.start()}",
                                    payload={"col": m.start(), "op": m.group(1), "form": "coll"}))
            for m in TAKE.finditer(l):
                out.append(Site("date_off_by_one", rel, i, key=f"take@{rel}:{i}:{m.start()}",
                                payload={"col": m.start("arg"), "arg": m.group("arg"),
                                         "form": "take"}))
    return out


def build_date_off_by_one(s: Site, c: CsCorpus) -> Mutation:
    raw = c.files[s.file][s.line - 1]
    p = s.payload
    form = p.get("form", "date")
    col = p["col"]
    if form == "take":
        arg = p["arg"]
        new_raw = raw[:col] + f"{arg} + 1" + raw[col + len(arg):]
        what = (f"`.Take({arg})` became `.Take({arg} + 1)`, so every page or batch now returns "
                f"one item more than its declared size")
    elif form == "coll":
        op = p["op"]
        new_raw = raw[:col] + op + "=" + raw[col + 1:]
        what = (f"the collection bound `{op}` became `{op}=`, so the loop or check now also "
                f"accepts the index equal to the count")
        op_new = op + "="
    else:
        op = p["op"]
        new_raw = raw[:col] + (">" if op == ">=" else "<") + raw[col + 2:]
        what = (f"`{op}` became `{'>' if op == '>=' else '<'}`, so values falling exactly on the "
                f"boundary date are now excluded")
    stmt = (f"The boundary at {s.file}:{s.line} is off by one: {what}. A correct specification "
            f"must state the exact boundary.")
    return Mutation(
        category=s.category, site=s, statement=stmt, item_type="rule",
        flags=["logic_flaw"], evidence_class="Verified",
        edits=[Edit(s.file, s.line, s.line, [new_raw])],
        before=raw.strip(), after=new_raw.strip(),
        detect_terms=["off-by-one", "off by one", "excludes the boundary", "exclusive",
                      "exactly on", "one day", "one more", "one extra", "strictly less",
                      "boundary mismatch", "page size", "misses rows"])


# --------------------------------------------------------------------------- 8. constant alias
CONST_DECL = re.compile(
    r"^(?P<ind>\s*)(?P<mods>(?:(?:public|private|internal|protected)\s+)?(?:const|static\s+readonly)"
    r")\s+(?P<type>[\w.<>?]+)\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>[^;]+?)\s*;\s*$")


def sites_constant_alias_rename(c: CsCorpus) -> list[Site]:
    out = []
    for rel in c.cs():
        for i, l in enumerate(c.code[rel], 1):
            m = CONST_DECL.match(l)
            if not m:
                continue
            name = m.group("name")
            hits = [h for h in c.word_hits(name) if h != (rel, i)]
            if not hits:
                continue
            alias = name + "_ALT"
            if c.word_hits(alias):
                continue
            raw = c.files[rel][i - 1]
            rm = CONST_DECL.match(raw)
            if not rm:
                continue
            use = hits[0]
            out.append(Site("constant_alias_rename", rel, i, key=f"const:{name}@{rel}:{i}",
                            payload={"name": name, "alias": alias, "type": rm.group("type"),
                                     "value": rm.group("value").strip(), "mods": rm.group("mods"),
                                     "indent": rm.group("ind"), "use_file": use[0],
                                     "use_line": use[1], "n_uses": len(hits)}))
    return out


def build_constant_alias_rename(s: Site, c: CsCorpus) -> Mutation:
    p = s.payload
    if p["use_file"] == s.file and p["use_line"] == s.line:
        raise ValueError("alias use site collides with the declaration")
    decl_raw = c.files[s.file][s.line - 1]
    alias_decl = f"{p['indent']}{p['mods']} {p['type']} {p['alias']} = {p['value']};"
    use_raw = c.files[p["use_file"]][p["use_line"] - 1]
    code_use = c.code[p["use_file"]][p["use_line"] - 1]
    m = re.search(r"(?<![\w])" + re.escape(p["name"]) + r"(?![\w])", code_use)
    if not m:
        raise ValueError("use site not found in code text")
    new_use = use_raw[:m.start()] + p["alias"] + use_raw[m.end():]
    edits = [Edit(s.file, s.line, s.line, [decl_raw, alias_decl]),
             Edit(p["use_file"], p["use_line"], p["use_line"], [new_use])]
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
assert set(OPERATORS) == set(CATEGORIES)


def all_sites(c: CsCorpus, categories: list[str] | None = None) -> list[Site]:
    cats = categories or sorted(OPERATORS)
    out: list[Site] = []
    for cat in cats:
        if cat not in OPERATORS:
            raise KeyError(f"unknown mutation category: {cat}")
        out += OPERATORS[cat][0](c)
    seen, uniq = set(), []
    for s in sorted(out, key=lambda s: s.sort_key()):
        if s.key not in seen:
            seen.add(s.key)
            uniq.append(s)
    return uniq
