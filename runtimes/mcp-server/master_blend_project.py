#!/usr/bin/env python3
"""
master_blend_project.py — Phase-2: project the master-blend graph into the master
specification (spec-projection/master-blend, target T-master-blend-1..10). This REPLACES the
superseded byte-budget positional assembler (`master_blend.py`) — it does not extend it.

The deliverable is a PROJECTION of the classified graph (System §7.2), not a re-selection:
inclusion + grouping + cross-technique unification are settled UPSTREAM by knowledge-layer
classification (the grouping edges `concept_in_story` / `story_in_epic` and the findings edges
`inconsistency` already persisted in `ontology_edges`). This module TRAVERSES and RENDERS; it
has no scorer, no selection budget, no "what's important" logic (design "Where the cut lives").

**The deliverable is a FILE TREE, not one document.** The graph carries a System→Epic→Story
ontology; the faithful rendering of a tree is a tree of files — a directory per epic, a file
per story, an epic-level file per epic, and a System-level root file — mirroring the ontology
exactly (the same shape the framework's own `spec/` takes). A single concatenated document
would collapse the very hierarchy the graph encodes (and would re-enact the superseded
byte-budget assembler in new clothes).

TWO SEAMS, kept separate (build invariant R-spec-projection-9): the *select-the-cut* seam
(walk edges → ordered System→Epic→Story tree) NEVER references the output grammar; the
*render-in-grammar-G* seam (materialize) consumes the tree. So the same cut can render to
narrative-prose files now and other grammars/shapes later without touching cut/order logic.

  build_projection_tree()   ← the CUT: deterministic graph walk + canonical ordering (no LLM)
       │                       T-1 scope/families · T-2 ontology assembly · T-3 ungrouped
       │                       holder · T-4 one-concept-once · T-9 canonical ordering
       ▼
  fidelity_check()          ← T-6: rendered concept set == graph-included set (guardrail)
       │
       ▼
  materialize_tree()        ← the GRAMMAR/SHAPE (narrative-prose file tree): writes
                              <out>/SYSTEM.md, <out>/epics/<epic-slug>/EPIC.md, and
                              <out>/epics/<epic-slug>/stories/<story-slug>.md.
                              T-5 compose · T-7 findings view · T-8 audit inline (bottling off)

Verbosity/scope are caller filters (T-1/R-6): --scope all|epic:<id>, --audit on|off,
--compose on|off. Changing them changes HOW MUCH / WHICH families render, never the cut.

Discipline: reads the WIP store copy read-only for the cut; the tree is written UNDER --out
(new files, not a store mutation; refuses to write into a non-empty dir unless --force).
Auditable/resumable (T-10): reuse an existing tree unless --force; emit counts/sizes. LLM
composition (T-5) is OFF by default and runs per-node under the bounded-emit protocol so a
sub-scope is testable locally before any full pass.

Output layout (per-run GUID, immutable + addressable):
    output/projections/<GUID>/PROJECTION.json          run manifest (scope/counts/fidelity)
    output/projections/<GUID>/spec/SYSTEM.md           the singular System + epic index + findings
    output/projections/<GUID>/spec/epics/<eslug>/EPIC.md
    output/projections/<GUID>/spec/epics/<eslug>/stories/<sslug>.md

CLI (run from runtimes/mcp-server so relative ../../output resolves):
    # deterministic cut only (no LLM), whole graph, audit inline — cheap, local:
    python master_blend_project.py <wip_db>
    # one epic, with LLM prose composition (local test of T-5):
    python master_blend_project.py <wip_db> --scope epic:<epic_id> --compose --profile P
    # BOTTLING (Phase 3): same cut, audit family suppressed + normative voice recast:
    python master_blend_project.py <wip_db> --audit off --compose --voice normative --profile P
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import defaultdict

import spine_extract as SE

# Claude Haiku 4.5 pricing ($/1M tokens) — same constants the anneal/residue tiers use, for the
# compose cost meter (L8 observability of a real-spend job).
PRICE_IN, PRICE_OUT = 1.0, 5.0
HEARTBEAT_EVERY = 250            # stories between progress heartbeats when composing


# ── stable ids (the SAME hashes the upstream tiers mint — one bridge, no divergence) ──
def _story_key(members) -> str:
    return "story_" + hashlib.sha256("\x1f".join(sorted(members)).encode()).hexdigest()[:16]


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _slugify(text: str, fallback: str) -> str:
    """Deterministic, filesystem-safe slug from a human label. Not identity (the id is the
    hash); the slug is a readable directory/file HANDLE derived from the name — annotation,
    not a join key (the label-identity discipline: never key on this). Falls back to a hash
    fragment when the label is empty/degenerate so a node always gets a stable path."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:60].strip("-")
    return s or fallback


def _story_slug(story_node: dict) -> str:
    """Story slug from its highest-signal concept title (deterministic: canonical order already
    applied, so concepts[0] is stable), disambiguated by the story id tail so two stories that
    lead with the same title still get distinct paths."""
    lead = story_node["concepts"][0]["title"] if story_node["concepts"] else ""
    tail = story_node["story_id"].replace("story_", "")[:8]
    base = _slugify(lead, fallback=tail)
    return f"{base}--{tail}"


# ── results-vs-statements + lifecycle ordering (source-agnostic ontology signals only) ──
# Two reader-facing defects the concept-count sort left in the story list (R-master-blend-7,
# R-spec-projection-9): (1) findings/analysis-notes ABOUT the study were interleaved as if they
# were product capabilities; (2) the order buried the highest-stakes capability behind a
# concept-count-heavy access rule. Both are fixed HERE in the CUT, on ONTOLOGY-level signals
# (block_type + a small closed analysis-meta title vocabulary) — never on domain/source words —
# so the rule generalizes to any source tree, not a specific target's schema.

# Analysis-META concept markers. These name a result ABOUT the distillation (a gap, a decision,
# a discovered-but-unverified requirement, a migration note), NOT a normative statement of what
# the system DOES. block_type is the ontology-level signal; the title-prefix set is the
# extraction still's meta naming convention (English analysis vocabulary — process terms, never
# domain terms — so an explicit closed set is source-agnostic). Keep-don't-drop: meta content is
# ROUTED to a findings view, never deleted (L2/L3c).
_META_BLOCK_TYPES = {"finding", "observation"}
_META_TITLE_PREFIXES = {
    "coverage gap", "discovered requirement", "decision", "dependency", "migration",
    "naming conflict", "non-finding", "finding", "observation", "gap",
}


def _title_prefix(title: str) -> str:
    """The extraction-still naming prefix (text before the first ':'), lowercased. Annotation
    only — never an identity key (label-identity discipline)."""
    t = (title or "")
    return (t.split(":", 1)[0].strip().lower() if ":" in t else "").strip()


def _concept_is_meta(c: dict) -> bool:
    return (c.get("block_type") in _META_BLOCK_TYPES
            or _title_prefix(c.get("title", "")) in _META_TITLE_PREFIXES)


def _story_is_meta(story_node: dict) -> bool:
    """A story is META (a result about the analysis, not a system capability) iff EVERY concept
    it carries is meta. A story mixing meta + spec concepts stays a capability — the spec content
    dominates and the meta concepts ride along (conservative: never route a capability out)."""
    cs = story_node["concepts"]
    return bool(cs) and all(_concept_is_meta(c) for c in cs)


# Lifecycle rank over the ontology block-type vocabulary (NOT domain terms) — a stable narrative
# arc for equal-materiality stories: define the thing → put it in a state → act on it → constrain
# it → bound/guard it → recover from failure. Source-agnostic; unknown types sort in the middle.
_LIFECYCLE_RANK = {
    "entity": 0, "interface": 0,            # define / structure
    "state": 1,                             # states it can be in
    "workflow": 2, "event": 2, "command": 2, "action": 2,  # act on it
    "rule": 3, "obligation": 3, "invariant": 3, "permission": 3,  # constrain / govern
    "boundary": 4,                          # bound / segregate
    "exception": 5,                         # recover / fail
}
_LIFECYCLE_DEFAULT = 3  # unknown ontology type → govern band (middle), never first/last


