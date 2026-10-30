# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Viewer (no AWS): Issues & Insights and Business Spec pages render, existing pages still render,
and the tabs degrade to the placeholder when their inputs are absent."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

KIRO = Path(__file__).resolve().parents[1]
VIEWER = KIRO / "generate-viewer-v3.py"

ISSUES = {
    "status": "ok",
    "meta": {"candidates": 4, "model": "test-model"},
    "issues": [
        {"id": "ISS-001", "category": "Inconsistency", "title": "Limit differs </script> between UI and DB",
         "description": "The screen checks > 5000 but the procedure checks >= 5000.",
         "business_impact": "Exactly-5000 items take different paths.", "severity": "High",
         "evidence_class": "Verified", "sme_question": "Is 5000 itself approved?",
         "locations": [{"file": "App/a.vb", "start": 3, "end": 3, "ref": "App/a.vb:L3-L3",
                        "excerpt": [[2, "Sub A()"], [3, "If amt > 5000 Then"], [4, "End Sub"]]}]},
        {"id": "ISS-002", "category": "Dead/obsolete code", "title": "Old calc still shipped",
         "description": "d", "business_impact": "b", "severity": "Low", "evidence_class": "Inferred",
         "sme_question": "", "locations": [{"file": "Db/p.sql", "start": 1, "end": 2, "ref": "Db/p.sql:L1-L2",
                                            "excerpt": [[1, "CREATE PROC p"], [2, "AS"]]}]},
    ],
}


def run_viewer(base: Path) -> Path:
    out = base / "viewer"
    r = subprocess.run([sys.executable, str(VIEWER), str(base), "Fixture", "--viewer-dir", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return out


def make_base(tmp: Path, with_issues=True, with_bottled=True) -> Path:
    base = tmp / "run"
    (base / "bottled_spec").mkdir(parents=True)
    (base / "bottled_spec" / "README.md").write_text("# Exec summary\n\nShort.\n")
    if with_bottled:
        (base / "bottled_spec" / "bottled_spec.md").write_text(
            "# Fixture — Spec\n\n## Business Rules\n\n### Approval limit\n\nInvoices over the limit need approval "
            "(App/a.vb#L3).\n\n## Workflows\n\nText.\n")
    (base / "master_blend").mkdir()
    (base / "master_blend" / "master_blend.md").write_text("# Blend\n\n## Section\n\nBody.\n")
    still = base / "stills" / "rule" / "final"
    still.mkdir(parents=True)
    (still / "catalog.md").write_text("# rule\n\n## Rules\n\n### Rule: One\n- Definition: x\n")
    if with_issues:
        (base / "issues").mkdir()
        (base / "issues" / "issues.json").write_text(json.dumps(ISSUES))
    return base


def test_issues_and_bottled_pages_render(tmp_path):
    out = run_viewer(make_base(tmp_path))
    index = (out / "index.html").read_text()
    for page in ("issues", "bottled_spec", "executive_summary", "master_blend", "stills_rule"):
        assert (out / "pages" / f"{page}.html").is_file(), page
        assert f'pages/{page}.html' in index, page
    assert re.search(r'data-page="pages/issues.html"\s+onclick', index)  # enabled tab
    assert re.search(r'data-page="pages/bottled_spec.html"\s+onclick', index)

    html = (out / "pages" / "issues.html").read_text()
    data = re.search(r'<script type="application/json" id="issues-data"[^>]*>(.*?)</script>', html, re.S).group(1)
    assert "</script>" not in data  # embedded JSON cannot terminate its own script tag
    parsed = json.loads(data)
    assert [i["id"] for i in parsed["issues"]] == ["ISS-001", "ISS-002"]
    assert 'href="#ISS-001"' in html and "Summary" in html and "iss-bar" in html
    assert "App/a.vb:L3-L3" in html  # cited location is present (and its excerpt lines)
    assert "If amt &gt; 5000 Then" in html or "If amt > 5000 Then" in data

    spec = (out / "pages" / "bottled_spec.html").read_text()
    assert "Approval limit" in spec and 'id="toc-data"' in spec
    toc = json.loads(re.search(r'id="toc-data">(.*?)</script>', spec, re.S).group(1))
    assert {"Business Rules", "Workflows"} <= {e["title"] for e in toc}
    assert 'class="evidence-link"' in spec  # .vb citations are linkified


def test_tabs_disabled_when_inputs_absent(tmp_path):
    out = run_viewer(make_base(tmp_path, with_issues=False, with_bottled=False))
    index = (out / "index.html").read_text()
    assert re.search(r'data-page="pages/issues.html" data-disabled="true"', index)
    assert re.search(r'data-page="pages/bottled_spec.html" data-disabled="true"', index)
    assert (out / "pages" / "executive_summary.html").is_file()


def test_failed_issues_report_not_shown(tmp_path):
    base = make_base(tmp_path)
    doc = dict(ISSUES, status="failed: candidates existed but no issue survived", issues=[])
    (base / "issues" / "issues.json").write_text(json.dumps(doc))
    out = run_viewer(base)
    assert re.search(r'data-page="pages/issues.html" data-disabled="true"', (out / "index.html").read_text())
