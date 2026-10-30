#!/usr/bin/env python3
"""
interaction_graph.py — Phase B: project the code-dependency graph into the ontology as an
INTERACTION MODEL (story↔story and epic↔epic), and resolve the System-candidate split
(system-wide-business-rule vs. true-plumbing) via the served-vertical signal.

Why (SYSTEM_SPEC §7.2 + the gold-star gap): the deliverable's stories are isolated islands; a
rational reader's first question is relational ("what happens when X, how does X interact with
Y"). The code-dependency graph (code_edges: file → code-object, typed by operation) is already
in the store but inert. This pass joins it to the ontology so the spec can render "this
capability feeds/depends-on that one" — and so a platform concern can be told apart from a
business rule by WHAT IT SERVES (a plumbing rule that a compliance vertical depends on is a
system-wide business rule, not plumbing — the two-signal safeguard, §7.2).

DETERMINISTIC (no LLM, L5). Join chain (validated): concept → its Evidence file paths →
code_edges.src=path → dst code-object (+ operation) → the set of stories/epics whose concepts
also touch that dst. Shared-object ⇒ interaction; operation typing (insert/select/exec/…)
gives producer→consumer direction.

Writes (additive, sibling tables; baseline/grouping untouched; reversible):
  - ontology_story_object   : (story_id, code_object, operation)  — what each story touches
  - ontology_epic_interaction: (src_epic, dst_epic, shared_objects, kinds)  — epic↔epic edges
  - ontology_epic_class.served_verticals / system_split refined for SYSTEM candidates

CLI:
  python interaction_graph.py <wip_db> [--commit]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sqlite3
from collections import Counter, defaultdict

_EVIDENCE_PATH = re.compile(r"([A-Za-z0-9][^\s,]*?\.(?:sql|vb|cs|cube|rdl|xml|config|vbs|js))", re.I)


def _now():
    return _dt.datetime.now(_dt.UTC).isoformat()


def story_paths(conn, criterion="assembled_backbone"):
    """story_id -> set(file paths) from member concepts' Evidence citations (deterministic)."""
    s2c = defaultdict(list)
    for src, dst in conn.execute(
        "SELECT src, dst FROM ontology_edges WHERE relation='concept_in_story' AND criterion=?",
        (criterion,)):
        s2c[dst].append(src)
    need = {c for cs in s2c.values() for c in cs}
    body = {}
    nl = list(need)
    for i in range(0, len(nl), 900):
        chunk = nl[i:i+900]
        for cid, b in conn.execute(
            "SELECT concept_id, body_raw FROM unified_concepts WHERE concept_id IN (%s)" % ",".join("?"*len(chunk)), chunk):
            body[cid] = b
    sp = defaultdict(set)
    for st, cids in s2c.items():
        for c in cids:
            for m in _EVIDENCE_PATH.findall(body.get(c, "") or ""):
                sp[st].add(m)
    return sp


