# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Section 1 — deterministic structural integrity of the exported graphs.

Every check here is exact: no LLM, no embeddings, no thresholds. Each one answers a question
that has a yes/no answer in the source tree or in the graph's own declared invariants.

Code graph (anchors — "does this node point at something that really exists?")
  * every source_files.path resolves to a real file under the source root
  * every code_objects.(path, object) — the object's bare name occurs in that file
  * every code_entities.(path, code_name) — the name occurs in that file
  * every code_edges.src resolves, and the edge's dst token occurs in the src file
    (dynamic-SQL edges are exempt by construction — code_graph.py builds them from
     EXEC(@sql)/sp_executesql, where the name is assembled at runtime)

Bridge (concept <-> code)
  * the link's own assertion holds in the cited file — the identifier it claims is there
    (`join_name`) occurs at identifier boundaries, and a dotted qualifier it carries resolved —
    reported at five strictnesses so the failure mode is identifiable rather than just a number
    (see bridge.tiers and bridge.failure_classes)
  * links the join REFUSED to emit are counted from the bridge's own `dropped[]` ledger, so a
    bridge that fails closed is distinguishable from one that had nothing to drop

Concept graph (shape — "are the graph's own invariants intact?")
  * no duplicate node ids, no dangling edge endpoints, no self-loops, no duplicate edges
  * same_as merge classes: within-still (the documented dedup boundary), single block_type,
    and mapping onto exactly one unified concept — a class that splits across two unified
    concepts, or merges two stills, is a contradictory merge
  * CONTRIBUTES_TO is a DAG, bipartite block -> unified_concept, out-degree 1 per block
  * unified concept ids are reproducible from their contributor ids (knowledge.py derives
    them as sha256 of the sorted contributor ids), so an id is verifiable, not just unique
  * orphans: concept nodes with no edge at all, code objects with no incident code edge
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict

from .load import ConceptResolver, GraphBundle, bare_name, norm_full

MAX_EXAMPLES = 25
HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _pct(a: int, b: int) -> float | None:
    return round(100.0 * a / b, 2) if b else None


class _DSU:
    __slots__ = ("p",)

    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        p = self.p
        p.setdefault(x, x)
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:        # path compression
            p[x], x = root, p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb

    def groups(self) -> dict:
        out: dict = defaultdict(list)
        for x in self.p:
            out[self.find(x)].append(x)
        return out


