# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Load and normalise everything graph_eval needs, tolerating absent/partial artifacts.

A run directory produced by run_distillation.sh is expected to contain:

    <output>/knowledge_graph/knowledge_graph.json      concept graph (NetworkX node-link)
    <output>/knowledge_graph/code_graph.json           source_files/code_objects/code_edges/code_entities
    <output>/knowledge_graph/concept_code_bridge.json  concept <-> code symbol links
    <output>/knowledge_graph/index.json                counts + source paths
    <output>/facts/facts.json                          deterministic parser facts   (optional)
    <output>/facts/graph.json                          deterministic parser graph   (optional)
    <output>/.enumeration/source_inventory.json        per-file sha256 of the scanned source (optional)
    <output>/run_manifest.json                         run identity incl. source_inventory_hash (optional)

Nothing here raises on a missing optional artifact: the absence is recorded in
`GraphBundle.notes` and the dependent section degrades (and says so) instead of failing.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

CODE_TABLES = ("source_files", "code_objects", "code_edges", "code_entities")

# The canonical concept key is defined ONCE, by the pipeline, in
# runtimes/mcp-server/concept_key.py — the same module concept_symbols.py and knowledge.py use to
# WRITE the key. graph_eval imports it rather than keeping a second copy, because a scorer with its
# own private idea of the key cannot tell "the artifacts agree" from "my regex happens to agree
# with both". If the import ever breaks, that is a real failure and should be loud.
_MCP_DIR = Path(__file__).resolve().parents[2] / "runtimes" / "mcp-server"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))
try:
    import concept_key as ckey
except ImportError as _exc:  # pragma: no cover - layout guarantees this import
    raise ImportError(
        f"graph_eval needs the pipeline's canonical concept key from {_MCP_DIR}/concept_key.py "
        f"(the same definition concept_symbols.py writes); it is not importable: {_exc}") from _exc


class ArtifactError(Exception):
    """A required artifact is missing or unreadable."""


# ----------------------------------------------------------------------------- names
def bare_name(s: str | None) -> str:
    """Join key used across the pipeline: strip brackets/quotes, drop the schema prefix,
    unify '-' and '_', casefold. Mirrors export_graph._norm + code_graph._present."""
    s = re.sub(r"[\[\]'\"`]", "", (s or "")).strip().casefold()
    return s.split(".")[-1].replace("-", "_")


def norm_full(s: str | None) -> str:
    """Casefolded symbol with brackets/quotes stripped but the dotted qualifier kept."""
    return re.sub(r"[\[\]'\"`]", "", (s or "")).strip().casefold()


ID_PREFIX = ckey.ID_PREFIX
# "Data Invariant: ", "Rule: ", "Entity: ", "Obligation: ", "Edge Case: ", "Decision: " …
TYPE_PREFIX = ckey.TYPE_PREFIX


def _title_forms(title: str) -> dict[str, str]:
    """The three spellings of one concept title, from strictest to loosest.

    raw        exactly as written
    no_id      the assigned "[DT-0021] " id prefix removed
    canonical  id prefix AND the leading "<Type>: " prefix removed, casefolded
               (== concept_key.canonical_title — the pipeline's own definition)
    """
    t = (title or "").strip()
    return {"raw": t, "no_id": ckey.strip_id(t), "canonical": ckey.canonical_title(t)}


