#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""export_graph.py — write the run's graph artifacts to ONE predictable place.

The concept graph, the code-dependency graph and the concept<->code bridge used to live only as
side effects (stills/knowledge_graph.json, tables inside stills/shared_vectors.db,
stills/concept_symbols.json). This exports them, deterministically and without LLM calls, to:

    <output>/knowledge_graph/
        index.json               what is here, counts, and the source paths it came from
        knowledge_graph.json     concept graph (NetworkX node-link) — copy of stills/knowledge_graph.json
        code_graph.json          code-dependency graph: source_files, code_objects, code_edges,
                                 code_entities (from shared_vectors.db, written by code_graph.py)
        concept_code_bridge.json concept -> code links, each one a CHECKED assertion (see below)

The bridge join (why it is not just a name match)
-------------------------------------------------
A concept's Statement writes identifiers the way a human would: `h.Vendor`, `inserted.APRef`,
`invoice.APTrans`, `APInvHdr.Job`. The old join called `_norm()` on that and kept only the LAST
dotted segment, so `h.Vendor` became `vendor` and matched any file containing the four letters
"vendor" — including `frmAPInvoiceEntry.Designer.vb`, where `vendor` exists only inside `cboVendor`
and `txtVendor`. That is a plausible but WRONG file, and the link asserted something false.

The join now carries the qualifier and emits a link only when the link's own assertion is TRUE:

  1. `join_name` (the identifier the link claims is in the cited file) must occur there at
     IDENTIFIER BOUNDARIES — so `cboVendor` no longer satisfies a link about `Vendor`.
  2. a qualifier must RESOLVE: to a table/view (`APInvHdr.Job`), to the trigger's table for the
     `inserted.` / `deleted.` pseudo-tables, to a declaring type for an object prefix, or — for a
     business-style qualifier like `invoice.` / `hold.` / a SQL alias `h.` — to the table that
     declares the column, preferring one the cited file is already shown to touch.
  3. the resolution must be CORROBORATED in the cited file: the resolved name occurs there, or the
     code graph connects the file to it, or the deterministic parser records the file touching that
     column. Corroboration that only reaches the column is kept and FLAGGED (`weak_qualifier`),
     never silently upgraded.

Anything that fails 1-3 is DROPPED and recorded in `dropped[]` with a reason, so the gap is
visible and countable instead of becoming a wrong link. Fail closed: no file is ever guessed.

Source text for step 1 comes from, in order: --source-root, `<output>/facts/source` (the staged
tree build_facts.py symlinks), or the reconstructed `<output>/.pages`. With none of them the join
degrades to the old bare-name behaviour, every link is marked `resolution="unverified"` and
`source_available: false` is recorded — a degraded bridge says so rather than claiming checks it
did not run.

Concept keys
------------
Each link's `concept` is the CANONICAL key from concept_key.py (id prefix and `"<Type>: "` prefix
stripped exactly once), which is the same string the concept graph stores on its nodes. The two
artifacts therefore share a key verbatim; nothing downstream has to normalise.

CLI:  export_graph.py <output_dir> [--require-code-graph] [--source-root DIR]
Exit 1 if the concept graph is missing, or (--require-code-graph) the code graph is empty.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import concept_key as ckey

CODE_TABLES = ("source_files", "code_objects", "code_edges", "code_entities")

# Trigger pseudo-tables: `inserted.X` / `deleted.X` name a column of the trigger's own table.
PSEUDO_TABLES = {"inserted", "deleted"}
# Qualifiers that carry no identity of their own (a self reference, not a table or a type).
SELF_QUALIFIERS = {"me", "this", "mybase", "myclass", "self"}
TABLE_KINDS = {"table", "view", "synonym"}
TYPE_KINDS = {"class", "module", "interface", "structure", "struct", "record", "enum", "type"}


def _norm(s: str) -> str:
    """Bare join key: lowercase, drop brackets/quotes, keep only the LAST dotted segment,
    unify - and _. This is the UNQUALIFIED name; see norm_full() for the qualified one."""
    return norm_full(s).split(".")[-1]


def norm_full(s: str) -> str:
    """Qualified join key: the whole symbol, brackets/quotes stripped, '-'->'_', lowercased.
    `[APInvHdr].Job` -> `apinvhdr.job`. The qualifier is KEPT — that is the point."""
    s = re.sub(r"[\[\]'\"`]", "", (s or "")).strip().lower()
    return s.replace("-", "_")


