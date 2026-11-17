#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""treesitter_facts.py: Phase 1.2 structural facts for Python, Java and JavaScript/TypeScript.

The .NET structure tool (structure/, Roslyn + ScriptDom) covers VB.NET, C# and T-SQL. This module
covers the languages it does not, with Tree-sitter grammars (pure pip wheels, no SDK), so it runs
whether or not `dotnet` is installed. build_facts.py merges its sections into facts.json next to
the Roslyn ones; runtimes/mcp-server/code_graph.py (FactsIndex) reads them to confirm the model's
edges and backfill the ones it missed, exactly as it does for `vb` / `cs`.

One section per language family, with the SAME keys and fact shapes as the Roslyn `vb`/`cs`
sections where the concept exists (types, methods, calls, inline_sql), plus two it does not have:

  py | java | js          (js covers .js .jsx .mjs .cjs .ts .tsx)
    types       [{name, kind, file, start, end, inherits}]
    methods     [{id, class, name, kind, file, start, end, static, async}]
    calls       [{from, to, file, line, callee_file, resolution, text}]   callee defined in the corpus
    call_sites  [{from, callee, receiver, file, line, resolution, module}] EVERY call site, including
                builtins, external-library calls and the ones that could not be resolved
    imports     [{file, line, module, names, alias, external, stdlib, resolved_file}]
    inline_sql  [{file, line, reads: [{table}], writes: [{table, op}], text}]   SQL in string literals
    parse_errors [file]   files Tree-sitter parsed with error recovery (facts are still emitted)

Lines are 1-based and inclusive, as in the Roslyn output. Call resolution is syntactic and
deliberately conservative: a callee is resolved through local definitions, imports, `self`/`this`,
base classes, and receiver types that are visible in the file (annotations, `x = Cls()`,
`Repo r = new Repo()`, fields assigned in the constructor). Anything else stays an unresolved call
site rather than a guessed edge.

Usage:
  treesitter_facts.py <source-root> [rel_path ...]     # prints the sections as JSON
