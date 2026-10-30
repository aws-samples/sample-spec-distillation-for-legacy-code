# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Section 2 — the headline metric: the LLM code graph scored against the deterministic parser.

`<output>/facts/graph.json` is produced by a Roslyn VB + ScriptDom T-SQL pass (runtimes/facts),
so for the relations it models it is ground truth, not another opinion. Scoring the LLM-built
`code_graph.json` against it gives a defensible accuracy number with no judge in the loop.

Both graphs are projected into one comparable space:

    (source file, dependency target bare name, relation)

    relation        parser edge                      LLM code_graph edge
    ------------    ----------------------------     ---------------------------------------------
    table_read      reads   unit -> table            REFERENCES kind=table/view, select|join
    table_write     writes  unit -> table            REFERENCES kind=table/view, insert|update|delete
    proc_exec       exec    unit -> proc             REFERENCES kind=stored_procedure|function|procedure
    trigger_on_tbl  fires   table -> trigger         REFERENCES kind=trigger
    method_call     calls   unit -> unit             CALLS kind=method        (CORE set, see below)

The CORE relation set — table_read / table_write / proc_exec / trigger_on_table — is the headline,
and its definition is FROZEN so the number stays comparable to the recorded baseline
(97.56% precision / 66.67% recall on output/ap-invoices-demo).

`method_call` is scored separately, under `extended`. The LLM VB/C# prompts used to emit only
`Imports` namespaces and remote gateway namespaces, so parser `calls` edges were reported as "not
representable"; they are now expressible. Folding ~100 new parser edges into the headline
denominator would have silently redefined the metric, so the headline keeps the four core relations
and the new capability is measured next to it, not mixed into it.

Three precision/recall pairs are reported, strictest last, because they answer different questions:

  pair      did the LLM connect the right two things?        (relation label ignored)
  typed     ...and label the relation correctly?             <- headline
  directed  ...and get read vs write right?                  (= typed, kept explicit)

Relations the parser does not model at all (VB `Imports` namespace edges) are NOT counted as
false positives — they are reported separately as `not_comparable`. Likewise, parser edges the
extraction prompt explicitly excludes (e.g. `#temp` tables) are reported as `excluded` with the
reason, so recall is not quietly deflated by a scope difference.