def _story_lifecycle_rank(story_node: dict) -> int:
    """Lowest (earliest-lifecycle) rank among the story's concepts — a story that defines an
    entity leads its state/rule siblings. Deterministic; ontology-vocabulary only."""
    cs = story_node["concepts"]
    if not cs:
        return _LIFECYCLE_DEFAULT
    return min(_LIFECYCLE_RANK.get(c.get("block_type"), _LIFECYCLE_DEFAULT) for c in cs)


# ════════════════════════════════════════════════════════════════════════════════════════
#  SEAM 1 — THE CUT: deterministic graph walk + canonical ordering (no LLM, no grammar)
# ════════════════════════════════════════════════════════════════════════════════════════

def build_projection_tree(conn: sqlite3.Connection, scope: str = "all") -> dict:
    """Walk the grouping edges into an ordered System→Epic→Story→concept tree.

    Structure is READ FROM EDGES, never invented (R-2). Ordering is by an explicit
    deterministic rule at every tier (R-8/T-9), so re-projecting an unchanged graph yields the
    same sequence. Returns a plain dict (grammar-free) that the renderer consumes.

    scope: 'all' = whole graph; 'epic:<epic_id>' = the sub-graph reachable from one epic
    (the sub-spec case, R-6). The scope filter changes WHICH nodes, never the cut.
    """
    epic_filter = None
    if scope.startswith("epic:"):
        epic_filter = scope.split(":", 1)[1]

    # ── epic structural role (V/H/S) if the classification pass has been committed ──
    epic_role = {}
    has_class = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='ontology_epic_class'"
    ).fetchone()[0]
    if has_class:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ontology_epic_class)")]
        has_served = "served_verticals" in cols
        sel = "epic_id, role, system_split" + (", served_verticals" if has_served else "")
        for row in conn.execute(f"SELECT {sel} FROM ontology_epic_class"):
            eid, role, split = row[0], row[1], row[2]
            served = json.loads(row[3]) if (has_served and row[3]) else []
            epic_role[eid] = {"role": role, "system_split": split, "served_verticals": served}

    # ── epic↔epic interaction edges (Phase B) if committed: strongest neighbors per epic ──
    epic_interactions = defaultdict(list)
    if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='ontology_epic_interaction'").fetchone()[0]:
        for a, b, shared, strength, kinds in conn.execute(
            "SELECT src_epic, dst_epic, shared_objects, strength, kinds FROM ontology_epic_interaction"):
            k = json.loads(kinds) if kinds else {}
            epic_interactions[a].append({"other": b, "strength": strength, "shared": shared, "kinds": k})
            epic_interactions[b].append({"other": a, "strength": strength, "shared": shared, "kinds": k})

    # ── epics (with human-handle name; name is ANNOTATION only, id is identity) ──
    epics = {}
    for epic_id, name, story_count in conn.execute(
        "SELECT epic_id, name, story_count FROM ontology_epic"
    ):
        if epic_filter and epic_id != epic_filter:
            continue
        rc = epic_role.get(epic_id, {})
        epics[epic_id] = {"epic_id": epic_id, "name": name, "story_count": story_count,
                          "role": rc.get("role", "UNCLASSIFIED"),
                          "system_split": rc.get("system_split"),
                          "served_verticals": rc.get("served_verticals", []),
                          "interactions": sorted(epic_interactions.get(epic_id, []),
                                                  key=lambda x: -x["strength"])[:8],
                          "stories": []}

    # ── story → epic grouping edges (R-2): assign stories to their epic ──
    story_to_epic = {}
    for src, dst in conn.execute(
        "SELECT src, dst FROM ontology_edges WHERE relation='story_in_epic'"
    ):
        if dst in epics:
            story_to_epic[src] = dst

    # ── concept → story grouping edges (R-2): gather each story's concepts ──
    story_concepts = defaultdict(list)
    for src, dst in conn.execute(
        "SELECT src, dst FROM ontology_edges WHERE relation='concept_in_story'"
    ):
        story_concepts[dst].append(src)

    # ── concept bodies (one concept, once — R-4; each carries its full contribution) ──
    uc = {}
    for cid, title, btype, body_syn, body_raw, contributors, stills in conn.execute(
        "SELECT concept_id, title, block_type, body_synthesized, body_raw, "
        "contributor_count, source_stills FROM unified_concepts"
    ):
        uc[cid] = {"concept_id": cid, "title": title, "block_type": btype,
                   "body": (body_syn or body_raw or ""), "contributor_count": contributors,
                   "source_stills": stills}

    included_concepts = set()
    ungrouped_stories = []      # stories with no epic edge (T-3 / L3c)
    for sid, cids in story_concepts.items():
        if epic_filter and story_to_epic.get(sid) != epic_filter:
            continue
        # canonical concept order within a story (R-8): SPEC concepts before META concepts, then
        # block_type, then title, then id. The lead concept sets the story's title/slug — so a
        # mixed story (real behavior documented under a "Coverage Gap:"/"Decision:" framing) leads
        # with its substantive capability, not the analysis-note framing (R-master-blend-7). The
        # meta concept still rides along inside the story (kept, never dropped). Ontology signals
        # only → deterministic + source-agnostic.
        concepts = [uc[c] for c in cids if c in uc]
        concepts.sort(key=lambda c: (_concept_is_meta(c), c["block_type"],
                                     c["title"].lower(), c["concept_id"]))
        for c in concepts:
            included_concepts.add(c["concept_id"])
        # story-level materiality = max recovered Business-Relevance across concepts (a graph
        # signal, deterministically parsed — NOT grammar; the primary ordering key so the
        # highest-stakes capability leads, never buried by concept-count).
        smat = 0
        for c in concepts:
            try:
                sp = SE.spine_of(c["body"])
                for v in sp["calibration"].get("Business Relevance", []):
                    m = re.match(r"\s*(\d+)", v)
                    if m:
                        smat = max(smat, int(m.group(1)))
            except Exception as e:
                # A spine-parse failure silently yields materiality 0 → mis-ranks this story in the
                # ordering. Don't swallow: surface which concept failed so the mis-rank is visible.
                print(f"[master_blend] WARN materiality parse failed for concept "
                      f"{c.get('id', c.get('title', '?'))}: {type(e).__name__}: {e}", flush=True)
        node = {"story_id": sid, "concepts": concepts, "concept_count": len(concepts),
                "materiality": smat, "is_meta": False,
                "lifecycle_rank": _story_lifecycle_rank({"concepts": concepts})}
        node["is_meta"] = _story_is_meta(node)
        epic_id = story_to_epic.get(sid)
        if epic_id is None:
            ungrouped_stories.append(node)
        elif epic_id in epics:
            epics[epic_id]["stories"].append(node)

    # resolve interaction/served-vertical epic-ids → names (names for rendering; id stays identity)
    id2name = {eid: e["name"] for eid, e in epics.items()}
    # (some interaction targets may be out-of-scope when epic_filter is set — resolve via a full map)
    if epic_filter:
        for eid, nm in conn.execute("SELECT epic_id, name FROM ontology_epic"):
            id2name.setdefault(eid, nm)
    for e in epics.values():
        for it in e["interactions"]:
            it["other_name"] = id2name.get(it["other"], it["other"])

    # Split each epic's stories into CAPABILITIES (statements of what the system does) and
    # META (results about the analysis — findings/gaps/decisions). Meta is routed to a distinct
    # findings view per epic (R-master-blend-7), never dropped (L2/L3c). Capabilities are ordered
    # for a rational reader (R-spec-projection-9): materiality first (highest-stakes leads, so the
    # core calculation is never buried), then lifecycle arc (define→act→constrain→bound→recover),
    # then concept_count, then story_id (stable). Meta stories keep the materiality/id order.
    # All keys are ontology/graph signals only → deterministic + source-tree-agnostic (R-8/T-9).
    def _cap_key(s):
        return (-s["materiality"], s["lifecycle_rank"], -s["concept_count"], s["story_id"])
    def _meta_key(s):
        return (-s["materiality"], -s["concept_count"], s["story_id"])
    for e in epics.values():
        caps = [s for s in e["stories"] if not s["is_meta"]]
        meta = [s for s in e["stories"] if s["is_meta"]]
        caps.sort(key=_cap_key)
        meta.sort(key=_meta_key)
        e["stories"] = caps           # the story index is capabilities only
        e["meta_stories"] = meta      # routed findings view (still real files, still linked)
        e["capability_count"] = len(caps)
        e["meta_count"] = len(meta)
    # Epic order in the System index sorts on the SAME number the index DISPLAYS — the
    # capability count — so the list reads as monotonically ordered to a reader. (Sorting on the
    # total story_count while displaying capability-only count produced apparent inversions:
    # an epic with routed findings sorts by its larger total but shows its smaller capability
    # count.) Total story_count is a stable secondary tiebreak; then name, then id (deterministic).
    ordered_epics = sorted(epics.values(),
                           key=lambda e: (-e["capability_count"], -e["story_count"],
                                          e["name"].lower(), e["epic_id"]))

    # ── findings (R-7): inconsistency edges between epics in scope ──
    findings = []
    for src, dst, evidence in conn.execute(
        "SELECT src, dst, evidence FROM ontology_edges WHERE relation='inconsistency'"
    ):
        if epic_filter and (src != epic_filter and dst != epic_filter):
            continue
        if src in epics or dst in epics or epic_filter is None:
            ev = json.loads(evidence) if evidence else {}
            findings.append({"epic_ids": [src, dst],
                             "epic_names": ev.get("epic_names"),
                             "kind": ev.get("kind", "stable_distinct"),
                             "note": ev.get("note", "")})
    findings.sort(key=lambda f: (f["epic_ids"][0], f["epic_ids"][1]))

    return {
        "scope": scope,
        "epics": ordered_epics,
        "ungrouped_stories": ungrouped_stories,
        "findings": findings,
        "included_concepts": included_concepts,
        "counts": {
            "epics": len(ordered_epics),
            "stories": sum(len(e["stories"]) + len(e.get("meta_stories", [])) for e in ordered_epics)
                       + len(ungrouped_stories),
            "capability_stories": sum(len(e["stories"]) for e in ordered_epics),
            "meta_stories": sum(len(e.get("meta_stories", [])) for e in ordered_epics),
            "concepts": len(included_concepts),
            "ungrouped_stories": len(ungrouped_stories),
            "findings": len(findings),
        },
    }


