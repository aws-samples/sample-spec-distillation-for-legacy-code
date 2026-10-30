# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""HARVEST: deterministic detectors that turn code constructs into candidate key items.

No AI anywhere. Every detector reads the comment/string-blanked code (so nothing inside a
comment or a literal is ever mistaken for code), cites exact lines, and writes its statement
from a fixed template. A statement is therefore only as good as its template: it says *what the
code enforces*, in plain English, and never guesses *why*.

Detectors (``detector`` name -> what it finds):

C# / VB.NET
  guard_ardalis      ``Guard.Against.X(...)`` (Ardalis.GuardClauses), incl. custom guard extensions
  if_throw           ``if (cond) throw new X(...)``, ``?? throw``, VB ``If .. Then Throw New``
  exception_type     custom ``class XException : ...`` + where it is thrown and handled
  enum               ``enum`` declarations and their values
  constant           ``const`` / ``static readonly`` literals (and VB ``Const``)
  literal_threshold  a comparison against a numeric literal (``x > 10``) in a decision
  data_annotation    ``[Required]`` ``[Range]`` ``[StringLength]`` ``[MaxLength]`` ``[RegularExpression]``...
  fluent_validation  ``RuleFor(x => x.P).NotEmpty().MaximumLength(50)...``
  ef_config          EF Core fluent config: IsRequired / HasMaxLength / HasPrecision / HasColumnType /
                     HasConversion / HasDefaultValue / HasIndex().IsUnique() / HasForeignKey / OwnsOne
  authorization      ``[Authorize(Roles=.., Policy=..)]`` / ``[AllowAnonymous]`` / RequireAuthorization /
                     RequireRole / User.IsInRole / AddPolicy
  route              MVC ``[Route]`` + ``[HttpGet]``..., minimal API ``MapGet/MapPost/..``, Razor Pages
  mediatr            ``IRequestHandler<TReq,TResp>`` / ``INotificationHandler<T>`` and ``Send(new TReq``
  specification      Ardalis.Specification ``Query.Where(..)`` / paging in ``Specification<T>`` classes
  entity_rule        domain-entity invariants: computed members, conditional branches, RemoveAll rules

T-SQL
  sql_check / sql_unique / sql_fk / sql_default / sql_trigger / sql_proc / sql_raise
"""
from __future__ import annotations

import re
from pathlib import Path

from kg_source import (Candidate, Index, SourceFile, balanced, cap1, clip, first_string, humanize,
                       loc, split_args)

MAXV = re.compile(r"^(?:int|long|short|decimal|double|float|uint|ulong)\.MaxValue$")


def _raw_span(f: SourceFile, a: int, b: int) -> str:
    return "\n".join(f.lines[a - 1:b])


def _member_phrase(expr: str) -> str:
    """``nameof(details.Price)`` / ``details.Price`` -> ``price``; ``User.Identity.Name`` -> ``user
    identity name`` (a leading lower-case local/parameter segment is dropped)."""
    expr = re.sub(r"^nameof\((.*)\)$", r"\1", expr.strip()).replace("?", "")
    segs = [s for s in re.split(r"\.", expr) if s]
    if len(segs) > 1 and segs[0][:1].islower():
        segs = segs[1:]
    return " ".join(humanize(s) for s in segs)


def _subject(index: Index, f: SourceFile, line: int, member: str) -> tuple[str, str]:
    """(subject phrase, where) for a rule on ``member`` at ``line``."""
    cls, where = index.where(f.rel, line)
    t = index.type_at(f.rel, line)
    ch = humanize(cls)
    if index.is_entity(t) and not member.startswith(ch):
        return f"{ch} {member}".strip(), where
    return member, where


# =========================================================================== C# / VB detectors
GUARD = re.compile(r"Guard\s*\.\s*Against\s*\.\s*(?P<kind>\w+)\s*(?:<[^()]*>)?\s*\(")
GUARD_PHRASE = {
    "Null": "must be provided (not null)",
    "NullOrEmpty": "must be provided and not empty",
    "NullOrWhiteSpace": "must be provided and not blank",
    "NegativeOrZero": "must be greater than zero",
    "Negative": "must not be negative",
    "Zero": "must not be zero",
    "Default": "must not be the default value",
    "NullOrOutOfRange": "must be provided and within range",
    "EnumOutOfRange": "must be a defined enum value",
    "StringTooLong": "must not exceed the maximum length",
    "StringTooShort": "must meet the minimum length",
    "InvalidFormat": "must match the required format",
    "NotFound": "must exist (not found otherwise)",
    "Expression": "must satisfy the guard expression",
    "InvalidInput": "must satisfy the input predicate",
}


def _custom_guards(files: dict[str, SourceFile]) -> dict[str, dict]:
    """Custom guard extensions ``static void Name(this IGuardClause g, ...)`` -> definition info."""
    out: dict[str, dict] = {}
    pat = re.compile(r"static\s+[\w<>?]+\s+(?P<name>\w+)\s*(?:<[^()]*>)?\s*\(\s*this\s+IGuardClause\b")
    for rel, f in sorted(files.items()):
        if f.lang != "cs" or f.is_test:
            continue
        text = f.code_text
        for m in pat.finditer(text):
            start = f.offset_to_line(m.start())
            ob = text.find("{", m.end())
            end = f.offset_to_line(balanced(text, ob, "{", "}") - 1) if ob != -1 else start
            body = _raw_span(f, start, end)
            th = re.search(r"if\s*\((?P<cond>.+?)\)\s*(?:\{\s*)?throw\s+new\s+(?P<exc>\w+)", body, re.S)
            out[m.group("name")] = {"file": rel, "start": start, "end": end,
                                    "cond": clip(th.group("cond"), 100) if th else None,
                                    "exc": th.group("exc") if th else None}
    return out


def detect_guards(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    out = []
    text = f.code_text
    for m in GUARD.finditer(text):
        po = m.end() - 1
        pc = balanced(text, po)
        if pc < 0:
            continue
        line = f.offset_to_line(m.start())
        end_line = f.offset_to_line(pc)
        raw_args = f.text[po + 1:pc - 1]
        args = split_args(raw_args)
        kind = m.group("kind")
        name_arg = next((a for a in args if a.startswith("nameof(")), None)
        member = _member_phrase(name_arg or (args[0] if args else "value"))
        subj, where = _subject(index, f, line, member)
        locs = [loc(f.rel, line, end_line)]
        keys = [f"member:{re.sub(r'[^a-z0-9]', '', member.split()[-1] if member else '')}"]
        typ = "rule"
        if kind == "OutOfRange" and len(args) >= 4:
            lo, hi = args[2], args[3]
            if MAXV.match(hi):
                phrase = "must not be negative (0 or more)" if lo == "0" else f"must be at least {lo}"
            else:
                phrase = f"must be between {lo} and {hi} (inclusive)"
        elif kind in GUARD_PHRASE:
            phrase = GUARD_PHRASE[kind]
        elif kind in ctx["custom_guards"]:
            g = ctx["custom_guards"][kind]
            locs.append(loc(g["file"], g["start"], g["end"]))
            what = humanize(kind)
            if g["exc"]:
                stmt = (f"{cap1(what)}: {where} throws {g['exc']} when `{g['cond']}` "
                        f"(custom guard Guard.Against.{kind}).")
                typ = "exception" if g["exc"] in ctx["custom_exceptions"] else "rule"
            else:
                stmt = f"{cap1(what)} is guarded in {where} (custom guard Guard.Against.{kind})."
            out.append(Candidate(typ, stmt, locs, "harvest", "guard_ardalis",
                                 keys=keys + [f"guard:{kind}"],
                                 extra={"snippet": clip(f.lines[line - 1]), "where": where}))
            continue
        else:
            phrase = f"must pass Guard.Against.{kind}"
        stmt = f"{cap1(subj)} {phrase} (Guard.Against.{kind} in {where})."
        out.append(Candidate(typ, stmt, locs, "harvest", "guard_ardalis", keys=keys,
                             extra={"snippet": clip(f.lines[line - 1]), "where": where,
                                    "group": (subj, phrase, kind, where.split(".")[0])}))
    # the same guard on the same member in several methods of one class is one rule
    groups: dict[tuple, list[Candidate]] = {}
    for c in out:
        groups.setdefault(c.extra.get("group") or (id(c),), []).append(c)
    merged = []
    for g, cs in groups.items():
        if len(cs) == 1 or len(g) != 4:
            merged += cs
            continue
        subj, phrase, kind, cls = g
        wheres = ", ".join(c.extra["where"] for c in cs)
        merged.append(Candidate(cs[0].type, f"{cap1(subj)} {phrase} (Guard.Against.{kind} in {wheres}).",
                                [l for c in cs for l in c.locations], "harvest", "guard_ardalis",
                                keys=cs[0].keys, extra={"snippet": cs[0].extra["snippet"],
                                                        "where": wheres, "occurrences": len(cs)}))
    return merged


THROW = re.compile(r"\bthrow\s+new\s+(?P<exc>[\w.]+)\s*(?:<[^()]*>)?\s*\(")
VB_THROW = re.compile(r"\bThrow\s+New\s+(?P<exc>[\w.]+)\s*\(", re.I)
ARG_EXC = {"ArgumentException", "ArgumentNullException", "ArgumentOutOfRangeException",
           "ValidationException", "InvalidEnumArgumentException", "FormatException"}


def _condition_before(f: SourceFile, line: int, col: int) -> str | None:
    """Condition guarding a ``throw`` at (line, col): same-line ``if (..)``, ``?? throw``, or an
    ``if (..)`` up to three lines above (skipping ``{`` lines)."""
    code = f.code[line - 1]
    pre = code[:col]
    raw_pre = f.lines[line - 1][:col]
    if f.lang == "vb":
        m = re.search(r"\bIf\s+(?P<c>.+?)\s+Then\s*$", pre, re.I)
        if m:
            return raw_pre[m.start("c"):m.end("c")].strip()
        for k in range(line - 1, max(0, line - 4), -1):
            m = re.search(r"^\s*(?:Else)?If\s+(?P<c>.+?)\s+Then\s*$", f.code[k - 1], re.I)
            if m:
                return f.lines[k - 1][m.start("c"):m.end("c")].strip()
            if f.code[k - 1].strip():
                break
        return None
    m = re.search(r"\?\?\s*$", pre)
    if m:
        lhs = re.search(r"([\w.?()\[\]]+)\s*\?\?\s*$", raw_pre)
        return f"{lhs.group(1) if lhs else 'value'} is null"
    m = re.search(r"\bif\s*\(", pre)
    if m:
        e = balanced(code, m.end() - 1)
        if e > 0:
            return f.lines[line - 1][m.end():e - 1].strip()
    for k in range(line - 1, max(0, line - 4), -1):
        s = f.code[k - 1].strip()
        if s in ("{", ""):
            continue
        m = re.search(r"\bif\s*\(", f.code[k - 1])
        if m:
            e = balanced(f.code[k - 1], m.end() - 1)
            if e > 0 and not f.code[k - 1][e:].strip():
                return f.lines[k - 1][m.end():e - 1].strip()
        break
    return None


def detect_throws(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    out = []
    pat = VB_THROW if f.lang == "vb" else THROW
    for i, code in enumerate(f.code, 1):
        for m in pat.finditer(code):
            exc = m.group("exc").split(".")[-1]
            cond = _condition_before(f, i, m.start())
            msg = first_string(f.lines[i - 1][m.end() - 1:])
            cls, where = index.where(f.rel, i)
            msg_txt = f' ("{clip(msg, 90)}")' if msg else ""
            cond_txt = f"when `{clip(cond, 110)}`" if cond else "unconditionally on this path"
            if exc in ARG_EXC:
                typ = "rule"
                stmt = f"{where} rejects the input {cond_txt} with {exc}{msg_txt}."
            else:
                typ = "exception"
                stmt = f"{where} throws {exc} {cond_txt}{msg_txt}."
            keys = [f"exc:{exc}"]
            if cond:
                ids = re.findall(r"[A-Za-z_]\w*", cond)
                keys += [f"member:{x.lower()}" for x in ids[-2:]]
            out.append(Candidate(typ, stmt, [loc(f.rel, max(1, i - (0 if cond is None else _cond_up(f, i))), i)],
                                 "harvest", "if_throw", keys=keys,
                                 extra={"snippet": clip(f.lines[i - 1]), "where": where,
                                        "group": (cls, exc, clip(cond or "", 110), clip(msg or "", 90))}))
    return _group_throws(out)


def _cond_up(f: SourceFile, line: int) -> int:
    """How many lines above ``line`` the guarding ``if`` sits (0 = same line)."""
    if re.search(r"\b(if|If)\b|\?\?", f.code[line - 1].split("throw")[0].split("Throw")[0]):
        return 0
    for k in range(line - 1, max(0, line - 4), -1):
        s = f.code[k - 1].strip()
        if s in ("{", ""):
            continue
        return line - k
    return 0


def _group_throws(cands: list[Candidate]) -> list[Candidate]:
    """Identical (class, exception, condition, message) throws in several methods of one class
    become one item: one rule, several enforcement sites."""
    groups: dict[tuple, list[Candidate]] = {}
    order: list[tuple] = []
    for c in cands:
        g = c.extra["group"]
        if g not in groups:
            order.append(g)
        groups.setdefault(g, []).append(c)
    out = []
    for g in order:
        cs = groups[g]
        if len(cs) == 1:
            out.append(cs[0])
            continue
        cls, exc, cond, msg = g
        first = cs[0]
        msg_txt = f' ("{msg}")' if msg else ""
        cond_txt = f"when `{cond}`" if cond else "unconditionally"
        verb = "rejects the input" if exc in ARG_EXC else "throws"
        stmt = (f"{cls} {verb} {exc if verb == 'throws' else 'with ' + exc} {cond_txt}{msg_txt} "
                f"in {len(cs)} methods.")
        if verb != "throws":
            stmt = f"{cls} rejects the input {cond_txt} with {exc}{msg_txt} in {len(cs)} methods."
        locs = [l for c in cs for l in c.locations]
        out.append(Candidate(first.type, stmt, locs[:12], "harvest", "if_throw", keys=first.keys,
                             extra={"snippet": first.extra["snippet"], "where": cls,
                                    "occurrences": len(cs),
                                    "methods": sorted({c.extra['where'] for c in cs})}))
    return out


EXC_CLASS = re.compile(r"\bclass\s+(?P<name>\w+Exception)\s*(?:\([^)]*\))?\s*:\s*(?P<base>[\w.]+)")
VB_EXC_CLASS = re.compile(r"\bClass\s+(?P<name>\w+Exception)\b", re.I)


def custom_exception_names(files: dict[str, SourceFile]) -> set[str]:
    names = set()
    for f in files.values():
        if f.is_test:
            continue
        for m in (EXC_CLASS if f.lang == "cs" else VB_EXC_CLASS).finditer(f.code_text):
            names.add(m.group("name"))
    return names


def detect_exception_types(files: dict[str, SourceFile], index: Index) -> list[Candidate]:
    out = []
    prod = {r: f for r, f in files.items() if not f.is_test and f.lang in ("cs", "vb")}
    for rel, f in sorted(prod.items()):
        pat = EXC_CLASS if f.lang == "cs" else VB_EXC_CLASS
        for m in pat.finditer(f.code_text):
            name = m.group("name")
            line = f.offset_to_line(m.start())
            t = index.type_at(rel, line)
            end = t.end if t and t.name == name else line
            body = _raw_span(f, line, end)
            base = m.groupdict().get("base") or ""
            bm = re.search(r":\s*base\s*\(\s*\$?\"([^\"]*)\"", body)
            msg = bm.group(1) if bm else None
            throws, handles, mapped = [], [], []
            npat = re.compile(r"\bnew\s+" + re.escape(name) + r"\s*\(|\bNew\s+" + re.escape(name) + r"\b")
            hpat = re.compile(r"catch\s*\(\s*" + re.escape(name) + r"\b|\bis\s+" + re.escape(name)
                              + r"\b|typeof\(\s*" + re.escape(name) + r"\s*\)|Catch\s+\w+\s+As\s+"
                              + re.escape(name) + r"\b")
            for r2, f2 in sorted(prod.items()):
                for i, code in enumerate(f2.code, 1):
                    if npat.search(code):
                        throws.append((r2, i))
                    if hpat.search(code):
                        handles.append((r2, i))
                        win = " ".join(f2.code[i - 1:i + 5])
                        sm = re.search(r"HttpStatusCode\.(\w+)|StatusCodes\.Status(\d+\w*)|"
                                       r"StatusCode\s*=\s*(\d{3})", win)
                        if sm:
                            mapped.append(next(g for g in sm.groups() if g))
            where_thrown = sorted({index.where(r2, i)[1] for r2, i in throws})
            stmt = f"{name} signals \"{humanize(name[:-len('Exception')])}\""
            if base:
                stmt += f" (custom exception deriving from {base.split('.')[-1]})"
            if msg:
                stmt += f' with message "{clip(msg, 80)}"'
            stmt += "; thrown by " + (", ".join(where_thrown[:4]) if where_thrown else "nothing in the code base")
            if mapped:
                stmt += f"; handled and mapped to HTTP {mapped[0]}"
            elif handles:
                stmt += f"; handled at {len(handles)} site(s)"
            stmt += "."
            locs = [loc(rel, line, end)] + [loc(r2, i) for r2, i in throws[:5]] + \
                   [loc(r2, i) for r2, i in handles[:3]]
            flags = [] if throws else ["dead_code"]
            out.append(Candidate("exception", stmt, locs, "harvest", "exception_type",
                                 flags=flags, keys=[f"exc:{name}"],
                                 extra={"snippet": clip(f.lines[line - 1])}))
    return out


ENUM = re.compile(r"\benum\s+(?P<name>\w+)\s*(?::\s*\w+\s*)?\{")
VB_ENUM = re.compile(r"^[ \t]*(?:(?:Public|Private|Friend|Protected)\s+)?Enum\s+(?P<name>\w+)", re.I | re.M)


def detect_enums(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    out = []
    text = f.code_text
    if f.lang == "cs":
        for m in ENUM.finditer(text):
            ob = m.end() - 1
            cb = balanced(text, ob, "{", "}")
            if cb < 0:
                continue
            body = text[ob + 1:cb - 1]
            vals = [re.split(r"\s*=\s*", v.strip())[0] for v in split_args(re.sub(r"\[[^\]]*\]", "", body))]
            vals = [v for v in vals if re.fullmatch(r"\w+", v)]
            a, b = f.offset_to_line(m.start()), f.offset_to_line(cb - 1)
            out.append(_enum_cand(f, m.group("name"), vals, a, b))
    elif f.lang == "vb":
        for m in VB_ENUM.finditer(text):
            a = f.offset_to_line(m.start())
            em = re.compile(r"^\s*End\s+Enum\b", re.I | re.M).search(text, m.end())
            if not em:
                continue
            b = f.offset_to_line(em.start())
            vals = [re.split(r"\s*=\s*", f.code[k - 1].strip())[0] for k in range(a + 1, b)
                    if f.code[k - 1].strip()]
            out.append(_enum_cand(f, m.group("name"), vals, a, b))
    return out


def _enum_cand(f: SourceFile, name: str, vals: list[str], a: int, b: int) -> Candidate:
    shown = ", ".join(vals[:12]) + (", …" if len(vals) > 12 else "")
    stmt = f"{cap1(humanize(name))} has {len(vals)} defined value(s): {shown} (enum {name})."
    return Candidate("state", stmt, [loc(f.rel, a, b)], "harvest", "enum",
                     keys=[f"enum:{name.lower()}"], extra={"snippet": clip(f.lines[a - 1])})


CONST = re.compile(r"^\s*(?:(?:public|private|internal|protected)\s+)*(?:const|static\s+readonly)\s+"
                   r"(?P<type>[\w.<>?\[\]]+)\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>[^;]+?)\s*;", re.M)
VB_CONST = re.compile(r"^\s*(?:(?:Public|Private|Friend|Protected|Shared)\s+)*Const\s+(?P<name>\w+)"
                      r"(?:\s+As\s+(?P<type>[\w.]+))?\s*=\s*(?P<value>[^'\n]+?)\s*$", re.I | re.M)
LITERALISH = re.compile(r'^(?:-?\d[\d_.]*[mMdDfFuUlL]{0,2}|".*"|\$?@?".*"|true|false|\'.\'|'
                        r'TimeSpan\.From\w+\(\s*[\d.]+\s*\)|nameof\(\w+\))$', re.I)


def detect_constants(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    out = []
    pat = CONST if f.lang == "cs" else VB_CONST
    for m in pat.finditer(f.code_text):
        line = f.offset_to_line(m.start("name"))
        raw = f.lines[line - 1]
        rm = pat.match(raw + ("" if f.lang == "vb" else ""))
        value = (rm.group("value") if rm else m.group("value")).strip()
        if not LITERALISH.match(value):
            continue
        name = m.group("name")
        cls, where = index.where(f.rel, line)
        uses = ctx["const_uses"].get(name, [])
        uses = [u for u in uses if u != (f.rel, line)]
        is_num = bool(re.match(r"^-?\d|TimeSpan", value))
        in_cmp = any(re.search(r"[<>]=?|==|!=", ctx["files"][r].code[i - 1]) for r, i in uses)
        typ = "rule" if is_num and in_cmp else "data"
        val_txt = clip(value, 70)
        stmt = f"{cap1(humanize(name))} is fixed at {val_txt} ({cls}.{name})"
        stmt += f"; used in {len(uses)} place(s)." if uses else "; not referenced anywhere else."
        locs = [loc(f.rel, line)] + [loc(r, i) for r, i in uses[:4]]
        out.append(Candidate(typ, stmt, locs, "harvest", "constant",
                             flags=[] if uses else ["unused_constant"],
                             keys=[f"const:{name.lower()}"], extra={"snippet": clip(raw)}))
    return out


CMP_LIT = re.compile(r"(?P<lhs>[\w.()\[\]]+)\s*(?P<op><=|>=|==|!=|<|>)\s*(?P<num>-?\d+(?:\.\d+)?)[mMdDfFL]?\b"
                     r"|(?P<num2>-?\d+(?:\.\d+)?)[mMdDfFL]?\s*(?P<op2><=|>=|==|!=|<|>)\s*(?P<rhs>[\w.()\[\]]+)")
DECISION = re.compile(r"(?<![\w.])(?:if|while|return|case|when|If|ElseIf|While|Return|Until)\b|\?|&&|\|\||"
                      r"AndAlso|OrElse")


def detect_literal_thresholds(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    out = []
    for i, code in enumerate(f.code, 1):
        if not DECISION.search(code) or re.match(r"^\s*(for|using|#)", code):
            continue
        for m in CMP_LIT.finditer(code):
            num = m.group("num") or m.group("num2")
            op = m.group("op") or m.group("op2")
            subj = m.group("lhs") or m.group("rhs")
            if num in ("0", "1", "-1") and op in ("==", "!="):
                continue
            if re.match(r"^\d", subj or "") or "<" in code[max(0, m.start() - 1):m.start()]:
                continue
            if f.lang == "cs" and re.search(r"<\s*\w+\s*>", code[m.start():m.end() + 2]):
                continue                                   # generics, not a comparison
            cls, where = index.where(f.rel, i)
            expr = f.lines[i - 1][m.start():m.end()]
            while expr.count("(") > expr.count(")") and expr.startswith("("):
                expr = expr[1:]
            while expr.count(")") > expr.count("(") and expr.endswith(")"):
                expr = expr[:-1]
            expr = clip(expr, 80)
            stmt = f"{where} decides on `{expr}` (literal threshold {num})."
            out.append(Candidate("rule", stmt, [loc(f.rel, i)], "harvest", "literal_threshold",
                                 keys=[f"member:{re.split(r'[.(]', subj)[-1].lower()}"],
                                 extra={"snippet": clip(f.lines[i - 1])}))
    return out


ATTR_BLOCK = re.compile(r"^\s*\[(?P<body>.+)\]\s*$")
PROP = re.compile(r"^\s*(?:(?:public|private|protected|internal|virtual|override|required|static)\s+)+"
                  r"(?P<type>[\w.<>?\[\],]+)\s+(?P<name>[A-Za-z_]\w*)\s*(?:\{|=>|;|=)")
VB_PROP = re.compile(r"^\s*(?:(?:Public|Private|Friend|Protected|Overridable)\s+)*Property\s+(?P<name>\w+)", re.I)
DA_NAMES = {"Required", "Range", "StringLength", "MaxLength", "MinLength", "RegularExpression",
            "EmailAddress", "Compare", "Phone", "Url", "CreditCard", "DataType"}


def _attr_list(body: str) -> list[tuple[str, list[str]]]:
    out = []
    for part in split_args(body):
        m = re.match(r"(?P<n>[\w.]+?)(?:Attribute)?\s*(?:\((?P<a>.*)\))?$", part.strip(), re.S)
        if m:
            out.append((m.group("n").split(".")[-1], split_args(m.group("a") or "")))
    return out


def _da_phrase(name: str, args: list[str]) -> str | None:
    pos = [a for a in args if not re.match(r"^\w+\s*[=:]=?", a)]
    named = {k.strip(): v.strip() for a in args if (mm := re.match(r"^(\w+)\s*:?=\s*(.+)$", a))
             for k, v in [mm.groups()]}
    if name == "Required":
        return "is required"
    if name == "Range" and len(pos) >= 2:
        lo, hi = pos[0], pos[1]
        if len(pos) >= 3 and pos[0].startswith("typeof"):
            lo, hi = pos[1].strip('"'), pos[2].strip('"')
        if MAXV.match(hi):
            return f"must be at least {lo}"
        return f"must be between {lo} and {hi}"
    if name == "StringLength" and pos:
        mn = named.get("MinimumLength")
        return f"must be {mn} to {pos[0]} characters long" if mn else f"must be at most {pos[0]} characters"
    if name == "MaxLength" and pos:
        return f"must be at most {pos[0]} characters/items"
    if name == "MinLength" and pos:
        return f"must be at least {pos[0]} characters/items"
    if name == "RegularExpression" and pos:
        return f"must match the pattern {clip(pos[0], 60)}"
    if name == "EmailAddress":
        return "must be a valid email address"
    if name == "Compare" and pos:
        return f"must match {pos[0].strip(chr(34))}"
    if name == "Phone":
        return "must be a valid phone number"
    if name == "Url":
        return "must be a valid URL"
    if name == "CreditCard":
        return "must be a valid credit card number"
    if name == "DataType" and pos:
        return f"is treated as {pos[0].split('.')[-1].lower()}"
    return None


def detect_data_annotations(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    out = []
    n = len(f.lines)
    i = 0
    while i < n:
        attrs, first = [], None
        j = i
        while j < n:
            raw = f.lines[j]
            if f.lang == "cs":
                am = ATTR_BLOCK.match(raw) if ATTR_BLOCK.match(f.code[j]) else None
                body = am.group("body") if am else None
            else:
                am = re.match(r"^\s*<(?P<body>.+)>\s*_?\s*$", raw)
                body = am.group("body") if am else None
            if body is None:
                break
            if first is None:
                first = j + 1
            attrs += _attr_list(body.replace(":=", "="))
            j += 1
        if first is None:
            i += 1
            continue
        da = [(a, args) for a, args in attrs if a in DA_NAMES]
        prop_m = (PROP if f.lang == "cs" else VB_PROP).match(f.code[j] if j < n else "")
        if da and prop_m:
            prop = prop_m.group("name")
            cls, where = index.where(f.rel, j + 1)
            phrases = [p for a, args in da if (p := _da_phrase(a, args))]
            msgs = [m for a, args in da for x in args if (mm := re.match(r'ErrorMessage\s*=\s*"(.*)"', x))
                    for m in [mm.group(1)]]
            if phrases:
                subj = f"{humanize(cls)} {humanize(prop)}"
                stmt = f"{cap1(subj)} {'; '.join(phrases)} (DataAnnotations on {cls}.{prop})"
                if msgs:
                    stmt += f'; message "{clip(msgs[0], 80)}"'
                stmt += "."
                out.append(Candidate("rule", stmt, [loc(f.rel, first, j + 1)], "harvest",
                                     "data_annotation", keys=[f"member:{prop.lower()}"],
                                     extra={"snippet": clip(f.lines[first - 1]), "where": f"{cls}.{prop}"}))
        i = j + 1
    return out


FV_RULE = re.compile(r"RuleFor\s*\(\s*\w+\s*=>\s*\w+\.(?P<prop>[\w.]+)\s*\)")
FV_PHRASE = {
    "NotEmpty": lambda a: "is required (not empty)", "NotNull": lambda a: "is required (not null)",
    "MaximumLength": lambda a: f"must be at most {a[0]} characters",
    "MinimumLength": lambda a: f"must be at least {a[0]} characters",
    "Length": lambda a: f"must be {a[0]} to {a[1]} characters" if len(a) > 1 else f"must be {a[0]} characters",
    "GreaterThan": lambda a: f"must be greater than {a[0]}",
    "GreaterThanOrEqualTo": lambda a: f"must be at least {a[0]}",
    "LessThan": lambda a: f"must be less than {a[0]}",
    "LessThanOrEqualTo": lambda a: f"must be at most {a[0]}",
    "InclusiveBetween": lambda a: f"must be between {a[0]} and {a[1]} (inclusive)",
    "ExclusiveBetween": lambda a: f"must be strictly between {a[0]} and {a[1]}",
    "EmailAddress": lambda a: "must be a valid email address",
    "Matches": lambda a: f"must match the pattern {clip(a[0], 60)}",
    "Equal": lambda a: f"must equal {a[0]}", "NotEqual": lambda a: f"must not equal {a[0]}",
    "IsInEnum": lambda a: "must be a defined enum value",
    "Must": lambda a: "must satisfy a custom predicate",
    "PrecisionScale": lambda a: f"must have precision {a[0]} and scale {a[1]}" if len(a) > 1 else None,
}


def _chain(text: str, pos: int) -> tuple[list[tuple[str, list[str]]], int]:
    """Parse ``.A(args).B(args)...`` starting at ``pos``; returns ([(name, args)], end offset)."""
    calls = []
    while True:
        m = re.compile(r"\s*\.\s*(\w+)\s*(?:<(?P<g>[^<>()]*)>)?\s*\(").match(text, pos)
        if not m:
            break
        po = m.end() - 1
        pc = balanced(text, po)
        if pc < 0:
            break
        calls.append((m.group(1), split_args(text[po + 1:pc - 1]) + ([f"<{m.group('g')}>"] if m.group("g") else [])))
        pos = pc
    return calls, pos


def detect_fluent_validation(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    out = []
    text = f.code_text
    raw = f.text
    for m in FV_RULE.finditer(text):
        calls, end = _chain(text, m.end())
        rcalls, _ = _chain(raw, m.end())
        if len(rcalls) == len(calls):
            calls = rcalls
        phrases, msg = [], None
        for name, args in calls:
            if name == "WithMessage" and args:
                msg = args[0].strip('"')
            elif name in FV_PHRASE:
                p = FV_PHRASE[name](args)
                if p:
                    phrases.append(p)
        if not phrases:
            continue
        a, b = f.offset_to_line(m.start()), f.offset_to_line(end)
        t = index.type_at(f.rel, a)
        target = None
        if t:
            for base in t.bases:
                bm = re.search(r"AbstractValidator<\s*([\w.]+)\s*>", base)
                if bm:
                    target = bm.group(1).split(".")[-1]
        subj = f"{humanize(target or (t.name if t else 'value'))} {_member_phrase(m.group('prop'))}"
        stmt = f"{cap1(subj)} {'; '.join(phrases)} (FluentValidation in {t.name if t else f.rel})"
        stmt += f'; message "{clip(msg, 80)}".' if msg else "."
        out.append(Candidate("rule", stmt, [loc(f.rel, a, b)], "harvest", "fluent_validation",
                             keys=[f"member:{m.group('prop').split('.')[-1].lower()}"],
                             extra={"snippet": clip(f.lines[a - 1])}))
    return out


EF_PROP = re.compile(r"(?P<recv>\w+)\s*\.\s*(?P<kind>Property|HasIndex|HasKey|Navigation|HasOne|HasMany|"
                     r"OwnsOne|OwnsMany)\s*\(\s*(?P<p>\w+)\s*=>\s*(?P=p)\.(?P<member>\w+)")
EF_PHRASE = {
    "IsRequired": lambda a: "is optional" if a and a[0] == "false" else "is required",
    "HasMaxLength": lambda a: f"is at most {a[0]} characters" if a else None,
    "HasPrecision": lambda a: f"has precision {a[0]}" + (f" and scale {a[1]}" if len(a) > 1 else ""),
    "HasColumnType": lambda a: f"is stored as {a[0].strip(chr(34))}" if a else None,
    "HasConversion": lambda a: "is stored through a value conversion" + (f" ({a[-1]})" if a and a[-1].startswith("<") else ""),
    "HasDefaultValue": lambda a: f"defaults to {a[0]}" if a else None,
    "HasDefaultValueSql": lambda a: f"defaults to SQL {a[0]}" if a else None,
    "UseHiLo": lambda a: f"is generated by the HiLo sequence {a[0]}" if a else "is generated by a HiLo sequence",
    "ValueGeneratedNever": lambda a: "is never generated by the database",
    "ValueGeneratedOnAdd": lambda a: "is generated by the database on insert",
    "IsUnique": lambda a: "must be unique",
    "IsFixedLength": lambda a: "is fixed length",
    "IsUnicode": lambda a: "is non-Unicode" if a and a[0] == "false" else "is Unicode",
    "IsConcurrencyToken": lambda a: "is a concurrency token",
    "IsRowVersion": lambda a: "is a row version",
}


def detect_ef_config(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    if f.lang != "cs" or not re.search(r"EntityTypeBuilder|ModelBuilder|IEntityTypeConfiguration", f.code_text):
        return []
    out = []
    text, raw = f.code_text, f.text
    owners = [(m.start(), m.group("var"), m.group("member")) for m in re.finditer(
        r"Owns(?:One|Many)\s*\(\s*\w+\s*=>\s*\w+\.(?P<member>\w+)\s*,\s*(?P<var>\w+)\s*=>", text)]
    for m in EF_PROP.finditer(text):
        line = f.offset_to_line(m.start())
        t = index.type_at(f.rel, line)
        entity = None
        if t:
            for base in t.bases:
                bm = re.search(r"IEntityTypeConfiguration<\s*([\w.]+)\s*>", base)
                if bm:
                    entity = bm.group(1).split(".")[-1]
        gm = re.search(r"Entity<\s*([\w.]+)\s*>", text[max(0, m.start() - 300):m.start()])
        entity = entity or (gm.group(1).split(".")[-1] if gm else (t.name.replace("Configuration", "") if t else "entity"))
        owner = next((o for o in reversed(owners) if o[0] < m.start() and o[1] == m.group("recv")), None)
        member, kind = m.group("member"), m.group("kind")
        pc = balanced(text, text.find("(", m.start("kind")))
        calls, end = _chain(text, pc)
        rcalls, _ = _chain(raw, pc)
        if len(rcalls) == len(calls):
            calls = rcalls
        subj = humanize(entity) + (f" {humanize(owner[2])}" if owner else "") + f" {humanize(member)}"
        a, b = line, f.offset_to_line(end)
        if kind in ("OwnsOne", "OwnsMany"):
            stmt = (f"{cap1(humanize(entity))} owns {humanize(member)} as a value object "
                    f"({kind}; stored with the owner, EF Core {t.name if t else ''}).").replace(" ,", ",")
            out.append(Candidate("data", stmt, [loc(f.rel, a, a)], "harvest", "ef_config",
                                 keys=[f"member:{member.lower()}"], extra={"snippet": clip(f.lines[a - 1])}))
            continue
        if kind in ("HasOne", "HasMany"):
            fk = next((args[0] for n, args in calls if n == "HasForeignKey" and args), None)
            req = any(n == "IsRequired" for n, _ in calls)
            if fk:
                fkm = re.search(r"=>\s*\w+\.(\w+)", fk)
                fkn = fkm.group(1) if fkm else fk.strip('"')
                stmt = (f"{cap1(humanize(entity))} references {humanize(member)} through foreign key "
                        f"{fkn}{' (required)' if req else ''} (EF Core {t.name if t else ''}).")
                out.append(Candidate("data", stmt, [loc(f.rel, a, b)], "harvest", "ef_config",
                                     keys=[f"member:{fkn.lower()}"], extra={"snippet": clip(f.lines[a - 1])}))
            continue
        phrases = [p for n, args in calls if n in EF_PHRASE and (p := EF_PHRASE[n](args))]
        if kind == "HasKey":
            phrases.insert(0, "is the primary key")
        if kind == "HasIndex" and not phrases:
            phrases = ["is indexed"]
        if not phrases:
            continue
        stmt = f"{cap1(subj)} {', '.join(phrases)} (database, EF Core {t.name if t else 'model'})."
        out.append(Candidate("data", stmt, [loc(f.rel, a, b)], "harvest", "ef_config",
                             keys=[f"member:{member.lower()}"], extra={"snippet": clip(f.lines[a - 1])}))
    return out


AUTH_ATTR = re.compile(r"[\[<,]\s*(?P<n>Authorize|AllowAnonymous)\s*(?:\((?P<a>[^\]>]*)\))?\s*[\],>]")


def _resolve_const(expr: str, ctx: dict) -> str:
    name = expr.strip().split(".")[-1]
    v = ctx["const_values"].get(name)
    return f"{v.strip(chr(34))} ({'.'.join(expr.strip().split('.')[-2:])})" if v else expr.strip().strip('"')


def detect_authorization(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    out = []
    targets: dict[tuple[int, str], dict] = {}
    for i, code in enumerate(f.code, 1):
        raw = f.lines[i - 1]
        for m in AUTH_ATTR.finditer(code):
            rm = AUTH_ATTR.match(raw, m.start()) or m
            # target: the next declaration line (class / method), or the lambda for inline use
            target, tline = None, i
            after = code[m.end():]
            prev = next((f.code[k - 1] for k in range(i - 1, 0, -1) if f.code[k - 1].strip()), "")
            if re.match(r"\s*(async\b|\()", after) or re.search(r"[(,]\s*$", prev + code[:m.start()].strip()):
                _, where = index.where(f.rel, i)
                target = f"{where} (endpoint delegate)"
            else:
                for k in range(i, min(len(f.code), i + 8) + 1):
                    rest = after if k == i else f.code[k - 1]
                    rest = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", rest)
                    if not rest.strip():
                        continue
                    cm = re.search(r"\b(?:class|record)\s+(\w+)", rest)
                    if cm:
                        target, tline = f"every action of {cm.group(1)}", k
                        break
                    if re.search(r"\w\s*(?:<[^()]*>)?\s*\(", rest):
                        _, where = index.where(f.rel, k)
                        target, tline = where, k
                        break
                    break
            if target is None:
                continue
            d = targets.setdefault((tline, target), {"auth": [], "anon": False, "lines": set()})
            d["lines"].add(i)
            if m.group("n") == "AllowAnonymous":
                d["anon"] = True
            else:
                args = {}
                for a in split_args(rm.group("a") or ""):
                    am = re.match(r"(\w+)\s*:?=\s*(.+)", a)
                    if am:
                        args[am.group(1)] = am.group(2).strip()
                    elif a.strip():
                        args["Policy"] = a.strip()
                d["auth"].append(args)
    for (tline, target), d in sorted(targets.items()):
        lines = sorted(d["lines"] | {tline})
        if d["anon"]:
            stmt = f"{cap1(target)} allows anonymous access" + (
                " ([AllowAnonymous] overrides [Authorize])" if d["auth"] else "") + "."
        else:
            parts = []
            for args in d["auth"]:
                if "Roles" in args:
                    parts.append(f"role {_resolve_const(args['Roles'], ctx)}")
                if "Policy" in args:
                    parts.append(f"policy {_resolve_const(args['Policy'], ctx)}")
                if "AuthenticationSchemes" in args:
                    parts.append(f"authentication scheme {args['AuthenticationSchemes'].split('.')[-1]}")
            stmt = f"{cap1(target)} requires an authenticated user" + (
                f" ({'; '.join(parts)})" if parts else "") + "."
        keys = [f"auth:{target.lower()}"]
        out.append(Candidate("permission", stmt, [loc(f.rel, lines[0], lines[-1])], "harvest",
                             "authorization", keys=keys, extra={"snippet": clip(f.lines[lines[0] - 1])}))
    # programmatic checks
    for i, code in enumerate(f.code, 1):
        raw = f.lines[i - 1]
        for pat, fmt in ((r"\bIsInRole\s*\(", "checks that the user is in role {}"),
                         (r"\bRequireRole\s*\(", "requires role {}"),
                         (r"\bRequireAuthorization\s*\(", "requires authorization {}"),
                         (r"\bAddPolicy\s*\(", "defines authorization policy {}")):
            for m in re.finditer(pat, code):
                if "AddPolicy" in pat:
                    window = " ".join(f.code[max(0, i - 6):i])
                    if "AddAuthorization" not in window or "AddCors" in window:
                        continue                   # CORS policies are not authorization
                pc = balanced(code, m.end() - 1)
                arg = raw[m.end():pc - 1].strip() if pc > 0 else ""
                _, where = index.where(f.rel, i)
                what = _resolve_const(split_args(arg)[0], ctx) if arg else "(default policy)"
                out.append(Candidate("permission", f"{where} {fmt.format(clip(what, 80))}.",
                                     [loc(f.rel, i)], "harvest", "authorization",
                                     keys=[f"auth:{where.lower()}"], extra={"snippet": clip(raw)}))
    return out


HTTP_ATTR = re.compile(r"\[\s*Http(?P<verb>Get|Post|Put|Delete|Patch|Head|Options)\s*(?:\(\s*\"(?P<t>[^\"]*)\"[^)]*\))?\s*\]")
ROUTE_ATTR = re.compile(r"\[\s*Route\s*\(\s*\"(?P<t>[^\"]*)\"")
MAP_CALL = re.compile(r"\.\s*Map(?P<verb>Get|Post|Put|Delete|Patch|Methods)\s*\(\s*\"(?P<t>[^\"]*)\"")


def detect_routes(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    if f.lang != "cs":
        return []
    out = []
    raw_text = f.text
    # minimal API
    for m in MAP_CALL.finditer(raw_text):
        if f.code_text[m.start():m.start() + 1] != ".":
            continue
        line = f.offset_to_line(m.start())
        cls, where = index.where(f.rel, line)
        stmt = f"HTTP {m.group('verb').upper()} {m.group('t')} is served by {cls} (minimal API)."
        out.append(Candidate("interface", stmt, [loc(f.rel, line)], "harvest", "route",
                             keys=[f"route:{m.group('t').lower()}"], extra={"snippet": clip(f.lines[line - 1])}))
    # MVC controllers
    for t in index.types.get(f.rel, []):
        if not (t.name.endswith("Controller") or any("Controller" in b for b in t.bases)):
            continue
        prefix = ""
        for k in range(max(1, t.start - 4), t.start + 1):
            rm = ROUTE_ATTR.search(f.lines[k - 1])
            if rm:
                prefix = rm.group("t")
        ctrl = re.sub(r"Controller$", "", t.name)
        for meth in index.methods.get(f.rel, []):
            if meth.cls != t.name or meth.kind == "Constructor":
                continue
            head = f.code[meth.start - 1]
            if not re.search(r"\bpublic\b", head):
                continue
            verbs, tmpl = [], None
            for k in range(max(t.start, meth.start - 6), meth.start):
                for hm in HTTP_ATTR.finditer(f.lines[k - 1]):
                    verbs.append(hm.group("verb").upper())
                    tmpl = hm.group("t") if hm.group("t") is not None else tmpl
                rm = ROUTE_ATTR.search(f.lines[k - 1])
                if rm:
                    tmpl = rm.group("t")
            if not verbs and not re.search(r"IActionResult|ActionResult|IResult", head):
                continue
            path = (prefix + ("/" + tmpl if tmpl else "")) if tmpl is None or not tmpl.startswith("/") else tmpl
            path = path.replace("[controller]", ctrl).replace("[action]", meth.name) or f"{ctrl}/{meth.name}"
            stmt = f"HTTP {'/'.join(verbs) or 'GET'} {path} is handled by {t.name}.{meth.name} (MVC action)."
            out.append(Candidate("interface", stmt, [loc(f.rel, meth.start, meth.start)], "harvest", "route",
                                 keys=[f"route:{path.lower()}"], extra={"snippet": clip(f.lines[meth.start - 1])}))
    # Razor Pages
    if f.rel.endswith(".cshtml.cs"):
        page = re.sub(r"\.cshtml\.cs$", "", f.rel)
        pm = re.search(r"(?:Areas/(?P<area>\w+)/)?Pages/(?P<p>.+)$", page)
        if pm:
            route = ("/" + pm.group("area") if pm.group("area") else "") + "/" + pm.group("p")
            route = re.sub(r"/Index$", "/", route) or "/"
            handlers = []
            for meth in index.methods.get(f.rel, []):
                hm = re.match(r"On(Get|Post|Put|Delete|Patch)(\w*?)(?:Async)?$", meth.name)
                if hm:
                    handlers.append((meth, hm.group(1).upper() + (f" ({hm.group(2)})" if hm.group(2) else "")))
            if handlers:
                stmt = (f"Razor page {route} handles " + ", ".join(h for _, h in handlers) +
                        f" ({', '.join(m.name for m, _ in handlers)}).")
                locs = [loc(f.rel, m.start) for m, _ in handlers]
                out.append(Candidate("interface", stmt, locs, "harvest", "route",
                                     keys=[f"route:{route.lower()}"], extra={"snippet": clip(f.lines[handlers[0][0].start - 1])}))
    return out


HANDLER = re.compile(r"\bclass\s+(?P<name>\w+)\s*(?:<[^>]*>)?\s*:\s*[^{]*?\b(?P<kind>IRequestHandler|INotificationHandler)"
                     r"\s*<\s*(?P<req>[\w.]+)\s*(?:,\s*(?P<resp>[^>{]+?(?:<[^>]*>)?))?\s*>")


def detect_mediatr(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    out = []
    for m in HANDLER.finditer(f.code_text):
        line = f.offset_to_line(m.start())
        name, req, resp = m.group("name"), m.group("req").split(".")[-1], (m.group("resp") or "").strip()
        locs = [loc(f.rel, line)]
        handle = next((x for x in index.methods.get(f.rel, []) if x.cls == name and x.name == "Handle"), None)
        if handle:
            locs.append(loc(f.rel, handle.start, handle.end))
        for t in index.types_by_name.get(req, [])[:1]:
            locs.append(loc(t.file, t.start, t.start))
        sends = [s for s in ctx["sends"].get(req, [])][:3]
        locs += [loc(r, i) for r, i in sends]
        if m.group("kind") == "IRequestHandler":
            stmt = (f"Request {req} ({humanize(req)}) is handled by {name}" +
                    (f", returning {clip(resp, 60)}" if resp else "") +
                    (f"; sent from {', '.join(index.where(r, i)[1] for r, i in sends)}" if sends else "") + ".")
        else:
            stmt = f"Notification {req} is handled by {name}."
        out.append(Candidate("workflow", stmt, locs, "harvest", "mediatr",
                             keys=[f"mediatr:{req.lower()}"], extra={"snippet": clip(f.lines[line - 1])}))
    return out


def detect_specifications(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    out = []
    for t in index.types.get(f.rel, []):
        sm = next((re.search(r"Specification<\s*([\w.]+)", b) for b in t.bases
                   if re.search(r"Specification<", b)), None)
        if not sm:
            continue
        entity = sm.group(1).split(".")[-1]
        body_code = "\n".join(f.code[t.start - 1:t.end])
        body_raw = "\n".join(f.lines[t.start - 1:t.end])
        parts, lines = [], []
        for qm in re.finditer(r"\bQuery\b", body_code):
            calls, end = _chain(body_raw, qm.end())
            for n, args in calls:
                if n in ("Where", "Search") and args:
                    parts.append(f"filters where `{clip(args[0], 120)}`")
                elif n in ("OrderBy", "OrderByDescending", "ThenBy") and args:
                    parts.append(f"{'orders' if n.startswith('Order') else 'then orders'} by `{clip(args[0], 60)}`"
                                 + (" descending" if "Desc" in n else ""))
                elif n in ("Skip", "Take") and args:
                    parts.append(f"{n.lower()}s `{args[0]}`")
                elif n in ("Include", "ThenInclude") and args:
                    parts.append(f"includes `{clip(args[0], 60)}`")
            lines.append(t.start + body_code[:qm.start()].count("\n"))
        if not parts:
            continue
        stmt = f"{t.name} selects {humanize(entity)} records: " + "; ".join(parts) + "."
        out.append(Candidate("rule", stmt, [loc(f.rel, t.start, t.end)], "harvest", "specification",
                             keys=[f"spec:{t.name.lower()}"], extra={"snippet": clip(f.lines[t.start - 1])}))
    return out


def detect_entity_rules(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    if f.lang != "cs":
        return []
    out = []
    for t in index.types.get(f.rel, []):
        if not index.is_entity(t) or t.kind in ("interface", "enum"):
            continue
        for i in range(t.start, t.end + 1):
            code, raw = f.code[i - 1], f.lines[i - 1]
            if index.type_at(f.rel, i) is not t:
                continue
            # computed members: `public int TotalItems => _items.Sum(i => i.Quantity);`
            em = re.match(r"^\s*public\s+(?:[\w<>?\[\],.]+)\s+(?P<n>\w+)\s*=>\s*(?P<e>.+?);\s*$", code)
            if em and re.search(r"Sum|Count|Any|All|[*+/-]|\?", em.group("e")):
                expr = raw[em.start("e"):em.end("e")]
                out.append(Candidate("rule", f"{humanize(t.name).capitalize()} {humanize(em.group('n'))} is computed as "
                                     f"`{clip(expr, 110)}` ({t.name}.{em.group('n')}).", [loc(f.rel, i)], "harvest",
                                     "entity_rule", keys=[f"member:{em.group('n').lower()}"],
                                     extra={"snippet": clip(raw)}))
            # conditional branches inside entity methods
            im = re.search(r"\bif\s*\(", code)
            if im:
                e = balanced(code, im.end() - 1)
                meth = index.method_at(f.rel, i)
                if e > 0 and meth and meth.cls == t.name:
                    cond = raw[im.end():e - 1].strip()
                    nxt = next((f.lines[k - 1].strip() for k in range(i + 1, min(i + 4, t.end) + 1)
                                if f.code[k - 1].strip() not in ("", "{")), "")
                    then = raw[e:].strip() or nxt
                    if "throw" in then:
                        continue                  # covered by if_throw
                    stmt = (f"{t.name}.{meth.name} branches on `{clip(cond, 100)}`"
                            + (f": then `{clip(then, 90)}`" if then else "") + ".")
                    out.append(Candidate("rule", stmt, [loc(f.rel, i, min(i + 3, meth.end))], "harvest",
                                         "entity_rule", keys=[f"member:{w.lower()}" for w in re.findall(r"[A-Z]\w+", cond)[-2:]],
                                         extra={"snippet": clip(raw), "template": "weak"}))
            am = re.match(r"^\s*(?P<v>\w+)\s*(?P<op>[+\-*]=)\s*(?P<e>.+?);\s*$", code)
            meth = index.method_at(f.rel, i)
            if am and meth and meth.cls == t.name:
                loop = next((f.lines[k - 1].strip() for k in range(i - 1, max(meth.start, i - 3) - 1, -1)
                             if re.search(r"\b(foreach|for|while)\s*\(", f.code[k - 1])), None)
                expr = raw[am.start("e"):am.end("e")]
                out.append(Candidate("rule", f"{t.name}.{meth.name} accumulates `{am.group('v')} {am.group('op')} "
                                     f"{clip(expr, 80)}`" + (f" in `{clip(loop, 60)}`" if loop else "") + ".",
                                     [loc(f.rel, i)], "harvest", "entity_rule",
                                     keys=[f"member:{meth.name.lower()}"], extra={"snippet": clip(raw)}))
            rm = re.search(r"\.\s*(RemoveAll|RemoveWhere)\s*\(\s*(?P<p>.+)\)\s*;", code)
            if rm:
                meth = index.method_at(f.rel, i)
                pred = raw[rm.start("p"):rm.end("p")]
                out.append(Candidate("rule", f"{t.name}.{meth.name if meth else '?'} removes items where "
                                     f"`{clip(pred, 100)}`.", [loc(f.rel, i)], "harvest", "entity_rule",
                                     keys=[f"member:{w.lower()}" for w in re.findall(r"[A-Z]\w+", pred)[-2:]],
                                     extra={"snippet": clip(raw)}))
    return out


# =========================================================================== SQL detectors
SQL_CREATE_TABLE = re.compile(r"\bCREATE\s+TABLE\s+(?P<name>[\w.\[\]]+)\s*\(", re.I)
SQL_CHECK = re.compile(r"\bCHECK\s*\(", re.I)
SQL_FK = re.compile(r"\b(?:FOREIGN\s+KEY\s*\((?P<cols>[^)]*)\)\s*)?REFERENCES\s+(?P<t>[\w.\[\]]+)\s*(?:\((?P<rc>[^)]*)\))?", re.I)
SQL_UNIQUE = re.compile(r"\bUNIQUE\b(?:\s+(?:NON)?CLUSTERED)?\s*(?:\((?P<cols>[^)]*)\))?", re.I)
SQL_DEFAULT = re.compile(r"^\s*\[?(?P<col>\w+)\]?\s+[\w()\[\], ]+?\bDEFAULT\s*(?P<v>\(?[^,\n]+?\)?)\s*(?:,|NOT\b|NULL\b|$)", re.I)
SQL_TRIGGER = re.compile(r"\bCREATE\s+(?:OR\s+ALTER\s+)?TRIGGER\s+(?P<name>[\w.\[\]]+)\s+ON\s+(?P<t>[\w.\[\]]+)\s+"
                         r"(?P<timing>AFTER|FOR|INSTEAD\s+OF)\s+(?P<ev>[\w,\s]+?)\s+AS\b", re.I)
SQL_PROC = re.compile(r"\bCREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\s+(?P<name>[\w.\[\]]+)", re.I)
SQL_RAISE = re.compile(r"\b(?P<k>RAISERROR|THROW)\b\s*\(?\s*(?P<a>[^;\n]*)", re.I)


def _unq(s: str) -> str:
    return s.replace("[", "").replace("]", "").split(".")[-1]


def _table_at(f: SourceFile, off: int) -> str | None:
    best = None
    for m in SQL_CREATE_TABLE.finditer(f.code_text):
        if m.start() > off:
            break
        e = balanced(f.code_text, m.end() - 1)
        if e > off:
            best = _unq(m.group("name"))
    return best


def detect_sql(f: SourceFile, index: Index, ctx: dict) -> list[Candidate]:
    if f.lang != "sql":
        return []
    out = []
    text, raw = f.code_text, f.text
    for m in SQL_CHECK.finditer(text):
        e = balanced(text, m.end() - 1)
        if e < 0:
            continue
        a, b = f.offset_to_line(m.start()), f.offset_to_line(e - 1)
        expr = clip(raw[m.end():e - 1], 120)
        tbl = _table_at(f, m.start()) or "table"
        out.append(Candidate("data", f"{tbl} rows must satisfy CHECK `{expr}` (database constraint).",
                             [loc(f.rel, a, b)], "harvest", "sql_check",
                             keys=[f"member:{w.lower()}" for w in re.findall(r"[A-Za-z_]\w+", expr)[:2]],
                             extra={"snippet": clip(f.lines[a - 1])}))
    for m in SQL_UNIQUE.finditer(text):
        a = f.offset_to_line(m.start())
        tbl = _table_at(f, m.start())
        if not tbl:
            continue
        cols = m.group("cols") or re.match(r"\s*\[?(\w+)", f.code[a - 1]).group(1)
        out.append(Candidate("data", f"{tbl}: {clip(cols, 60).strip()} must be unique (UNIQUE constraint).",
                             [loc(f.rel, a)], "harvest", "sql_unique",
                             keys=[f"member:{c.strip().lower()}" for c in cols.split(",")[:2]],
                             extra={"snippet": clip(f.lines[a - 1])}))
    for m in SQL_FK.finditer(text):
        a = f.offset_to_line(m.start())
        tbl = _table_at(f, m.start()) or "table"
        cols = m.group("cols") or (re.match(r"\s*\[?(\w+)", f.code[a - 1]).group(1))
        out.append(Candidate("data", f"{tbl}.{clip(cols, 60).strip()} must reference an existing "
                             f"{_unq(m.group('t'))}{('(' + m.group('rc').strip() + ')') if m.group('rc') else ''} "
                             f"(foreign key).", [loc(f.rel, a)], "harvest", "sql_fk",
                             keys=[f"member:{c.strip().lower()}" for c in cols.split(",")[:2]],
                             extra={"snippet": clip(f.lines[a - 1])}))
    for i, code in enumerate(f.code, 1):
        dm = SQL_DEFAULT.match(code)
        if dm and _table_at(f, sum(len(x) + 1 for x in f.code[:i - 1])):
            v = f.lines[i - 1][dm.start("v"):dm.end("v")].strip()
            out.append(Candidate("data", f"{_table_at(f, sum(len(x) + 1 for x in f.code[:i - 1]))}.{dm.group('col')} "
                                 f"defaults to {clip(v, 40)}.", [loc(f.rel, i)], "harvest", "sql_default",
                                 keys=[f"member:{dm.group('col').lower()}"], extra={"snippet": clip(f.lines[i - 1])}))
    for m in SQL_TRIGGER.finditer(text):
        a = f.offset_to_line(m.start())
        evs = ", ".join(e.strip().upper() for e in m.group("ev").split(","))
        out.append(Candidate("workflow", f"Trigger {_unq(m.group('name'))} runs {m.group('timing').upper()} {evs} "
                             f"on {_unq(m.group('t'))}.", [loc(f.rel, a)], "harvest", "sql_trigger",
                             keys=[f"proc:{_unq(m.group('name')).lower()}"], extra={"snippet": clip(f.lines[a - 1])}))
    procs = [(m.start(), _unq(m.group("name"))) for m in SQL_PROC.finditer(text)]
    for off, name in procs:
        a = f.offset_to_line(off)
        callers = ctx["proc_refs"].get(name.lower(), [])
        out.append(Candidate("interface", f"Stored procedure {name} is part of the database interface"
                             + (f"; called from {', '.join(sorted({index.where(r, i)[1] for r, i in callers})[:3])}"
                                if callers else "; no caller found in the application code") + ".",
                             [loc(f.rel, a)] + [loc(r, i) for r, i in callers[:3]], "harvest", "sql_proc",
                             flags=[] if callers else ["db_only"], keys=[f"proc:{name.lower()}"],
                             extra={"snippet": clip(f.lines[a - 1])}))
    for m in SQL_RAISE.finditer(text):
        a = f.offset_to_line(m.start())
        if m.group("k").upper() == "THROW" and not m.group("a").strip():
            continue
        msg = first_string(raw[m.start():m.start() + 300].replace("'", '"')) or clip(m.group("a"), 60)
        cond = None
        for k in range(a, max(0, a - 4), -1):
            cm = re.search(r"\bIF\s+(?P<c>.+?)(?:\s+BEGIN)?\s*$", f.code[k - 1], re.I)
            if cm:
                cond = f.lines[k - 1][cm.start("c"):cm.end("c")].strip()
                break
        proc = next((n for o, n in reversed(procs) if o < m.start()), None)
        stmt = f"{proc or 'The script'} raises \"{clip(msg, 80)}\"" + (f" when `{clip(cond, 100)}`" if cond else "") + "."
        out.append(Candidate("rule", stmt, [loc(f.rel, a)], "harvest", "sql_raise",
                             keys=[f"proc:{(proc or '').lower()}"], extra={"snippet": clip(f.lines[a - 1])}))
    return out


# =========================================================================== driver
PER_FILE = [detect_guards, detect_throws, detect_enums, detect_constants, detect_literal_thresholds,
            detect_data_annotations, detect_fluent_validation, detect_ef_config, detect_authorization,
            detect_routes, detect_mediatr, detect_specifications, detect_entity_rules, detect_sql]


def build_context(files: dict[str, SourceFile], index: Index, facts: dict | None) -> dict:
    ctx: dict = {"files": files}
    ctx["custom_guards"] = _custom_guards(files)
    ctx["custom_exceptions"] = custom_exception_names(files)
    consts: dict[str, str] = {}
    for f in files.values():
        if f.is_test:
            continue
        for m in (CONST if f.lang == "cs" else VB_CONST).finditer(f.text if f.lang == "cs" else f.code_text):
            consts.setdefault(m.group("name"), m.group("value").strip())
    if facts:
        for sec in ("cs", "vb"):
            for c in (facts.get(sec) or {}).get("constants") or []:
                consts.setdefault(c["name"], str(c.get("value", "")))
    ctx["const_values"] = consts
    uses: dict[str, list[tuple[str, int]]] = {}
    if consts:
        names = sorted(consts, key=len, reverse=True)
        pat = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in names) + r")(?![\w])")
        for rel, f in sorted(files.items()):
            if f.is_test:
                continue
            for i, code in enumerate(f.code, 1):
                for m in pat.finditer(code):
                    uses.setdefault(m.group(1), []).append((rel, i))
    for k in uses:
        uses[k] = sorted(set(uses[k]))
    ctx["const_uses"] = uses
    sends: dict[str, list[tuple[str, int]]] = {}
    proc_refs: dict[str, list[tuple[str, int]]] = {}
    proc_pat = re.compile(r'"(?:dbo\.)?(\w*(?:usp|sp|proc)\w*)"', re.I)
    for rel, f in sorted(files.items()):
        if f.lang not in ("cs", "vb") or f.is_test:
            continue
        for i, code in enumerate(f.code, 1):
            for m in re.finditer(r"\bSend\s*\(\s*new\s+(\w+)", code):
                sends.setdefault(m.group(1), []).append((rel, i))
            for m in proc_pat.finditer(f.lines[i - 1]):
                proc_refs.setdefault(m.group(1).lower(), []).append((rel, i))
    if facts:
        for sec in ("cs", "vb"):
            for r in (facts.get(sec) or {}).get("proc_refs") or []:
                if r.get("file") in files:
                    proc_refs.setdefault(str(r["proc"]).lower(), []).append((r["file"], int(r["line"])))
    ctx["sends"] = sends
    ctx["proc_refs"] = {k: sorted(set(v)) for k, v in proc_refs.items()}
    return ctx


def harvest(files: dict[str, SourceFile], index: Index, facts: dict | None = None,
            detectors: list[str] | None = None) -> list[Candidate]:
    ctx = build_context(files, index, facts)
    out: list[Candidate] = []
    for rel, f in sorted(files.items()):
        if f.is_test:
            continue
        for det in PER_FILE:
            out += det(f, index, ctx)
    out += detect_exception_types(files, index)
    if detectors:
        out = [c for c in out if c.detector in detectors]
    return out
