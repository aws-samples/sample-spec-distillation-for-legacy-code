# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests (no AWS): input formats, content-keyed ingest cache, source inventory identity,
refusal / marker-only detection, preflight census + referential grading, run_identity source gate."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import code_graph as CG  # noqa: E402
import content_guard  # noqa: E402
import ingest_helpers as IH  # noqa: E402
import preflight  # noqa: E402
import run_identity  # noqa: E402
import source_inventory as SI  # noqa: E402

# Verbatim refusals from the pipeline-assessment negative control (synthetic public COBOL input).
REFUSAL_RULE = """# rule — Raw Extraction Concat

Pages: 1
Generated: 2026-10-02 22:03:02 UTC

---

<!-- PAGE: page_000 -->
I don't have the actual file contents for data.cob, main.cob, and operations.cob — only placeholder markers showing their size (650, 1319, 1537 bytes/lines) with no actual COBOL code inside. The files were provided as empty shells with just `[FILE: ...]` and `[END FILE: ...]` tags and no source content between them.

I cannot extract business rules, constraints, or invariants without seeing the actual source code.

To proceed, please provide the actual file contents with line numbers, e.g.:
"""
REFUSAL_DATA = """# data — Raw Extraction Concat

Pages: 1

---

<!-- PAGE: page_000 -->
I need to see the actual content of data.cob, main.cob, and operations.cob to extract findings, but the files were provided only as referential stubs (file names, kind, and size) without actual source content. No line-numbered code was included in this concat page.

**No findings produced — source content absent from provided page.**
"""
GOOD = """# rule — Raw Extraction Concat

Pages: 1

---

<!-- PAGE: page_000 -->
### Rule: Sufficient Funds Required for Debit
- Definition: An account cannot be debited for more than its current available balance.
- Evidence: operations.cob#L28-L39
"""


# ── 3. input formats ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["main.cob", "PAYROLL.CBL", "copy/acct.cpy", "rep.rdl", "rep.rdlc",
                                  "pkg.dtsx"])
def test_text_native_formats_are_raw(tmp_path, name):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("       IDENTIFICATION DIVISION.\n")
    assert IH.resolve(str(p)).kind == IH.KIND_RAW


def test_unknown_extension_sniffed(tmp_path):
    t = tmp_path / "job.xyz"
    t.write_text("//STEP1 EXEC PGM=PAYROLL\n")
    b = tmp_path / "blob.xyz"
    b.write_bytes(b"\x00\x01\x02\x03binary")
    assert IH.resolve(str(t)).kind == IH.KIND_RAW
    assert IH.resolve(str(b)).kind == IH.KIND_REFERENTIAL


def test_enumeration_rules_and_explicit_exclusions(tmp_path):
    for n in ["a.cob", "r.rdl", "r.rdlc", "p.dtsx", "r.rpt", "i.png", "node_modules/x.js", "keep.sql"]:
        f = tmp_path / n
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n")
    cands, excl, meta = SI.enumerate_source(str(tmp_path))
    assert {"a.cob", "r.rdl", "r.rdlc", "p.dtsx", "keep.sql"} <= set(cands)
    reasons = dict(excl)
    assert "i.png" in reasons and "node_modules/x.js" in reasons
    if not meta["include_rpt"]:  # no JRC runtime on a test box -> explicit reason, not silent
        assert "r.rpt" in reasons and "RptJrcHelper unavailable" in reasons["r.rpt"]


def test_source_root_under_build_dir_not_excluded(tmp_path):
    root = tmp_path / "build" / "src"
    root.mkdir(parents=True)
    (root / "a.py").write_text("x\n")
    cands, _, _ = SI.enumerate_source(str(root))
    assert cands == ["a.py"]


# ── 1. source identity + ingest cache ────────────────────────────────────────
def _inv(root):
    c, _, m = SI.enumerate_source(str(root))
    return SI.build_inventory(str(root), c, m, {"token_budget": "300000"})


def test_inventory_identity_tracks_change_delete_add(tmp_path):
    (tmp_path / "a.py").write_text("1\n")
    (tmp_path / "b.py").write_text("2\n")
    i1 = _inv(tmp_path)
    assert _inv(tmp_path)["source_identity"] == i1["source_identity"]  # deterministic
    (tmp_path / "a.py").write_text("one\n")
    (tmp_path / "b.py").unlink()
    (tmp_path / "c.py").write_text("3\n")
    i2 = _inv(tmp_path)
    assert i2["source_identity"] != i1["source_identity"]
    assert SI.diff_inventories(i1, i2) == {"added": ["c.py"], "deleted": ["b.py"], "changed": ["a.py"]}
    # config is part of the identity
    c, _, m = SI.enumerate_source(str(tmp_path))
    assert SI.build_inventory(str(tmp_path), c, m, {"token_budget": "1"})["source_identity"] \
        != i2["source_identity"]


def test_stage_id_chains(tmp_path):
    code = tmp_path / "s.py"
    code.write_text("v1")
    a = SI.stage_id("up1", [str(code)], ["k=v"])
    assert a == SI.stage_id("up1", [str(code)], ["k=v"])
    assert a != SI.stage_id("up2", [str(code)], ["k=v"])
    code.write_text("v2")
    assert a != SI.stage_id("up1", [str(code)], ["k=v"])


class _FakeLift(IH.IngestHelper):
    id, version, rank = "fake-lift", "1", 1

    def handles(self, path):
        return path.endswith(".bin2")

    def lift(self, path):
        return IH.LiftResult(Path(path).read_text().upper(), self.id, 0.5)