def split_symbol(s: str) -> tuple[str, str]:
    """(qualifier, bare) for a possibly dotted symbol; qualifier is '' when unqualified.
    For `a.b.c` the qualifier is the IMMEDIATE one (`b`), which is the one that names the owner."""
    full = norm_full(s)
    if "." not in full:
        return "", full
    parts = [p for p in full.split(".") if p]
    if len(parts) < 2:
        return "", parts[0] if parts else ""
    return parts[-2], parts[-1]


_WORD_CACHE: dict[tuple[str, str], bool] = {}


def word_re(needle: str) -> re.Pattern:
    """`needle` at identifier boundaries — the test `cboVendor` must fail for `Vendor`."""
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(needle) + r"(?![A-Za-z0-9_])")


# ── source text (for the occurrence checks) ──────────────────────────────────

class SourceText:
    """Lowercased file text for the corpus the run was built from, with memoised lookups.

    Resolution order for a code_graph path: exact relative path, case-insensitive relative path,
    then unique case-insensitive basename. An ambiguous basename is NOT resolved (a wrong file must
    not make a check pass). `available` is False when no source tree could be found at all.
    """

    def __init__(self, root: Path | None, files: dict[str, Path] | None = None):
        self.root = root
        self.files: dict[str, Path] = dict(files or {})
        self._blob: dict[str, str] = {}
        self._by_rel_ci: dict[str, list[str]] = defaultdict(list)
        self._by_base_ci: dict[str, list[str]] = defaultdict(list)
        if root is not None and root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    self.files[p.relative_to(root).as_posix()] = p
        for rel in self.files:
            self._by_rel_ci[rel.lower()].append(rel)
            self._by_base_ci[rel.split("/")[-1].lower()].append(rel)
        self._hit: dict[tuple, bool] = {}

    @property
    def available(self) -> bool:
        return bool(self.files) or bool(self._blob)

    @classmethod
    def discover(cls, output: Path, explicit: str | Path | None = None) -> tuple["SourceText", str]:
        """(SourceText, how) — the first source tree that exists, else an empty one."""
        if explicit:
            p = Path(explicit)
            if p.is_dir():
                return cls(p), f"--source-root {p}"
        staged = output / "facts" / "source"
        if staged.is_dir():
            return cls(staged), str(staged)
        pages = output / ".pages"
        if pages.is_dir():
            files = _reconstruct_from_pages(pages)
            if files:
                st = cls(None)
                st._blob = {k: v.lower() for k, v in files.items()}
                st.files = {}
                for rel in files:
                    st._by_rel_ci[rel.lower()].append(rel)
                    st._by_base_ci[rel.split("/")[-1].lower()].append(rel)
                return st, f"{pages} (reconstructed from page concat)"
        return cls(None), "none"

    def resolve(self, name: str | None) -> str | None:
        if not name:
            return None
        n = str(name).replace("\\", "/").lstrip("./")
        if n in self.files or n in self._blob:
            return n
        for table, key in ((self._by_rel_ci, n.lower()),
                           (self._by_base_ci, n.split("/")[-1].lower())):
            c = table.get(key) or []
            if len(c) == 1:
                return c[0]
        return None

    def text(self, rel: str) -> str:
        if rel in self._blob:
            return self._blob[rel]
        if rel not in self._blob:
            p = self.files.get(rel)
            try:
                self._blob[rel] = p.read_text(errors="replace").lower() if p else ""
            except OSError:
                self._blob[rel] = ""
        return self._blob[rel]

    def occurs(self, rel: str, needle: str, word: bool = False) -> bool:
        needle = (needle or "").lower()
        if not needle:
            return False
        k = (rel, needle, word)
        hit = self._hit.get(k)
        if hit is None:
            body = self.text(rel)
            hit = bool(word_re(needle).search(body)) if word else needle in body
            self._hit[k] = hit
        return hit


_FILE_MARKER = re.compile(r"^\[FILE:\s*(.+?)\]\s*$")
_END_MARKER = re.compile(r"^\[END FILE:")
_LINE_PREFIX = re.compile(r"^\s*\d+\s\|\s?")
_MARKER_ATTR = re.compile(
    r"\s+(?:KIND:|SIZE:|VIA:|\[PART\b|ORIGINAL_LINE_START:|LINE_SEGMENT:|REASON:)")