class ConceptResolver:
    """Resolve a bridge link's concept key onto concept-graph node ids, reporting HOW it matched.

    The two artifacts now SHARE a key. `concept_key.py` defines one canonical spelling (the assigned
    `[DT-0021] ` id prefix and the leading `"<Type>: "` label stripped exactly once, casefolded);
    `concept_symbols.py` stores it on every concept, `knowledge.py` stores it on every graph node,
    and `export_graph.py` puts it on every bridge link. So the normal outcome is `exact`: the two
    artifacts agree on a key by equality, with no ladder involved.

    The looser tiers remain, because a run produced before the shared key existed still has to be
    scorable — and because an `id_stripped`/`canonical` match on a NEW run is a regression signal
    (something in the chain stopped writing the canonical key):

      exact        the bridge key IS a node's canonical key (or its raw title) verbatim
      id_stripped  matched only after removing the "[DT-NNNN] " prefix   (legacy / drifted)
      canonical    matched only after also removing "<Type>: " and casefolding (legacy / drifted)
      unresolved   no node in the graph carries this concept under any of those spellings
    """

    TIERS = ("exact", "id_stripped", "canonical", "unresolved")

    def __init__(self, nodes: list[dict]):
        self.exact: dict[str, list[str]] = {}
        self.no_id: dict[str, list[str]] = {}
        self.canonical: dict[str, list[str]] = {}
        for n in nodes:
            nid = n.get("id")
            forms = _title_forms(n.get("title") or "")
            stills = n.get("source_stills") or ([n["source_still"]] if n.get("source_still") else [])
            # The node's own canonical key(s): stored by knowledge.py when the run has them,
            # otherwise derived here with the SAME function the writer uses. Either way the key is
            # the pipeline's, not the scorer's — so `exact` means the artifacts really agree.
            stored = n.get("canonical_keys") or ([n["canonical_key"]] if n.get("canonical_key") else [])
            derived = ([ckey.concept_key(s, forms["raw"]) for s in stills]
                       or [ckey.concept_key("", forms["raw"])]) if forms["raw"] else []
            for k in list(stored) + derived:
                if k:
                    self.exact.setdefault(k, []).append(nid)
            if not forms["raw"]:
                continue
            for table, key in ((self.exact, forms["raw"]), (self.no_id, forms["no_id"]),
                               (self.canonical, forms["canonical"])):
                if not key:
                    continue
                table.setdefault(key, []).append(nid)
                for still in stills:
                    table.setdefault(f"{still}::{key}", []).append(nid)

    def resolve(self, concept_key: str) -> tuple[str, list[str]]:
        k = (concept_key or "").strip()
        still, title = ckey.split_key(k)
        forms = _title_forms(title)
        for tier, table, key in (
            ("exact", self.exact, forms["raw"]),
            ("id_stripped", self.no_id, forms["no_id"]),
            ("canonical", self.canonical, forms["canonical"]),
        ):
            for probe in ([f"{still}{ckey.KEY_SEP}{key}", key] if still else [key]):
                hit = table.get(probe)
                if hit:
                    return tier, hit
        return "unresolved", []


def concept_key_variants(concept_key: str) -> list[str]:
    """Flat list of spellings to try for a concept key (kept for simple lookups)."""
    k = (concept_key or "").strip()
    still, title = ckey.split_key(k)
    forms = _title_forms(title)
    out, seen = [], set()
    for key in (forms["raw"], forms["no_id"], forms["canonical"]):
        for v in ([f"{still}{ckey.KEY_SEP}{key}", key] if still else [key]):
            v = v.strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


def word_re(needle: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(needle) + r"(?![A-Za-z0-9_])")


def _strip_path_prefix(n: str) -> str:
    """Remove leading ``./`` segments and leading ``/``; leave dots that belong to a name."""
    while True:
        if n.startswith("./"):
            n = n[2:]
        elif n.startswith("/"):
            n = n[1:]
        else:
            return n