def fidelity_check(tree: dict, conn: sqlite3.Connection) -> dict:
    """T-6 (contract): the rendered concept set MUST equal the graph-included set for the
    scope. This is the guardrail that a (future) composing LLM cannot add/drop/alter concepts —
    the cut is owned upstream, the renderer may not re-cut. Computed on the deterministic tree
    (pre-prose), so it validates the CUT independently of any grammar."""
    scope = tree["scope"]
    if scope.startswith("epic:"):
        epic_id = scope.split(":", 1)[1]
        story_ids = [r[0] for r in conn.execute(
            "SELECT src FROM ontology_edges WHERE relation='story_in_epic' AND dst=?", (epic_id,))]
        graph_concepts = set()
        if story_ids:
            q = ("SELECT DISTINCT src FROM ontology_edges WHERE relation='concept_in_story' "
                 f"AND dst IN ({','.join('?' * len(story_ids))})")
            graph_concepts = {r[0] for r in conn.execute(q, story_ids)}
    else:
        graph_concepts = {r[0] for r in conn.execute(
            "SELECT DISTINCT src FROM ontology_edges WHERE relation='concept_in_story'")}
    rendered = tree["included_concepts"]
    missing = graph_concepts - rendered      # in graph, not rendered → drop (fidelity fail)
    extra = rendered - graph_concepts        # rendered, not in graph → fabrication (fail)
    return {"ok": not missing and not extra,
            "graph_concepts": len(graph_concepts), "rendered_concepts": len(rendered),
            "missing": len(missing), "extra": len(extra)}


# ── T-master-blend-11: the SPINE-SURVIVAL gate (L14) ─────────────────────────────────────────
# fidelity_check (above) guards the CUT — that the right concept SET is present. It does NOT
# guard that each concept's FORMAL SPINE (the normative payload: mechanism/limits/schema) actually
# survived VERBATIM into the written file. A concept can pass the set-gate while its formula/
# predicate/limit was paraphrased away in prose — an L14 violation the set-gate is blind to.
# This gate closes that: after the tree is written, it re-reads each spec-grammar story file and
# asserts every rendered concept's spine values appear (verbatim, modulo fence/whitespace
# normalization) in the file. It is the spine analogue of fidelity_check: refuse-to-ship on loss.
#
# Proven before codified (experiments/probe_spine_gate.py, on a large baseline store): PASSes the
# faithful --grammar spec render on every spine-bearing concept in the probe sample, and CATCHes a
# lossy render on every one — a gate that both never false-fails the conformant path and never
# misses a drop.

def _spine_norm(s: str) -> str:
    """Normalize for the survival comparison: drop fence markers + collapse whitespace. The
    renderer transforms a spine value through _as_fenced (strips/re-fences) and _split_predicate_
    and_note (splits notes out), so survival is checked on this normal form, never raw bytes."""
    return re.sub(r"\s+", " ", s.replace("```", " ")).strip()


def _spine_probes(spine: dict) -> list:
    """The verbatim strings whose survival L14 requires: every mechanism/limits/schema value,
    split into its predicate part + any trailing notes (matching how the renderer lays them out).
    Trivially short probes (<8 normalized chars) are skipped — they are labels/fragments, not a
    formal payload, and would match incidentally (noise, not signal)."""
    probes = []
    for layer in ("mechanism", "limits", "schema"):
        for _label, vals in spine.get(layer, {}).items():
            for v in vals:
                pred, notes = _split_predicate_and_note(v)
                for raw in ([pred] if pred else [v]) + notes:
                    p = _spine_norm(raw)
                    if len(p) >= 8:
                        probes.append(p)
    return probes


def spine_survival_check(tree: dict, spec_root: str, layout: list) -> dict:
    """T-master-blend-11 (L14): assert each rendered concept's formal spine survived VERBATIM
    into its written story file. Re-reads the actual files on disk (ground truth — not the
    in-memory tree), extracts each concept's spine deterministically (spine_of), and confirms
    every mechanism/limits/schema value's normalized form is present in the normalized file text.
    Returns {ok, concepts_checked, spine_bearing, violations:[{concept_id,file,missing:[...]}]}.
    Only meaningful for the SPEC grammar (the prose grammar is knowingly lossy — T-master-blend-12);
    the caller gates on grammar. Fail-closed: any dropped spine value → ok=False → refuse to ship."""
    violations = []
    checked = spine_bearing = 0
    for e, eslug, stories in layout:
        sdir = os.path.join(spec_root, e.get("_roledir", "epics"), eslug, "stories")
        for s, sslug, fname in list(stories) + list(e.get("_meta_layout", [])):
            fpath = os.path.join(sdir, fname)
            try:
                with open(fpath) as f:
                    ntext = _spine_norm(f.read())
            except OSError:
                violations.append({"concept_id": None, "file": fpath,
                                   "missing": ["<file not found for spine check>"]})
                continue
            for c in s["concepts"]:
                checked += 1
                probes = _spine_probes(SE.spine_of(c["body"]))
                if not probes:
                    continue
                spine_bearing += 1
                miss = [p for p in probes if p not in ntext]
                if miss:
                    violations.append({"concept_id": c["concept_id"], "file": fpath,
                                       "missing": miss[:5], "missing_count": len(miss)})
    return {"ok": not violations, "concepts_checked": checked,
            "spine_bearing": spine_bearing, "violations": violations[:50],
            "violation_count": len(violations)}


# ════════════════════════════════════════════════════════════════════════════════════════
#  SEAM 2 — THE GRAMMAR/SHAPE: materialize the cut into a System→Epic→Story FILE TREE
#  (narrative-prose grammar). Writes one file per node, mirroring the ontology. Consumes the
#  tree; never re-cuts. T-5 compose · T-7 findings view · T-8 audit inline (bottling off).
# ════════════════════════════════════════════════════════════════════════════════════════

def _fmt_spine_field(vals: list) -> str:
    """Render a spine field value verbatim. If it already carries a fenced block, keep it;
    otherwise, multi-line values render as-is (verbatim — L14, never paraphrased)."""
    return "\n\n".join(v for v in vals)


