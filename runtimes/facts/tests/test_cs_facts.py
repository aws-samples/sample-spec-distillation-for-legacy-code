# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""C# structural facts (no AWS, no network, no dotnet required).

`fixtures/cs-sample/` is a small synthetic corpus: a class, a method calling another method, a
`SqlCommand` carrying a stored-procedure name, a `const` threshold used in a comparison, inline SQL
over two tables, a DB column read through a row indexer, and a business rule stated only in a
comment. `fixtures/cs_sample_facts_raw.json.gz` is a recorded run of the real structure tool on it
(produced by runtimes/facts/structure on AL2023 with the .NET 8 SDK).

Every assertion below is on a FACT the C# rig has to produce, keyed exactly as the VB rig keys it —
the point of the C# work is that the two sections are interchangeable to everything downstream.

When a `dotnet` SDK is on PATH the tests re-run the real tool and assert the live output agrees with
the recording on the facts that matter, so the recording cannot silently drift from the parser.

Run:  python3 -m pytest -q runtimes/facts/tests/test_cs_facts.py
"""
from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
FACTS = REPO / "runtimes" / "facts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CS_SAMPLE = FIXTURES / "cs-sample"
CS_RAW = FIXTURES / "cs_sample_facts_raw.json.gz"
SERVICE = "ClientSide/ApInvoiceService.cs"
PAYMENT = "ClientSide/ApPaymentService.cs"
PROC_SQL = "Scripts/StoredProcedures/usp_ApInvPost.sql"

sys.path.insert(0, str(FACTS))
import facts_post  # noqa: E402


# ----------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def raw() -> dict:
    return json.loads(gzip.open(CS_RAW, "rt").read())


@pytest.fixture(scope="module")
def enriched(raw) -> dict:
    return facts_post.enrich(json.loads(json.dumps(raw)), CS_SAMPLE)


def _by(rows, key, value):
    return [r for r in rows if r.get(key) == value]


# ----------------------------------------------------------------------------- the raw facts
def test_cs_section_has_the_same_keys_as_vb(raw):
    """Interchangeability is the whole point: everything downstream reads one shape."""
    assert set(raw["cs"]) == set(raw["vb"]), set(raw["cs"]) ^ set(raw["vb"])
    assert raw["tools"]["cs"].startswith("Microsoft.CodeAnalysis.CSharp")
    langs = {f["file"]: f["language"] for f in raw["files"]}
    assert langs[SERVICE] == "c#" and langs[PROC_SQL] == "t-sql"
    # VB behaviour unchanged: with no .vb files every VB list is empty, not missing
    assert all(raw["vb"][k] == [] for k in raw["vb"])


def test_types_and_methods_with_line_numbers(raw):
    types = {t["name"]: t for t in raw["cs"]["types"]}
    assert set(types) == {"ApInvoiceService", "ApInvoiceRepository", "ApPaymentService"}
    assert types["ApInvoiceService"]["kind"] == "class"
    assert types["ApInvoiceService"]["file"] == SERVICE
    assert types["ApInvoiceService"]["start"] < types["ApInvoiceService"]["end"]

    methods = {m["id"]: m for m in raw["cs"]["methods"]}
    assert "ApInvoiceService.PostInvoice" in methods
    assert methods["ApInvoiceService.PostInvoice"]["access"] == "public"
    assert methods["ApInvoiceService.RouteToApproval"]["access"] == "private"
    assert methods["ApInvoiceService.ApInvoiceService"]["kind"] == "Constructor"
    for m in methods.values():
        assert m["start"] >= 1 and m["end"] >= m["start"]


def test_method_to_method_calls_resolve_across_classes(raw):
    calls = {(c["from"], c["to"]): c for c in raw["cs"]["calls"]}
    # same-class call
    assert ("ApInvoiceService.PostInvoice", "ApInvoiceService.RouteToApproval") in calls
    assert calls[("ApInvoiceService.PostInvoice",
                  "ApInvoiceService.RouteToApproval")]["resolution"] == "same-class"
    # call through a field whose declared type names the callee's class
    assert ("ApInvoiceService.PostInvoice", "ApInvoiceRepository.LoadHeader") in calls
    assert calls[("ApInvoiceService.PostInvoice",
                  "ApInvoiceRepository.LoadHeader")]["resolution"] == "receiver-type"
    # cross-FILE call: the callee lives in the other C# file
    assert ("ApPaymentService.Validate", "ApInvoiceRepository.LoadHeader") in calls
    for c in raw["cs"]["calls"]:
        assert c["line"] >= 1 and c["file"].endswith(".cs") and c["callee_file"].endswith(".cs")


def test_sqlcommand_proc_name_is_found_and_bound_to_the_proc(raw):
    refs = _by(raw["cs"]["proc_refs"], "proc", "usp_ApInvPost")
    assert refs, raw["cs"]["proc_refs"]
    # the SqlCommand("usp_ApInvPost", cn) site, attributed to its enclosing method
    from_exec = [r for r in refs if r["from"] == "ApInvoiceRepository.ExecutePost"]
    assert len(from_exec) == 1
    assert from_exec[0]["defined_in_corpus"] is True       # the .sql file declares it
    assert "SqlCommand" in from_exec[0]["text"]
    assert from_exec[0]["line"] >= 1


def test_const_threshold_carries_its_value_line_and_use(raw):
    consts = {c["name"]: c for c in raw["cs"]["constants"] if c["file"] == SERVICE}
    assert consts["ApprovalThreshold"]["value"] == "5000m"
    assert consts["ApprovalThreshold"]["type"] == "decimal"
    assert consts["ApprovalThreshold"]["start"] >= 1
    uses = _by(raw["cs"]["const_uses"], "const", "ApprovalThreshold")
    assert uses, "the threshold must be linked to the comparison that uses it"
    assert any(u["comparison"] == ">=" for u in uses)


def test_comparisons_split_operands_and_resolve_the_constant_value(raw):
    cmp_ = [c for c in raw["cs"]["comparisons"]
            if c["from"] == "ApInvoiceService.PostInvoice" and c["op"] == ">="]
    assert len(cmp_) == 1
    c = cmp_[0]
    assert "grossamt" in [i.lower() for i in c["left"]["ids"]]
    assert c["right"]["consts"] == ["ApprovalThreshold"]
    # the C# decimal suffix must normalise, or no C#-vs-T-SQL threshold can ever match
    assert c["right"]["value"] == "5000"
    assert c["context"] == "if"


def test_inline_sql_sites_name_the_tables_they_read_and_write(raw):
    sites = {s["from"]: s for s in raw["cs"]["inline_sql"]}
    assert {t["table"] for t in sites["ApInvoiceRepository.LoadHeader"]["reads"]} == {"ApInvHdr"}
    assert {t["table"] for t in sites["ApInvoiceRepository.LoadLines"]["reads"]} == {"ApInvLine"}
    w = sites["ApInvoiceRepository.SetStatus"]["writes"]
    assert [(x["table"], x["op"]) for x in w] == [("ApInvHdr", "UPDATE")]
    for s in raw["cs"]["inline_sql"]:
        assert s["parse_ok"] is True, s.get("parse_error")
        assert s["start"] >= 1 and s["end"] >= s["start"]


def test_column_reference_through_a_row_indexer(raw):
    gross = [c for c in raw["cs"]["column_refs"] if c["name"] == "GrossAmt" and c["how"] == "read"]
    assert gross, raw["cs"]["column_refs"]
    assert gross[0]["file"] == SERVICE
    assert any(c["how"] == "sql-param" for c in raw["cs"]["column_refs"])


def test_comment_stated_rule_is_captured_with_its_line_range(raw):
    cs_comments = [c for c in raw["comments"] if c["language"] == "c#"]
    rules = [c for c in cs_comments if "Business rule" in c["text"]]
    assert len(rules) >= 2, [c["text"][:60] for c in cs_comments]
    for c in rules:
        assert c["start"] >= 1 and c["end"] >= c["start"]
    # the rule that exists ONLY in a comment (nothing in the C# enforces the stricter boundary)
    assert any("STRICTER boundary" in c["text"] for c in cs_comments)
    # /// doc comments are captured too
    assert any("<summary>" in c["text"] for c in cs_comments)


def test_counts_include_the_cs_section(raw):
    c = raw["derived"]["counts"]
    for k in ("cs_types", "cs_methods", "cs_calls", "cs_proc_refs", "cs_inline_sql",
              "cs_constants", "cs_comparisons", "cs_column_refs"):
        assert c[k] > 0, k
    assert c["vb_methods"] == 0            # VB untouched by a C#-only corpus


def test_proc_index_lists_the_cs_caller(raw):
    pi = {p["proc"]: p for p in raw["derived"]["proc_index"]}
    p = pi["usp_ApInvPost"]
    assert p["defined"] is True and p["file"] == PROC_SQL
    assert any(c["from"] == "ApInvoiceRepository.ExecutePost" for c in p["cs_callers"])
    assert p["vb_callers"] == []


# ----------------------------------------------------------------------------- facts_post
def test_units_and_graph_use_a_cs_prefix(enriched):
    ids = {u["id"] for u in enriched["v1"]["units"]}
    assert "cs:ApInvoiceService.PostInvoice" in ids
    assert "sql:usp_ApInvPost" in ids
    g = facts_post.build_graph(enriched, enriched["v1"])
    edges = {(e["src"], e["dst"], e["kind"]) for e in g["edges"]}
    assert ("cs:ApInvoiceService.PostInvoice", "cs:ApInvoiceService.RouteToApproval", "calls") in edges
    assert ("cs:ApInvoiceRepository.ExecutePost", "sql:usp_ApInvPost", "exec") in edges
    assert ("cs:ApInvoiceRepository.LoadHeader", "table:ApInvHdr", "reads") in edges
    assert ("cs:ApInvoiceRepository.SetStatus", "table:ApInvHdr", "writes") in edges
    assert ("cstype:ApInvoiceService", "cs:ApInvoiceService.PostInvoice", "member") in edges
    # a proc name in a const initialiser has no enclosing method -> attributed to the FILE,
    # never to an invented `cs:None` unit
    assert not any(str(s).endswith(":None") or str(dd).endswith(":None") for s, dd, _ in edges)


def test_cross_layer_candidate_spans_cs_and_tsql(enriched):
    """The C# facts must feed the SAME inconsistency detector the VB facts feed."""
    cands = enriched["v1"]["cross_layer_candidates"]
    assert cands, "the C#/T-SQL boundary drift on 5000 must be reported"
    g = cands[0]
    assert set(g["layers"]) == {"client", "db"}
    assert "boundary differs" in g["differs"]
    units = {m["unit"] for m in g["members"]}
    assert any(u.startswith("cs:") for u in units) and "sql:usp_ApInvPost" in units
    assert {m["value"] for m in g["members"]} == {"5000"}
    assert {m["op"] for m in g["members"]} == {">=", ">"}


