# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""check-dependencies.sh: the default page-centric strategy passes the strategy check and the
unsupported still-centric strategy fails it. No AWS: the script runs in a temp copy whose venv
python only answers the orchestration-strategy resolver (every other check fails offline)."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture()
def rig(tmp_path):
    root = tmp_path / "repo"
    mcp = root / "runtimes" / "mcp-server"
    mcp.mkdir(parents=True)
    shutil.copy2(REPO / "check-dependencies.sh", root / "check-dependencies.sh")
    for f in ("orchestration_strategy.py",):
        shutil.copy2(REPO / "runtimes" / "mcp-server" / f, mcp / f)
    vbin = mcp / ".venv" / "bin"
    vbin.mkdir(parents=True)
    py = vbin / "python"
    py.write_text('#!/bin/sh\ncase "$1" in *orchestration_strategy.py) exec "%s" "$@";; '
                  '--version) echo "Python 3 (stub)"; exit 0;; esac\nexit 1\n' % sys.executable)
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    tbin = tmp_path / "bin"
    tbin.mkdir()
    for tool in ("aws",):  # aws fails offline: no credentials, no network
        t = tbin / tool
        t.write_text("#!/bin/sh\nexit 1\n")
        t.chmod(t.stat().st_mode | stat.S_IXUSR)

    def run(**env_over):
        env = {"PATH": f"{tbin}:/usr/bin:/bin", "HOME": str(tmp_path)}
        env.update(env_over)
        p = subprocess.run(["bash", str(root / "check-dependencies.sh"), str(tmp_path / "chk")],
                           env=env, capture_output=True, text=True, timeout=300)
        return p.stdout + p.stderr

    return run


def test_page_centric_passes_strategy_check(rig):
    out = rig()
    assert "✓ orchestration strategy: page-centric" in out
    assert "✗ orchestration strategy" not in out


def test_still_centric_is_reported_unsupported(rig):
    out = rig(ORCHESTRATION_STRATEGY="still-centric")
    assert "✗ orchestration strategy: still-centric is not supported by this sample" in out