def _reconstruct_from_pages(pages_dir: Path) -> dict[str, str]:
    """{rel_path: text} for every [FILE:] segment in the page concats (last resort source).

    Deliberately duplicated from code_graph.parse_file_marker rather than imported: importing
    code_graph pulls in bedrock_client/boto3, and this export must stay dependency-free.
    """
    out: dict[str, list[str]] = defaultdict(list)
    for page in sorted(pages_dir.glob("page_*")):
        concat = page / "concat.txt"
        if not concat.is_file():
            continue
        cur: str | None = None
        for ln in concat.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _FILE_MARKER.match(ln)
            if m:
                inner = m.group(1)
                am = _MARKER_ATTR.search(inner)
                cur = (inner[:am.start()] if am else inner).strip()
                continue
            if _END_MARKER.match(ln):
                cur = None
                continue
            if cur:
                out[cur].append(_LINE_PREFIX.sub("", ln))
    return {k: "\n".join(v) for k, v in out.items() if v}


# ── qualifier resolution ─────────────────────────────────────────────────────

class QualifierResolver:
    """Resolves the qualifier of a dotted concept symbol to the thing that OWNS the bare name.

    Knowledge comes from (in order of authority) the deterministic parser facts
    (`<output>/facts/facts.json`), then the code graph's own objects and edges. Nothing here
    guesses a file: it answers "what does `h.` / `inserted.` / `invoice.` / `APInvHdr.` name?" and
    "is that corroborated in THIS file?", and the caller drops the link when the answer is no.
    """

    def __init__(self, facts: dict | None, code: dict, src: SourceText):
        self.src = src
        self.tables: dict[str, str] = {}              # lower -> canonical table name
        self.table_columns: dict[str, set[str]] = defaultdict(set)   # table lower -> cols lower
        self.column_tables: dict[str, set[str]] = defaultdict(set)   # col lower -> table names
        self.types: dict[str, str] = {}               # lower -> canonical type name
        self.member_type: dict[str, str] = {}         # member/local lower -> type name
        self.trigger_table_by_file: dict[str, str] = {}
        self.trigger_tables: set[str] = set()
        self.file_tables: dict[str, set[str]] = defaultdict(set)     # file -> table names touched
        self.file_columns: dict[str, set[str]] = defaultdict(set)    # file -> cols lower touched
        self.facts_present = bool(facts)

        f = facts or {}
        for t in (f.get("sql") or {}).get("tables") or []:
            name = t.get("name")
            if not name:
                continue
            self.tables[name.lower()] = name
            for c in t.get("columns") or []:
                cn = (c.get("name") or "").lower()
                if cn:
                    self.table_columns[name.lower()].add(cn)
                    self.column_tables[cn].add(name)
            if t.get("file"):
                self.file_tables[t["file"]].add(name)
        for t in (f.get("sql") or {}).get("triggers") or []:
            if t.get("table"):
                self.trigger_tables.add(t["table"])
                if t.get("file"):
                    self.trigger_table_by_file[t["file"]] = t["table"]
        # CLR types and the declared type of each field/property (an object prefix resolves here)
        for sect in ("vb", "cs"):
            s = f.get(sect) or {}
            for ty in s.get("types") or []:
                if ty.get("name"):
                    self.types[ty["name"].lower()] = ty["name"]
            for m in (s.get("members") or []) + (s.get("methods") or []):
                nm, ty = m.get("name"), m.get("type")
                if nm and ty:
                    self.member_type.setdefault(nm.lower(), ty)
            for cr in s.get("column_refs") or []:
                if cr.get("file") and cr.get("name"):
                    self.file_columns[cr["file"]].add(cr["name"].lower())
            for isql in s.get("inline_sql") or []:
                for key in ("reads", "writes"):
                    for x in isql.get(key) or []:
                        if isql.get("file") and x.get("table"):
                            self.file_tables[isql["file"]].add(x["table"])
                for c in isql.get("columns") or []:
                    if isql.get("file") and c.get("column"):
                        self.file_columns[isql["file"]].add(c["column"].lower())
        # SQL units: tables and columns each FILE touches
        for group in ("procs", "triggers", "scripts"):
            for u in (f.get("sql") or {}).get(group) or []:
                fl = u.get("file")
                if not fl:
                    continue
                for key in ("reads", "writes"):
                    for x in u.get(key) or []:
                        if x.get("table"):
                            self.file_tables[fl].add(x["table"])
                for c in u.get("columns") or []:
                    if c.get("column"):
                        self.file_columns[fl].add(c["column"].lower())
                    if c.get("table"):
                        self.file_tables[fl].add(c["table"])
        # the code graph itself: declared objects and the edges out of each file
        for o in code.get("code_objects") or []:
            kind = (o.get("kind") or "").lower()
            bare = _norm(o.get("object"))
            if not bare:
                continue
            if kind in TABLE_KINDS:
                self.tables.setdefault(bare, o.get("object") or bare)
            elif kind in TYPE_KINDS:
                self.types.setdefault(bare, o.get("object") or bare)
        for e in code.get("code_edges") or []:
            if e.get("src") and e.get("dst"):
                self.file_tables[str(e["src"])].add(str(e["dst"]))

    # -- helpers ------------------------------------------------------------
    def _touches_name(self, file_rel: str, graph_path: str, name: str) -> bool:
        """Is `name` corroborated for this file — named in it, or linked to it by the graph/parser?"""
        bare = _norm(name)
        for key in {file_rel, graph_path}:
            for t in self.file_tables.get(key, ()):
                if _norm(t) == bare:
                    return True
        return self.src.occurs(file_rel, bare, word=True) if self.src.available else False

    def _touches_column(self, file_rel: str, graph_path: str, col: str) -> bool:
        c = (col or "").lower()
        return any(c in self.file_columns.get(k, ()) for k in {file_rel, graph_path})

    # -- the resolution -----------------------------------------------------
    def resolve(self, qual: str, bare: str, file_rel: str, graph_path: str) -> dict | None:
        """{kind, name, corroboration} for the qualifier, or None when it cannot be resolved.

        `corroboration` is "named" (the resolved name is in the file or linked to it by the graph)
        or "column" (only the column is corroborated — kept, but the caller flags it)."""
        q = (qual or "").lower()
        if not q or q in SELF_QUALIFIERS:
            return None

        cands: list[tuple[str, str]] = []            # (kind, canonical name)
        if q in PSEUDO_TABLES:
            # `inserted.X` / `deleted.X` -> the trigger's own table
            own = self.trigger_table_by_file.get(graph_path) or self.trigger_table_by_file.get(file_rel)
            if own:
                cands.append(("trigger_table", own))
            for t in sorted(self.trigger_tables):
                if bare in self.table_columns.get(t.lower(), set()) or not self.table_columns:
                    cands.append(("trigger_table", t))
        if q in self.tables:
            cands.append(("table", self.tables[q]))
        if q in self.types:
            cands.append(("type", self.types[q]))
        if q in self.member_type:
            ty = self.member_type[q]
            cands.append(("type", self.types.get(ty.lower(), ty)))
        # a business-style qualifier or a SQL alias (`invoice.`, `hold.`, `h.`): the owner is
        # whichever table DECLARES this column; the cited file decides between several.
        for t in sorted(self.column_tables.get(bare, ())):
            cands.append(("table_by_column", t))

        if not cands:
            return None
        # prefer a candidate the file is shown to touch; keep the order deterministic
        seen: set[tuple[str, str]] = set()
        ordered = [c for c in cands if not (c in seen or seen.add(c))]
        for kind, name in ordered:
            if self._touches_name(file_rel, graph_path, name):
                return {"kind": kind, "name": name, "corroboration": "named"}
        if self._touches_column(file_rel, graph_path, bare):
            kind, name = ordered[0]
            return {"kind": kind, "name": name, "corroboration": "column"}
        return None