def build(conn, commit=False):
    # story → epic, epic role
    story_epic = {s: e for s, e in conn.execute(
        "SELECT src, dst FROM ontology_edges WHERE relation='story_in_epic'")}
    epic_role = {e: r for e, r in conn.execute("SELECT epic_id, role FROM ontology_epic_class")} \
        if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='ontology_epic_class'").fetchone()[0] else {}
    epic_name = {e: n for e, n in conn.execute("SELECT epic_id, name FROM ontology_epic")}

    sp = story_paths(conn)

    # path → [(dst_object, operation)]  (index code_edges once)
    path_objs = defaultdict(list)
    for src, dst, op in conn.execute("SELECT src, dst, operation FROM code_edges"):
        path_objs[src].append((dst, op or "ref"))

    # story → {object: Counter(op)} ; object → set(story)
    story_obj = defaultdict(lambda: defaultdict(Counter))
    obj_story = defaultdict(set)
    for st, paths in sp.items():
        for p in paths:
            for dst, op in path_objs.get(p, ()):  # exact path match
                story_obj[st][dst][op] += 1
                obj_story[dst].add(st)

    # epic → objects (with op), object → epics
    epic_obj = defaultdict(lambda: defaultdict(Counter))
    obj_epic = defaultdict(set)
    for st, objs in story_obj.items():
        e = story_epic.get(st)
        if not e:
            continue
        for dst, ops in objs.items():
            for op, n in ops.items():
                epic_obj[e][dst][op] += n
            obj_epic[dst].add(e)

    # epic↔epic interaction edges with HUB-DAMPENING. A ubiquitous object (company table, security
    # table) touched by many epics creates false density — an epic pair sharing ONLY such hubs is
    # not really interacting. So each shared object contributes 1/(epics_touching_it): a rare,
    # specific shared object (weight → ~1) is a strong signal; a hub shared by 50 epics contributes
    # ~0.02. This is the same hub-fanout discipline the concept comparator used. `strength` is the
    # dampened sum; `objects` keeps the raw count for reference. Also carry producer→consumer sense.
    WRITE = {"insert", "update", "delete", "exec"}
    HUB_MIN_FANOUT = 2   # an object shared by >HUB_CAP epics is treated as a hub (heavily damped)
    epic_edges = defaultdict(lambda: {"objects": 0, "strength": 0.0, "kinds": Counter()})
    for dst, epics in obj_epic.items():
        el = sorted(epics)
        if len(el) < 2:
            continue
        fanout = len(el)
        contrib = 1.0 / fanout                 # inverse-fanout dampening (IDF-style)
        for i in range(len(el)):
            for j in range(i+1, len(el)):
                a, b = el[i], el[j]
                key = (a, b)
                epic_edges[key]["objects"] += 1
                epic_edges[key]["strength"] += contrib
                aw = any(op in WRITE for op in epic_obj[a][dst])
                bw = any(op in WRITE for op in epic_obj[b][dst])
                if aw and not bw:
                    epic_edges[key]["kinds"]["a_writes_b_reads"] += 1
                elif bw and not aw:
                    epic_edges[key]["kinds"]["b_writes_a_reads"] += 1
                else:
                    epic_edges[key]["kinds"]["shared"] += 1

    # A real interaction edge must clear a dampened-strength floor (not just share a hub once).
    STRENGTH_FLOOR = 0.5   # ~ one specific shared object, or several rare-ish ones
    strong_edges = {k: d for k, d in epic_edges.items() if d["strength"] >= STRENGTH_FLOOR}

    # served-verticals for SYSTEM candidates: VERTICAL epics with a STRONG shared-object edge
    # (so a system rule counts as "serving" a vertical only on a real, specific dependency —
    # the two-signal safeguard: a compliance-serving system rule surfaces in its vertical).
    served = defaultdict(set)
    for (a, b), d in strong_edges.items():
        for x, y in ((a, b), (b, a)):
            if epic_role.get(x) == "SYSTEM" and epic_role.get(y) == "VERTICAL":
                served[x].add(y)

    report = {
        "stories_with_objects": sum(1 for s in story_obj if story_obj[s]),
        "distinct_objects_touched": len(obj_story),
        "raw_epic_edges": len(epic_edges),
        "strong_epic_edges": len(strong_edges),
        "system_candidates": sum(1 for r in epic_role.values() if r == "SYSTEM"),
        "system_serving_a_vertical": len(served),
        "system_serving_nothing": sum(1 for e, r in epic_role.items()
                                      if r == "SYSTEM" and e not in served),
    }
    if not commit:
        top = sorted(strong_edges.items(), key=lambda kv: -kv[1]["strength"])[:10]
        report["top_epic_edges"] = [
            {"a": epic_name.get(a, a)[:38], "b": epic_name.get(b, b)[:38],
             "strength": round(d["strength"], 1), "shared_objects": d["objects"],
             "kinds": dict(d["kinds"])}
            for (a, b), d in top]
        return report

    now = _now()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ontology_story_object (
            story_id TEXT, code_object TEXT, ops TEXT, PRIMARY KEY (story_id, code_object));
        CREATE TABLE IF NOT EXISTS ontology_epic_interaction (
            src_epic TEXT, dst_epic TEXT, shared_objects INTEGER, strength REAL, kinds TEXT,
            PRIMARY KEY (src_epic, dst_epic));
        """
    )
    conn.execute("DELETE FROM ontology_story_object")
    conn.execute("DELETE FROM ontology_epic_interaction")
    for st, objs in story_obj.items():
        for dst, ops in objs.items():
            conn.execute("INSERT OR REPLACE INTO ontology_story_object VALUES (?,?,?)",
                         (st, dst, json.dumps(dict(ops))))
    for (a, b), d in strong_edges.items():   # persist only the strong (hub-damped) edges
        conn.execute("INSERT OR REPLACE INTO ontology_epic_interaction VALUES (?,?,?,?,?)",
                     (a, b, d["objects"], round(d["strength"], 3), json.dumps(dict(d["kinds"]))))
    # refine SYSTEM candidates: record served-verticals + resolve split
    for e, r in epic_role.items():
        if r != "SYSTEM":
            continue
        sv = sorted(served.get(e, []))
        split = "system_wide_business_rule" if sv else "true_plumbing_candidate"
        conn.execute(
            "UPDATE ontology_epic_class SET system_split=?, served_verticals=? WHERE epic_id=?",
            (split, json.dumps([epic_name.get(x, x) for x in sv][:20]), e))
    conn.commit()
    report["mode"] = "committed"
    return report


def _ensure_served_col(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ontology_epic_class)")]
    if "served_verticals" not in cols:
        conn.execute("ALTER TABLE ontology_epic_class ADD COLUMN served_verticals TEXT")
        conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    if args.commit:
        _ensure_served_col(conn)
    print(json.dumps(build(conn, commit=args.commit), indent=2))


if __name__ == "__main__":
    main()
