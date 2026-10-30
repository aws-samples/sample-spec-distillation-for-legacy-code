# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The JUDGED layer of the scorer: an LLM judge with measured uncertainty.

Everything in this package is probabilistic. Nothing here is treated as exact.
The deterministic layer lives in ``eval/score.py`` and never calls Bedrock.

Modules
  schema.py    JSON Schema for judge output + a dependency-free validator
  client.py    Bedrock Converse *tool-use* call, throttle retry, call logging
  evidence.py  bounded, cited source excerpts (replaces the 200K-char dump)
  runner.py    repeated judging, majority vote, per-item agreement, caching
  cache.py     content-addressed verdict cache (hash of prompt+model+schema)
  stats.py     bootstrap CIs, Fleiss' kappa, Cohen's kappa
  golden/      human-labelled calibration subsets
"""
from __future__ import annotations

import sys
from pathlib import Path

# eval/ on sys.path so `import bedrock_util` works however this package is imported.
_EVAL_DIR = Path(__file__).resolve().parent.parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

EVAL_DIR = _EVAL_DIR
JUDGE_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = JUDGE_DIR / "cache"
GOLDEN_DIR = JUDGE_DIR / "golden"