# ── reading the run ─────────────────────────────────────────────────────────

def _read_tables(db: Path) -> dict:
    out = {t: [] for t in CODE_TABLES}
    if not db.is_file():
        return out
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in CODE_TABLES:
            if t in have:
                out[t] = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
    finally:
        conn.close()
    return out


def _read_facts(output: Path) -> dict | None:
    p = output / "facts" / "facts.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return None


# ── the bridge ──────────────────────────────────────────────────────────────

def _canonical_concept_keys(concept_symbols: dict) -> dict[str, str]:
    """index key -> CANONICAL key. Uses the stored `canonical_key` when the index has one (new
    runs) and derives it with the same function when it does not (runs made before concept_key.py).
    """
    out = {}
    for k, info in (concept_symbols.get("by_concept") or {}).items():
        canon = (info or {}).get("canonical_key")
        out[k] = canon or ckey.canonicalize_key(k)
    return out


def build_bridge(concept_symbols: dict, code: dict, src: SourceText | None = None,
                 facts: dict | None = None) -> tuple[list[dict], list[dict], dict]:
    """(links, dropped, stats).

    A link is an assertion: "concept C refers to `symbol`, whose identifier `join_name` occurs in
    `code_path`". Every emitted link has been checked against the source; everything that could
    not be checked or did not hold is in `dropped` with a reason, never silently emitted.
    """
    src = src if src is not None else SourceText(None)
    verify = src.available
    resolver = QualifierResolver(facts, code, src)

    def _index(rows, name_key) -> dict[str, list[dict]]:
        """Index each row under its bare name AND, when it is qualified, its full name — so a
        concept that writes `dbo.APInvHdr` matches the object exactly instead of by bare name."""
        idx: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            bare, full = _norm(r.get(name_key)), norm_full(r.get(name_key))
            if bare:
                idx[bare].append(r)
            if full and full != bare:
                idx[full].append(r)
        return idx

    objects = _index(code.get("code_objects", []), "object")
    entities = _index(code.get("code_entities", []), "code_name")

    canon = _canonical_concept_keys(concept_symbols)
    links: list[dict] = []
    dropped: list[dict] = []
    stats: dict[str, int] = defaultdict(int)

    def candidates(key: str) -> list[tuple[str, dict]]:
        return ([("code_object", o) for o in objects.get(key, [])]
                + [("code_entity", e) for e in entities.get(key, [])])

    for sym, concepts in sorted((concept_symbols or {}).get("by_symbol", {}).items()):
        qual, bare = split_symbol(sym)
        full = norm_full(sym)
        # An exactly-qualified hit (the graph really has an object called `dbo.APInvHdr`) is the
        # strongest match and is tried first; only then the bare name.
        rows = candidates(full) if qual else []
        exact_qualified = bool(rows)
        if not rows:
            rows = candidates(bare)
        if not rows:
            stats["symbols_with_no_code_match"] += 1
            continue
        stats["symbols_joined"] += 1
        for match, row in rows:
            path = row.get("path")
            join_name = row.get("object") if match == "code_object" else row.get("code_name")
            base = {"symbol": sym, "qualifier": qual or None, "join_name": join_name,
                    "match": match, "code_path": path,
                    "code_name": row.get("object") if match == "code_object" else row.get("code_name")}
            if match == "code_object":
                base["kind"] = row.get("kind")
            else:
                base["business_name"] = row.get("business_name")

            if not verify:
                # No source tree: emit, but say plainly that nothing was verified.
                for c in concepts:
                    links.append({"concept": canon.get(c, c), "concept_index_key": c,
                                  **base, "resolution": "unverified"})
                stats["links_unverified"] += len(concepts)
                continue

            rel = src.resolve(path)
            if rel is None:
                dropped.append({**base, "concepts": len(concepts), "reason": "file_unresolved",
                                "detail": f"{path!r} does not resolve to one file under the source root"})
                stats["dropped_file_unresolved"] += 1
                continue

            needle = _norm(join_name)
            if exact_qualified and src.occurs(rel, norm_full(join_name)):
                resolution, flag = "qualified_object", None
            elif src.occurs(rel, full):
                resolution, flag = "verbatim", None
            elif not src.occurs(rel, needle, word=True):
                reason = "substring_only" if src.occurs(rel, needle) else "name_absent"
                dropped.append({**base, "concepts": len(concepts), "reason": reason,
                                "detail": (f"{needle!r} occurs in {rel} only inside a longer "
                                           "identifier" if reason == "substring_only"
                                           else f"{needle!r} does not occur in {rel}")})
                stats[f"dropped_{reason}"] += 1
                continue
            elif not qual:
                resolution, flag = "bare", None
            else:
                q = resolver.resolve(qual, needle, rel, str(path))
                if q is None:
                    dropped.append({**base, "concepts": len(concepts),
                                    "reason": "qualifier_unresolved",
                                    "detail": (f"qualifier {qual!r} names no table, trigger "
                                               f"pseudo-table or declaring type corroborated in {rel}")})
                    stats["dropped_qualifier_unresolved"] += 1
                    continue
                resolution = f"qualifier_{q['kind']}"
                base["qualifier_resolved_to"] = q["name"]
                base["qualifier_corroboration"] = q["corroboration"]
                flag = "weak_qualifier" if q["corroboration"] == "column" else None
            stats[f"resolution_{resolution}"] += 1
            if flag:
                stats[f"flag_{flag}"] += len(concepts)
            for c in concepts:
                lk = {"concept": canon.get(c, c), "concept_index_key": c, **base,
                      "resolution": resolution}
                if flag:
                    lk["flags"] = [flag]
                links.append(lk)
    return links, dropped, dict(stats)


