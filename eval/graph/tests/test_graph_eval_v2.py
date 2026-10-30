# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""graph_eval, second generation: the bridge's own assertion, the extended relation set, provenance.

What changed in the pipeline, and therefore what has to be scored:

  * the bridge join carries the dotted qualifier and records how it resolved, so a link asserts
    something CHECKABLE (`join_name` occurs at identifier boundaries) instead of "the last dotted
    segment appears somewhere in this file";
  * `export_graph` refuses to emit a link it cannot verify and keeps a `dropped[]` ledger, so
    failing closed is visible rather than looking like a clean bridge;
  * the VB/C# extraction prompts now emit method->method calls, which the parser graph has had all
    along — scored as an EXTENDED relation so the frozen headline stays comparable;
  * `code_graph.py` backfills relations from `facts.json`, so recall has to be reported per
    provenance: scoring a parser-backfilled edge against the parser is circular;
  * both ends of a bridge link use one canonical concept key, so `exact` is the expected tier.

No AWS, no network.  Run:  python3 -m pytest -q eval/graph/tests/test_graph_eval_v2.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import write_run, write_source  # noqa: E402  (tests dir on sys.path)
from graph import load, parity, report, structural  # noqa: E402

MCP = Path(__file__).resolve().parents[3] / "runtimes" / "mcp-server"
if str(MCP) not in sys.path:
    sys.path.insert(0, str(MCP))
import concept_key as ckey  # noqa: E402

BL_VB = """\
Public Class clsAPInvoiceBL
    Public Function ValidateHeader(ByVal Vendor As Integer) As ArrayList
        Dim hdr As DataRow = m_DAL.GetHeader(Vendor)
        Return errs
    End Function
End Class
"""
DESIGNER_VB = """\
Partial Class frmAPInvoiceEntry
    Friend WithEvents cboVendor As System.Windows.Forms.ComboBox
End Class
"""
TABLES_SQL = "CREATE TABLE dbo.APInvHdr (APCo tinyint, Vendor int, Status char(1))\n"


def _run(tmp_path, *, links, dropped=None, kg=None, code=None):
    root = tmp_path / "src"
    write_source(root, {"ClientSide/clsAPInvoiceBL.vb": BL_VB,
                        "ClientSide/frmAPInvoiceEntry.Designer.vb": DESIGNER_VB,
                        "Scripts/AP_Tables.sql": TABLES_SQL})
    kg = kg or {"nodes": [{"id": "a" * 16, "title": "Entity: AP Invoice Header",
                           "block_type": "entity", "source_still": "data",
                           "canonical_key": "data::ap invoice header"}],
                "edges": []}
    code = code or {"source_files": [], "code_objects": [], "code_edges": [], "code_entities": []}
    bridge = {"links": links, "dropped": dropped if dropped is not None else []}
    run = write_run(tmp_path / "run", kg=kg, code=code, bridge=bridge)
    return load.load_bundle(run, root)


# ----------------------------------------------------------------------------- bridge assertion
def test_resolved_qualifier_is_not_counted_as_a_failure(tmp_path):
    """`h.Vendor` is not verbatim in the VB file, but the link says the qualifier resolved to
    APInvHdr and `Vendor` really is there at identifier boundaries. That is a true assertion."""
    b = _run(tmp_path, links=[{
        "concept": "data::ap invoice header", "symbol": "h.Vendor", "qualifier": "h",
        "join_name": "Vendor", "qualifier_resolved_to": "APInvHdr",
        "qualifier_corroboration": "named", "resolution": "qualifier_table_by_column",
        "match": "code_entity", "code_path": "ClientSide/clsAPInvoiceBL.vb", "code_name": "Vendor"}])
    br = structural.check(b)["bridge"]
    assert br["failure_classes"] == {"qualifier_resolved": 1}
    assert br["tiers"]["full_sub"]["passed"] == 0        # the symbol itself is not verbatim
    assert br["tiers"]["join_word"]["passed"] == 1       # what it CLAIMS is there, is there
    assert br["link_assertion_pct"] == 100.0
    assert br["headline_symbol_occurrence_pct"] == 0.0   # the old metric, unchanged in meaning
    assert br["resolutions"] == {"qualifier_table_by_column": 1}
    assert br["links_with_a_join_name"] == 1