"""
from __future__ import annotations

import importlib
import json
import posixpath
import re
import sys
from collections import defaultdict
from pathlib import Path

# extension -> (section, grammar module, language() function name)
GRAMMARS = {
    ".py": ("py", "tree_sitter_python", "language"),
    ".java": ("java", "tree_sitter_java", "language"),
    ".js": ("js", "tree_sitter_javascript", "language"),
    ".jsx": ("js", "tree_sitter_javascript", "language"),
    ".mjs": ("js", "tree_sitter_javascript", "language"),
    ".cjs": ("js", "tree_sitter_javascript", "language"),
    ".ts": ("js", "tree_sitter_typescript", "language_typescript"),
    ".tsx": ("js", "tree_sitter_typescript", "language_tsx"),
}
SECTIONS = ("py", "java", "js")
LANGUAGE_LABEL = {"py": "python", "java": "java", "js": "javascript"}
MAX_FILE_BYTES = 2_000_000          # generated bundles / minified files are not source structure

_PY_STDLIB = set(getattr(sys, "stdlib_module_names", ()))
_NODE_BUILTINS = {"assert", "buffer", "child_process", "cluster", "crypto", "dns", "events", "fs",
                  "http", "https", "net", "os", "path", "querystring", "readline", "stream",
                  "string_decoder", "timers", "tls", "url", "util", "vm", "zlib", "worker_threads"}
_PY_BUILTINS = set(dir(__import__("builtins")))
_SELF = {"self", "cls", "this"}

_PARSERS: dict = {}


def _parser(ext: str):
    """A cached tree_sitter.Parser for the extension, or None when the grammar is not installed."""
    if ext in _PARSERS:
        return _PARSERS[ext]
    p = None
    spec = GRAMMARS.get(ext)
    if spec:
        try:
            import tree_sitter as ts
            mod = importlib.import_module(spec[1])
            p = ts.Parser(ts.Language(getattr(mod, spec[2])()))
        except Exception:  # noqa: BLE001 - a missing/incompatible grammar just means "not parsed"
            p = None
    _PARSERS[ext] = p
    return p


def tool_versions() -> dict:
    """{section: "tree-sitter X + <grammar> Y"} for the grammars that load."""
    from importlib import metadata
    grammars: dict[str, list[str]] = defaultdict(list)
    for ext, (sect, mod, _) in GRAMMARS.items():
        dist = mod.replace("_", "-")
        if _parser(ext) is None or dist in grammars[sect]:
            continue
        grammars[sect].append(dist)

    def ver(dist):
        try:
            return f"{dist} {metadata.version(dist)}"
        except metadata.PackageNotFoundError:
            return dist
    return {sect: f"{ver('tree-sitter')} + " + ", ".join(ver(d) for d in dists)
            for sect, dists in grammars.items() if dists}


def supported(rel: str) -> bool:
    return _parser(Path(rel).suffix.lower()) is not None


# ── small helpers ─────────────────────────────────────────────────────────────────────────
def _t(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace") if node else ""


def _l(node) -> int:
    return node.start_point[0] + 1


def _e(node) -> int:
    return node.end_point[0] + 1


def _f(node, name):
    return node.child_by_field_name(name)


def _line_text(lines: list[str], line: int) -> str:
    s = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    return s if len(s) <= 160 else s[:157] + "..."


_GENERIC_WRAP = re.compile(r"^(?:Optional|typing\.Optional|Annotated)\[(.+?)(?:,.*)?\]$")


def _type_name(text: str) -> str | None:
    """The class a type annotation / declaration names (`Optional[Repo]` -> Repo, `a.B` -> a.B,
    `List<Order>` -> List). None for anything that is not a plain (dotted) name."""
    t = (text or "").strip().lstrip(":").strip().strip("'\"")
    m = _GENERIC_WRAP.match(t)
    if m:
        t = m.group(1).strip().strip("'\"")
    t = re.sub(r"<.*>$", "", t).strip()
    return t if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", t) else None


def _parts(node, src: bytes) -> list[str]:
    """A receiver expression as dotted name parts (`self.repo` -> [self, repo]); ['?'] when it is
    not a plain name chain (a call result, a subscript, ...)."""
    if node is None:
        return ["?"]
    if node.type in ("identifier", "this", "super", "type_identifier", "property_identifier"):
        return [_t(node, src)]
    if node.type in ("attribute", "member_expression", "field_access"):
        obj = _f(node, "object")
        prop = _f(node, "attribute") or _f(node, "property") or _f(node, "field")
        head = _parts(obj, src)
        return ["?"] if head == ["?"] or prop is None else head + [_t(prop, src)]
    return ["?"]


# SQL in string literals. Positive-only and anchored: a string must START with a SQL verb, so prose
# ("Select an item from the list") rarely qualifies, and only named targets are kept.
_SQL_START = re.compile(r"^\s*(?:select|insert|update|delete|merge|with)\b", re.I)
_SQL_NAME = r"((?:[\w$]+|\"[^\"]+\"|`[^`]+`|\[[^\]]+\])(?:\.(?:[\w$]+|\"[^\"]+\"|`[^`]+`|\[[^\]]+\]))*)"
_SQL_WRITES = (("insert", re.compile(r"\binsert\s+(?:into\s+)?" + _SQL_NAME, re.I)),
               ("update", re.compile(r"\bupdate\s+" + _SQL_NAME + r"\s+set\b", re.I)),
               ("delete", re.compile(r"\bdelete\s+(?:from\s+)?" + _SQL_NAME, re.I)),
               ("merge", re.compile(r"\bmerge\s+(?:into\s+)?" + _SQL_NAME, re.I)))
_SQL_READS = re.compile(r"\b(?:from|join)\s+" + _SQL_NAME, re.I)
_SQL_NOT_TABLE = {"select", "set", "where", "values", "dual", "into", "lateral", "unnest"}


def sql_access(text: str) -> dict | None:
    """{reads: [{table}], writes: [{table, op}]} for a SQL-looking string, else None."""
    body = re.sub(r'^[A-Za-z]*("""|\'\'\'|"|\'|`)', "", text or "")
    if not _SQL_START.match(body):
        return None
    clean = lambda n: n.strip("\"`[]").replace('"', "").replace("`", "")  # noqa: E731
    writes, seen = [], set()
    masked = body
    for op, rx in _SQL_WRITES:
        for m in rx.finditer(body):
            name = clean(m.group(1))
            if name.lower() not in _SQL_NOT_TABLE and (name, op) not in seen:
                seen.add((name, op))
                writes.append({"table": name, "op": op})
            masked = masked.replace(m.group(0), " " * len(m.group(0)), 1)
    reads = []
    for m in _SQL_READS.finditer(masked):
        name = clean(m.group(1))
        if name.lower() not in _SQL_NOT_TABLE and name not in {r["table"] for r in reads}:
            reads.append({"table": name})
    return {"reads": reads, "writes": writes} if reads or writes else None


# ── per-file model ────────────────────────────────────────────────────────────────────────
class FileModel:
    """What one file defines, binds and calls, before corpus-wide resolution."""

    def __init__(self, sect: str, rel: str, module: str, src: bytes):
        self.sect, self.file, self.module, self.src = sect, rel, module, src
        self.lines = src.decode("utf-8", errors="replace").splitlines()
        self.types: dict[str, dict] = {}        # simple name -> {id, methods{}, bases[], fields{}}
        self.funcs: dict[str, str] = {}         # top-level callable name -> id
        self.bindings: dict[str, tuple] = {}    # local name -> ("module", spec) | ("symbol", spec, name)
        self.wildcards: list[str] = []          # java `import a.b.*`
        self.scopes: dict[str, dict] = defaultdict(dict)   # caller id -> {var: type name}
        self.sites: list[dict] = []
        self.raw_imports: list[dict] = []
        self.default_export: str | None = None
        self.out = {"types": [], "methods": [], "inline_sql": []}
        self.has_error = False

    def add_type(self, name, kind, node_start, node_end, bases, qual=None):
        qual = qual or name
        tid = self.qid(qual)
        self.types.setdefault(name, {"id": tid, "qual": qual, "methods": {}, "bases": bases, "fields": {}})
        self.out["types"].append({"name": qual, "kind": kind, "file": self.file, "start": node_start,
                                  "end": node_end, "inherits": bases})
        return self.types[name]

    def add_method(self, mid, cls, name, kind, start, end, static=False, is_async=False):
        self.out["methods"].append({"id": mid, "class": cls, "name": name, "kind": kind,
                                    "file": self.file, "start": start, "end": end,
                                    "static": static, "async": is_async})

    def qid(self, qual: str) -> str:
        return f"{self.module}:{qual}" if self.sect == "js" else f"{self.module}.{qual}" if self.module else qual

    def site(self, caller, cls, recv, name, node, new=False):
        self.sites.append({"from": caller, "cls": cls, "recv": recv, "name": name, "line": _l(node),
                           "new": new})

    def string(self, node):
        acc = sql_access(_t(node, self.src))
        if acc:
            self.out["inline_sql"].append({"file": self.file, "line": _l(node), **acc,
                                           "text": _line_text(self.lines, _l(node))})