def _as_fenced(body: str) -> str:
    """Render a formal-rule value as a clean, BALANCED fenced block.

    A source value can be one of three shapes:
      1. plain text (no fence)                     → wrap once.
      2. a single clean ```-fenced block           → dedent + re-fence once.
      3. a MIXTURE: several ```-clauses interleaved with '- Protects:'/'- Affected:' bullets
         (a multi-contributor concept concatenated its clauses)                → the risky case.
    For (3) we must NOT strip-and-rewrap (that leaves inner fences unbalanced and swallows the
    rest of the document — the observed bug). Instead we STRIP ALL fence markers and emit the
    whole value as ONE balanced fenced block, so every predicate clause and its inline prose is
    preserved verbatim inside a single well-formed fence (L14 verbatim, but syntactically safe).
    """
    b = body.strip()
    # FIELD-AGNOSTIC fence sanitize: source values are untrusted markdown that may carry fence
    # markers ANYWHERE (unclosed, nested, mid-line) in ANY field. We strip EVERY ``` token from
    # the content and emit exactly ONE fence we open and close ourselves → always balanced,
    # regardless of what the source contained. (This is a position-based rule — "block position"
    # — not a per-field patch.)
    inner = "\n".join(l.rstrip() for l in b.replace("```", "").splitlines()).strip()
    return f"```\n{inner}\n```"


def _inline(text: str) -> str:
    """FIELD-AGNOSTIC sanitize for any value rendered in INLINE position (not fenced): collapse
    to a single line and remove EVERY ``` fence token wherever it appears (line-start or mid-line)
    so an inline value can never open/unbalance a code fence. Applied by POSITION to every inline
    value, never keyed to a field name — so an unknown/new field cannot reintroduce the bug."""
    return " ".join(seg for seg in
                    (l.replace("```", "").strip() for l in text.splitlines())
                    if seg)


def _split_predicate_and_note(value: str):
    """A mechanism value is often a fenced predicate followed by trailing prose (e.g. a
    '- Protects:'/'- Affected entity:' note the source attached). Split into (predicate_text,
    [note lines]) so the predicate renders fenced and the notes render as plain bullets OUTSIDE
    the fence — never trapping prose inside a code block. If there is no fence, the whole value
    is treated as the predicate (plain)."""
    lines = value.splitlines()
    pred_lines, note_lines = [], []
    seen_fence_open = seen_fence_close = False
    for l in lines:
        s = l.lstrip()
        if s.startswith("```"):
            if not seen_fence_open:
                seen_fence_open = True
            else:
                seen_fence_close = True
            continue
        if seen_fence_open and not seen_fence_close:
            pred_lines.append(l.rstrip())
        elif seen_fence_close:
            if s:  # trailing prose after the fence → a note
                note_lines.append(s.lstrip("-* ").rstrip())
        else:
            pred_lines.append(l.rstrip())  # no fence yet → part of the predicate/plain text
    pred = "\n".join(pl for pl in pred_lines).strip()
    notes = [n for n in note_lines if n]
    return pred, notes


def _materiality(spine: dict) -> tuple[int, str]:
    """Aggregate recovered calibration into (materiality 0-5, confidence badge). Max across
    contributors — the highest stakes any source assigned (never invented; L4)."""
    def _max_int(field):
        best = 0
        for v in spine["calibration"].get(field, []):
            m = re.match(r"\s*(\d+)", v)
            if m:
                best = max(best, int(m.group(1)))
        return best
    rel = _max_int("Business Relevance")
    conf = _max_int("Confidence")
    return rel, (f"confidence {conf}/5" if conf else "confidence n/r")


def _render_concept_spec(c: dict, spine: dict, *, audit: bool) -> str:
    """Render ONE concept in the L14 spec grammar: the formal SPINE inline + verbatim, LIMITs as
    explicit clauses, business schema named, WHY as grounding, calibration + evidence shown.
    Self-contained — a reader groks it here, no provenance traversal (L14)."""
    L = [f"\n### {c['title']}\n"]
    rel, conf_badge = _materiality(spine)
    badge = f"_{c['block_type']}"
    if rel:
        badge += f" · materiality {rel}/5"
    badge += f" · {conf_badge}_\n"
    L.append(badge)

    why = spine["why"]
    if why.get("Definition"):
        L.append(f"\n{_inline(why['Definition'][0])}\n")

    # THE SPINE — mechanism, verbatim, inline, set off (L14: the normative payload)
    mech = spine["mechanism"]
    if mech:
        L.append("\n**Rule (formal):**\n")
        for label, vals in mech.items():
            for v in vals:
                pred, notes = _split_predicate_and_note(v)
                if pred:
                    L.append(f"\n{_as_fenced(pred)}\n")   # predicate: one clean balanced fence
                for n in notes:                            # notes: plain bullets OUTSIDE the fence
                    L.append(f"- _{_inline(n)}_\n")

    # LIMITS / boundary conditions — explicit clauses (steering of the valid WHAT)
    lim = spine["limits"]
    if lim:
        L.append("\n**Conditions & bounds:**\n")
        for label, vals in lim.items():
            for v in vals:
                if "```" in v:  # a fenced predicate inside a limit → predicate fenced, notes outside
                    pred, notes = _split_predicate_and_note(v)
                    L.append(f"- _{label}:_\n\n{_as_fenced(pred)}\n")
                    for n in notes:
                        L.append(f"  - _{_inline(n)}_\n")
                else:
                    L.append(f"- _{label}:_ {_inline(v)}\n")

    # BUSINESS SCHEMA — the fields/entities it operates on (named, not invented)
    sch = spine["schema"]
    if sch:
        names = "; ".join(_inline(v) for vals in sch.values() for v in vals)
        L.append(f"\n**Schema / fields:** {names}\n")

    # WHY grounding — rationale + consequence
    if why.get("Business rationale"):
        L.append(f"\n**Why:** {_inline(why['Business rationale'][0])}\n")
    if why.get("What breaks if violated"):
        L.append(f"\n**Risk if violated:** {_inline(why['What breaks if violated'][0])}\n")

    # EVIDENCE — provenance available (a citation, not a scavenger hunt)
    ev = spine["evidence"]
    if ev:
        cites = "; ".join(_inline(v) for vals in ev.values() for v in vals)
        L.append(f"\n**Evidence:** `{cites}`\n")

    if audit:
        L.append(f"\n<!-- concept_id={c['concept_id']} contributors={c['contributor_count']} "
                 f"stills={c['source_stills']} -->\n")
    out = "".join(L)
    # Per-concept fence-balance guard (belt-and-suspenders): a malformed source value could
    # leave an odd number of ``` markers, which would swallow the NEXT concept. Close any
    # dangling fence so a concept can never leak into its neighbor (the trapped-markdown bug).
    if out.count("```") % 2 == 1:
        out += "\n```\n"
    return out


def _render_story_spec_md(story_node: dict, epic: dict, *, audit: bool, prose: str | None = None) -> str:
    """One story file in the L14 SPEC grammar: optional WHY through-line prose, then each
    concept rendered with its formal spine inline + verbatim (self-contained). The spine is
    extracted DETERMINISTICALLY (no LLM touches it); `prose`, if present, is only the connective
    WHY narrative the LLM composed AROUND the spine."""
    concepts = story_node["concepts"]
    lead = concepts[0]["title"] if concepts else story_node["story_id"]
    # story-level materiality = max concept materiality (surfaces the highest-stakes capability)
    spines = [SE.spine_of(c["body"]) for c in concepts]
    story_rel = max((_materiality(s)[0] for s in spines), default=0)
    # honest node label: a pure-meta story is an analysis note ABOUT the epic, not a capability
    kind = "Finding / analysis note in" if story_node.get("is_meta") else "Capability of epic"
    hdr = f"> {kind} **{epic['name']}** · {len(concepts)} concepts"
    if story_rel:
        hdr += f" · materiality {story_rel}/5"
    hdr += " · formal spine rendered inline (verbatim; L14).\n"
    L = [f"# {lead}\n", hdr]
    if audit:
        ids = ", ".join(c["concept_id"] for c in concepts)
        L.append(f"<!-- story_id={story_node['story_id']} epic_id={epic['epic_id']} concepts=[{ids}] -->\n")
    if prose:
        # the LLM connective prose is untrusted-inline too: strip any fence tokens it emitted
        L.append("\n" + prose.replace("```", "").strip() + "\n")
    for c, spine in zip(concepts, spines):
        L.append(_render_concept_spec(c, spine, audit=audit))
    return "".join(L)