def test_substring_only_is_still_a_failure_when_a_link_claims_it(tmp_path):
    """If a bridge ever emits a link into the Designer file, the scorer must still catch it: the
    scorer does not take the join's word for it."""
    b = _run(tmp_path, links=[{
        "concept": "data::ap invoice header", "symbol": "h.Vendor", "qualifier": "h",
        "join_name": "Vendor", "qualifier_resolved_to": "APInvHdr",
        "qualifier_corroboration": "named", "resolution": "qualifier_table",
        "match": "code_entity", "code_path": "ClientSide/frmAPInvoiceEntry.Designer.vb",
        "code_name": "Vendor"}])
    s = structural.check(b)
    br = s["bridge"]
    assert br["failure_classes"] == {"substring_only": 1}
    assert br["link_assertion_pct"] == 0.0
    th = report.thresholds({"structural": s})
    assert th["bridge_substring_only_links"] == 1
    assert th["bridge_link_assertion_pct"] == 0.0


def test_unresolved_qualifier_without_a_resolution_is_the_legacy_class(tmp_path):
    """A bridge from before the fix carries no join_name: it is scored exactly as before."""
    b = _run(tmp_path, links=[{
        "concept": "data::ap invoice header", "symbol": "h.Vendor", "match": "code_entity",
        "code_path": "ClientSide/clsAPInvoiceBL.vb", "code_name": "Vendor"}])
    br = structural.check(b)["bridge"]
    assert br["failure_classes"] == {"qualifier_dropped": 1}
    assert br["links_with_a_join_name"] == 0
    assert br["link_assertion_pct"] == 0.0


def test_flags_and_the_dropped_ledger_are_reported(tmp_path):
    b = _run(tmp_path,
             links=[{"concept": "data::ap invoice header", "symbol": "APInvHdr.Vendor",
                     "qualifier": "apinvhdr", "join_name": "Vendor",
                     "qualifier_resolved_to": "APInvHdr", "qualifier_corroboration": "column",
                     "resolution": "qualifier_table", "flags": ["weak_qualifier"],
                     "match": "code_entity", "code_path": "ClientSide/clsAPInvoiceBL.vb",
                     "code_name": "Vendor"}],
             dropped=[{"symbol": "h.Vendor", "code_path": "ClientSide/frmAPInvoiceEntry.Designer.vb",
                       "reason": "substring_only", "detail": "..."},
                      {"symbol": "Nope", "code_path": "x.vb", "reason": "name_absent",
                       "detail": "..."}])
    s = structural.check(b)
    br = s["bridge"]
    assert br["flags"] == {"weak_qualifier": 1}
    assert br["dropped_by_the_join"] == 2
    assert br["dropped_reasons"] == {"substring_only": 1, "name_absent": 1}
    th = report.thresholds({"structural": s})
    assert th["bridge_links_dropped_by_the_join"] == 2
    # dropping is not a hard failure: the bridge refusing to guess is correct behaviour
    assert s["hard_failures"] == 0


def test_canonical_key_makes_the_concept_end_exact(tmp_path):
    """Both artifacts carry the key concept_key.py defines, so the tier is `exact` — no ladder."""
    b = _run(tmp_path, links=[{
        "concept": ckey.concept_key("data", "[DT-0037] Entity: AP Invoice Header"),
        "symbol": "Vendor", "join_name": "Vendor", "resolution": "bare", "match": "code_entity",
        "code_path": "ClientSide/clsAPInvoiceBL.vb", "code_name": "Vendor"}])
    crn = structural.check(b)["bridge"]["concept_resolution"]
    assert crn["by_tier_keys"] == {"exact": 1}
    assert crn["links_exact_pct"] == 100.0
    assert crn["unresolved"] == 0