def _has_cycle(adj: dict[str, list[str]], nodes) -> list[list[str]]:
    """Iterative DFS cycle detection. Returns up to MAX_EXAMPLES cycles (as node lists)."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict = {}
    cycles: list[list[str]] = []
    for start in nodes:
        if colour.get(start, WHITE) != WHITE:
            continue
        stack = [(start, iter(adj.get(start, ())))]
        path = [start]
        colour[start] = GREY
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                colour[node] = BLACK
                stack.pop()
                path.pop()
                continue
            c = colour.get(nxt, WHITE)
            if c == GREY:
                if len(cycles) < MAX_EXAMPLES:
                    i = path.index(nxt) if nxt in path else 0
                    cycles.append(path[i:] + [nxt])
            elif c == WHITE:
                colour[nxt] = GREY
                stack.append((nxt, iter(adj.get(nxt, ()))))
                path.append(nxt)
    return cycles


# ----------------------------------------------------------------------------- code graph anchors
def _check_code_anchors(b: GraphBundle) -> dict:
    src = b.src
    res: dict = {}

    def anchor(rows, path_key, name_key, label, word=False, verdict_key=None):
        """`verdict_key` cross-tabs our independent result against the row's own claim, so a row the
        pipeline already flags as unanchored is not confused with one it asserts is confirmed."""
        total = len(rows)
        ok = unresolved = absent = 0
        fails = []
        claimed_ok_but_failed = []
        for r in rows:
            raw = r.get(path_key)
            rel, why = src.resolve(raw)
            if rel is None:
                unresolved += 1
                if len(fails) < MAX_EXAMPLES:
                    fails.append({"path": raw, "name": r.get(name_key), "reason": why,
                                  "declared": r.get(verdict_key) if verdict_key else None})
                continue
            if name_key is None:
                ok += 1
                continue
            needle = bare_name(r.get(name_key))
            if needle and src.occurs(rel, needle, word=word):
                ok += 1
            else:
                absent += 1
                declared = r.get(verdict_key) if verdict_key else None
                if declared == "confirmed":
                    claimed_ok_but_failed.append({"path": rel, "name": r.get(name_key)})
                if len(fails) < MAX_EXAMPLES:
                    fails.append({"path": rel, "name": r.get(name_key),
                                  "reason": "name does not occur in the file",
                                  "declared": declared})
        out = {"total": total, "resolved_and_anchored": ok, "file_unresolved": unresolved,
               "name_absent": absent, "pct": _pct(ok, total), "examples": fails}
        if verdict_key:
            out["declared_verdicts"] = dict(Counter(r.get(verdict_key) for r in rows))
            out["declared_confirmed_but_failed"] = len(claimed_ok_but_failed)
            out["declared_confirmed_but_failed_examples"] = claimed_ok_but_failed[:MAX_EXAMPLES]
        return out

    res["source_files"] = anchor(b.code_files, "path", None, "source file")
    res["code_objects"] = anchor(b.code_objects, "path", "object", "code object")
    res["code_entities"] = anchor(b.code_entities, "path", "code_name", "code entity",
                                 verdict_key="anchor_verdict")

    # edges: src must resolve; dst token must occur in the src file unless dynamic
    total = len(b.code_edges)
    ok = unresolved = absent = dynamic = 0
    fails = []
    claimed_ok_but_failed = []
    for e in b.code_edges:
        rel, why = src.resolve(e.get("src"))
        if rel is None:
            unresolved += 1
            if len(fails) < MAX_EXAMPLES:
                fails.append({"src": e.get("src"), "dst": e.get("dst"), "reason": why,
                              "declared": e.get("verdict")})
            continue
        if int(e.get("dynamic") or 0):
            dynamic += 1
            continue
        needle = bare_name(e.get("dst"))
        if needle and src.occurs(rel, needle):
            ok += 1
        else:
            absent += 1
            if e.get("verdict") == "confirmed":
                claimed_ok_but_failed.append({"src": rel, "dst": e.get("dst")})
            if len(fails) < MAX_EXAMPLES:
                fails.append({"src": rel, "dst": e.get("dst"), "type": e.get("type"),
                              "reason": "dst token does not occur in the src file",
                              "declared": e.get("verdict")})
    checkable = total - dynamic
    res["code_edges"] = {
        "total": total, "dynamic_exempt": dynamic, "checkable": checkable,
        "anchored": ok, "src_unresolved": unresolved, "dst_absent": absent,
        "pct": _pct(ok, checkable), "examples": fails,
        # the graph's own verdict vs ours — an independent re-audit of code_graph.py's regex pass
        "declared_verdicts": dict(Counter(e.get("verdict") for e in b.code_edges)),
        "declared_confirmed_but_failed": len(claimed_ok_but_failed),
        "declared_confirmed_but_failed_examples": claimed_ok_but_failed[:MAX_EXAMPLES],
    }
    return res


# ----------------------------------------------------------------------------- bridge
def _check_bridge(b: GraphBundle) -> dict:
    """Does each bridge link's own assertion hold in the file it cites?

    Five strictnesses on the same question, so a failure is identifiable rather than just a number:

    full_word  the symbol exactly as written, at identifier boundaries   (strictest)
    full_sub   the symbol exactly as written, anywhere
    join_word  the identifier the link CLAIMS is there (`join_name`), at identifier boundaries
               <- headline for a run whose bridge carries a join_name (export_graph >= the
               qualifier-carrying join); this is the link's own assertion
    bare_word  the last dotted segment, at identifier boundaries
    bare_sub   the last dotted segment, anywhere                         (the OLD join's semantics:
               export_graph._norm() kept only the last segment, so this is all it asserted)

    A link that fails `full_sub` is not necessarily a wrong file. A concept writes `h.Vendor`,
    `inserted.APRef`, `APInvHdr.Job`; the file holds the column `Vendor` / `APRef` / `Job`. The join
    now carries that qualifier, resolves it (alias -> table, `inserted.`/`deleted.` -> the trigger's
    table, an object prefix -> its declaring type) and records the resolution on the link, so the
    classes separate the two cases exactly:

      ok                 the symbol is in the file verbatim
      qualifier_resolved the symbol is not verbatim, but the link says which table/type the
                         qualifier resolved to and the joined identifier is really there, at
                         identifier boundaries  -> a TRUE assertion, not a failure
      qualifier_dropped  the qualifier was dropped with no resolution recorded (the old behaviour)
      substring_only     the joined name only occurs inside a longer identifier (`cboVendor`)
      name_absent        the joined name is nowhere in the cited file
    """
    src = b.src
    links = b.bridge_links
    tiers = {k: 0 for k in ("full_word", "full_sub", "join_word", "bare_word", "bare_sub")}
    unresolved = 0
    classes: Counter = Counter()
    resolutions: Counter = Counter()
    flags: Counter = Counter()
    examples: dict[str, list[dict]] = defaultdict(list)
    per_link: list[dict] = []
    carries_join_name = sum(1 for lk in links if lk.get("join_name"))

    for i, lk in enumerate(links):
        sym = lk.get("symbol") or ""
        resolutions[lk.get("resolution") or "(none)"] += 1
        for fl in lk.get("flags") or []:
            flags[fl] += 1
        rel, why = src.resolve(lk.get("code_path"))
        if rel is None:
            unresolved += 1
            classes["file_unresolved"] += 1
            if len(examples["file_unresolved"]) < MAX_EXAMPLES:
                examples["file_unresolved"].append({"symbol": sym, "code_path": lk.get("code_path"),
                                                    "reason": why})
            per_link.append({"i": i, "tier": None})
            continue
        full, bare = norm_full(sym), bare_name(sym)
        # what the link CLAIMS is in the file; falls back to the bare symbol for an older bridge
        join = bare_name(lk.get("join_name") or sym)
        t = {
            "full_word": src.occurs(rel, full, word=True),
            "full_sub": src.occurs(rel, full),
            "join_word": src.occurs(rel, join, word=True),
            "bare_word": src.occurs(rel, bare, word=True),
            "bare_sub": src.occurs(rel, bare),
        }
        for k, v in t.items():
            tiers[k] += int(v)
        resolved_qualifier = bool(lk.get("qualifier_resolved_to")) or \
            str(lk.get("resolution") or "").startswith("qualifier_")
        if t["full_sub"]:
            cls = "ok"
        elif resolved_qualifier and t["join_word"]:
            cls = "qualifier_resolved"     # 'h.Vendor' -> APInvHdr.Vendor; `Vendor` really is here
        elif t["bare_word"]:
            cls = "qualifier_dropped"      # 'h.Vendor' -> file has the column 'Vendor', unresolved
        elif t["bare_sub"]:
            cls = "substring_only"         # bare name only inside a longer identifier (cboVendor)
        else:
            cls = "name_absent"            # the joined name is nowhere in the cited file
        classes[cls] += 1
        if cls not in ("ok", "qualifier_resolved") and len(examples[cls]) < MAX_EXAMPLES:
            examples[cls].append({"concept": lk.get("concept"), "symbol": sym, "code_path": rel,
                                  "code_name": lk.get("code_name"), "match": lk.get("match")})
        per_link.append({"i": i, "tier": cls, "resolved": rel})

    # The OTHER end of every bridge link: does its concept key name a node in the concept graph?
    # The two artifacts do not share a key (see load.ConceptResolver), so report HOW each key
    # matched: `exact` means the artifacts agree; `id_stripped`/`canonical` mean the concept is
    # there but the title has drifted; `unresolved` is a genuine dangling endpoint.
    resolver = ConceptResolver(b.kg_nodes)
    keys = sorted({str(lk.get("concept")) for lk in links if lk.get("concept") is not None})
    tier_of: dict[str, str] = {}
    key_tiers: Counter = Counter()
    unresolved_keys, drifted_keys = [], []
    for k in keys:
        tier, _hits = resolver.resolve(k)
        tier_of[k] = tier
        key_tiers[tier] += 1
        if tier == "unresolved":
            unresolved_keys.append(k)
        elif tier != "exact":
            drifted_keys.append({"concept_key": k, "matched_as": tier})
    link_tiers: Counter = Counter(tier_of.get(str(lk.get("concept")), "unresolved") for lk in links)
    concept_resolution = {
        "distinct_concept_keys": len(keys),
        "by_tier_keys": dict(key_tiers),
        "by_tier_links": dict(link_tiers),
        "resolved_to_a_graph_node": len(keys) - len(unresolved_keys),
        "exact": key_tiers.get("exact", 0),
        "title_drift": len(drifted_keys),
        "unresolved": len(unresolved_keys),
        "pct": _pct(len(keys) - len(unresolved_keys), len(keys)),
        "exact_pct": _pct(key_tiers.get("exact", 0), len(keys)),
        "links_with_an_unresolved_concept": link_tiers.get("unresolved", 0),
        "links_pct": _pct(len(links) - link_tiers.get("unresolved", 0), len(links)),
        "links_exact_pct": _pct(link_tiers.get("exact", 0), len(links)),
        "examples": unresolved_keys[:MAX_EXAMPLES],
        "title_drift_examples": drifted_keys[:MAX_EXAMPLES],
    }

    n = len(links)
    # the same check at the de-duplicated levels a reader may expect, so a quoted rate is unambiguous
    def dedup_rate(keyfn, checker):
        seen: dict = {}
        for lk in links:
            seen.setdefault(keyfn(lk), lk)
        bad = 0
        for lk in seen.values():
            rel, _ = src.resolve(lk.get("code_path"))
            if rel is None or not checker(rel, lk):
                bad += 1
        return {"n": len(seen), "failures": bad, "pct": _pct(len(seen) - bad, len(seen))}

    def fs(rel, lk):
        return src.occurs(rel, norm_full(lk.get("symbol") or ""))

    denominators = {
        "per_link": dedup_rate(lambda lk: id(lk), fs),
        "per_concept_symbol_file": dedup_rate(
            lambda lk: (lk.get("concept"), lk.get("symbol"), lk.get("code_path")), fs),
        "per_concept_symbol": dedup_rate(lambda lk: (lk.get("concept"), lk.get("symbol")), fs),
        "per_symbol_file": dedup_rate(lambda lk: (lk.get("symbol"), lk.get("code_path")), fs),
    }

    # The link's own assertion: verbatim, or a resolved qualifier whose joined identifier is really
    # in the file at identifier boundaries. This is the metric the join is now accountable to;
    # `headline_symbol_occurrence_pct` (verbatim symbol only) is kept unchanged for continuity with
    # the recorded baseline, and the two are reported side by side rather than one replacing the other.
    assertion_ok = classes.get("ok", 0) + classes.get("qualifier_resolved", 0)
    dropped = b.bridge_meta.get("dropped")
    return {
        "links": n,
        "file_unresolved": unresolved,
        "tiers": {k: {"passed": v, "failed": n - v, "pct": _pct(v, n)} for k, v in tiers.items()},
        "headline_symbol_occurrence_pct": _pct(tiers["full_sub"], n),
        "link_assertion_pct": _pct(assertion_ok, n),
        "links_with_a_join_name": carries_join_name,
        "resolutions": dict(resolutions),
        "flags": dict(flags),
        "dropped_by_the_join": (len(dropped) if isinstance(dropped, list)
                                else (dropped if isinstance(dropped, int) else None)),
        "dropped_reasons": (dict(Counter(d.get("reason") for d in dropped))
                            if isinstance(dropped, list) else {}),
        "failure_classes": dict(classes),
        "failure_examples": {k: v for k, v in examples.items()},
        "concept_resolution": concept_resolution,
        "denominators": denominators,
        "concepts_linked": len({lk.get("concept") for lk in links}),
        "distinct_symbols": len({lk.get("symbol") for lk in links}),
        "distinct_files": len({lk.get("code_path") for lk in links}),
        "by_match_kind": dict(Counter(lk.get("match") for lk in links)),
        "_per_link": per_link,
    }


# ----------------------------------------------------------------------------- concept graph
def _check_concept_graph(b: GraphBundle) -> dict:
    nodes, edges = b.kg_nodes, b.kg_edges
    ids = [n.get("id") for n in nodes]
    by_id = {n.get("id"): n for n in nodes}
    dup_ids = [i for i, c in Counter(ids).items() if c > 1]
    missing_id = sum(1 for i in ids if i is None)

    unified = {i for i, n in by_id.items() if n.get("node_type") == "unified_concept"}
    blocks = set(by_id) - unified

    dangling, self_loops = [], []
    dup_edges: Counter = Counter()
    deg: Counter = Counter()
    by_type: Counter = Counter()
    same_as: list[tuple] = []
    contributes: list[tuple] = []
    for e in edges:
        s, t = e.get("source"), e.get("target")
        et = e.get("edge_type") or "?"
        by_type[et] += 1
        if s not in by_id or t not in by_id:
            if len(dangling) < MAX_EXAMPLES:
                dangling.append({"source": s, "target": t, "edge_type": et,
                                 "missing": [x for x in (s, t) if x not in by_id]})
            continue
        if s == t:
            if len(self_loops) < MAX_EXAMPLES:
                self_loops.append({"node": s, "edge_type": et})
        dup_edges[(s, t, et)] += 1
        deg[s] += 1
        deg[t] += 1
        if et == "same_as":
            same_as.append((s, t))
        elif et == "CONTRIBUTES_TO":
            contributes.append((s, t))

    n_dangling = sum(1 for e in edges
                     if e.get("source") not in by_id or e.get("target") not in by_id)
    n_self = sum(1 for e in edges if e.get("source") == e.get("target"))
    n_dup = sum(c - 1 for c in dup_edges.values() if c > 1)
    orphans = [i for i in by_id if deg[i] == 0]

    # ---- same_as merge classes
    dsu = _DSU()
    reciprocal = 0
    seen_pairs = set()
    for s, t in same_as:
        dsu.union(s, t)
        if (t, s) in seen_pairs:
            reciprocal += 1
        seen_pairs.add((s, t))
    adj_same: dict = defaultdict(list)
    for s, t in same_as:
        adj_same[s].append(t)
    long_cycles = [c for c in _has_cycle(adj_same, list(adj_same)) if len(set(c)) > 2]

    classes = [g for g in dsu.groups().values() if len(g) > 1]
    cross_still, type_conflict, split_merge = [], [], []
    asserted = len({frozenset((s, t)) for s, t in same_as if s != t})
    implied = sum(len(g) * (len(g) - 1) // 2 for g in classes)
    # which unified concept each block contributes to
    block_to_uc: dict = defaultdict(set)
    for s, t in contributes:
        block_to_uc[s].add(t)
    for g in classes:
        stills = {x for i in g for x in (by_id.get(i, {}).get("source_still"),) if x}
        types = {by_id.get(i, {}).get("block_type") for i in g}
        ucs = {u for i in g for u in block_to_uc.get(i, ())}
        if len(stills) > 1 and len(cross_still) < MAX_EXAMPLES:
            cross_still.append({"members": g[:8], "stills": sorted(stills)})
        if len(types) > 1 and len(type_conflict) < MAX_EXAMPLES:
            type_conflict.append({"members": g[:8], "block_types": sorted(t or "?" for t in types)})
        if len(ucs) > 1 and len(split_merge) < MAX_EXAMPLES:
            split_merge.append({"members": g[:8], "unified_concepts": sorted(ucs)})
    n_cross = sum(1 for g in classes
                  if len({x for i in g for x in (by_id.get(i, {}).get("source_still"),) if x}) > 1)
    n_typeconf = sum(1 for g in classes if len({by_id.get(i, {}).get("block_type") for i in g}) > 1)
    n_split = sum(1 for g in classes if len({u for i in g for u in block_to_uc.get(i, ())}) > 1)

    # ---- CONTRIBUTES_TO shape
    adj_c: dict = defaultdict(list)
    for s, t in contributes:
        adj_c[s].append(t)
    c_cycles = _has_cycle(adj_c, list(adj_c))
    bad_direction = [(s, t) for s, t in contributes if s in unified or t not in unified]
    multi_parent = {s: sorted(v) for s, v in block_to_uc.items() if len(v) > 1}
    blocks_without_uc = sorted(blocks - set(block_to_uc))

    # ---- id shape + reproducibility
    uc_contrib: dict = defaultdict(list)
    for s, t in contributes:
        uc_contrib[t].append(s)
    bad_shape = [i for i in by_id
                 if not (HEX16.match(str(i)) or (str(i).startswith("uc_") and HEX16.match(str(i)[3:])))]
    id_mismatch = []
    reproducible = 0
    for uc in sorted(unified):
        contribs = sorted(uc_contrib.get(uc, []))
        if not contribs:
            continue
        if len(contribs) == 1:
            want = f"uc_{contribs[0]}"
        else:
            want = "uc_" + hashlib.sha256("|".join(contribs).encode()).hexdigest()[:16]
        if want == uc:
            reproducible += 1
        elif len(id_mismatch) < MAX_EXAMPLES:
            id_mismatch.append({"unified_concept": uc, "expected": want,
                                "contributors": len(contribs)})
    n_id_mismatch = 0
    for uc in unified:
        contribs = sorted(uc_contrib.get(uc, []))
        if not contribs:
            continue
        want = (f"uc_{contribs[0]}" if len(contribs) == 1
                else "uc_" + hashlib.sha256("|".join(contribs).encode()).hexdigest()[:16])
        n_id_mismatch += int(want != uc)

    return {
        "nodes": len(nodes), "edges": len(edges),
        "unified_concepts": len(unified), "observation_blocks": len(blocks),
        "edge_types": dict(by_type),
        "ids": {"distinct": len(by_id), "duplicate_ids": len(dup_ids), "missing_id": missing_id,
                "unexpected_shape": len(bad_shape), "examples": (dup_ids + bad_shape)[:MAX_EXAMPLES]},
        "integrity": {"dangling_edges": n_dangling, "self_loops": n_self,
                      "duplicate_edges": n_dup,
                      "dangling_examples": dangling, "self_loop_examples": self_loops,
                      "duplicate_edge_examples": [{"source": s, "target": t, "edge_type": e,
                                                   "count": c}
                                                  for (s, t, e), c in dup_edges.most_common()
                                                  if c > 1][:MAX_EXAMPLES]},
        "orphans": {"count": len(orphans), "pct": _pct(len(orphans), len(by_id)),
                    "examples": orphans[:MAX_EXAMPLES]},
        "same_as": {
            "edges": len(same_as), "merge_classes": len(classes),
            "largest_class": max((len(g) for g in classes), default=0),
            "members": sum(len(g) for g in classes),
            "asserted_pairs": asserted, "implied_pairs": implied,
            "implied_not_asserted": max(implied - asserted, 0),
            "reciprocal_pairs": reciprocal,
            "cycles_longer_than_2": len(long_cycles), "cycle_examples": long_cycles[:MAX_EXAMPLES],
            "cross_still_classes": n_cross, "cross_still_examples": cross_still,
            "block_type_conflict_classes": n_typeconf, "block_type_conflict_examples": type_conflict,
            "split_across_unified_concepts": n_split, "split_examples": split_merge,
            "contradictions": n_cross + n_split,
        },
        "contributes_to": {
            "edges": len(contributes), "cycles": len(c_cycles), "cycle_examples": c_cycles,
            "wrong_direction": len(bad_direction),
            "wrong_direction_examples": bad_direction[:MAX_EXAMPLES],
            "blocks_with_multiple_parents": len(multi_parent),
            "multiple_parent_examples": dict(list(multi_parent.items())[:MAX_EXAMPLES]),
            "blocks_without_unified_concept": len(blocks_without_uc),
            "blocks_without_unified_concept_examples": blocks_without_uc[:MAX_EXAMPLES],
            "is_dag": not c_cycles,
        },
        "unified_ids": {"checked": len([u for u in unified if uc_contrib.get(u)]),
                        "reproducible": reproducible, "mismatched": n_id_mismatch,
                        "pct": _pct(reproducible, len([u for u in unified if uc_contrib.get(u)])),
                        "examples": id_mismatch},
    }


# ----------------------------------------------------------------------------- orphan code objects
def _orphan_code_objects(b: GraphBundle) -> dict:
    edge_src = {str(e.get("src")) for e in b.code_edges}
    edge_dst = {bare_name(e.get("dst")) for e in b.code_edges}
    orphans = []
    for o in b.code_objects:
        if str(o.get("path")) in edge_src or bare_name(o.get("object")) in edge_dst:
            continue
        orphans.append({"path": o.get("path"), "object": o.get("object"), "kind": o.get("kind")})
    files_no_edges = sorted({str(f.get("path")) for f in b.code_files} - edge_src)
    return {"code_objects": len(b.code_objects), "orphan_code_objects": len(orphans),
            "pct": _pct(len(orphans), len(b.code_objects)), "examples": orphans[:MAX_EXAMPLES],
            "source_files_without_edges": len(files_no_edges),
            "source_files_without_edges_examples": files_no_edges[:MAX_EXAMPLES]}


# ----------------------------------------------------------------------------- entry point
def check(b: GraphBundle) -> dict:
    src = b.src
    out = {
        "source_root": {
            "files": len(src.files),
            "case_insensitive_path_collisions": [sorted(g) for g in src.case_collisions],
            "basename_collisions": [sorted(g) for g in src.basename_collisions][:MAX_EXAMPLES],
            "basename_collision_count": len(src.basename_collisions),
        },
        "code_graph": _check_code_anchors(b),
        "bridge": _check_bridge(b),
        "concept_graph": _check_concept_graph(b),
        "orphans": _orphan_code_objects(b),
    }
    cg, cp, br = out["code_graph"], out["concept_graph"], out["bridge"]
    failures = (
        cg["source_files"]["file_unresolved"] + cg["code_objects"]["file_unresolved"]
        + cg["code_objects"]["name_absent"] + cg["code_entities"]["file_unresolved"]
        + cg["code_entities"]["name_absent"] + cg["code_edges"]["src_unresolved"]
        + cg["code_edges"]["dst_absent"]
        + cp["ids"]["duplicate_ids"] + cp["integrity"]["dangling_edges"]
        + cp["integrity"]["self_loops"] + cp["integrity"]["duplicate_edges"]
        + cp["same_as"]["contradictions"] + cp["contributes_to"]["cycles"]
        + cp["contributes_to"]["wrong_direction"] + cp["contributes_to"]["blocks_with_multiple_parents"]
        + cp["unified_ids"]["mismatched"]
        + br["failure_classes"].get("name_absent", 0) + br["file_unresolved"]
        + br["concept_resolution"]["unresolved"]
    )
    out["hard_failures"] = failures
    out["verdict"] = "clean" if failures == 0 else "defects found"
    return out
