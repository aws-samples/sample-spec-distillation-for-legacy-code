# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Knowledge-graph validation for the spec-distillation pipeline.

Modules
  load        — read and normalise <output>/knowledge_graph/ + <output>/facts/ + the source root
  structural  — deterministic structural integrity checks (section 1)
  parity      — precision/recall of the LLM code graph vs the deterministic parser (section 2)
  coverage    — concept coverage vs an answer key (section 3)
  judge       — optional, bounded LLM check of concept<->code bridge links (section 4)
  report      — markdown rendering + machine-readable thresholds (section 5)

Everything except `judge` is deterministic and runs with no network and no LLM.
"""

__all__ = ["load", "structural", "parity", "coverage", "judge", "report"]