# ----------------------------------------------------------------------------- source root
@dataclass
class SourceIndex:
    """Case-aware index of the source tree, with memoised occurrence tests.

    Resolution order: exact relative path, then case-insensitive relative path, then
    case-insensitive basename. Any step that matches more than one distinct file is
    reported as `ambiguous` rather than silently resolved — a case-insensitive filename
    collision (common when a Windows-era corpus lands on a case-sensitive filesystem)
    must not quietly make a check pass against the wrong file.
    """

    root: Path
    files: dict[str, Path] = field(default_factory=dict)
    by_rel_ci: dict[str, list[str]] = field(default_factory=dict)
    by_base_ci: dict[str, list[str]] = field(default_factory=dict)
    _text: dict[str, str] = field(default_factory=dict, repr=False)
    _lines: dict[str, int] = field(default_factory=dict, repr=False)
    _hit: dict[tuple, bool] = field(default_factory=dict, repr=False)

    @classmethod
    def build(cls, root: Path, exclude: list[str] | None = None) -> "SourceIndex":
        from fnmatch import fnmatch

        idx = cls(root=root)
        if not root.is_dir():
            return idx
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if exclude and any(fnmatch(rel, g) or fnmatch(p.name, g) for g in exclude):
                continue
            idx.files[rel] = p
            idx.by_rel_ci.setdefault(rel.casefold(), []).append(rel)
            idx.by_base_ci.setdefault(p.name.casefold(), []).append(rel)
        return idx

    @property
    def case_collisions(self) -> list[list[str]]:
        """Groups of files whose relative paths differ only by case."""
        return [v for v in self.by_rel_ci.values() if len(v) > 1]

    @property
    def basename_collisions(self) -> list[list[str]]:
        """Groups of files sharing a basename (case-insensitively) in different directories."""
        return [v for v in self.by_base_ci.values() if len(v) > 1]

    def resolve(self, name: str | None) -> tuple[str | None, str]:
        if not name:
            return None, "empty path"
        n = str(name).replace("\\", "/")
        # Strip leading "./" and "/" as PREFIXES. ``str.lstrip("./")`` strips a set of
        # characters, so it also ate the leading dot of ".gitignore" / ".gitattributes"
        # and reported those inventory files as absent from the source root.
        n = _strip_path_prefix(n)
        if n in self.files:
            return n, "exact"
        c = self.by_rel_ci.get(n.casefold(), [])
        if len(c) == 1:
            return c[0], "case-insensitive path"
        if len(c) > 1:
            return None, f"ambiguous (case-insensitive collision: {', '.join(sorted(c))})"
        base = n.split("/")[-1].casefold()
        c = self.by_base_ci.get(base, [])
        if len(c) == 1:
            return c[0], "basename"
        if len(c) > 1:
            return None, f"ambiguous basename ({', '.join(sorted(c))})"
        return None, "file not found"

    def text(self, rel: str) -> str:
        if rel not in self._text:
            try:
                self._text[rel] = self.files[rel].read_text(errors="replace").casefold()
            except (OSError, KeyError):
                self._text[rel] = ""
        return self._text[rel]

    def line_count(self, rel: str) -> int:
        if rel not in self._lines:
            try:
                self._lines[rel] = len(self.files[rel].read_text(errors="replace").splitlines())
            except (OSError, KeyError):
                self._lines[rel] = 0
        return self._lines[rel]

    def occurs(self, rel: str, needle: str, word: bool = False) -> bool:
        """Memoised case-insensitive occurrence test; `word` requires identifier boundaries."""
        needle = (needle or "").casefold()
        if not needle:
            return False
        k = (rel, needle, word)
        hit = self._hit.get(k)
        if hit is None:
            body = self.text(rel)
            hit = bool(word_re(needle).search(body)) if word else needle in body
            self._hit[k] = hit
        return hit