def test_canonical_key_is_derived_when_a_node_does_not_store_one(tmp_path):
    """An older run has no `canonical_key` on its nodes. The resolver derives it with the SAME
    function the writer uses, so the key still matches by equality."""
    kg = {"nodes": [{"id": "a" * 16, "title": "Data Invariant: AP Invoice Header",
                     "block_type": "entity", "source_still": "data"}], "edges": []}
    b = _run(tmp_path, kg=kg, links=[{
        "concept": "data::ap invoice header", "symbol": "Vendor", "join_name": "Vendor",
        "resolution": "bare", "match": "code_entity",
        "code_path": "ClientSide/clsAPInvoiceBL.vb", "code_name": "Vendor"}])
    crn = structural.check(b)["bridge"]["concept_resolution"]
    assert crn["by_tier_keys"] == {"exact": 1}


# ----------------------------------------------------------------------------- parity
PARSER_GRAPH = {
    "schema": "lean-graph/v1",
    "nodes": [
        {"id": "vb:clsAPInvoiceBL.ValidateHeader", "kind": "vb-function",
         "name": "clsAPInvoiceBL.ValidateHeader", "file": "ClientSide/clsAPInvoiceBL.vb"},
        {"id": "vb:clsAPInvoiceDAL.GetHeader", "kind": "vb-function",
         "name": "clsAPInvoiceDAL.GetHeader", "file": "ClientSide/clsAPInvoiceDAL.vb"},
        {"id": "sql:usp_APInvPost", "kind": "procedure", "name": "usp_APInvPost",
         "file": "Scripts/usp_APInvPost.sql"},
        {"id": "table:APInvHdr", "kind": "table", "name": "APInvHdr", "file": "Scripts/AP_Tables.sql"},
        {"id": "trg:trg_APInvHdr_u", "kind": "trigger", "name": "trg_APInvHdr_u",
         "file": "Scripts/trg_APInvHdr_u.sql"},
    ],
    "edges": [
        # VB side — the whole-layer blind spot the fix is about
        {"src": "vb:clsAPInvoiceBL.ValidateHeader", "dst": "table:APInvHdr", "kind": "reads",
         "file": "ClientSide/clsAPInvoiceBL.vb", "line": 3},
        {"src": "vb:clsAPInvoiceBL.ValidateHeader", "dst": "table:APInvHdr", "kind": "writes",
         "file": "ClientSide/clsAPInvoiceBL.vb", "line": 4},
        {"src": "vb:clsAPInvoiceBL.ValidateHeader", "dst": "sql:usp_APInvPost", "kind": "exec",
         "file": "ClientSide/clsAPInvoiceBL.vb", "line": 5},
        # method -> method: expressible now, scored under `extended`
        {"src": "vb:clsAPInvoiceBL.ValidateHeader", "dst": "vb:clsAPInvoiceDAL.GetHeader",
         "kind": "calls", "file": "ClientSide/clsAPInvoiceBL.vb", "line": 3},
        # trigger attachment
        {"src": "table:APInvHdr", "dst": "trg:trg_APInvHdr_u", "kind": "fires",
         "file": "Scripts/trg_APInvHdr_u.sql", "line": 1},
    ],
}


def _edge(src, dst, kind, operation, direction, symbol=None):
    return {"src": src, "dst": dst, "type": "CALLS" if kind == "method" else "REFERENCES",
            "lang": "vbnet", "kind": kind, "operation": operation, "direction": direction,
            "resolution": "static", "symbol": symbol, "validated": 1, "dynamic": 0,
            "confidence": 0.9, "verdict": "confirmed", "direction_verdict": None,
            "derived_confidence": 0.9}


def _parity_bundle(tmp_path, code_edges, code_files=None):
    root = tmp_path / "src"
    write_source(root, {"ClientSide/clsAPInvoiceBL.vb": BL_VB})
    code = {"source_files": code_files or [
                {"path": "ClientSide/clsAPInvoiceBL.vb", "lang": "vbnet"},
                {"path": "Scripts/trg_APInvHdr_u.sql", "lang": "sql"}],
            "code_objects": [], "code_edges": code_edges, "code_entities": []}
    run = write_run(tmp_path / "run", code=code, parser_graph=PARSER_GRAPH, facts={"v1": {}})
    return load.load_bundle(run, root)