def _render_story_md(story_node: dict, epic: dict, *, audit: bool, prose: str | None = None) -> str:
    """One story file = one capability's spec prose (R-4 one-concept-once, all distinct
    contributions kept). Grammar = narrative prose."""
    concepts = story_node["concepts"]
    lead = concepts[0]["title"] if concepts else story_node["story_id"]
    L = [f"# {lead}\n",
         f"> Story of epic **{epic['name']}** · {len(concepts)} concepts · "
         f"projection of the classified graph (rendered, not re-cut).\n",
         "> ⚠️ _Non-normative orientation view (`--grammar prose`): the formal spine is flattened "
         "to narrative, not verbatim — the authoritative artifact is the `--grammar spec` "
         "projection of the same cut (T-master-blend-12; SYSTEM_SPEC §9.2)._\n"]
    if audit:
        ids = ", ".join(c["concept_id"] for c in concepts)
        L.append(f"<!-- story_id={story_node['story_id']} epic_id={epic['epic_id']} "
                 f"concepts=[{ids}] -->\n")
    if prose is not None:
        L.append(f"\n{prose}\n")
        if audit:
            for c in concepts:
                L.append(f"<!-- concept_id={c['concept_id']} type={c['block_type']} "
                         f"contributors={c['contributor_count']} stills={c['source_stills']} -->\n")
    else:
        for c in concepts:  # deterministic render: one concept once, full body kept
            L.append(f"\n## {c['title']}\n\n_{c['block_type']}_\n")
            if c["body"].strip():
                L.append(f"\n{c['body'].strip()}\n")
            if audit:
                L.append(f"\n<!-- concept_id={c['concept_id']} "
                         f"contributors={c['contributor_count']} stills={c['source_stills']} -->\n")
    return "".join(L)


def _render_epic_md(epic: dict, story_files: list, *, audit: bool, meta_files: list | None = None) -> str:
    """One epic file = the epic overview + an index of its story files (the roll-up). Two
    indexes: CAPABILITIES (statements of what the system does, ordered materiality→lifecycle)
    and, when present, FINDINGS & ANALYSIS NOTES (results ABOUT the system — gaps, decisions,
    migrations — routed out of the capability list per R-master-blend-7, kept + linked)."""
    role = epic.get("role", "UNCLASSIFIED")
    role_tag = {"VERTICAL": "Vertical (business-domain capability)",
                "HORIZONTAL": "Horizontal (cross-cutting business concern)",
                "SYSTEM": "System / platform (candidate — system-vs-plumbing pending)",
                "MIXED": "Flagged (ambiguous role — for judgment)",
                "UNCLASSIFIED": "Unclassified"}.get(role, role)
    cap = epic.get("capability_count", len(epic["stories"]))
    mc = epic.get("meta_count", len(meta_files) if meta_files else 0)
    cap_w = "capability" if cap == 1 else "capabilities"
    size = f"{cap} {cap_w}" + (f" · {mc} finding{'' if mc == 1 else 's'}/note{'' if mc == 1 else 's'}"
                               if mc else "")
    L = [f"# {epic['name']}\n",
         f"> Epic · **{role_tag}** · {size} · "
         "projection of the classified graph (candidate map — non-deterministic, "
         "human-validate §9.2).\n"]
    if audit:
        L.append(f"<!-- epic_id={epic['epic_id']} story_count={epic['story_count']} -->\n")

    # System-wide business rule: name the verticals it SERVES (why it carries business value —
    # the served-vertical signal that distinguishes it from true plumbing, §7.2)
    if epic.get("role") == "SYSTEM":
        sv = epic.get("served_verticals") or []
        if sv:
            L.append(f"\n> **System-wide business rule** — serves: {', '.join(sv[:10])}. "
                     "Structurally platform, but business/compliance-relevant to these domains "
                     "(surfaced at full materiality, §7.2).\n")
        else:
            L.append("\n> **True-plumbing candidate** — no served vertical found via the "
                     "dependency graph; pending the local business-value check before any "
                     "de-emphasis (kept, never dropped — L2/L3c).\n")

    # Interactions (Phase B): strongest epic↔epic dependencies (feeds / depends-on / shared)
    inter = epic.get("interactions") or []
    if inter:
        L.append("\n## Interacts with\n\n")
        L.append("> Epics sharing code objects (dependency graph); direction from write→read "
                 "operations where determinable.\n")
        for it in inter:
            k = it.get("kinds", {})
            aw, bw = k.get("a_writes_b_reads", 0), k.get("b_writes_a_reads", 0)
            rel = ("feeds" if aw > bw and aw else ("depends on" if bw > aw and bw else "shares data with"))
            L.append(f"- **{rel}** {it['other_name']} — {it['shared']} shared objects\n")

    L.append("\n## Capabilities\n\n")
    L.append("> What the system does, ordered by materiality then lifecycle "
             "(define → act → constrain → bound → recover).\n\n")
    for sf, s in story_files:
        lead = s["concepts"][0]["title"] if s["concepts"] else s["story_id"]
        mat = s.get("materiality", 0)
        tag = f"materiality {mat}/5" if mat else f"{s['concept_count']} concepts"
        L.append(f"- [{lead}](stories/{sf}) — {tag}\n")

    if meta_files:
        L.append("\n## Findings & analysis notes\n\n")
        L.append("> Results ABOUT this epic from the distillation — coverage gaps, decisions, "
                 "dependencies, migration notes — not statements of what the system does "
                 "(R-master-blend-7). Kept and linked; validate per §9.2.\n\n")
        for sf, s in meta_files:
            lead = s["concepts"][0]["title"] if s["concepts"] else s["story_id"]
            L.append(f"- [{lead}](stories/{sf}) — {s['concept_count']} concepts\n")
    return "".join(L)


# ── epistemic-status stamp (T-master-blend-12): every projection declares its CLASS + provenance ──
# The mission (strategy memo §5; SYSTEM_SPEC §9.2): nothing in the chain is the incontrovertible
# universal spec — each artifact is one path-dependent reconstruction. So every projection stamps
# WHAT it is, so a reader never mistakes a lossy orientation-view for the authoritative payload:
#   - spec grammar  = SPINE-BEARING, authoritative-at-this-resolution (formal spine verbatim + inline,
#                      guarded by the spine-survival gate). The reverse path to nuance is the graph.
#   - prose grammar = NON-NORMATIVE ORIENTATION VIEW: the formal spine is NOT rendered (it is
#                      flattened to narrative), so it must NOT be built from as the spec — the
#                      spine-bearing `--grammar spec` projection of the same cut is authoritative.
# Neither is absolute: both project a path-dependent graph sample (§9.2) and point back to it.
def _epistemic_banner(grammar: str) -> str:
    if grammar == "spec":
        return ("> **Epistemic status — spine-bearing projection (authoritative at this resolution).** "
                "The formal spine (mechanism/limits/schema) is rendered verbatim + inline and guarded "
                "by the spine-survival gate. This is NOT absolute truth: it is one reasoned, "
                "path-dependent reconstruction of a graph sample (a different pipeline config would "
                "differ) — every claim carries its evidence and is open to challenge, not beyond "
                "question (SYSTEM_SPEC §9.2; strategy memo §5).\n")
    return ("> ⚠️ **Epistemic status — NON-NORMATIVE ORIENTATION VIEW (`--grammar prose`).** The "
            "formal spine (formulas/predicates/limits/schema) is FLATTENED into narrative here and "
            "is NOT preserved verbatim — **do not build from this as the specification.** The "
            "authoritative, spine-bearing artifact is the `--grammar spec` projection of the SAME "
            "cut (and, beneath it, the classified graph). This view exists for orientation only; "
            "like any projection it is path-dependent and non-absolute (SYSTEM_SPEC §9.2; memo §5).\n")