def test_duplicate_logic_candidates_span_cs_files(enriched):
    kinds = {d["kind"] for d in enriched["v1"]["duplicate_candidates"]}
    assert {"same-expression", "same-message", "same-threshold-check"} <= kinds, kinds
    thr = [d for d in enriched["v1"]["duplicate_candidates"] if d["kind"] == "same-threshold-check"]
    files = {m["cite"].split(":L")[0] for g in thr for m in g["members"]}
    assert len(files) >= 2, files
    msg = [d for d in enriched["v1"]["duplicate_candidates"] if d["kind"] == "same-message"]
    assert any(".cs" in m["cite"] and True for g in msg for m in g["members"])


def test_contracts_report_the_cs_public_api(enriched):
    api = {x["method"] for x in enriched["v1"]["contracts"]["public_api"]}
    assert "ApInvoiceService.PostInvoice" in api
    # C# spells the modifier lowercase; a case-sensitive check would silently return nothing
    assert "ApInvoiceService.RouteToApproval" not in api       # private
    proc = enriched["v1"]["contracts"]["procs"][0]
    assert proc["cs_callers"] and proc["entry_point"] is False


def test_page_block_renders_cs_facts(enriched):
    import facts_block
    units = {SERVICE: [(1, 200)], PAYMENT: [(1, 200)], PROC_SQL: [(1, 200)]}
    block = facts_block.render_page_block(enriched, units, 6000)
    assert block.startswith(facts_block.HEADER)
    assert "C#" in facts_block.HEADER
    assert "ApInvoiceService.cs" in block
    assert "usp_ApInvPost" in block
    assert "ApprovalThreshold = 5000m" in block