Recall is broken out per source language, because the finding that drove this rig was a whole-layer
blind spot rather than a scattering of misses, and per PROVENANCE — a code_edge whose `symbol`
starts with `facts:` was backfilled from the parser itself, so scoring it against the parser is
circular. `by_provenance` gives the model-proposed-only numbers next to the combined ones; quote
the model-only recall when the question is "how good is the prompt".
"""
from __future__ import annotations

from collections import Counter

from .load import GraphBundle, bare_name

OP_READ = {"select", "join", "read", "join/select"}
OP_WRITE = {"insert", "update", "delete", "merge", "write", "upsert"}
PROC_KINDS = {"stored_procedure", "procedure", "function", "proc"}
TABLE_KINDS = {"table", "view", "synonym", None, "", "unknown"}
METHOD_KINDS = {"method", "sub", "function_member", "procedure_member"}
PARSER_REL = {"reads": "table_read", "writes": "table_write", "exec": "proc_exec",
              "calls": "method_call"}
CLASS = {"table_read": "table", "table_write": "table", "proc_exec": "proc",
         "trigger_on_table": "trigger", "method_call": "method"}
# The headline is computed over these only, so it stays comparable to the recorded baseline.
CORE_RELATIONS = ("table_read", "table_write", "proc_exec", "trigger_on_table")
EXTENDED_RELATIONS = ("method_call",)
# code_edges.symbol prefix written by code_graph.py for a relation BACKFILLED from facts.json.
FACTS_PROVENANCE = "facts:"
MAX_EXAMPLES = 60


def _pct(a: int, b: int) -> float | None:
    return round(100.0 * a / b, 2) if b else None


def _is_temp(name: str) -> bool:
    n = (name or "").strip().strip("[]\"'")
    return n.startswith("#") or n.startswith("@") or n.lower().startswith("tempdb.")


# ----------------------------------------------------------------------------- projections
def project_parser(graph: dict) -> tuple[set, list[dict], list[dict]]:
    """-> (triples, excluded, not_representable)"""
    nodes = {n.get("id"): n for n in (graph.get("nodes") or [])}
    triples: set = set()
    excluded: list[dict] = []
    not_repr: list[dict] = []
    for e in graph.get("edges") or []:
        kind = e.get("kind")
        s, t = e.get("src"), e.get("dst")
        sn, tn = nodes.get(s, {}), nodes.get(t, {})
        if kind in PARSER_REL:
            f = e.get("file") or sn.get("file")
            raw = tn.get("name") or (str(t).split(":", 1)[-1] if t else "")
            if _is_temp(raw):
                excluded.append({"src": f, "dst": raw, "relation": PARSER_REL[kind],
                                 "reason": "temp table / variable — excluded by the extraction prompt"})
                continue
            if not f:
                excluded.append({"src": None, "dst": raw, "relation": PARSER_REL[kind],
                                 "reason": "parser edge carries no source file"})
                continue
            triples.add((f, bare_name(raw), PARSER_REL[kind]))
        elif kind == "fires":
            f = tn.get("file") or e.get("file")
            raw = sn.get("name") or (str(s).split(":", 1)[-1] if s else "")
            if f:
                triples.add((f, bare_name(raw), "trigger_on_table"))
    return triples, excluded, not_repr


def project_llm(code_edges: list[dict]) -> tuple[set, list[dict], dict[tuple, set]]:
    """-> (triples, not_comparable, provenance_of_triple)

    `provenance_of_triple[(file, dst, relation)]` is the set of provenances that produced it:
    "model" for a relation the extraction prompt proposed, "parser_backfill" for one code_graph.py
    copied from facts.json. Scoring a parser-backfilled edge against the parser is circular, so the
    caller reports the two populations separately.
    """
    triples: set = set()
    noncomp: list[dict] = []
    prov: dict[tuple, set] = {}
    for e in code_edges:
        etype = (e.get("type") or "").upper()
        kind = (e.get("kind") or "").lower() or None
        op = (e.get("operation") or "").lower()
        direction = (e.get("direction") or "").lower()
        dst = e.get("dst")
        symbol = str(e.get("symbol") or "")
        if etype == "IMPORTS":
            noncomp.append({"src": e.get("src"), "dst": dst, "type": etype,
                            "reason": "namespace import — the parser graph has no import relation"})
            continue
        if kind == "comment_mention" or etype == "MENTIONS":
            # code_graph.py relabels a dependency whose name occurs ONLY inside a comment: the
            # mention is real and kept, but it is not an access, so it is not scored as one. The
            # parser models this separately too (`comment_mentions` in facts graph.json).
            noncomp.append({"src": e.get("src"), "dst": dst, "type": etype,
                            "reason": "named only in a comment — a mention, not a dependency "
                                      "(the parser models comment mentions separately)"})
            continue
        if _is_temp(str(dst)):
            noncomp.append({"src": e.get("src"), "dst": dst, "type": etype,
                            "reason": "temp table / variable"})
            continue
        if kind in METHOD_KINDS or (etype == "CALLS" and kind in METHOD_KINDS):
            rel = "method_call"
        elif kind == "trigger":
            rel = "trigger_on_table"
        elif kind in PROC_KINDS:
            rel = "proc_exec"
        elif kind in TABLE_KINDS:
            # operation is the structural signal (the LLM's own `direction` disagrees with it on
            # some rows — code_graph.py records that as direction_verdict=conflict); fall back to
            # direction only when the operation verb is unknown.
            if op in OP_WRITE:
                rel = "table_write"
            elif op in OP_READ:
                rel = "table_read"
            else:
                rel = "table_write" if direction == "write" else "table_read"
        elif etype == "CALLS":
            # COBOL `CALL 'PROG'` and any CALLS edge with no method kind: a program/proc invocation
            rel = "proc_exec"
        else:
            noncomp.append({"src": e.get("src"), "dst": dst, "type": etype, "kind": kind,
                            "reason": f"kind={kind!r} has no parser counterpart"})
            continue
        key = (str(e.get("src")), bare_name(dst), rel)
        triples.add(key)
        prov.setdefault(key, set()).add(
            "parser_backfill" if symbol.startswith(FACTS_PROVENANCE) else "model")
    return triples, noncomp, prov


# ----------------------------------------------------------------------------- scoring
def _score(llm: set, parser: set) -> dict:
    tp = llm & parser
    return {"llm_edges": len(llm), "parser_edges": len(parser), "confirmed": len(tp),
            "false_positives": len(llm - parser), "missing": len(parser - llm),
            "precision_pct": _pct(len(tp), len(llm)), "recall_pct": _pct(len(tp), len(parser)),
            "f1_pct": _pct(2 * len(tp), len(llm) + len(parser))}


def compare(b: GraphBundle) -> dict:
    if b.parser_graph is None:
        reason = ("<output>/facts/graph.json is absent"
                  + (" (facts.json is present — run `python3 runtimes/facts/facts_post.py --run "
                     "<output>/facts` to produce it)" if b.facts_present
                     else " and facts.json is absent — the deterministic parser did not run"))
        return {"status": "skipped", "reason": reason,
                "note": "no parser ground truth, so no accuracy number; sections 1, 3 and 4 are "
                        "unaffected and the thresholds for this section are reported as null."}

    parser_all, p_excluded, p_notrepr = project_parser(b.parser_graph)
    llm_all, noncomp, prov = project_llm(b.code_edges)

    # The HEADLINE is the frozen core relation set, so the number means the same thing it meant on
    # the recorded baseline. method_call is scored next to it, never folded into it.
    parser = {x for x in parser_all if x[2] in CORE_RELATIONS}
    llm = {x for x in llm_all if x[2] in CORE_RELATIONS}

    typed = _score(llm, parser)
    pair_l = {(a, d) for a, d, _ in llm}
    pair_p = {(a, d) for a, d, _ in parser}
    pair = _score(pair_l, pair_p)

    # the extended set (relations the LLM graph only recently became able to express)
    parser_ext = {x for x in parser_all if x[2] in EXTENDED_RELATIONS}
    llm_ext = {x for x in llm_all if x[2] in EXTENDED_RELATIONS}
    extended = {"relations": list(EXTENDED_RELATIONS), **_score(llm_ext, parser_ext),
                "note": "scored separately so the headline stays comparable to the baseline, in "
                        "which the LLM graph could not express these relations at all"}

    # provenance split: model-proposed edges only vs everything. Scoring a parser-backfilled edge
    # against the parser is circular, so the model-only numbers are the ones that measure the prompt.
    def _by_prov(which: str, rels) -> set:
        return {x for x in llm_all if x[2] in rels and which in prov.get(x, set())}

    by_provenance = {
        "model_proposed": {"core": _score(_by_prov("model", CORE_RELATIONS), parser),
                           "extended": _score(_by_prov("model", EXTENDED_RELATIONS), parser_ext)},
        "parser_backfilled_edges": sum(1 for x in llm_all
                                       if prov.get(x, set()) == {"parser_backfill"}),
        "model_and_parser_agree_edges": sum(1 for x in llm_all if len(prov.get(x, set())) > 1),
        "note": "`model_proposed` counts only code_edges the extraction prompt produced "
                "(code_edges.symbol does NOT start with 'facts:'); its recall is the honest "
                "measure of the prompt, since a parser-backfilled edge is scored against its own "
                "source.",
    }

    # per relation (over the full relation set, so nothing disappears from the table)
    per_rel = {}
    for rel in sorted({r for _, _, r in parser_all} | {r for _, _, r in llm_all}):
        per_rel[rel] = _score({x for x in llm_all if x[2] == rel},
                              {x for x in parser_all if x[2] == rel})

    # per language of the source file
    lang_of = {str(f.get("path")): f.get("lang") for f in b.code_files}

    def lang(path: str) -> str:
        if path in lang_of and lang_of[path]:
            return lang_of[path]
        p = str(path).lower()
        for ext, lg in ((".sql", "sql"), (".vb", "vbnet"), (".cs", "csharp"),
                        (".cob", "cobol"), (".cbl", "cobol")):
            if p.endswith(ext):
                return lg
        return "other"

    per_lang = {}
    per_lang_model = {}
    llm_model_core = _by_prov("model", CORE_RELATIONS)
    for lg in sorted({lang(a) for a, _, _ in parser} | {lang(a) for a, _, _ in llm}):
        per_lang[lg] = _score({x for x in llm if lang(x[0]) == lg},
                              {x for x in parser if lang(x[0]) == lg})
        per_lang_model[lg] = _score({x for x in llm_model_core if lang(x[0]) == lg},
                                    {x for x in parser if lang(x[0]) == lg})

    # classify false positives: a pair the parser also connects (relation mislabelled) vs
    # a pair the parser does not connect at all (the only class that can be a fabrication)
    fps = sorted(llm - parser)
    relabelled = [x for x in fps if (x[0], x[1]) in pair_p]
    unknown_pair = [x for x in fps if (x[0], x[1]) not in pair_p]
    fp_detail = []
    for a, d, r in fps[:MAX_EXAMPLES]:
        also = sorted(rr for (aa, dd, rr) in parser if (aa, dd) == (a, d))
        fp_detail.append({"src": a, "dst": d, "llm_relation": r,
                          "parser_relations_for_same_pair": also,
                          "class": "relation_mislabelled" if also else "pair_not_in_parser"})

    missing = sorted(parser - llm)
    miss_by_lang: Counter = Counter(lang(a) for a, _, _ in missing)
    miss_by_rel: Counter = Counter(r for _, _, r in missing)

    return {
        "status": "ok",
        "parser_graph_schema": b.parser_graph.get("schema"),
        "headline": {
            "parser_confirmed_edge_precision_pct": typed["precision_pct"],
            "parser_edge_recall_pct": typed["recall_pct"],
            "pair_precision_pct": pair["precision_pct"],
            "pair_recall_pct": pair["recall_pct"],
            "f1_pct": typed["f1_pct"],
        },
        "core_relations": list(CORE_RELATIONS),
        "typed": typed,
        "pair": pair,
        "extended": extended,
        "by_provenance": by_provenance,
        "by_relation": per_rel,
        "by_source_language": per_lang,
        "by_source_language_model_proposed": per_lang_model,
        "false_positives": {
            "total": len(fps),
            "relation_mislabelled": len(relabelled),
            "pair_not_in_parser": len(unknown_pair),
            "examples": fp_detail,
        },
        "missing_from_llm_graph": {
            "total": len(missing),
            "by_source_language": dict(miss_by_lang),
            "by_relation": dict(miss_by_rel),
            "examples": [{"src": a, "dst": d, "relation": r} for a, d, r in missing[:MAX_EXAMPLES]],
        },
        "scope": {
            "llm_edges_not_comparable": len(noncomp),
            "llm_not_comparable_by_reason": dict(Counter(x["reason"] for x in noncomp)),
            "parser_edges_excluded": len(p_excluded),
            "parser_excluded_by_reason": dict(Counter(x["reason"] for x in p_excluded)),
            "parser_edges_not_representable": len(p_notrepr),
            "parser_not_representable_by_reason": dict(Counter(x["reason"] for x in p_notrepr)),
            "parser_edges_outside_the_core_set": len(parser_ext),
            "note": "the headline is computed over the FROZEN core relation set "
                    f"{list(CORE_RELATIONS)}; the counts above are the scope difference, reported "
                    "rather than absorbed. Parser method->method `calls` edges used to be "
                    "'not representable' (the VB/C# prompts emitted no callees); they are now "
                    "expressible and are scored under `extended`, not in the headline, so the "
                    "headline denominator did not move.",
        },
    }