def test_ingest_cache_keyed_on_content_not_path(tmp_path, monkeypatch):
    monkeypatch.setattr(IH, "_REGISTRY", list(IH._REGISTRY) + [_FakeLift()])
    src = tmp_path / "src"
    src.mkdir()
    (src / "r.bin2").write_text("old")
    (tmp_path / "m.txt").write_text("r.bin2\n")
    cache = tmp_path / "cache"
    out = tmp_path / "mat.tsv"

    def unit_text():
        IH.materialize(str(tmp_path / "m.txt"), str(src), str(cache), str(out))
        row = next(csv.reader(open(out), delimiter="\t"))
        assert row[1] == "inferred"
        return Path(row[2]).read_text(), Path(row[2]).name

    t1, n1 = unit_text()
    assert t1 == "OLD"
    (src / "r.bin2").write_text("new")  # same path, new content -> must NOT reuse the old lift
    t2, n2 = unit_text()
    assert t2 == "NEW" and n1 != n2
    assert IH.INGEST_VERSION in n2 and "fake-lift-v1" in n2


def test_run_identity_source_gate(tmp_path):
    pages = tmp_path / ".pages" / "page_000"
    pages.mkdir(parents=True)
    (pages / "concat.txt").write_text("[FILE: a KIND: referential SIZE: 3]\n[END FILE: a]\n")
    stills = ["rule"]
    i1 = run_identity.compute_identity(tmp_path / ".pages", stills, source_inventory_hash="h1")
    i2 = run_identity.compute_identity(tmp_path / ".pages", stills, source_inventory_hash="h2")
    assert run_identity.diff(i1, i1)["state"] == "match"
    v = run_identity.diff(i1, i2)  # identical pages, different source -> nothing resumes
    assert v["state"] == "source_mismatch" and v["resumable"] == []
    legacy = dict(i1)
    legacy.pop("source_inventory_hash")
    assert run_identity.diff(legacy, i1)["state"] == "source_mismatch"


# ── 2. fail closed ───────────────────────────────────────────────────────────
def test_refusals_detected():
    assert content_guard.classify_text(REFUSAL_RULE)[0] == "refusal"
    assert content_guard.classify_text(REFUSAL_DATA)[0] == "refusal"
    assert content_guard.classify_text(GOOD)[0] == "ok"
    assert content_guard.classify_text("# x — Raw Extraction Concat\n\nPages: 1\n\n---\n")[0] == "empty"
    assert content_guard.classify_text("<!-- PAGE: page_000 -->\nNothing relevant on this page.\n")[0] \
        == "no_findings"
    # a refusal on one page does not sink a still that has real findings elsewhere
    mixed = GOOD + "\n<!-- PAGE: page_001 -->\n" + REFUSAL_DATA.split("-->", 1)[1]
    status, detail = content_guard.classify_text(mixed)
    assert status == "ok" and "page_001" in detail


def test_barrier_cli(tmp_path):
    for still, text in (("rule", REFUSAL_RULE), ("data", GOOD)):
        (tmp_path / still).mkdir()
        (tmp_path / still / f"{still}_raw_concat.md").write_text(text)
    (tmp_path / "state").mkdir()
    status = {s: content_guard.check_still(tmp_path, s)[0] for s in ("rule", "data", "state")}
    assert status == {"rule": "refusal", "data": "ok", "state": "missing"}


def _pages(tmp_path, body: str) -> Path:
    p = tmp_path / ".pages" / "page_000"
    p.mkdir(parents=True)
    (p / "concat.txt").write_text(body)
    return tmp_path / ".pages"


MARKER_ONLY = ("[FILE: data.cob KIND: referential SIZE: 650]\n[END FILE: data.cob]\n\n"
               "[FILE: main.cob KIND: referential SIZE: 1319]\n[END FILE: main.cob]\n\n"
               "[FILE: operations.cob KIND: referential SIZE: 1537]\n[END FILE: operations.cob]\n")
RAW3 = "".join(f"[FILE: {n}.cob KIND: raw SIZE: 10]\n    1 |        PROGRAM-ID. {n}.\n[END FILE: {n}.cob]\n\n"
               for n in ("data", "main", "operations"))


def test_marker_only_pages_detected(tmp_path):
    rep = content_guard.page_content(_pages(tmp_path, MARKER_ONLY))
    assert rep["content_pages"] == 0 and rep["files_marker_only"] == 3


def test_preflight_census_counts_every_file(tmp_path):
    pages = _pages(tmp_path, RAW3)
    cov = CG.compute_source_coverage(pages)
    assert cov["total_source_files"] == 3  # was 2: the page's last marker was dropped
    assert len(list(CG.reconstruct_code_files(pages))) == 3


def _mat(tmp_path, kind):
    m = tmp_path / "mat.tsv"
    m.write_text("".join(f"{n}.cob\t{kind}\t-\t0\t10\t\t1.00\t\n" for n in ("data", "main", "operations")))
    return str(m)


def test_preflight_not_safe_when_referential(tmp_path):
    pages = _pages(tmp_path, MARKER_ONLY)
    rep = preflight.preflight(pages, _mat(tmp_path, "referential"), None, False, "", "us-east-1")
    assert rep["verdict"] == "CRITICAL"
    assert "safe to run" not in rep["verdict_meaning"]
    f = next(x for x in rep["findings"] if x["kind"] == "content_not_paged")
    assert f["referential_files"] == 3 and f["materialized_files"] == 3
    assert rep["coverage"]["total_source_files"] == 3


def test_preflight_raw_cobol_not_critical(tmp_path):
    pages = _pages(tmp_path, RAW3)
    rep = preflight.preflight(pages, _mat(tmp_path, "raw"), None, False, "", "us-east-1")
    assert rep["verdict"] != "CRITICAL"
    assert not [x for x in rep["findings"] if x["kind"] == "content_not_paged"]