def _render_system_md(tree: dict, epic_files: list, *, audit: bool, grammar: str = "spec") -> str:
    """The System-level root file: the singular System over all epics, ORGANIZED BY STRUCTURAL
    AXIS (SYSTEM_SPEC §7.2): Verticals (the domain view) lead, then Horizontals (cross-cutting
    business concerns), then System (candidate platform — Phase-B decides system-vs-plumbing).
    Structural role NEVER lowers materiality — system concerns are organized separately, not
    demoted. Plus the findings view (R-7) + ungrouped holder (T-3/L3c). Leads with the
    epistemic-status stamp (T-master-blend-12) so the artifact's class is unmistakable."""
    c = tree["counts"]
    L = ["# System Specification\n",
         _epistemic_banner(grammar),
         f"> Singular System over {c['epics']} epics · {c['stories']} stories · "
         f"{c['concepts']} concepts · {c['findings']} findings · scope `{tree['scope']}` · "
         f"audit {'inline' if audit else 'suppressed'}.\n",
         f"> Generated {_now()}. Projection of the master-blend graph; the cut is owned "
         "upstream (knowledge-layer classification) — this is a faithful render, not a "
         "re-selection. Epics are organized by structural role (Vertical / Horizontal / System);"
         " role organizes and cross-references, it never lowers materiality (§7.2).\n"]

    # bucket epic_files by role
    buckets = defaultdict(list)
    for ef, e in epic_files:
        buckets[e.get("role", "UNCLASSIFIED")].append((ef, e))
    SECTIONS = [
        ("VERTICAL", "Verticals — business-domain capabilities",
         "The domain view: each is a value stream a domain owner owns."),
        ("HORIZONTAL", "Horizontals — cross-cutting business concerns",
         "Business-meaningful patterns applied across many domains (kept whole, not shredded); "
         "cross-referenced into the verticals they touch."),
        ("SYSTEM", "System — platform / infrastructure (candidate)",
         "Structurally platform concerns. CANDIDATE only: the system-wide-business-rule vs. "
         "true-plumbing split needs the served-vertical signal (dependency graph, pending). "
         "Organized separately, NOT demoted in materiality (§7.2)."),
        ("MIXED", "Flagged — ambiguous role (kept for judgment)",
         "Structural and semantic signals disagreed; surfaced for human classification (§9.2)."),
        ("UNCLASSIFIED", "Epics", ""),
    ]
    for role, heading, blurb in SECTIONS:
        items = buckets.get(role, [])
        if not items:
            continue
        L.append(f"\n## {heading} ({len(items)})\n\n")
        if blurb:
            L.append(f"> {blurb}\n\n")
        for ef, e in items:
            cap = e.get("capability_count", len(e["stories"]))
            mc = e.get("meta_count", 0)
            cap_w = "capability" if cap == 1 else "capabilities"
            suffix = f"{cap} {cap_w}" + (f" · {mc} finding{'' if mc == 1 else 's'}" if mc else "")
            L.append(f"- [{e['name']}]({ef}) — {suffix}\n")

    if tree["ungrouped_stories"]:
        L.append("\n## Ungrouped / needs classification (kept + flagged, L3c)\n\n")
        L.append(f"> {len(tree['ungrouped_stories'])} stories carry no epic grouping edge; "
                 "surfaced here, never dropped.\n")

    if tree["findings"]:
        L.append("\n## Findings — adjacent-but-distinct epics (results about the system)\n\n")
        L.append("> Behavioral divergence the graph flagged; distinct from spec statements "
                 "(R-7). Merged-then-resplit 2-cycles from the epic anneal falsifier.\n\n")
        for f in tree["findings"]:
            nm = f.get("epic_names") or f["epic_ids"]
            L.append(f"- **{nm[0]}** ⟺ **{nm[1]}** — {f['note'] or f['kind']}\n")
    return "".join(L)


def assign_slugs(tree: dict) -> list:
    """Deterministically assign epic/story slugs and file paths (no I/O, no LLM). Returns a list
    of (epic, eslug, [(story, sslug, fname), ...]). Slug assignment is the same whether or not we
    compose, so the compose fan-out and the tree write agree on paths (and resume can check the
    exact target file). Slugs are readable HANDLES; identity stays the hash."""
    # role → subdirectory bucket (SYSTEM_SPEC §7.2 structural axis). Verticals lead (the domain
    # view), horizontals are the cross-cutting section, system is candidate-platform (Phase-B
    # decides system-vs-plumbing). Unclassified (no class pass) all land in one bucket.
    ROLE_DIR = {"VERTICAL": "verticals", "HORIZONTAL": "horizontals", "SYSTEM": "system",
                "MIXED": "flagged", "UNCLASSIFIED": "epics"}
    out = []
    used_epic_slugs: dict[str, int] = {}
    for e in tree["epics"]:
        roledir = ROLE_DIR.get(e.get("role", "UNCLASSIFIED"), "epics")
        eslug = _slugify(e["name"], fallback=e["epic_id"].replace("epic_", "")[:8])
        # slug uniqueness is per role-bucket (paths differ by bucket)
        skey = f"{roledir}/{eslug}"
        if skey in used_epic_slugs:
            used_epic_slugs[skey] += 1
            eslug = f"{eslug}-{used_epic_slugs[skey]}"
        else:
            used_epic_slugs[skey] = 0
        e["_roledir"] = roledir
        stories = []
        meta_stories = []
        used_story_slugs: dict[str, int] = {}
        # slug BOTH capability and meta stories in one namespace (they share the stories/ dir),
        # so every node still gets a real, linkable file — routing changes WHERE it is indexed
        # in EPIC.md, not whether it is written (keep-don't-drop).
        for s in e["stories"]:
            sslug = _story_slug(s)
            if sslug in used_story_slugs:
                used_story_slugs[sslug] += 1
                sslug = f"{sslug}-{used_story_slugs[sslug]}"
            else:
                used_story_slugs[sslug] = 0
            stories.append((s, sslug, f"{sslug}.md"))
        for s in e.get("meta_stories", []):
            sslug = _story_slug(s)
            if sslug in used_story_slugs:
                used_story_slugs[sslug] += 1
                sslug = f"{sslug}-{used_story_slugs[sslug]}"
            else:
                used_story_slugs[sslug] = 0
            meta_stories.append((s, sslug, f"{sslug}.md"))
        e["_meta_layout"] = meta_stories
        out.append((e, eslug, stories))
    return out


def materialize_tree(tree: dict, spec_root: str, layout: list, *, audit: bool,
                     prose_by_story: dict | None = None, grammar: str = "prose") -> dict:
    """Write the ordered tree as a filesystem hierarchy under spec_root:
        SYSTEM.md
        epics/<epic-slug>/EPIC.md
        epics/<epic-slug>/stories/<story-slug>.md
    `layout` is the slug/path assignment from assign_slugs (shared with the compose fan-out).
    `prose_by_story` (story_id -> composed prose) is used when composing; a story missing from
    the map renders deterministically (fail-closed). New files only. Returns write counts."""
    os.makedirs(spec_root, exist_ok=True)
    files_written = 0
    epic_files = []   # (relpath, epic) — carries role for the SYSTEM.md axis sections
    for e, eslug, stories in layout:
        roledir = e.get("_roledir", "epics")
        edir = os.path.join(spec_root, roledir, eslug)
        sdir = os.path.join(edir, "stories")
        os.makedirs(sdir, exist_ok=True)
        render = _render_story_spec_md if grammar == "spec" else _render_story_md
        story_files = []
        for s, sslug, fname in stories:
            prose = prose_by_story.get(s["story_id"]) if prose_by_story is not None else None
            with open(os.path.join(sdir, fname), "w") as f:
                f.write(render(s, e, audit=audit, prose=prose))
            files_written += 1
            story_files.append((fname, s))
        # meta/findings stories: same real files, indexed under the epic's findings view
        meta_files = []
        for s, sslug, fname in e.get("_meta_layout", []):
            prose = prose_by_story.get(s["story_id"]) if prose_by_story is not None else None
            with open(os.path.join(sdir, fname), "w") as f:
                f.write(render(s, e, audit=audit, prose=prose))
            files_written += 1
            meta_files.append((fname, s))
        with open(os.path.join(edir, "EPIC.md"), "w") as f:
            f.write(_render_epic_md(e, story_files, audit=audit, meta_files=meta_files))
        files_written += 1
        epic_files.append((os.path.join(roledir, eslug, "EPIC.md"), e))
    with open(os.path.join(spec_root, "SYSTEM.md"), "w") as f:
        f.write(_render_system_md(tree, epic_files, audit=audit, grammar=grammar))
    files_written += 1
    return {"files_written": files_written, "epics": len(tree["epics"]),
            "stories": tree["counts"]["stories"]}