# ── Python ────────────────────────────────────────────────────────────────────────────────
def _py_module(rel: str) -> str:
    p = rel[:-3] if rel.endswith(".py") else rel
    parts = [x for x in p.replace("\\", "/").split("/") if x]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _walk_python(fm: FileModel, root) -> None:
    src = fm.src
    is_pkg = fm.file.endswith("__init__.py")
    package = fm.module if is_pkg else fm.module.rpartition(".")[0]

    def relative(level: int, rest: str) -> str:
        base = package.split(".") if package else []
        base = base[:len(base) - (level - 1)] if level > 1 else base
        return ".".join([*base, *([rest] if rest else [])])

    def visit(node, stack):
        t = node.type
        caller = next((s[1] for s in reversed(stack) if s[0] == "func"), fm.module or "<module>")
        cls = next((s[1] for s in reversed(stack) if s[0] == "class"), None)
        if t == "decorated_definition":
            d = _f(node, "definition")
            decos = {_t(c, src).lstrip("@").split("(")[0].strip() for c in node.children if c.type == "decorator"}
            for c in node.children:
                if c.type == "decorator":
                    visit(c, stack)
            if d is not None:
                define(d, stack, _l(node), decos)
            return
        if t in ("class_definition", "function_definition"):
            define(node, stack, _l(node), set())
            return
        if t == "import_statement":
            for c in node.named_children:
                name_node = _f(c, "name") if c.type == "aliased_import" else c
                alias = _t(_f(c, "alias"), src) if c.type == "aliased_import" else None
                mod = _t(name_node, src)
                fm.raw_imports.append({"line": _l(node), "module": mod, "names": [], "alias": alias,
                                       "relative": False})
                if alias:
                    fm.bindings[alias] = ("module", mod)
                else:
                    fm.bindings.setdefault(mod.split(".")[0], ("module", mod.split(".")[0]))
            return
        if t == "import_from_statement":
            mn = _f(node, "module_name")
            raw = _t(mn, src)
            level = len(raw) - len(raw.lstrip("."))
            mod = relative(level, raw.lstrip(".")) if level else raw
            names = []
            for c in node.named_children:
                if c == mn:
                    continue
                if c.type == "wildcard_import":
                    names.append("*")
                    continue
                nn = _f(c, "name") if c.type == "aliased_import" else c
                if nn is None or c.type not in ("dotted_name", "aliased_import", "identifier"):
                    continue
                n = _t(nn, src)
                alias = _t(_f(c, "alias"), src) if c.type == "aliased_import" else None
                names.append(n)
                fm.bindings[alias or n] = ("symbol", mod, n)
            fm.raw_imports.append({"line": _l(node), "module": mod, "names": names, "alias": None,
                                   "relative": bool(level)})
            return
        if t == "call":
            fn = _f(node, "function")
            if fn is not None and fn.type == "identifier":
                fm.site(caller, cls, None, _t(fn, src), node)
            elif fn is not None and fn.type == "attribute":
                fm.site(caller, cls, _parts(_f(fn, "object"), src), _t(_f(fn, "attribute"), src), node)
        elif t in ("assignment", "augmented_assignment"):
            left, right, ann = _f(node, "left"), _f(node, "right"), _f(node, "type")
            ty = _type_name(_t(ann, src)) if ann is not None else None
            if ty is None and right is not None and right.type == "call":
                fn = _f(right, "function")
                if fn is not None and fn.type in ("identifier", "attribute"):
                    ty = _t(fn, src)
            elif ty is None and right is not None and right.type == "identifier":
                ty = fm.scopes[caller].get(_t(right, src))      # `self.repo = repo` (repo: Repo)
            if ty and left is not None:
                lp = _parts(left, src)
                if len(lp) == 1 and lp != ["?"]:
                    fm.scopes[caller][lp[0]] = ty
                elif len(lp) == 2 and lp[0] in _SELF and cls and cls in fm.types:
                    fm.types[cls]["fields"].setdefault(lp[1], ty)
        elif t == "string":
            fm.string(node)
            return
        for c in node.children:
            visit(c, stack)

    def define(node, stack, start, decos):
        src_ = src
        name = _t(_f(node, "name"), src_)
        qual = f"{stack[-1][2]}.{name}" if stack else name
        if node.type == "class_definition":
            sup = _f(node, "superclasses")
            bases = [_t(c, src_) for c in (sup.named_children if sup else [])
                     if c.type in ("identifier", "attribute")]
            if not stack:
                info = fm.add_type(name, "class", start, _e(node), bases)
            else:
                info = fm.add_type(qual, "class", start, _e(node), bases, qual)
            body = _f(node, "body")
            if body is not None:
                for c in body.children:
                    visit(c, [*stack, ("class", name if not stack else qual, qual, info)])
            return
        parent = stack[-1] if stack else None
        is_method = parent is not None and parent[0] == "class"
        mid = fm.qid(qual)
        is_async = any(c.type == "async" for c in node.children)
        fm.add_method(mid, parent[1] if is_method else None, name, "Method" if is_method else "Function",
                      start, _e(node), static=bool(decos & {"staticmethod", "classmethod"}),
                      is_async=is_async)
        if is_method:
            parent[3]["methods"][name] = mid
        elif not stack:
            fm.funcs[name] = mid
        params = _f(node, "parameters")
        for p in (params.named_children if params else []):
            if p.type in ("typed_parameter", "typed_default_parameter"):
                pn = _f(p, "name") or next((c for c in p.named_children if c.type == "identifier"), None)
                ty = _type_name(_t(_f(p, "type"), src_))
                if pn is not None and ty:
                    fm.scopes[mid][_t(pn, src_)] = ty
        body = _f(node, "body")
        if body is not None:
            for c in body.children:
                visit(c, [*stack, ("func", mid, qual, None)])

    visit(root, [])


