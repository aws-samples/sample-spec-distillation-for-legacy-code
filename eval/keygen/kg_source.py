# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Source tree model for the key builder: files, blanked code, a structure index, candidates.

Everything here is deterministic. The structure index (types and methods with line ranges) comes
from the facts tool output when ``--facts`` is given (Roslyn, exact) and from a brace/keyword
scanner otherwise (approximate on unusual layouts, exact on conventional C# and VB.NET).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

BUILDER_NAME = "eval/keygen"
BUILDER_VERSION = "1.0.0"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mutate"))

from cs_scan import scan_cs                                  # noqa: E402
from sanity import scan_sql, scan_vb                         # noqa: E402

CODE_EXT = {".cs": "cs", ".vb": "vb", ".sql": "sql"}
DEFAULT_EXCLUDE = (
    ".git/*", "*/.git/*", "*/bin/*", "*/obj/*", "bin/*", "obj/*", "*/node_modules/*",
    "*/wwwroot/*", "*/Migrations/*", "*.Designer.cs", "*.designer.cs", "*.g.cs", "*.g.i.cs",
    "*/AssemblyInfo.cs", "*.min.js",
)
TEST_DIR = re.compile(r"(^|/)(tests?|[^/]*[._]?(unit|integration|functional)?tests?)(/|$)", re.I)
TEST_ATTR = re.compile(r"[\[<]\s*(Fact|Theory|Test|TestCase|TestMethod|DataTestMethod)\b")


# --------------------------------------------------------------------------- helpers
def humanize(name: str) -> str:
    """``CatalogItem`` -> ``catalog item``; ``ITEMS_PER_PAGE`` -> ``items per page``;
    ``CantAddItemWithNegativeQuantity`` -> ``cant add item with negative quantity``."""
    name = name.strip().strip("_")
    if not name:
        return ""
    if name.isupper() or "_" in name:
        parts = [p for p in re.split(r"[_\s]+", name) if p]
    else:
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+", name)
    return " ".join(p.lower() for p in parts)


def cap1(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def clip(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def balanced(text: str, start: int, open_ch: str = "(", close_ch: str = ")") -> int:
    """Index just past the bracket that closes ``text[start]`` (which must be ``open_ch``),
    or -1. ``text`` should be blanked code so brackets in literals are ignored."""
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def split_args(s: str) -> list[str]:
    """Split an argument list on top-level commas."""
    out, depth, cur, q = [], 0, [], None
    for c in s:
        if q:
            cur.append(c)
            if c == q:
                q = None
            continue
        if c in "\"'":
            q = c
        elif c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(c)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def first_string(raw: str) -> str | None:
    """First string literal in a raw source fragment, interpolation holes replaced by ``…``."""
    m = re.search(r'\$?@?"((?:[^"\\]|\\.|"")*)"', raw)
    if not m:
        return None
    s = m.group(1)
    s = re.sub(r"\{[^{}]*\}", "…", s) if raw[m.start()] == "$" or "$" in raw[m.start():m.start() + 2] else s
    return s.replace('""', '"')


def stable_hash(*parts: str, n: int = 6) -> str:
    h = hashlib.sha1("\x1f".join(parts).encode(), usedforsecurity=False).hexdigest()
    return h[:n]


# --------------------------------------------------------------------------- source files
@dataclass
class SourceFile:
    rel: str
    lang: str
    text: str
    lines: list[str]
    code: list[str]                     # comment- and string-blanked lines (same columns)
    is_test: bool = False

    @property
    def code_text(self) -> str:
        return "\n".join(self.code)

    def offset_to_line(self, off: int) -> int:
        """1-based line of a character offset into ``text``/``code_text``."""
        return self.text.count("\n", 0, off) + 1


def _excluded(rel: str, globs: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch("/" + rel, g) for g in globs)


def is_test_path(rel: str) -> bool:
    return bool(TEST_DIR.search(rel.rsplit("/", 1)[0] + "/")) if "/" in rel else False


def load_tree(root: Path, exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
              tests_glob: list[str] | None = None) -> dict[str, SourceFile]:
    files: dict[str, SourceFile] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        lang = CODE_EXT.get(p.suffix.lower())
        if not lang or _excluded(rel, exclude):
            continue
        text = p.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
        if lang == "cs":
            code, _ = scan_cs(text)
        elif lang == "vb":
            code, _ = scan_vb(text)
        else:
            code, _ = scan_sql(text)
        # the VB/SQL scanners drop trailing comments; pad so offsets in code == offsets in text
        code = [c.ljust(len(r)) for c, r in zip(code, text.split("\n"))]
        if tests_glob:
            is_test = any(fnmatch.fnmatch(rel, g) for g in tests_glob)
        else:
            is_test = is_test_path(rel) or bool(TEST_ATTR.search(text))
        files[rel] = SourceFile(rel, lang, text, text.split("\n"), code, is_test)
    return files


# --------------------------------------------------------------------------- structure index
@dataclass
class TypeDecl:
    name: str
    kind: str
    file: str
    start: int
    end: int
    bases: list[str] = field(default_factory=list)


@dataclass
class MethodDecl:
    cls: str
    name: str
    file: str
    start: int
    end: int
    kind: str = "Method"

    @property
    def id(self) -> str:
        return f"{self.cls}.{self.name}" if self.cls else self.name


CS_TYPE = re.compile(r"(?<![\w.@])(?P<kind>class|struct|interface|enum|record(?:\s+(?:class|struct))?)"
                     r"\s+(?P<name>[A-Za-z_]\w*)(?:\s*<[^>{]*>)?(?:\s*\([^)]*\))?"
                     r"(?:\s*:\s*(?P<bases>[^{;]+?))?\s*(?:where\b[^{]*)?(?=\{|;|$)", re.M)
CS_MODS = (r"(?:public|private|protected|internal|static|async|virtual|override|sealed|abstract|"
           r"extern|unsafe|new|partial|readonly)")
CS_METHOD = re.compile(
    r"^[ \t]*(?:" + CS_MODS + r"\s+)+(?:(?P<ret>[\w<>\[\],.?()\s]*?[\w>\]?)])\s+)?"
    r"(?P<name>[A-Za-z_]\w*)\s*(?:<[^()]*>)?\s*\(", re.M)
NOT_METHOD = re.compile(r"^\s*(?:return|new|await|throw|using|var|if|while|for|foreach|switch|"
                        r"case|else|lock|yield)\b")
VB_TYPE = re.compile(r"^[ \t]*(?:(?:Public|Private|Friend|Protected|Partial|MustInherit|NotInheritable|"
                     r"Shared)\s+)*(?P<kind>Class|Module|Structure|Interface|Enum)\s+(?P<name>\w+)",
                     re.I | re.M)
VB_METHOD = re.compile(r"^[ \t]*(?:(?:Public|Private|Friend|Protected|Shared|Overrides|Overridable|"
                       r"Overloads|Async|MustOverride|NotOverridable)\s+)*(?P<kind>Sub|Function)\s+"
                       r"(?P<name>\w+)", re.I | re.M)


class Index:
    """Types and methods with line ranges, by file. Built from facts when available."""

    def __init__(self, files: dict[str, SourceFile], facts: dict | None = None):
        self.files = files
        self.types: dict[str, list[TypeDecl]] = {}
        self.methods: dict[str, list[MethodDecl]] = {}
        self.from_facts = False
        if facts:
            self._from_facts(facts)
        for rel, f in files.items():
            if rel in self.types and self.from_facts:
                continue
            if f.lang == "cs":
                self._scan_cs(f)
            elif f.lang == "vb":
                self._scan_vb(f)
        self.types_by_name: dict[str, list[TypeDecl]] = {}
        for ts in self.types.values():
            for t in ts:
                self.types_by_name.setdefault(t.name, []).append(t)
        self.methods_by_name: dict[str, list[MethodDecl]] = {}
        for ms in self.methods.values():
            for m in ms:
                self.methods_by_name.setdefault(m.name, []).append(m)

    # ---- facts
    def _from_facts(self, facts: dict) -> None:
        n = 0
        for sec in ("cs", "vb"):
            s = facts.get(sec) or {}
            for t in s.get("types") or []:
                if t.get("file") in self.files:
                    self.types.setdefault(t["file"], []).append(
                        TypeDecl(t["name"], t.get("kind", "class"), t["file"], int(t["start"]),
                                 int(t["end"]), list(t.get("inherits") or [])))
                    n += 1
            for m in s.get("methods") or []:
                if m.get("file") in self.files and m.get("kind") != "Property":
                    self.methods.setdefault(m["file"], []).append(
                        MethodDecl(m.get("class") or "", m["name"], m["file"], int(m["start"]),
                                   int(m["end"]), m.get("kind", "Method")))
        for rel, v in list(self.methods.items()) + list(self.types.items()):
            # Roslyn spans include leading attributes; start a declaration at its own line
            code = self.files[rel].code
            for m in v:
                while m.start < m.end and (not code[m.start - 1].strip()
                                           or code[m.start - 1].lstrip().startswith(("[", "<"))):
                    m.start += 1
            v.sort(key=lambda m: (m.start, m.end, m.name))
        self.from_facts = n > 0

    # ---- C# scanner
    def _scan_cs(self, f: SourceFile) -> None:
        text = f.code_text
        types: list[TypeDecl] = []
        for m in CS_TYPE.finditer(text):
            line = f.offset_to_line(m.start())
            brace = text.find("{", m.end() - 1)
            semi = text.find(";", m.end() - 1)
            if brace == -1 or (semi != -1 and semi < brace and m.group("kind").startswith("record")):
                end = f.offset_to_line(semi if semi != -1 else m.end())
            else:
                close = balanced(text, brace, "{", "}")
                end = f.offset_to_line(close - 1 if close > 0 else len(text) - 1)
            bases = [b.strip() for b in split_args(m.group("bases") or "") if b.strip()]
            types.append(TypeDecl(m.group("name"), m.group("kind").split()[0], f.rel, line, end, bases))
        self.types[f.rel] = types
        methods: list[MethodDecl] = []
        for m in CS_METHOD.finditer(text):
            line_txt = f.code[f.offset_to_line(m.start()) - 1]
            if NOT_METHOD.match(line_txt) or "=" in text[m.start():m.start("name")]:
                continue
            start = f.offset_to_line(m.start())
            po = text.find("(", m.end("name"))
            pc = balanced(text, po)
            if pc < 0:
                continue
            rest = text[pc:pc + 400]
            mm = re.match(r"\s*(?::\s*(?:base|this)\s*\([^)]*\)\s*)?(?:where\b[^{;=]*)?(\{|=>|;)", rest)
            if not mm:
                continue
            if mm.group(1) == "{":
                ob = pc + mm.end(1) - 1
                close = balanced(text, ob, "{", "}")
                end = f.offset_to_line(close - 1) if close > 0 else start
            elif mm.group(1) == "=>":
                semi = text.find(";", pc + mm.end(1))
                end = f.offset_to_line(semi) if semi != -1 else start
            else:
                end = f.offset_to_line(pc + mm.end(1) - 1)
            cls = self._enclosing_type(types, start)
            kind = "Constructor" if cls and m.group("name") == cls.name else "Method"
            methods.append(MethodDecl(cls.name if cls else "", m.group("name"), f.rel, start, end, kind))
        # expression-bodied / auto properties count as members for "enclosing" purposes only
        self.methods[f.rel] = sorted(methods, key=lambda x: (x.start, x.end))

    def _scan_vb(self, f: SourceFile) -> None:
        text = f.code_text
        types, methods = [], []
        for m in VB_TYPE.finditer(text):
            start = f.offset_to_line(m.start())
            em = re.compile(r"^\s*End\s+" + m.group("kind") + r"\b", re.I | re.M).search(text, m.end())
            end = f.offset_to_line(em.start()) if em else start
            types.append(TypeDecl(m.group("name"), m.group("kind").lower(), f.rel, start, end))
        for m in VB_METHOD.finditer(text):
            if re.search(r"(Exit|End|Declare)\s*$", text[max(0, m.start() - 8):m.start("kind")], re.I):
                continue
            start = f.offset_to_line(m.start())
            em = re.compile(r"^\s*End\s+" + m.group("kind") + r"\b", re.I | re.M).search(text, m.end())
            end = f.offset_to_line(em.start()) if em else start
            cls = self._enclosing_type(types, start)
            methods.append(MethodDecl(cls.name if cls else "", m.group("name"), f.rel, start, end))
        self.types[f.rel] = types
        self.methods[f.rel] = methods

    @staticmethod
    def _enclosing_type(types: list[TypeDecl], line: int) -> TypeDecl | None:
        best = None
        for t in types:
            if t.start <= line <= t.end and (best is None or t.start >= best.start):
                best = t
        return best

    # ---- queries
    def type_at(self, rel: str, line: int) -> TypeDecl | None:
        return self._enclosing_type(self.types.get(rel, []), line)

    def method_at(self, rel: str, line: int) -> MethodDecl | None:
        best = None
        for m in self.methods.get(rel, []):
            if m.start <= line <= m.end and (best is None or m.start >= best.start):
                best = m
        return best

    def where(self, rel: str, line: int) -> tuple[str, str]:
        """(class name, 'Class.Method' or 'Class') of a line."""
        t = self.type_at(rel, line)
        m = self.method_at(rel, line)
        cls = (m.cls if m and m.cls else (t.name if t else Path(rel).stem.split(".")[0]))
        return cls, (f"{cls}.{m.name}" if m else cls)

    def is_entity(self, t: TypeDecl | None) -> bool:
        if t is None:
            return False
        if any(re.search(r"\b(BaseEntity|IAggregateRoot|ValueObject|Entity)\b", b) for b in t.bases):
            return True
        return "/Entities/" in "/" + t.file


# --------------------------------------------------------------------------- candidates
TYPE_PREFIX = {"rule": "BR", "data": "DM", "state": "ST", "workflow": "WF", "exception": "EX",
               "interface": "IF", "permission": "PM"}


@dataclass
class Candidate:
    type: str
    statement: str
    locations: list[dict]
    source: str                         # harvest | tests | docs | llm-draft | planted
    detector: str
    evidence_class: str = "Verified"
    flags: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)     # subject keys used by the merger
    extra: dict = field(default_factory=dict)

    @property
    def source_tag(self) -> str:
        return f"{self.source}:{self.detector}" if self.source == "harvest" else self.source


def loc(rel: str, a: int, b: int | None = None) -> dict:
    return {"file": rel, "start_line": int(a), "end_line": int(b if b is not None else a)}


def load_facts(path: Path | None) -> dict | None:
    if not path:
        return None
    p = Path(path)
    if p.suffix == ".gz":
        import gzip
        return json.loads(gzip.open(p, "rt").read())
    return json.loads(p.read_text())