# ── optional LLM prose composer (T-5), bounded-emit; only when --compose ──
# Returns (prose, tok_in, tok_out, ok) so the caller can meter cost (L8) and count fail-closed
# fallbacks. On any error it FAILS CLOSED to the deterministic per-concept render (never
# fabricates — L4), so a call failure degrades one story's prose, never the corpus.
def make_composer(profile: str, region: str, model: str, voice: str = "as_graph"):
    """`voice='as_graph'` (master-blend doc set): weave the concepts as-is.
    `voice='normative'` (bottling): recast findings-voice → authoritative specification voice —
    what the system DOES / REQUIRES — the one irreducible transform bottling adds (R-bottling-3,
    L5). BOTH are cut-determined: they may rewrite voice but MUST NOT add/drop/alter the concept
    set (the fidelity gate enforces this), and MUST NOT invent (L4)."""
    import bedrock_client
    llm = bedrock_client.create_llm_client(profile, region)
    if voice == "connective":
        # For the L14 SPEC grammar: the verbatim formal spine is rendered separately BELOW this
        # prose. The prose's ONLY job is a SHORT connective WHY/overview (2-4 sentences) that
        # orients the reader — what this capability is and why it matters — grounding the exact
        # rules that follow. It MUST NOT restate the formulas/thresholds (those render verbatim
        # below) and MUST NOT add claims. Prose grounds the spine; it never replaces it (L14).
        SYS = ("You write a SHORT (2-4 sentence) orienting overview for one capability of a "
               "specification: what it is and why it matters to the business. The precise rules, "
               "formulas, and conditions are rendered separately and verbatim below your text — "
               "so do NOT restate them, do NOT invent anything, and do NOT list conditions. "
               "Just orient the reader. No headings, no preamble.")
    elif voice == "normative":
        SYS = ("You recast already-selected concepts of a single capability into an "
               "AUTHORITATIVE SPECIFICATION passage — normative statements of what the system "
               "DOES and REQUIRES, in the system's own terms (not findings or observations about "
               "a study, and with no methodology/provenance/scores). You MUST NOT add any claim "
               "not present in the given concepts, and MUST NOT drop, merge away, or alter any "
               "concept — recast VOICE only. No preamble, no headings.")
    else:
        SYS = ("You compose ONE short spec passage from the given already-selected concepts of a "
               "single capability. Weave them into coherent prose. You MUST NOT add, drop, merge, "
               "reorder as facts, or invent — render only what is given. No preamble, no headings.")

    def _deterministic(concepts) -> str:
        parts = []
        for c in concepts:
            parts.append(f"\n## {c['title']}\n\n_{c['block_type']}_\n")
            if c["body"].strip():
                parts.append(f"\n{c['body'].strip()}\n")
        return "".join(parts)

    def compose(concepts):
        import random as _r
        bullets = "\n".join(f"- [{c['block_type']}] {c['title']}: {c['body'][:300]}"
                            for c in concepts[:25])
        user = f"Concepts:\n{bullets}\n\nCompose the passage."
        max_retries = 5
        for attempt in range(max_retries):
            try:
                res = bedrock_client.call_llm(llm, model, SYS, user,
                                                 max_tokens=800, temperature=0.0)
                text = (res.output_text or "").strip()
                if not text:
                    return _deterministic(concepts), res.input_tokens, res.output_tokens, False
                return text, res.input_tokens, res.output_tokens, True
            except Exception as e:
                es = str(e).lower()
                is_throttle = "429" in str(e) or "rate_limit" in es or "throttl" in es or "tpm" in es
                if is_throttle and attempt < max_retries - 1:
                    time.sleep((5 * (2 ** attempt)) + _r.uniform(0, 5))  # backoff + jitter
                    continue
                # non-throttle error, or retries exhausted: fail closed (never fabricate; L4)
                return _deterministic(concepts), 0, 0, False

    return compose


def _read_existing_prose(fpath: str) -> str | None:
    """Resume: recover a story's already-composed prose from a prior run's file, stripping the
    header/audit lines so re-materialization is byte-identical. A story file starts with a
    '# <title>', a '> ...' blurb, an optional audit comment, then a blank line, then the prose.

    GRAMMAR-AWARE (critical): in the SPEC grammar the composed prose is ONLY the short connective
    overview that sits BETWEEN the preamble and the first rendered spine section ('### ' concept
    header); the spine below it is re-rendered deterministically from the graph every time. So we
    must recover ONLY that connective paragraph and STOP at the first '### ' — otherwise the
    recovered "prose" would include the whole rendered spine, which the renderer then appends
    again, duplicating every concept on each resume (the observed ×N bug). In the PROSE grammar
    there are no '### ' sections, so we recover to end-of-file as before."""
    try:
        with open(fpath) as f:
            lines = f.readlines()
    except OSError:
        return None
    i = 0
    n = len(lines)
    if i < n and lines[i].startswith("# "):
        i += 1
    while i < n and lines[i].startswith(">"):
        i += 1
    while i < n and lines[i].startswith("<!--"):
        i += 1
    # the renderer writes "\n{prose}\n" → a leading blank line before prose
    if i < n and lines[i].strip() == "":
        i += 1
    # SPEC grammar: stop at the first spine section header so we recover the connective prose
    # ONLY (never the rendered spine, which is regenerated deterministically below the prose).
    end = i
    while end < n and not lines[end].startswith("### "):
        end += 1
    body = "".join(lines[i:end]).rstrip("\n")
    return body or None