# ----------------------------------------------------------------------------- live parser
def _dotnet_tool() -> list[str] | None:
    """The published tool, if a local .NET SDK can produce one. None -> skip the live check."""
    if not shutil.which("dotnet"):
        return None
    dll = FACTS / "structure" / "out" / "LeanStructure.dll"
    if not dll.is_file():
        r = subprocess.run(["dotnet", "publish", "-c", "Release", "-o", str(dll.parent)],
                           cwd=FACTS / "structure", capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not dll.is_file():
            return None
    return ["dotnet", str(dll)]


def test_recording_matches_the_live_parser():
    """Guards the recording against parser drift. Skipped where there is no .NET SDK."""
    tool = _dotnet_tool()
    if tool is None:
        pytest.skip("no usable local .NET 8 SDK; the recorded fixture is used instead "
                    "(see runtimes/facts/README.md for the container recipe)")
    r = subprocess.run(tool + [str(CS_SAMPLE)], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    live = json.loads(r.stdout)
    rec = json.loads(gzip.open(CS_RAW, "rt").read())
    for key in ("types", "methods", "calls", "proc_refs", "constants", "inline_sql",
                "comparisons", "column_refs", "const_uses"):
        assert len(live["cs"][key]) == len(rec["cs"][key]), key
    assert ({(c["from"], c["to"]) for c in live["cs"]["calls"]}
            == {(c["from"], c["to"]) for c in rec["cs"]["calls"]})
    assert live["derived"]["counts"] == rec["derived"]["counts"]