# ----------------------------------------------------------------------------- provenance
@dataclass
class Provenance:
    """Run identity vs the source tree being scored against.

    `status` is one of
      ok        every inventory entry exists at --source-root with the recorded sha256
      mismatch  at least one entry is missing or has a different digest => REFUSE to score
      unknown   no source_inventory.json in the run (older run) => cannot prove, warn only
    """

    status: str = "unknown"
    detail: str = ""
    inventory_hash: str | None = None
    manifest_inventory_hash: str | None = None
    source_identity: str | None = None
    file_count: int | None = None
    verified: int = 0
    missing: list[str] = field(default_factory=list)
    digest_mismatch: list[dict] = field(default_factory=list)
    untracked_in_source_root: list[str] = field(default_factory=list)

    @property
    def refuse(self) -> bool:
        return self.status == "mismatch"


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_provenance(output_dir: Path, src: SourceIndex) -> Provenance:
    inv_path = output_dir / ".enumeration" / "source_inventory.json"
    man_path = output_dir / "run_manifest.json"
    prov = Provenance()
    manifest_hash = None
    if man_path.is_file():
        try:
            man = json.loads(man_path.read_text())
            manifest_hash = (man.get("run_identity") or {}).get("source_inventory_hash")
        except (OSError, ValueError):
            manifest_hash = None
    prov.manifest_inventory_hash = manifest_hash

    if not inv_path.is_file():
        prov.detail = f"{inv_path} absent — cannot prove the code graph was built from this source tree"
        return prov
    try:
        inv = json.loads(inv_path.read_text())
    except (OSError, ValueError) as exc:
        prov.detail = f"{inv_path} unreadable: {exc}"
        return prov

    prov.inventory_hash = inv.get("inventory_hash")
    prov.source_identity = inv.get("source_identity")
    prov.file_count = inv.get("file_count")
    entries = inv.get("entries") or []
    if manifest_hash and prov.inventory_hash and manifest_hash != prov.inventory_hash:
        prov.status = "mismatch"
        prov.detail = ("run_manifest.source_inventory_hash != .enumeration inventory_hash "
                       f"({manifest_hash} != {prov.inventory_hash}) — the run directory is internally "
                       "inconsistent (artifacts from two different runs?)")
        return prov
    if not src.files:
        prov.detail = f"source root {src.root} is empty or missing"
        return prov

    tracked = set()
    for e in entries:
        rel = str(e.get("path", "")).replace("\\", "/")
        want = e.get("sha256")
        resolved, _why = src.resolve(rel)
        if resolved is None:
            prov.missing.append(rel)
            continue
        tracked.add(resolved)
        if not isinstance(want, str) or want.startswith("unreadable:"):
            prov.verified += 1       # the run itself could not digest it; nothing to compare
            continue
        got = _sha256_file(src.files[resolved])
        if got != want:
            prov.digest_mismatch.append({"path": rel, "expected": want, "actual": got})
        else:
            prov.verified += 1
    prov.untracked_in_source_root = sorted(set(src.files) - tracked)

    if prov.missing or prov.digest_mismatch:
        prov.status = "mismatch"
        bits = []
        if prov.missing:
            bits.append(f"{len(prov.missing)} inventory file(s) absent from the source root")
        if prov.digest_mismatch:
            bits.append(f"{len(prov.digest_mismatch)} file(s) with a different sha256")
        prov.detail = ("the run was built from a different source revision: " + "; ".join(bits))
    else:
        prov.status = "ok"
        prov.detail = (f"{prov.verified}/{len(entries)} inventory entries verified by sha256"
                       + (f"; {len(prov.untracked_in_source_root)} file(s) under the source root are not in "
                          "the inventory (excluded by the enumeration rules, or added since the run)"
                          if prov.untracked_in_source_root else ""))
    return prov


# ----------------------------------------------------------------------------- concept bodies
def read_concept_bodies(output_dir: Path) -> tuple[dict[str, str], str]:
    """Full concept bodies, needed to extract citations.

    knowledge_graph.json keeps only a 300-char `body_preview` per observation block and no body at
    all for unified concepts, so the citations in a trailing `Evidence:` line are usually truncated
    away. The run's shared vector DB keeps the full text, so prefer it.

    Returns ({node_id: body}, source_label).
    """
    db = output_dir / "stills" / "shared_vectors.db"
    if not db.is_file():
        return {}, "absent"
    out: dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}, "absent"
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "unified_concepts" in have:
            for cid, body in conn.execute("SELECT concept_id, body_raw FROM unified_concepts"):
                if body:
                    out[cid] = body
        if "vectors" in have:
            try:
                for bid, body in conn.execute("SELECT block_id, body FROM vectors"):
                    if body:
                        out[bid] = body
            except sqlite3.Error:
                pass
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out, ("shared_vectors.db" if out else "absent")


# ----------------------------------------------------------------------------- bundle
@dataclass
class GraphBundle:
    output_dir: Path
    source_root: Path
    src: SourceIndex
    provenance: Provenance
    index: dict = field(default_factory=dict)
    kg_nodes: list[dict] = field(default_factory=list)
    kg_edges: list[dict] = field(default_factory=list)
    code_files: list[dict] = field(default_factory=list)
    code_objects: list[dict] = field(default_factory=list)
    code_edges: list[dict] = field(default_factory=list)
    code_entities: list[dict] = field(default_factory=list)
    bridge_links: list[dict] = field(default_factory=list)
    bridge_meta: dict = field(default_factory=dict)
    parser_graph: dict | None = None
    facts_present: bool = False
    concept_bodies: dict[str, str] = field(default_factory=dict)
    concept_bodies_source: str = "absent"
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.kg_nodes or self.code_files or self.code_objects
                    or self.code_edges or self.code_entities or self.bridge_links)

    def counts(self) -> dict:
        return {
            "concept_nodes": len(self.kg_nodes), "concept_edges": len(self.kg_edges),
            "code_source_files": len(self.code_files), "code_objects": len(self.code_objects),
            "code_edges": len(self.code_edges), "code_entities": len(self.code_entities),
            "bridge_links": len(self.bridge_links),
            "source_files_on_disk": len(self.src.files),
            "parser_graph_nodes": len((self.parser_graph or {}).get("nodes") or []),
            "parser_graph_edges": len((self.parser_graph or {}).get("edges") or []),
        }