def test_vb_relations_now_score_against_the_parser(tmp_path):
    """The regression this whole change exists to prevent: VB recall of 0%."""
    vb = "ClientSide/clsAPInvoiceBL.vb"
    b = _parity_bundle(tmp_path, [
        _edge(vb, "APInvHdr", "table", "select", "read"),
        _edge(vb, "APInvHdr", "table", "update", "write"),
        _edge(vb, "usp_APInvPost", "stored_procedure", "exec", "call"),
        _edge("Scripts/trg_APInvHdr_u.sql", "APInvHdr", "trigger", "trigger_on_table", "write"),
    ])
    p = parity.compare(b)
    assert p["status"] == "ok"
    assert p["core_relations"] == list(parity.CORE_RELATIONS)
    assert p["typed"]["parser_edges"] == 4           # 3 VB + 1 trigger; `calls` is NOT in here
    assert p["typed"]["recall_pct"] == 100.0
    assert p["typed"]["precision_pct"] == 100.0
    assert p["by_source_language"]["vbnet"]["recall_pct"] == 100.0
    assert p["by_relation"]["trigger_on_table"]["recall_pct"] == 100.0
    th = report.thresholds({"parity": p, "structural": {}})
    assert th["parser_edge_recall_vbnet_pct"] == 100.0


def test_method_calls_are_scored_separately_from_the_frozen_headline(tmp_path):
    vb = "ClientSide/clsAPInvoiceBL.vb"
    base = [_edge(vb, "APInvHdr", "table", "select", "read"),
            _edge(vb, "APInvHdr", "table", "update", "write"),
            _edge(vb, "usp_APInvPost", "stored_procedure", "exec", "call"),
            _edge("Scripts/trg_APInvHdr_u.sql", "APInvHdr", "trigger", "trigger_on_table", "write")]
    without = parity.compare(_parity_bundle(tmp_path / "a", base))
    with_calls = parity.compare(_parity_bundle(
        tmp_path / "b", base + [_edge(vb, "clsAPInvoiceDAL.GetHeader", "method", "call", "call")]))
    # the headline denominator did NOT move when a new relation became expressible
    assert without["typed"] == with_calls["typed"]
    assert without["extended"]["parser_edges"] == with_calls["extended"]["parser_edges"] == 1
    assert without["extended"]["recall_pct"] == 0.0
    assert with_calls["extended"]["recall_pct"] == 100.0
    assert with_calls["extended"]["precision_pct"] == 100.0
    assert with_calls["by_relation"]["method_call"]["confirmed"] == 1
    th = report.thresholds({"parity": with_calls, "structural": {}})
    assert th["parser_method_call_recall_pct"] == 100.0
    # a method call must NOT be silently folded into proc_exec (which would be a false positive)
    assert with_calls["false_positives"]["total"] == 0


def test_provenance_splits_the_prompt_from_the_parser_backfill(tmp_path):
    """A parser-backfilled edge scored against the parser is circular; the split says so."""
    vb = "ClientSide/clsAPInvoiceBL.vb"
    b = _parity_bundle(tmp_path, [
        _edge(vb, "APInvHdr", "table", "select", "read"),                      # model
        _edge(vb, "APInvHdr", "table", "update", "write", "facts:vb_inline_sql"),   # backfilled
        _edge(vb, "usp_APInvPost", "stored_procedure", "exec", "call", "facts:vb_proc_ref"),
        _edge("Scripts/trg_APInvHdr_u.sql", "APInvHdr", "trigger", "trigger_on_table", "write",
              "facts:sql_trigger"),
    ])
    p = parity.compare(b)
    assert p["typed"]["recall_pct"] == 100.0                 # combined
    model = p["by_provenance"]["model_proposed"]["core"]
    assert model["llm_edges"] == 1 and model["confirmed"] == 1
    assert model["recall_pct"] == 25.0                       # the prompt alone found 1 of 4
    assert p["by_provenance"]["parser_backfilled_edges"] == 3
    assert p["by_source_language_model_proposed"]["vbnet"]["recall_pct"] == pytest.approx(33.33,
                                                                                         abs=0.01)
    th = report.thresholds({"parity": p, "structural": {}})
    assert th["parser_edge_recall_model_only_pct"] == 25.0
    assert th["parser_edge_recall_vbnet_model_only_pct"] == pytest.approx(33.33, abs=0.01)
    assert th["parser_edge_recall_pct"] == 100.0


