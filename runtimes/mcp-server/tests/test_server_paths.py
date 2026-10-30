# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""bedrock-distill MCP tools refuse file and directory arguments outside the allowed roots.
No AWS: the tools return before any Bedrock call."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

MCP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP))

server = pytest.importorskip("server")


@pytest.fixture
def roots(monkeypatch, tmp_path):
    for var in ("DISTILL_TARGET", "DISTILL_OUTPUT", "DISTILL_MCP_ALLOWED_ROOTS"):
        monkeypatch.delenv(var, raising=False)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("DISTILL_MCP_ALLOWED_ROOTS", str(allowed))
    return allowed


def _err(out: str) -> str:
    d = json.loads(out)
    assert d.get("error"), d
    return d["message"]


def test_repo_and_configured_roots_allowed(roots):
    assert server._outside_roots(server._REPO_ROOT, str(roots / "a" / "b.md")) == ""


def test_outside_path_refused(roots, tmp_path):
    assert "outside allowed roots" in server._outside_roots(str(tmp_path / "elsewhere.txt"))


def test_dotdot_and_symlink_escape_refused(roots, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    (roots / "link").symlink_to(secret)
    assert server._outside_roots(str(roots / ".." / "secret.txt"))
    assert server._outside_roots(str(roots / "link"))


def test_submit_distillation_refuses_outside_pages_file(roots, tmp_path):
    pages = tmp_path / "pages.txt"
    pages.write_text("/etc/hosts\n")
    out = asyncio.run(server.submit_distillation(pages_file=str(pages), still_prompt="p"))
    assert "outside allowed roots" in _err(out)


def test_submit_distillation_refuses_outside_page_listed_in_allowed_file(roots):
    pages = roots / "pages.txt"
    pages.write_text("/etc/hosts\n")
    out = asyncio.run(server.submit_distillation(pages_file=str(pages), still_prompt="p"))
    assert "/etc/hosts" in _err(out)


def test_reduce_refuses_outside_prompt_file(roots, tmp_path):
    out = asyncio.run(server.reduce_distillation(
        partials_dir=str(roots), still_prompt="", still_name="rule", output_dir=str(roots),
        still_prompt_file=str(tmp_path / "prompt.md")))
    assert "outside allowed roots" in _err(out)


def test_query_spec_refuses_outside_db(roots, tmp_path):
    out = asyncio.run(server.query_spec(question="q", vector_db_path=str(tmp_path / "v.db")))
    assert "outside allowed roots" in _err(out)


def test_read_confined_reads_inside_and_refuses_escape(roots, tmp_path):
    (roots / "p.md").write_text("prompt")
    assert server._read_confined(str(roots / "p.md")) == "prompt"
    (tmp_path / "secret.txt").write_text("x")
    with pytest.raises(ValueError):
        server._read_confined(str(roots / ".." / "secret.txt"))