def compose_all_parallel(layout: list, spec_root: str, composer, *, concurrency: int,
                         resume: bool) -> dict:
    """Fan out per-story prose composition across `concurrency` workers (the parallel-submit
    stage), observable (heartbeat with rate/ETA/cost, L8) and resumable (reuse a prior run's
    composed file rather than re-spend). Returns {story_id: prose} + a meter. No cost cap."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # build the work list, applying resume up front (skip stories already composed on disk)
    work = []            # (story_id, concepts)
    prose_by_story = {}  # story_id -> prose (prefilled from resume)
    resumed = 0
    total = 0
    for e, eslug, stories in layout:
        sdir = os.path.join(spec_root, e.get("_roledir", "epics"), eslug, "stories")
        # compose BOTH capability and meta stories (both are real files); routing only affects
        # where EPIC.md indexes them, not whether they get connective prose.
        for s, sslug, fname in list(stories) + list(e.get("_meta_layout", [])):
            total += 1
            fpath = os.path.join(sdir, fname)
            if resume and os.path.exists(fpath) and os.path.getsize(fpath) > 0:
                prior = _read_existing_prose(fpath)
                if prior is not None:
                    prose_by_story[s["story_id"]] = prior
                    resumed += 1
                    continue
            work.append((s["story_id"], s["concepts"]))

    meter = {"tok_in": 0, "tok_out": 0, "composed": 0, "fell_back": 0}
    t0 = time.time()
    done = 0
    print(json.dumps({"stage": "compose-start", "stories_total": total,
                      "to_compose": len(work), "resumed": resumed,
                      "concurrency": concurrency}), flush=True)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(composer, concepts): sid for sid, concepts in work}
        for fut in as_completed(futs):
            sid = futs[fut]
            prose, ti, to, ok = fut.result()
            prose_by_story[sid] = prose
            meter["tok_in"] += ti; meter["tok_out"] += to
            meter["composed"] += 1 if ok else 0
            meter["fell_back"] += 0 if ok else 1
            done += 1
            if done % HEARTBEAT_EVERY == 0 or done == len(work):
                el = time.time() - t0
                rate = done / el if el > 0 else 0.0
                eta_min = (len(work) - done) / rate / 60 if rate > 0 else 0.0
                cost = meter["tok_in"] / 1e6 * PRICE_IN + meter["tok_out"] / 1e6 * PRICE_OUT
                print(json.dumps({"stage": "compose", "done": f"{done}/{len(work)}",
                                  "composed": meter["composed"], "fell_back": meter["fell_back"],
                                  "resumed": resumed, "rate_per_s": round(rate, 1),
                                  "eta_min": round(eta_min, 1), "cost_usd": round(cost, 2)}),
                      flush=True)

    cost = meter["tok_in"] / 1e6 * PRICE_IN + meter["tok_out"] / 1e6 * PRICE_OUT
    return {"prose_by_story": prose_by_story, "resumed": resumed,
            "composed": meter["composed"], "fell_back": meter["fell_back"],
            "tok_in": meter["tok_in"], "tok_out": meter["tok_out"],
            "cost_usd": round(cost, 2), "elapsed_s": round(time.time() - t0, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="WIP store copy (read-only for the cut)")
    ap.add_argument("--scope", default="all", help="'all' or 'epic:<epic_id>' (sub-spec)")
    ap.add_argument("--audit", choices=["on", "off"], default="on",
                    help="audit family inline (master-blend doc set) or suppressed (bottling filter)")
    ap.add_argument("--compose", action="store_true", help="LLM prose composition (T-5); OFF by default")
    ap.add_argument("--voice", choices=["as_graph", "normative", "connective", "auto"], default="auto",
                    help="'auto' = connective for --grammar spec (short WHY over verbatim spine), "
                         "as_graph otherwise; 'normative' = bottling recast; 'as_graph' = prose weave")
    ap.add_argument("--grammar", choices=["prose", "spec"], default="spec",
                    help="'spec' (L14 default) = formal spine rendered inline + verbatim, "
                         "self-contained; 'prose' = legacy narrative weave (lossy of the spine)")
    ap.add_argument("--model", default="anthropic.claude-haiku-4-5")
    ap.add_argument("--profile", default=""); ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--projections-base", default="../../output/projections",
                    help="base dir for per-run projection artifacts (each run gets its own GUID)")
    ap.add_argument("--projection-id", default="",
                    help="reuse/reference a specific run GUID; default = a fresh uuid4")
    ap.add_argument("--force", action="store_true",
                    help="regenerate into an existing projection dir (default: reuse, no silent regen)")
    ap.add_argument("--resume", action="store_true",
                    help="with --projection-id + --compose: resume in place, reusing already-composed "
                         "story files (skip re-spend), (re)compose only the rest")
    ap.add_argument("--concurrency", type=int, default=16,
                    help="parallel compose workers (LLM fan-out); ignored without --compose")
    args = ap.parse_args()

    import uuid
    # Each projection run is its own immutable, addressable artifact: output/projections/<GUID>/spec/*
    guid = args.projection_id or str(uuid.uuid4())
    run_root = os.path.join(args.projections_base, guid)
    spec_root = os.path.join(run_root, "spec")

    # T-10 resumable: reuse an existing projection rather than silently regenerate — UNLESS
    # --resume (continue composing in place) or --force (regenerate) is set.
    if (os.path.isdir(spec_root) and os.listdir(spec_root)
            and not args.force and not args.resume):
        print(json.dumps({"phase": "master-blend-projection", "status": "reused-existing",
                          "projection_id": guid, "spec_root": spec_root,
                          "hint": "pass --force to regenerate, --resume to continue composing, "
                                  "or omit --projection-id for a fresh run"}),
              flush=True)
        return 0

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA query_only=ON")  # the cut reads; never mutates the store
    tree = build_projection_tree(conn, scope=args.scope)
    fid = fidelity_check(tree, conn)
    print(json.dumps({"phase": "master-blend-projection", "scope": args.scope,
                      "projection_id": guid, "counts": tree["counts"], "fidelity": fid},
                     indent=2), flush=True)
    if not fid["ok"]:
        print("[project] FIDELITY FAILURE — rendered set != graph set; refusing to write", flush=True)
        conn.close(); return 2

    # --force into an existing dir: clear it first so the tree is a clean render (new files only).
    # --resume deliberately does NOT clear — it reuses already-composed story files.
    if args.force and os.path.isdir(spec_root):
        shutil.rmtree(spec_root)

    layout = assign_slugs(tree)   # deterministic slug/path assignment (shared by compose + write)

    compose_summary = None
    prose_by_story = None
    if args.compose:
        eff_voice = args.voice
        if eff_voice == "auto":
            eff_voice = "connective" if args.grammar == "spec" else "as_graph"
        composer = make_composer(args.profile, args.region, args.model, voice=eff_voice)
        compose_summary = compose_all_parallel(layout, spec_root, composer,
                                                concurrency=args.concurrency, resume=args.resume)
        prose_by_story = compose_summary.pop("prose_by_story")

    written = materialize_tree(tree, spec_root, layout, audit=(args.audit == "on"),
                               prose_by_story=prose_by_story, grammar=args.grammar)

    # a small run manifest at the projection root (addressable provenance, not a store mutation)
    os.makedirs(run_root, exist_ok=True)
    with open(os.path.join(run_root, "PROJECTION.json"), "w") as f:
        json.dump({"projection_id": guid, "scope": args.scope, "audit": args.audit,
                   "voice": (eff_voice if args.compose else args.voice), "grammar": args.grammar,
                   "composed": bool(args.compose), "generated_at": _now(),
                   # epistemic-status class (T-master-blend-12): spine-bearing spec vs non-normative
                   # prose view — so a machine consumer never treats a lossy view as authoritative.
                   "spine_bearing": (args.grammar == "spec"),
                   "epistemic_status": ("spine-bearing projection (authoritative at this resolution; "
                                        "spine verbatim+inline, gate-guarded)" if args.grammar == "spec"
                                        else "non-normative orientation view (spine flattened; the "
                                        "--grammar spec projection of the same cut is authoritative)"),
                   "counts": tree["counts"], "fidelity": fid,
                   "written": written, "compose": compose_summary}, f, indent=2)
    conn.close()

    # T-master-blend-11 (L14): spine-SURVIVAL gate. fidelity_check guarded the CUT (concept set)
    # pre-write; this guards the SPINE post-write — that each rendered concept's formal payload
    # (mechanism/limits/schema) actually survived verbatim into the written file. Only the SPEC
    # grammar carries the verbatim spine; the prose grammar is knowingly lossy (T-master-blend-12),
    # so the gate applies to --grammar spec only. Fail-closed: a dropped spine → exit 3.
    spine_gate = None
    if args.grammar == "spec":
        spine_gate = spine_survival_check(tree, spec_root, layout)
        print(json.dumps({"phase": "master-blend-projection", "spine_survival": {
            "ok": spine_gate["ok"], "concepts_checked": spine_gate["concepts_checked"],
            "spine_bearing": spine_gate["spine_bearing"],
            "violation_count": spine_gate["violation_count"]}}, indent=2), flush=True)
        if not spine_gate["ok"]:
            print("[project] SPINE-SURVIVAL FAILURE — a rendered concept's formal spine did not "
                  "survive verbatim into its file (L14). First violations:", flush=True)
            for v in spine_gate["violations"][:10]:
                print(f"  {v['concept_id']} in {v['file']}: missing {v.get('missing')}", flush=True)

    # record the spine gate alongside the cut fidelity in the run manifest (addressable provenance)
    if spine_gate is not None:
        try:
            with open(os.path.join(run_root, "PROJECTION.json")) as f:
                _pj = json.load(f)
            _pj["spine_survival"] = {k: spine_gate[k] for k in
                                     ("ok", "concepts_checked", "spine_bearing", "violation_count")}
            with open(os.path.join(run_root, "PROJECTION.json"), "w") as f:
                json.dump(_pj, f, indent=2)
        except (OSError, ValueError) as e:
            # Persisting the spine gate into the manifest is provenance — a silent write failure
            # loses the addressable record. Surface it (WARN) rather than swallow; the primary
            # PROJECTION output already exists, so this is a warning, not a fatal.
            print(f"[master_blend] WARN failed to record spine_survival into PROJECTION.json "
                  f"at {run_root}: {type(e).__name__}: {e}", flush=True)

    print(json.dumps({"status": "written", "projection_id": guid, "spec_root": spec_root,
                      "written": written, "audit": args.audit, "composed": bool(args.compose),
                      "compose": compose_summary,
                      "spine_survival_ok": (spine_gate["ok"] if spine_gate else None)}), flush=True)
    return 3 if (spine_gate is not None and not spine_gate["ok"]) else 0


if __name__ == "__main__":
    sys.exit(main())