def test_an_edge_both_populations_produced_is_counted_once(tmp_path):
    vb = "ClientSide/clsAPInvoiceBL.vb"
    b = _parity_bundle(tmp_path, [
        _edge(vb, "APInvHdr", "table", "select", "read"),
        _edge(vb, "APInvHdr", "table", "select", "read", "facts:vb_inline_sql"),
    ])
    p = parity.compare(b)
    assert p["typed"]["llm_edges"] == 1
    assert p["by_provenance"]["model_and_parser_agree_edges"] == 1
    assert p["by_provenance"]["model_proposed"]["core"]["llm_edges"] == 1


def test_a_comment_mention_is_not_scored_as_a_dependency(tmp_path):
    """code_graph.py relabels a name that occurs only inside a comment. It stays in the graph (the
    mention is real) but must not be counted as an access, in either direction."""
    vb = "ClientSide/clsAPInvoiceBL.vb"
    mention = _edge(vb, "APCoParm", "comment_mention", "mention", None,
                    "arbitrated:comment_only:table")
    mention["type"] = "MENTIONS"
    b = _parity_bundle(tmp_path, [_edge(vb, "APInvHdr", "table", "select", "read"), mention])
    p = parity.compare(b)
    assert p["typed"]["llm_edges"] == 1                 # the mention is not an LLM dependency edge
    assert p["false_positives"]["total"] == 0
    assert p["scope"]["llm_edges_not_comparable"] == 1
    assert any("mention, not a dependency" in r
               for r in p["scope"]["llm_not_comparable_by_reason"])


def test_parser_calls_are_no_longer_reported_as_not_representable(tmp_path):
    p = parity.compare(_parity_bundle(tmp_path, []))
    assert p["scope"]["parser_edges_not_representable"] == 0
    assert p["scope"]["parser_edges_outside_the_core_set"] == 1
    assert "scored under `extended`" in p["scope"]["note"]


def test_every_new_threshold_is_documented_and_produced():
    produced = set(report.thresholds({}))
    assert produced == set(report.THRESHOLD_DOC)
    for name in ("parser_edge_recall_vbnet_pct", "parser_edge_recall_model_only_pct",
                 "parser_edge_recall_vbnet_model_only_pct", "parser_method_call_recall_pct",
                 "parser_method_call_precision_pct", "bridge_link_assertion_pct",
                 "bridge_substring_only_links", "bridge_qualifier_dropped_links",
                 "bridge_links_dropped_by_the_join"):
        assert name in produced, name
        assert report.THRESHOLD_DOC[name][0] in ("min", "max")


def test_markdown_renders_the_new_sections(tmp_path):
    vb = "ClientSide/clsAPInvoiceBL.vb"
    b = _parity_bundle(tmp_path, [
        _edge(vb, "APInvHdr", "table", "select", "read"),
        _edge(vb, "clsAPInvoiceDAL.GetHeader", "method", "call", "call", "facts:vb_call"),
    ])
    res = {"meta": {}, "provenance": {"status": "unknown", "detail": "x"}, "counts": b.counts(),
           "structural": structural.check(b), "parity": parity.compare(b),
           "concept_coverage": {"status": "skipped", "reason": "no key"},
           "bridge_llm": {"status": "skipped", "reason": "no --llm"}, "notes": []}
    res["structural"]["bridge"].pop("_per_link", None)
    res["thresholds"] = report.thresholds(res)
    md = report.to_markdown(res)
    assert "By provenance" in md
    assert "Extended relation set" in md
    assert "Core relation set (frozen" in md
    assert "Recall % (model only)" in md
    assert "the link's own assertion holds" in md
    assert json.dumps(res, default=str)      # the whole result stays JSON-serialisable