def export(output_dir: str | Path, source_root: str | Path | None = None) -> dict:
    out = Path(output_dir)
    stills = out / "stills"
    dest = out / "knowledge_graph"
    dest.mkdir(parents=True, exist_ok=True)
    kg_src = stills / "knowledge_graph.json"
    db = stills / "shared_vectors.db"
    cs_src = stills / "concept_symbols.json"

    src, how = SourceText.discover(out, source_root)
    facts = _read_facts(out)
    index: dict = {"output_dir": str(out), "artifacts": {}, "sources": {
        "knowledge_graph": str(kg_src), "shared_vectors_db": str(db),
        "concept_symbols": str(cs_src), "source_text": how,
        "parser_facts": str(out / "facts" / "facts.json") if facts else None}}

    if kg_src.is_file():
        shutil.copyfile(kg_src, dest / "knowledge_graph.json")
        try:
            kg = json.loads(kg_src.read_text(encoding="utf-8"))
            n_nodes = len(kg.get("nodes", []))
            n_edges = len(kg.get("links", kg.get("edges", [])))
        except Exception:
            n_nodes = n_edges = None
        index["artifacts"]["knowledge_graph"] = {"path": str(dest / "knowledge_graph.json"),
                                                 "nodes": n_nodes, "edges": n_edges}

    code = _read_tables(db)
    (dest / "code_graph.json").write_text(json.dumps(code, indent=1, default=str), encoding="utf-8")
    index["artifacts"]["code_graph"] = {"path": str(dest / "code_graph.json"),
                                        **{t: len(code[t]) for t in CODE_TABLES}}

    concept_symbols = {}
    if cs_src.is_file():
        try:
            concept_symbols = json.loads(cs_src.read_text(encoding="utf-8"))
        except Exception:
            concept_symbols = {}
    links, dropped, stats = build_bridge(concept_symbols, code, src, facts)
    (dest / "concept_code_bridge.json").write_text(
        json.dumps({"links": links, "dropped": dropped,
                    "concepts_with_symbols": len(concept_symbols.get("by_concept", {})),
                    "distinct_symbols": len(concept_symbols.get("by_symbol", {})),
                    "concept_key": "canonical (runtimes/mcp-server/concept_key.py)",
                    "source_text": how, "source_available": src.available,
                    "parser_facts_used": bool(facts), "join_stats": stats},
                   indent=1), encoding="utf-8")
    index["artifacts"]["concept_code_bridge"] = {
        "path": str(dest / "concept_code_bridge.json"), "links": len(links),
        "dropped": len(dropped),
        "concepts_linked": len({l["concept"] for l in links}),
        "concept_symbols_present": cs_src.is_file(),
        "source_available": src.available, "parser_facts_used": bool(facts)}
    (dest / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description="Export graph artifacts to <output>/knowledge_graph/")
    ap.add_argument("output_dir")
    ap.add_argument("--require-code-graph", action="store_true")
    ap.add_argument("--source-root", default="",
                    help="corpus the run was built from (default: <output>/facts/source, "
                         "else the reconstructed <output>/.pages)")
    args = ap.parse_args()
    idx = export(args.output_dir, args.source_root or None)
    print(json.dumps(idx["artifacts"], indent=2))
    br = idx["artifacts"].get("concept_code_bridge") or {}
    if br and not br.get("source_available"):
        print("[export_graph] WARNING: no source tree found — bridge links are UNVERIFIED "
              "(resolution=\"unverified\"); pass --source-root to check them.", file=sys.stderr)
    if "knowledge_graph" not in idx["artifacts"]:
        print("[export_graph] ERROR: stills/knowledge_graph.json missing — no concept graph",
              file=sys.stderr)
        return 1
    if args.require_code_graph and not idx["artifacts"]["code_graph"]["source_files"]:
        print("[export_graph] ERROR: code graph is empty (no source_files rows)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