def _read_json(p: Path, notes: list[str], what: str):
    if not p.is_file():
        notes.append(f"{what}: {p} absent")
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except ValueError as exc:
        notes.append(f"{what}: {p} is not valid JSON ({exc})")
        return None


def load_bundle(output_dir: str | Path, source_root: str | Path,
                exclude: list[str] | None = None,
                concept_bodies: str = "auto") -> GraphBundle:
    """Read a run's graph artifacts. Raises ArtifactError only when <output>/knowledge_graph/
    does not exist at all — every other absence degrades a section."""
    out = Path(output_dir)
    kgdir = out / "knowledge_graph"
    if not kgdir.is_dir():
        raise ArtifactError(
            f"{kgdir} does not exist — this run has no exported knowledge graph. "
            f"Run `python3 runtimes/mcp-server/export_graph.py {out}` after a distillation run.")

    notes: list[str] = []
    src = SourceIndex.build(Path(source_root), exclude)
    if not src.files:
        notes.append(f"source root {source_root} is empty or missing — every anchor check will fail")

    kg = _read_json(kgdir / "knowledge_graph.json", notes, "concept graph") or {}
    code = _read_json(kgdir / "code_graph.json", notes, "code graph") or {}
    bridge = _read_json(kgdir / "concept_code_bridge.json", notes, "bridge") or {}
    index = _read_json(kgdir / "index.json", notes, "index") or {}

    kg_nodes = kg.get("nodes") or []
    kg_edges = kg.get("links") if isinstance(kg.get("links"), list) else (kg.get("edges") or [])

    facts_json = out / "facts" / "facts.json"
    parser_graph = _read_json(out / "facts" / "graph.json", notes, "parser graph")
    if parser_graph is not None and not isinstance(parser_graph.get("edges"), list):
        notes.append("parser graph: no `edges` list — treating as absent")
        parser_graph = None

    bodies: dict[str, str] = {}
    bsrc = "preview"
    if concept_bodies in ("auto", "db"):
        bodies, bsrc = read_concept_bodies(out)
        if not bodies:
            bsrc = "preview"
            if concept_bodies == "db":
                notes.append("--concept-bodies db requested but stills/shared_vectors.db has no bodies")
    if bsrc == "preview":
        notes.append("concept bodies taken from knowledge_graph.json `body_preview` (300 chars) — "
                     "citations beyond the preview are invisible, so concept coverage is a LOWER BOUND")
        for n in kg_nodes:
            if n.get("body_preview"):
                bodies[n["id"]] = n["body_preview"]

    bundle = GraphBundle(
        output_dir=out, source_root=Path(source_root), src=src,
        provenance=read_provenance(out, src),
        index=index, kg_nodes=kg_nodes, kg_edges=kg_edges,
        code_files=code.get("source_files") or [],
        code_objects=code.get("code_objects") or [],
        code_edges=code.get("code_edges") or [],
        code_entities=code.get("code_entities") or [],
        bridge_links=(bridge.get("links") or []),
        bridge_meta={k: v for k, v in bridge.items() if k != "links"},
        parser_graph=parser_graph, facts_present=facts_json.is_file(),
        concept_bodies=bodies, concept_bodies_source=bsrc, notes=notes,
    )
    if bundle.is_empty:
        notes.append("the exported graph is EMPTY (no concept nodes, code rows or bridge links) — "
                     "every rate below is reported as null, not as a pass")
    return bundle