# ── Java ──────────────────────────────────────────────────────────────────────────────────
_JAVA_TYPES = {"class_declaration": "class", "interface_declaration": "interface",
               "enum_declaration": "enum", "record_declaration": "record"}


def _walk_java(fm: FileModel, root) -> None:
    src = fm.src

    def type_of(node) -> str | None:
        if node is None:
            return None
        if node.type == "generic_type":
            node = next((c for c in node.named_children if c.type in ("type_identifier", "scoped_type_identifier")), node)
        return _type_name(_t(node, src).split(".")[-1])

    def visit(node, stack):
        t = node.type
        caller = next((s[1] for s in reversed(stack) if s[0] == "func"), None)
        cls = next((s[1] for s in reversed(stack) if s[0] == "class"), None)
        if t == "package_declaration":
            fm.module = _t(next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None), src)
            return
        if t == "import_declaration":
            text = re.sub(r"^\s*import\s+|;\s*$", "", _t(node, src)).strip()
            static = text.startswith("static ")
            path = text[len("static "):].strip() if static else text
            if path.endswith(".*"):
                if not static:
                    fm.wildcards.append(path[:-2])
                fm.raw_imports.append({"line": _l(node), "module": path[:-2], "names": ["*"], "alias": None})
            else:
                mod, _, name = path.rpartition(".")
                fm.raw_imports.append({"line": _l(node), "module": mod, "names": [name], "alias": None})
                if not static:
                    fm.bindings[name] = ("symbol", mod, name)
            return
        if t in _JAVA_TYPES:
            name = _t(_f(node, "name"), src)
            qual = ".".join([*[s[2] for s in stack if s[0] == "class"], name])
            bases = []
            sup = _f(node, "superclass")
            if sup is not None:
                bases += [type_of(c) for c in sup.named_children]
            itf = _f(node, "interfaces") or next((c for c in node.children if c.type == "extends_interfaces"), None)
            if itf is not None:
                for tl in itf.named_children:
                    bases += [type_of(c) for c in (tl.named_children if tl.type == "type_list" else [tl])]
            info = fm.add_type(name, _JAVA_TYPES[t], _l(node), _e(node), [b for b in bases if b], qual)
            body = _f(node, "body")
            for c in (body.children if body else []):
                visit(c, [*stack, ("class", name, qual, info)])
            return
        if t in ("method_declaration", "constructor_declaration") and stack and stack[-1][0] == "class":
            _, cname, qual, info = stack[-1]
            name = _t(_f(node, "name"), src)
            ctor = t == "constructor_declaration"
            mid = fm.qid(f"{qual}.{name}")
            mods = next((c for c in node.children if c.type == "modifiers"), None)
            fm.add_method(mid, qual, name, "Constructor" if ctor else "Method", _l(node), _e(node),
                          static="static" in _t(mods, src).split())
            info["methods"]["<init>" if ctor else name] = mid
            for p in (_f(node, "parameters").named_children if _f(node, "parameters") else []):
                if p.type in ("formal_parameter", "spread_parameter") and _f(p, "name") is not None:
                    ty = type_of(_f(p, "type"))
                    if ty:
                        fm.scopes[mid][_t(_f(p, "name"), src)] = ty
            body = _f(node, "body")
            for c in (body.children if body else []):
                visit(c, [*stack, ("func", mid, f"{qual}.{name}", None)])
            return
        if t in ("field_declaration", "local_variable_declaration"):
            ty = type_of(_f(node, "type"))
            for d in node.named_children:
                if d.type != "variable_declarator":
                    continue
                v = _f(d, "value")
                vt = type_of(_f(v, "type")) if v is not None and v.type == "object_creation_expression" else None
                dt = vt if ty in (None, "var") else ty
                if dt:
                    if t == "field_declaration" and cls in fm.types:
                        fm.types[cls]["fields"][_t(_f(d, "name"), src)] = dt
                    elif caller:
                        fm.scopes[caller][_t(_f(d, "name"), src)] = dt
        elif t == "method_invocation" and caller:
            obj = _f(node, "object")
            fm.site(caller, cls, None if obj is None else _parts(obj, src), _t(_f(node, "name"), src), node)
        elif t == "object_creation_expression" and caller:
            ty = type_of(_f(node, "type"))
            if ty:
                fm.site(caller, cls, None, ty, node, new=True)
        elif t == "string_literal":
            fm.string(node)
            return
        for c in node.children:
            visit(c, stack)

    visit(root, [])


