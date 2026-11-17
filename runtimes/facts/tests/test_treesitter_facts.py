# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tree-sitter structural facts (Python, Java, JavaScript/TypeScript). No AWS, no dotnet.

Fixtures: fixtures/py-sample (a synthetic order-handling package) and fixtures/ts-sample (a Java
billing pair and a TypeScript + JavaScript cart). What is under test: the extracted definitions,
line ranges, imports and resolved calls; that the sections have the shapes FactsIndex and
facts_block consume; and that build_facts produces facts without dotnet when the corpus has a
Tree-sitter language (and still skips, as before, when it has none).

Run:  python3 -m pytest -q runtimes/facts/tests/test_treesitter_facts.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")

REPO = Path(__file__).resolve().parents[3]
FACTS = REPO / "runtimes" / "facts"
MCP = REPO / "runtimes" / "mcp-server"
PY = Path(__file__).resolve().parent / "fixtures" / "py-sample"
TS = Path(__file__).resolve().parent / "fixtures" / "ts-sample"

sys.path.insert(0, str(FACTS))
sys.path.insert(0, str(MCP))
import build_facts  # noqa: E402
import facts_block  # noqa: E402
import treesitter_facts as tsf  # noqa: E402


def _rels(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.fixture(scope="module")
def py_facts():
    return tsf.extract(PY, _rels(PY))


def _calls(sect: dict) -> set[tuple[str, str, str]]:
    return {(c["from"], c["to"], c["resolution"]) for c in sect["calls"]}


# ── Python ────────────────────────────────────────────────────────────────────────────────
def test_python_definitions_with_line_ranges(py_facts):
    py = py_facts["py"]
    types = {t["name"]: t for t in py["types"]}
    assert types["Order"]["start"] == 9 and types["Order"]["end"] == 16      # incl. @dataclass
    assert types["PriorityOrder"]["inherits"] == ["Order"]
    meths = {m["id"]: m for m in py["methods"]}
    place = meths["shop.service.OrderService.place"]
    assert (place["kind"], place["class"], place["start"], place["end"]) == ("Method", "OrderService", 21, 30)
    assert meths["shop.service.approve"]["kind"] == "Function"
    assert meths["shop.service.approve"]["class"] is None
    assert {"file", "start", "end", "id", "name"} <= set(place)


def test_python_imports_both_forms_and_external_libraries(py_facts):
    imps = {(i["file"], i["module"]): i for i in py_facts["py"]["imports"]}
    # `import x` / `import x as y`
    req = imps[("shop/repository.py", "requests")]
    assert req["alias"] == "http" and req["external"] and not req["stdlib"]
    assert imps[("shop/repository.py", "sqlite3")]["stdlib"] is True
    assert imps[("shop/service.py", "shop.models")]["resolved_file"] == "shop/models.py"
    # `from a import b`, relative `from .models import Order` resolved to the absolute module
    rel = imps[("shop/repository.py", "shop.models")]
    assert rel["names"] == ["Order"] and rel["resolved_file"] == "shop/models.py" and not rel["external"]
    frm = imps[("shop/service.py", "shop.repository")]
    assert frm["names"] == ["OrderRepository"] and frm["resolved_file"] == "shop/repository.py"
    assert imps[("shop/models.py", "decimal")]["names"] == ["Decimal"]


def test_python_call_sites_resolve_through_self_imports_and_receiver_types(py_facts):
    got = _calls(py_facts["py"])
    svc = "shop.service.OrderService"
    assert (f"{svc}.place", f"{svc}._hold", "same-class") in got
    assert (f"{svc}.place", "shop.service.approve", "same-file") in got
    # `self.repo` typed by the constructor parameter annotation, `self.audit = AuditTrail()`
    assert (f"{svc}.place", "shop.repository.OrderRepository.load", "receiver-type") in got
    assert (f"{svc}.place", "shop.service.AuditTrail.record", "receiver-type") in got
    # inherited method, constructor through a relative import, `import shop.models` dotted call
    assert ("shop.models.PriorityOrder.escalate", "shop.models.Order.is_large", "same-class") in got
    assert ("shop.repository.OrderRepository.load", "shop.models.Order", "constructor") in got
    assert (f"{svc}.place", "shop.models.Order", "constructor") in got
    assert ("shop.service.main", "shop.service.OrderService.place", "receiver-type") in got
    line = {(c["to"], c["line"]) for c in py_facts["py"]["calls"] if c["from"] == f"{svc}.place"}
    assert (f"{svc}._hold", 24) in line


def test_python_unresolved_and_external_call_sites_are_kept_not_guessed(py_facts):
    sites = py_facts["py"]["call_sites"]
    by = {(s["callee"], s["resolution"]) for s in sites}
    assert ("http.post", "external") in by
    assert ("print", "builtin") in by
    assert ("order.is_large", "unresolved") in by          # return type not inferred: no edge
    ext = {s["module"] for s in sites if s["resolution"] == "external"}
    assert {"requests", "sqlite3", "logging"} <= ext
    # only resolved sites become `calls`
    assert all(c["to"] for c in py_facts["py"]["calls"])
    assert not any(c["to"] is None for c in sites if c["resolution"] not in ("external", "unresolved", "builtin"))


def test_python_sql_in_string_literals(py_facts):
    sql = {(q["line"], tuple(r["table"] for r in q["reads"]), tuple((w["table"], w["op"]) for w in q["writes"]))
           for q in py_facts["py"]["inline_sql"]}
    assert (14, ("orders",), ()) in sql
    assert (18, (), (("orders", "update"),)) in sql
    assert (19, (), (("order_audit", "insert"),)) in sql


def test_sql_access_is_anchored_and_positive_only():
    assert tsf.sql_access('"Please select from the menu"') is None                  # not anchored
    assert tsf.sql_access("'DELETE FROM dbo.Cart WHERE id = 1'") == {
        "reads": [], "writes": [{"table": "dbo.Cart", "op": "delete"}]}
    acc = tsf.sql_access('"""SELECT a.x FROM orders a JOIN [order lines] b ON a.id = b.id"""')
    assert [r["table"] for r in acc["reads"]] == ["orders", "order lines"]


def test_files_inventory_and_tool_versions(py_facts):
    files = {f["file"]: f for f in py_facts["files"]}
    assert files["shop/service.py"]["language"] == "python" and files["shop/service.py"]["end"] >= 44
    assert py_facts["tools"]["py"].startswith("tree-sitter ")
    assert py_facts["py"]["parse_errors"] == []


# ── Java / JavaScript / TypeScript ────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def ts_facts():
    pytest.importorskip("tree_sitter_java")
    pytest.importorskip("tree_sitter_javascript")
    pytest.importorskip("tree_sitter_typescript")
    return tsf.extract(TS, _rels(TS))


def test_java_types_methods_and_resolved_calls(ts_facts):
    j = ts_facts["java"]
    assert {t["name"] for t in j["types"]} == {"InvoiceRepository", "InvoiceService", "BaseService"}
    got = _calls(j)
    p = "com.acme.billing"
    assert (f"{p}.InvoiceService.pay", f"{p}.InvoiceService.isOpen", "same-class") in got
    assert (f"{p}.InvoiceService.pay", f"{p}.InvoiceRepository.markPaid", "receiver-type") in got   # field
    assert (f"{p}.InvoiceService.isOpen", f"{p}.BaseService.audit", "same-class") in got           # inherited
    assert (f"{p}.InvoiceService.pay", f"{p}.InvoiceRepository.InvoiceRepository", "constructor") in got
    ext = {(i["module"], i["stdlib"]) for i in j["imports"] if i["external"]}
    assert ("org.slf4j", False) in ext and ("java.sql", True) in ext
    assert j["inline_sql"][0]["writes"] == [{"table": "invoices", "op": "update"}]


def test_js_and_ts_imports_classes_and_calls(ts_facts):
    js = ts_facts["js"]
    got = _calls(js)
    assert ("web/services/cart:CartService.checkout", "web/services/cart:PriceApi.price", "receiver-type") in got
    assert ("web/services/cart:CartService.checkout", "web/services/format:formatTotal", "import") in got
    assert ("web/services/format:formatTotal", "web/services/format:helper", "same-file") in got
    imps = {(i["file"], i["module"]): i for i in js["imports"]}
    assert imps[("web/services/cart.ts", "./format")]["resolved_file"] == "web/services/format.js"
    assert imps[("web/services/cart.ts", "axios")]["external"] is True
    assert imps[("web/services/format.js", "path")]["stdlib"] is True      # require('path')


# ── facts.json shape: merge, FactsIndex, page blocks ──────────────────────────────────────
def test_merge_keeps_roslyn_sections_and_relabels_inventoried_files(py_facts):
    roslyn = {"schema": "lean-facts/v1", "tools": {"vb": "roslyn"},
              "files": [{"file": "shop/service.py", "language": None, "start": 1, "end": 44},
                        {"file": "a.vb", "language": "vb.net", "start": 1, "end": 3}],
              "vb": {"methods": [{"id": "A.B"}]}, "cs": {}, "sql": {}}
    merged = tsf.merge(roslyn, json.loads(json.dumps(py_facts)))
    assert merged["vb"] == {"methods": [{"id": "A.B"}]} and merged["tools"]["vb"] == "roslyn"
    files = {f["file"]: f for f in merged["files"]}
    assert files["shop/service.py"]["language"] == "python" and files["a.vb"]["language"] == "vb.net"
    assert len([f for f in merged["files"] if f["file"] == "shop/service.py"]) == 1
    assert "py" in merged and merged["tools"]["py"].startswith("tree-sitter")


def test_facts_index_consumes_python_sections(py_facts):
    import code_graph
    idx = code_graph.FactsIndex(py_facts)
    assert idx.present
    call = idx.find("shop/service.py", "load", "method")
    assert call and call["tag"] == "py_call" and call["direction"] == "call"
    imp = idx.find("shop/repository.py", "requests", "import")
    assert imp and imp["tag"] == "py_import_external"
    assert idx.find("shop/repository.py", "sqlite3", "import")["tag"] == "py_import_stdlib"
    assert idx.find("shop/repository.py", "order_audit", "table", "write")["operation"] == "insert"
    assert idx.find("shop/repository.py", "orders", "table", "read")["tag"] == "py_inline_sql"
    names = {o["name"] for o in idx.objects_for("shop/service.py")}
    assert {"OrderService", "OrderService.place", "approve"} <= names
    assert "place" in idx.method_names


def test_page_block_lists_python_calls_and_imports(py_facts, tmp_path):
    pg = tmp_path / "page_000"
    pg.mkdir()
    units = [{"path": p, "line_start": 1, "line_end": 200, "status": "emitted"}
             for p in ("shop/service.py", "shop/repository.py")]
    (pg / "units.jsonl").write_text("".join(json.dumps(u) + "\n" for u in units))
    facts = tsf.merge(build_facts.empty_facts(PY, _rels(PY)), json.loads(json.dumps(py_facts)))
    block = facts_block.render_page_block(facts, facts_block.page_units(pg), 6000)
    assert block.startswith(facts_block.HEADER + facts_block.TS_HEADER_NOTE)
    assert "OrderService.place calls shop.repository.OrderRepository.load [repository.py:L13-L15] @L22" in block
    assert "repository.py imports" in block and "external: requests" in block
    assert "SQL string [repository.py:L19] inserts order_audit" in block


def test_dotnet_only_facts_render_without_the_tree_sitter_note():
    facts = build_facts.empty_facts(PY, [])
    assert not any(facts.get(s) for s in tsf.SECTIONS)
    assert facts_block._imports(facts, facts_block._Page({}), facts_block._Names([])) == []


# ── build_facts: runs without dotnet ──────────────────────────────────────────────────────
def _pages(tmp: Path, rels: list[str]) -> Path:
    pages = tmp / ".pages" / "page_000"
    pages.mkdir(parents=True)
    (pages / "units.jsonl").write_text("".join(
        json.dumps({"path": r, "line_start": 1, "line_end": 500, "status": "emitted"}) + "\n" for r in rels))
    return pages.parent


def test_build_without_dotnet_produces_python_facts(tmp_path, monkeypatch):
    monkeypatch.delenv("DISTILL_FACTS_CMD", raising=False)
    monkeypatch.setattr(build_facts.shutil, "which", lambda _: None)
    rels = _rels(PY)
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("\n".join(rels) + "\n")
    out = tmp_path / "facts"
    st = build_facts.build(PY, manifest, _pages(tmp_path, rels), out, 30000)
    assert st["status"] == "ok" and st["reason"] == "tree-sitter", st
    assert "dotnet not found" in st["dotnet_skipped"]
    assert st["counts"]["py_methods"] >= 12 and st["counts"]["py_calls"] >= 10
    facts = json.loads((out / "facts.json").read_text())
    assert facts["vb"]["methods"] == [] and facts["py"]["calls"] and "v1" in facts
    assert (out / "pages" / "page_000.md").is_file()
    assert build_facts.blocks_hash(out).startswith("sha256:")


def test_build_without_dotnet_or_tree_sitter_language_still_skips(tmp_path, monkeypatch):
    monkeypatch.delenv("DISTILL_FACTS_CMD", raising=False)
    monkeypatch.setattr(build_facts.shutil, "which", lambda _: None)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.vb").write_text("Module A\nEnd Module\n")
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("a.vb\n")
    st = build_facts.build(src, manifest, _pages(tmp_path, ["a.vb"]), tmp_path / "facts", 30000)
    assert st["status"] == "skipped" and "dotnet not found" in st["reason"]
    assert not (tmp_path / "facts" / "source").exists()       # unchanged: nothing staged


def test_build_merges_roslyn_and_tree_sitter_facts(tmp_path, monkeypatch):
    """Both parsers on one corpus: the .NET sections are the tool's, the py section is added."""
    import gzip
    import shutil
    cs_src = Path(__file__).resolve().parent / "fixtures" / "cs-sample"
    raw = Path(__file__).resolve().parent / "fixtures" / "cs_sample_facts_raw.json.gz"
    src = tmp_path / "src"
    shutil.copytree(cs_src, src)
    shutil.copytree(PY / "shop", src / "shop")
    tool = tmp_path / "fake_tool.py"
    tool.write_text(f"import gzip, sys\nsys.stdout.write(gzip.open({str(raw)!r}, 'rt').read())\n")
    monkeypatch.setenv("DISTILL_FACTS_CMD", f"{sys.executable} {tool}")
    rels = _rels(src)
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("\n".join(rels) + "\n")
    st = build_facts.build(src, manifest, _pages(tmp_path, rels), tmp_path / "facts", 30000)
    assert st["status"] == "ok" and st["reason"] == "DISTILL_FACTS_CMD+tree-sitter", st
    facts = json.loads((tmp_path / "facts" / "facts.json").read_text())
    expect = json.loads(gzip.open(raw, "rt").read())
    assert facts["cs"]["methods"] == expect["cs"]["methods"] and facts["sql"]["procs"]
    assert facts["py"]["calls"] and st["counts"]["cs_methods"] == len(expect["cs"]["methods"])
    langs = {f["file"]: f["language"] for f in facts["files"]}
    assert langs["shop/service.py"] == "python"


def test_tree_sitter_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.delenv("DISTILL_FACTS_CMD", raising=False)
    monkeypatch.setenv("DISTILL_FACTS_TREESITTER", "0")
    monkeypatch.setattr(build_facts.shutil, "which", lambda _: None)
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("\n".join(_rels(PY)) + "\n")
    st = build_facts.build(PY, manifest, _pages(tmp_path, _rels(PY)), tmp_path / "facts", 30000)
    assert st["status"] == "skipped"
