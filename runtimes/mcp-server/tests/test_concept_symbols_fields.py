# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""concept_symbols reads Statement and Aliases whether the format pass writes the labels plain
('- Statement:') or bold ('- **Statement:**'). With bold labels unread, the concept-symbol index was
empty and the concept-to-code bridge had no links."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import concept_symbols as cs  # noqa: E402

BOLD = """### [RL-0008] User Identity Is Email, Not Username
- **Definition:** The user model logs in by email.
- **Statement:**
```formal
REQUIRE User.USERNAME_FIELD = "email"
```
- **Aliases:** `USERNAME_FIELD`, `UserManager`
- **Evidence:** accounts/models.py:L10-L20
"""

PLAIN = BOLD.replace("- **Statement:**", "- Statement:").replace("- **Aliases:**", "- Aliases:") \
            .replace("- **Definition:**", "- Definition:").replace("- **Evidence:**", "- Evidence:")


def test_bold_and_plain_labels_read_the_same():
    for text in (BOLD, PLAIN):
        assert "USERNAME_FIELD" in cs._field_value(text, "Statement")
        aliases = cs._field_value(text, "Aliases")
        assert "UserManager" in aliases and "Evidence" not in aliases     # stops at the next field


def test_bold_labels_yield_symbols():
    st, al = cs._field_value(BOLD, "Statement"), cs._field_value(BOLD, "Aliases")
    syms = {s.lower() for s in cs.extract_symbols(st, al)}
    assert {"user.username_field", "usermanager"} <= syms