# ── JavaScript / TypeScript ───────────────────────────────────────────────────────────────
_JS_FUNCS = {"arrow_function", "function_expression", "function", "generator_function"}


def _js_module(rel: str) -> str:
    p = rel.replace("\\", "/")
    return p[: -len(Path(p).suffix)] if Path(p).suffix else p


def _walk_js(fm: FileModel, root) -> None:
    src = fm.src

    def ann(node) -> str | None:
        a = _f(node, "type") if node is not None else None
        return _type_name(_t(a, src)) if a is not None else None

    def spec_of(node) -> str:
        return _t(node, src).strip("'\"`")

    def func(node, name, stack, start, cls_entry=None, static=False):
        parent_func = next((s for s in reversed(stack) if s[0] == "func"), None)
        if cls_entry is not None:
            _, cname, cqual, info = cls_entry
            qual = f"{cqual}.{name}"
            mid = fm.qid(qual)
            ctor = name == "constructor"
            fm.add_method(mid, cqual, name, "Constructor" if ctor else "Method", start, _e(node),
                          static=static, is_async=any(c.type == "async" for c in node.children))
            info["methods"]["<init>" if ctor else name] = mid
        else:
            qual = f"{parent_func[2]}.{name}" if parent_func else name
            mid = fm.qid(qual)
            fm.add_method(mid, None, name, "Function", start, _e(node),
                          is_async=any(c.type == "async" for c in node.children))
            if parent_func is None:
                fm.funcs[name] = mid
        for p in (_f(node, "parameters").named_children if _f(node, "parameters") else []):
            pat = _f(p, "pattern") or (p if p.type == "identifier" else None)
            ty = ann(p)
            if pat is not None and pat.type == "identifier" and ty:
                fm.scopes[mid][_t(pat, src)] = ty
                if cls_entry is not None and any(c.type == "accessibility_modifier" for c in p.children):
                    cls_entry[3]["fields"][_t(pat, src)] = ty      # TS parameter property
        body = _f(node, "body")
        if body is not None:
            visit(body, [*stack, ("func", mid, qual, None)])

    def visit(node, stack):
        t = node.type
        caller = next((s[1] for s in reversed(stack) if s[0] == "func"), fm.module)
        cls = next((s for s in reversed(stack) if s[0] == "class"), None)
        if t == "import_statement":
            spec = spec_of(_f(node, "source"))
            names = []
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            for c in (clause.named_children if clause else []):
                if c.type == "identifier":
                    fm.bindings[_t(c, src)] = ("symbol", spec, "default")
                    names.append("default")
                elif c.type == "namespace_import":
                    ident = next((x for x in c.named_children if x.type == "identifier"), None)
                    if ident is not None:
                        fm.bindings[_t(ident, src)] = ("module", spec)
                    names.append("*")
                elif c.type == "named_imports":
                    for s in c.named_children:
                        if s.type == "import_specifier":
                            n = _t(_f(s, "name"), src)
                            fm.bindings[_t(_f(s, "alias"), src) or n] = ("symbol", spec, n)
                            names.append(n)
            fm.raw_imports.append({"line": _l(node), "module": spec, "names": names, "alias": None})
            return
        if t == "export_statement":
            if any(c.type == "default" for c in node.children):
                v, d = _f(node, "value"), _f(node, "declaration")
                if v is not None and v.type == "identifier":
                    fm.default_export = _t(v, src)
                elif d is not None and _f(d, "name") is not None:
                    fm.default_export = _t(_f(d, "name"), src)
        if t in ("class_declaration", "class", "abstract_class_declaration") and _f(node, "name") is not None:
            name = _t(_f(node, "name"), src)
            her = next((c for c in node.children if c.type == "class_heritage"), None)
            m = re.search(r"extends\s+([\w$.]+)", _t(her, src)) if her is not None else None
            info = fm.add_type(name, "class", _l(node), _e(node), [m.group(1)] if m else [], name)
            body = _f(node, "body")
            for c in (body.named_children if body else []):
                visit(c, [*stack, ("class", name, name, info)])
            return
        if t == "method_definition" and cls is not None:
            func(node, _t(_f(node, "name"), src), stack, _l(node), cls,
                 static=any(c.type == "static" for c in node.children))
            return
        if t in ("public_field_definition", "field_definition") and cls is not None:
            prop = _f(node, "name") or _f(node, "property")
            v = _f(node, "value")
            ty = ann(node) or (_type_name(_t(_f(v, "constructor"), src)) if v is not None and v.type == "new_expression" else None)
            if prop is not None and ty:
                cls[3]["fields"][_t(prop, src)] = ty
            if v is not None and v.type in _JS_FUNCS and prop is not None:
                func(v, _t(prop, src), stack, _l(node), cls)
                return
        if t in ("function_declaration", "generator_function_declaration"):
            func(node, _t(_f(node, "name"), src), stack, _l(node))
            return
        if t == "variable_declarator":
            name_n, v = _f(node, "name"), _f(node, "value")
            if v is not None and name_n is not None:
                if v.type in _JS_FUNCS and name_n.type == "identifier":
                    func(v, _t(name_n, src), stack, _l(node))
                    return
                if (v.type == "call_expression" and _t(_f(v, "function"), src) == "require"):
                    arg = next((a for a in _f(v, "arguments").named_children if a.type == "string"), None)
                    if arg is not None:
                        spec = spec_of(arg)
                        if name_n.type == "identifier":
                            fm.bindings[_t(name_n, src)] = ("module", spec)
                        names = []
                        for p in (name_n.named_children if name_n.type == "object_pattern" else []):
                            n = _t(p, src).split(":")[0].strip()
                            fm.bindings[_t(p, src).split(":")[-1].strip()] = ("symbol", spec, n)
                            names.append(n)
                        fm.raw_imports.append({"line": _l(node), "module": spec, "names": names, "alias": None})
                        return
                ty = ann(node) or (_type_name(_t(_f(v, "constructor"), src)) if v.type == "new_expression" else None)
                if ty and name_n.type == "identifier":
                    fm.scopes[caller][_t(name_n, src)] = ty
        elif t == "assignment_expression":
            left, right = _f(node, "left"), _f(node, "right")
            lp = _parts(left, src)
            if right is not None and right.type == "new_expression" and len(lp) == 2 and lp[0] == "this" and cls:
                ty = _type_name(_t(_f(right, "constructor"), src))
                if ty:
                    cls[3]["fields"][lp[1]] = ty
        elif t == "call_expression":
            fn = _f(node, "function")
            if fn is not None and fn.type == "identifier":
                fm.site(caller, cls[1] if cls else None, None, _t(fn, src), node)
            elif fn is not None and fn.type == "member_expression":
                fm.site(caller, cls[1] if cls else None, _parts(_f(fn, "object"), src),
                        _t(_f(fn, "property"), src), node)
        elif t == "new_expression":
            ty = _type_name(_t(_f(node, "constructor"), src))
            if ty:
                fm.site(caller, cls[1] if cls else None, None, ty, node, new=True)
        elif t in ("string", "template_string"):
            fm.string(node)
            return
        for c in node.children:
            visit(c, stack)

    visit(root, [])


