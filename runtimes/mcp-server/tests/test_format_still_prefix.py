# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""format_still: the requirement-id prefix comes from the still the file belongs to, never from
the document title.

Regression: the knowledge layer stopped writing the still name into materialized titles
(`# data — Persistent Objects` became `# Persistent Objects`), and format_still.py derived the
prefix from the file stem or the title, so every file whose stem lacks the still name
(persistent_objects.md, domain_map.md, role_registry.md, ...) got `XX-` ids: 181 of 328 AP and
240 of 458 JC requirements on the 2026-10-12 runs. These tests feed
format_still REAL knowledge-layer output (knowledge.materialize_manifest_files), so a header
change on either side fails here. No AWS: the Bedrock client and the ID vending machine are faked."""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MCP = HERE.parent
REPO = MCP.parent.parent
sys.path.insert(0, str(MCP))

import bedrock_client  # noqa: E402
import format_still  # noqa: E402
import id_vending_machine  # noqa: E402
import knowledge as kn  # noqa: E402

PREFIX = {"data": "DT", "domain_language": "DL", "exception": "EX", "history": "HI", "interface": "IF",
          "permission": "PM", "rule": "RL", "runtime": "RT", "state": "ST", "test": "TS", "workflow": "WF"}
STILLS = sorted(PREFIX)

# one block of every type the manifests route on, so every manifest file gets content
CONCAT = """<!-- PAGE: page_000 -->
### Entity: Invoice Header
The invoice header table. Source: APInvHdr.sql#L1-L30

### Invariant: Lines must reconcile to the total
REQUIRE sum(lines) = total. Source: clsAPInvoiceBL.vb#L103-L138

### Data Obligation: Posted invoices are kept seven years
Retention. Source: APInvHdr.sql#L40-L44

### Data Boundary: Company scoping
Every query filters on APCo. Source: clsAPInvoiceDAL.vb#L10-L20

### Rule: Approval threshold
Invoices over 10000 need approval. Source: modAPGlobals.vb#L5-L6

### State: Posted
A posted invoice. Source: APInvHdr.sql#L50-L52

### Workflow: Post an invoice
Validate, insert, route. Source: frmAPInvoiceEntry.vb#L200-L260

### Permission: Clerks enter invoices
Clerks may enter. Source: clsAPSecurity.vb#L1-L9

### Interface: Vendor import file
CSV import. Source: VendorImport.vb#L1-L40

### Exception: Insert failure returns 99
ExecProc returns 99 on SqlException. Source: clsAPInvoiceDAL.vb#L300-L320

### Obligation: Import format is stable
Callers depend on the columns. Source: VendorImport.vb#L1-L40

### Finding: Dead discount code
Nothing calls it. Source: modAPGlobals.vb#L80-L90
<!-- END PAGE: page_000 -->
"""


class _Res:
    def __init__(self, text):
        self.output_text, self.input_tokens, self.output_tokens = text, 1, 1


class _VM:
    def __init__(self):
        self.n, self.categories = 0, []

    def claim(self, prefix, key, **kw):
        self.n += 1
        self.categories.append(prefix)
        return {"id": f"{prefix}-{self.n:04d}"}


@pytest.fixture
def fake_llm(monkeypatch):
    def echo(client, model, system, user, **kw):                  # returns every block it was sent
        heads = re.findall(r"^### \[C\d+\.\d+\][^\n]*\n(?:(?!^### ).*\n?)*", user, re.M)
        return _Res("\n".join(h.rstrip() + "\n" for h in heads))
    vm = _VM()
    monkeypatch.setattr(bedrock_client, "create_llm_client", lambda *a, **k: object())
    monkeypatch.setattr(bedrock_client, "call_llm", echo)
    monkeypatch.setattr(bedrock_client, "log_llm_call", lambda **kw: None)
    monkeypatch.setattr(id_vending_machine, "get_default", lambda: vm)
    return vm


def _materialize(root: Path, still: str) -> Path:
    """The knowledge layer's own output for one still: <root>/stills/<still>/final/*.md."""
    concat = root / f"{still}_raw_concat.md"
    concat.write_text(CONCAT, encoding="utf-8")
    blocks = kn.parse_raw_concat(str(concat), still)
    graph = kn.KnowledgeGraph(str(root / f"{still}_graph.json"))
    for b in blocks:
        graph.add_block(b)
    final = root / "stills" / still / "final"
    manifest = REPO / "runtimes" / "pipeline" / "manifests" / f"{still}.txt"
    kn.materialize_manifest_files(graph, blocks, still, str(manifest), str(final))
    return final


