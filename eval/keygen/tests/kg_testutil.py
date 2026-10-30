# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared helpers for the eval/keygen tests (no AWS, no network, no dotnet)."""
from __future__ import annotations

import sys
from pathlib import Path

KG = Path(__file__).resolve().parents[1]
EVAL = KG.parent
REPO = EVAL.parent
FIX = Path(__file__).resolve().parent / "fixtures"
for p in (KG, EVAL / "mutate", EVAL):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import kg_harvest as H                                       # noqa: E402
import kg_source as S                                        # noqa: E402
import kg_testmine as T                                      # noqa: E402


def load(name: str, facts: dict | None = None):
    files = S.load_tree(FIX / name)
    return files, S.Index(files, facts)


def harvest(name: str, detector: str | None = None):
    files, idx = load(name)
    out = H.harvest(files, idx)
    return [c for c in out if detector is None or c.detector == detector]


def by_text(cands, needle: str):
    hits = [c for c in cands if needle in c.statement]
    assert hits, f"no candidate mentions {needle!r}: {[c.statement for c in cands]}"
    return hits[0]