_WALKERS = {"py": _walk_python, "java": _walk_java, "js": _walk_js}


def parse_file(src_root: Path, rel: str) -> FileModel | None:
    ext = Path(rel).suffix.lower()
    parser = _parser(ext)
    if parser is None:
        return None
    path = src_root / rel
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_FILE_BYTES:
        return None
    sect = GRAMMARS[ext][0]
    module = _py_module(rel) if sect == "py" else _js_module(rel) if sect == "js" else ""
    fm = FileModel(sect, rel, module, data)
    tree = parser.parse(data)
    fm.has_error = tree.root_node.has_error
    _WALKERS[sect](fm, tree.root_node)
    return fm


# ── corpus-wide resolution ────────────────────────────────────────────────────────────────
class Corpus:
    """Resolves names, imports and call sites across every parsed file of one section."""

    def __init__(self, sect: str, models: list[FileModel]):
        self.sect, self.models = sect, models
        self.by_module: dict[str, list[FileModel]] = defaultdict(list)
        for fm in models:
            self.by_module[fm.module].append(fm)
            if sect == "js" and fm.module.endswith("/index"):
                self.by_module[fm.module[: -len("/index")]].append(fm)

    # -- modules --------------------------------------------------------------------------
    def modules(self, spec: str, frm: FileModel) -> list[FileModel]:
        if self.sect == "js":
            if not spec.startswith((".", "/")):
                return []
            target = posixpath.normpath(posixpath.join(posixpath.dirname(frm.file), spec)).lstrip("/")
            target = re.sub(r"\.(?:[cm]?js|jsx|tsx?)$", "", target)
            return self.by_module.get(target, [])
        if spec in self.by_module:
            return self.by_module[spec]
        if self.sect == "py" and spec:
            # the source root may sit above the import root (src/shop/x.py imported as shop.x)
            hits = sorted((m for m in self.by_module if m.endswith("." + spec)), key=len)
            return self.by_module[hits[0]] if hits else []
        return []

    def external(self, spec: str, frm: FileModel) -> bool:
        return not self.modules(spec, frm) and not (self.sect == "py" and self._py_parent_known(spec, frm))

    def _py_parent_known(self, spec: str, frm: FileModel) -> bool:
        # `from shop import models` where shop/ has no __init__.py: the package itself is not a file
        return any(m == spec or m.startswith(spec + ".") or m.endswith("." + spec) or ("." + spec + ".") in m
                   for m in self.by_module) if spec else False

    def stdlib(self, spec: str) -> bool:
        if self.sect == "py":
            return spec.split(".")[0] in _PY_STDLIB
        if self.sect == "java":
            return spec.startswith(("java.", "javax.", "jdk.", "sun."))
        return spec.startswith("node:") or spec.split("/")[0] in _NODE_BUILTINS

    # -- symbols --------------------------------------------------------------------------
    def symbol(self, spec: str, name: str, frm: FileModel, depth: int = 0):
        """('type', fm, info) | ('func', fm, id) | ('module', [fm]) | ('external', spec) | None."""
        mods = self.modules(spec, frm)
        if not mods:
            if self.sect == "py":
                sub = self.modules(f"{spec}.{name}" if spec else name, frm)
                if sub:
                    return ("module", sub)
            return None if (self.sect == "py" and self._py_parent_known(spec, frm)) else ("external", spec)
        for m in mods:
            if name == "default" and m.default_export:
                return self.local(m, m.default_export, depth + 1)
            hit = self.local(m, name, depth + 1, follow_imports=depth < 3)
            if hit and hit[0] != "external":
                return hit
        if self.sect == "py":
            sub = self.modules(f"{spec}.{name}", frm)
            if sub:
                return ("module", sub)
        return None

    def local(self, fm: FileModel, name: str, depth: int = 0, follow_imports: bool = True):
        if name in fm.types:
            return ("type", fm, fm.types[name])
        if name in fm.funcs:
            return ("func", fm, fm.funcs[name])
        if self.sect == "java":
            for m in self.by_module.get(fm.module, []):
                if name in m.types:
                    return ("type", m, m.types[name])
            for w in fm.wildcards:
                for m in self.by_module.get(w, []):
                    if name in m.types:
                        return ("type", m, m.types[name])
        b = fm.bindings.get(name)
        if b and follow_imports and depth < 4:
            if b[0] == "module":
                mods = self.modules(b[1], fm)
                return ("module", mods) if mods else ("external", b[1])
            return self.symbol(b[1], b[2], fm, depth)
        return None

    def dotted(self, fm: FileModel, parts: list[str]):
        """Resolve a dotted name (`models.Order`, `os.path`) to a symbol, module or external."""
        hit = self.local(fm, parts[0])
        for p in parts[1:]:
            if hit is None:
                return None
            if hit[0] == "external":
                return hit           # the imported external module, not its attribute path
            if hit[0] == "module":
                mod_name = hit[1][0].module
                nxt = None
                for m in hit[1]:
                    nxt = self.local(m, p)
                    if nxt:
                        break
                hit = nxt or (("module", self.modules(f"{mod_name}.{p}", fm))
                              if self.modules(f"{mod_name}.{p}", fm) else None)
            elif hit[0] == "type":
                return None          # nested attribute of a class: not a type reference
            else:
                return None
        return hit

    def method(self, fm: FileModel, info: dict, name: str, seen=None):
        """(method id, defining FileModel) for `name` on a type, searching base classes."""
        seen = seen or set()
        if info["id"] in seen:
            return None
        seen.add(info["id"])
        if name in info["methods"]:
            return info["methods"][name], fm
        for b in info["bases"]:
            hit = self.dotted(fm, b.split("."))
            if hit and hit[0] == "type":
                found = self.method(hit[1], hit[2], name, seen)
                if found:
                    return found
        return None

    def field_type(self, fm: FileModel, info: dict, attr: str, seen=None):
        seen = seen or set()
        if info["id"] in seen:
            return None
        seen.add(info["id"])
        if attr in info["fields"]:
            return fm, info["fields"][attr]
        for b in info["bases"]:
            hit = self.dotted(fm, b.split("."))
            if hit and hit[0] == "type":
                found = self.field_type(hit[1], hit[2], attr, seen)
                if found:
                    return found
        return None

    def type_ref(self, fm: FileModel, ty: str):
        hit = self.dotted(fm, ty.split("."))
        return hit if hit and hit[0] in ("type", "external") else None

    # -- call sites -----------------------------------------------------------------------
    def resolve(self, fm: FileModel, s: dict) -> tuple[str, str | None, FileModel | None, str | None]:
        """(resolution, target id, target file model, external module) for one call site."""
        name, recv = s["name"], s["recv"]
        cls_info = fm.types.get(s["cls"]) if s["cls"] else None

        def on_type(hit, how):
            if hit is None:
                return ("unresolved", None, None, None)
            if hit[0] == "external":
                return ("external", None, None, hit[1])
            if hit[0] == "type":
                found = self.method(hit[1], hit[2], name)
                if found:
                    return (how, found[0], found[1], None)
            return ("unresolved", None, None, None)

        if s["new"] or (recv is None and self.sect == "py" and name in fm.types):
            hit = self.type_ref(fm, name) if s["new"] else ("type", fm, fm.types[name])
            if hit and hit[0] == "type":
                info = hit[2]
                ctor = self.method(hit[1], info, "__init__" if self.sect == "py" else "<init>")
                return ("constructor", ctor[0] if ctor else info["id"], ctor[1] if ctor else hit[1], None)
            if hit and hit[0] == "external":
                return ("external", None, None, hit[1])
        if recv is None:
            if self.sect == "java" and cls_info is not None:
                found = self.method(fm, cls_info, name)
                if found:
                    return ("same-class", found[0], found[1], None)
            hit = self.local(fm, name)
            if hit is None:
                return ("builtin" if self.sect == "py" and name in _PY_BUILTINS else "unresolved",
                        None, None, None)
            if hit[0] == "func":
                return ("same-file" if hit[1] is fm else "import", hit[2], hit[1], None)
            if hit[0] == "type":
                ctor = self.method(hit[1], hit[2], "__init__" if self.sect == "py" else "<init>")
                return ("constructor", ctor[0] if ctor else hit[2]["id"], ctor[1] if ctor else hit[1], None)
            if hit[0] == "external":
                return ("external", None, None, hit[1])
            return ("unresolved", None, None, None)
        if recv == ["?"]:
            return ("unresolved", None, None, None)
        if recv[0] in _SELF and cls_info is not None:
            if len(recv) == 1:
                return on_type(("type", fm, cls_info), "same-class")
            if len(recv) == 2:
                ft = self.field_type(fm, cls_info, recv[1])
                return on_type(self.type_ref(ft[0], ft[1]) if ft else None, "receiver-type")
            return ("unresolved", None, None, None)
        if recv[0] == "super" and cls_info is not None:
            for b in cls_info["bases"]:
                r = on_type(self.type_ref(fm, b), "inherited")
                if r[1] or r[0] == "external":
                    return r
            return ("unresolved", None, None, None)
        if len(recv) == 1:
            var_ty = (fm.scopes.get(s["from"], {}).get(recv[0])
                      or fm.scopes.get(fm.module or "<module>", {}).get(recv[0]))
            if var_ty:
                return on_type(self.type_ref(fm, var_ty), "receiver-type")
            if self.sect == "java" and cls_info is not None:     # a bare field name (`repo.save()`)
                ft = self.field_type(fm, cls_info, recv[0])
                if ft:
                    return on_type(self.type_ref(ft[0], ft[1]), "receiver-type")
        hit = self.dotted(fm, recv)
        if hit is None:
            return ("unresolved", None, None, None)
        if hit[0] == "module":
            for m in hit[1]:
                h2 = self.local(m, name, follow_imports=True)
                if h2 and h2[0] == "func":
                    return ("module", h2[2], h2[1], None)
                if h2 and h2[0] == "type":
                    ctor = self.method(h2[1], h2[2], "__init__" if self.sect == "py" else "<init>")
                    return ("constructor", ctor[0] if ctor else h2[2]["id"], ctor[1] if ctor else h2[1], None)
            return ("unresolved", None, None, None)
        return on_type(hit, "static" if hit[0] == "type" else "receiver-type")

    # -- output ---------------------------------------------------------------------------
    def section(self) -> dict:
        out = {"types": [], "methods": [], "calls": [], "call_sites": [], "imports": [],
               "inline_sql": [], "parse_errors": []}
        for fm in self.models:
            out["types"] += fm.out["types"]
            out["methods"] += fm.out["methods"]
            out["inline_sql"] += fm.out["inline_sql"]
            if fm.has_error:
                out["parse_errors"].append(fm.file)
            for imp in fm.raw_imports:
                mods = self.modules(imp["module"], fm)
                if not mods and self.sect == "java" and imp["names"] and imp["names"] != ["*"]:
                    mods = [m for m in self.by_module.get(imp["module"], []) if imp["names"][0] in m.types]
                if not mods and self.sect == "py" and imp["names"]:
                    mods = self.modules(f"{imp['module']}.{imp['names'][0]}", fm)
                ext = not mods and self.external(imp["module"], fm)
                out["imports"].append({"file": fm.file, "line": imp["line"], "module": imp["module"],
                                       "names": imp["names"], "alias": imp["alias"],
                                       "external": ext, "stdlib": ext and self.stdlib(imp["module"]),
                                       "resolved_file": mods[0].file if mods else None})
            for s in fm.sites:
                how, to, tfm, ext_mod = self.resolve(fm, s)
                recv = None if s["recv"] is None else ".".join(s["recv"])
                callee = f"new {s['name']}" if s["new"] else (f"{recv}.{s['name']}" if recv else s["name"])
                out["call_sites"].append({"from": s["from"], "callee": callee, "receiver": recv,
                                          "file": fm.file, "line": s["line"], "resolution": how,
                                          "to": to, "module": ext_mod})
                if to:
                    out["calls"].append({"from": s["from"], "to": to, "file": fm.file, "line": s["line"],
                                         "callee_file": tfm.file if tfm else None, "resolution": how,
                                         "text": _line_text(fm.lines, s["line"])})
        return out