def _ids(text: str) -> list[str]:
    return re.findall(r"^### \[([A-Z]{2}-\d+)\]", text, re.M)


@pytest.mark.parametrize("still", STILLS)
def test_materialized_files_get_their_still_prefix(tmp_path, fake_llm, still):
    final = _materialize(tmp_path, still)
    prefix = PREFIX[still]
    files = [f for f in sorted(final.glob("*.md")) if f.name != "README.md" and "### " in f.read_text()]
    assert files, f"knowledge layer materialized nothing for {still}"
    for f in files:
        out = tmp_path / "out" / still / f.name
        out.parent.mkdir(parents=True, exist_ok=True)
        format_still.format_manifest_file(str(f), str(out), concurrency=1)
        ids = _ids(out.read_text())
        assert ids, f.name
        assert {i.split("-")[0] for i in ids} == {prefix}, (f.name, ids)
    assert "XX" not in fake_llm.categories


def test_title_without_the_still_name_still_gets_the_prefix(tmp_path, fake_llm):
    """The exact case of the regression: persistent_objects.md titled `# Persistent Objects`."""
    final = _materialize(tmp_path, "data")
    src = final / "persistent_objects.md"
    assert src.read_text().splitlines()[0] == "# Persistent Objects"       # no still in the title
    format_still.format_manifest_file(str(src), str(tmp_path / "po.md"), concurrency=1)
    assert _ids((tmp_path / "po.md").read_text()) and \
        all(i.startswith("DT-") for i in _ids((tmp_path / "po.md").read_text()))


def test_metadata_line_is_enough_outside_the_stills_tree(tmp_path, fake_llm):
    """A copied file (no stills/<still>/final/ path) keeps its prefix from the metadata line."""
    final = _materialize(tmp_path, "permission")
    loose = tmp_path / "loose" / "role_registry.md"
    loose.parent.mkdir()
    shutil.copy(final / "role_registry.md", loose)
    assert format_still.still_prefix_for(str(loose), "Role Registry", loose.read_text()) == \
        ("permission", "PM", "metadata")
    format_still.format_manifest_file(str(loose), str(tmp_path / "rr.md"), concurrency=1)
    assert {i.split("-")[0] for i in _ids((tmp_path / "rr.md").read_text())} == {"PM"}


def test_prefix_table_is_the_format_still_table():
    assert format_still.STILL_PREFIXES == PREFIX


def test_explicit_still_wins_and_unknown_stays_xx(tmp_path):
    p = tmp_path / "domain_map.md"
    assert format_still.still_prefix_for(str(p), "Domain Map", "", still="domain_language")[1] == "DL"
    assert format_still.still_prefix_for(str(p), "Domain Map", "")[1] == "XX"
    # the path: stills/<still>/final/ (and final_v2/ variants)
    q = tmp_path / "stills" / "state" / "final_v2" / "lifecycle_registry.md"
    assert format_still.still_prefix_for(str(q), "Lifecycle Registry", "")[:2] == ("state", "ST")
    # the legacy title form still works
    assert format_still.still_prefix_for(str(p), "data — Domain Map", "")[1] == "DT"


def test_format_all_stills_passes_the_still_name(tmp_path, monkeypatch):
    seen = []
    for s in ("data", "permission"):
        (tmp_path / "stills" / s / "final").mkdir(parents=True)
        (tmp_path / "stills" / s / "final" / "x.md").write_text("# X\n\n### Entity: A\nbody\n")

    def fake(inp, outp, **kw):
        seen.append((Path(inp).parent.parent.name, kw.get("still")))
        Path(outp).write_text("### [AA-0001] A\n")
        return {"status": "ok", "input_blocks": 1, "output_blocks": 1}
    monkeypatch.setattr(format_still, "format_manifest_file", fake)
    format_still.format_all_stills(str(tmp_path / "stills"))
    assert sorted(seen) == [("data", "data"), ("permission", "permission")]


# ── everything keyed on the id prefix ───────────────────────────────────────────────────────
def test_progress_viewer_knows_every_still_prefix():
    """The progress dashboard groups requirements by the same prefixes."""
    src = (MCP / "progress_server.py").read_text()
    full = dict(re.findall(r"'([A-Z]{2})':'([A-Za-z ]+)'", src[src.index("const STILL_FULL"):][:400]))
    assert set(format_still.STILL_PREFIXES.values()) <= set(full)