def extract(src_root: Path, rel_paths: list[str]) -> dict:
    """{"files": [...], "tools": {...}, <section>: {...}} for the supported files in rel_paths.
    Sections with no parsed file are omitted; an empty dict means nothing was parsed."""
    by_sect: dict[str, list[FileModel]] = defaultdict(list)
    for rel in rel_paths:
        fm = parse_file(src_root, rel)
        if fm is not None:
            by_sect[fm.sect].append(fm)
    if not by_sect:
        return {}
    versions = tool_versions()
    out: dict = {"files": [], "tools": {s: versions.get(s, "tree-sitter") for s in by_sect}}
    for sect, models in by_sect.items():
        out[sect] = Corpus(sect, models).section()
        out["files"] += [{"file": fm.file, "language": LANGUAGE_LABEL[sect], "start": 1,
                          "end": max(1, len(fm.lines))} for fm in models]
    return out


def merge(facts: dict, ts: dict) -> dict:
    """Merge Tree-sitter sections into a (Roslyn or skeleton) facts dict, in place."""
    if not ts:
        return facts
    facts.setdefault("tools", {}).update({f"{s}": v for s, v in ts.get("tools", {}).items()})
    langs = {f["file"]: f for f in ts.get("files", [])}
    files = facts.setdefault("files", [])
    seen = set()
    for f in files:
        if f.get("file") in langs:
            f["language"] = langs[f["file"]]["language"]
            seen.add(f["file"])
    files += [f for k, f in langs.items() if k not in seen]
    for sect in SECTIONS:
        if sect in ts:
            facts[sect] = ts[sect]
    return facts


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    rels = sys.argv[2:] or sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    print(json.dumps(extract(root, rels), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
