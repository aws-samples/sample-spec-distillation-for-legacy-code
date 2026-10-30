# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""impact_eval.name_hit must accept non-string names. No AWS calls.

    python3 -m pytest -q eval/benefits/tests
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import impact_eval  # noqa: E402


def test_name_hit_accepts_an_integer_report_number():
    # Regression: an impact key listing `affected_reports: [615]` crashed the whole run
    # with "'int' object has no attribute 'lower'" after the first paid answer call.
    assert impact_eval.name_hit(615, ["Report 615"], "") is True
    assert impact_eval.name_hit(615, [], "see the 615 change order log") is True
    assert impact_eval.name_hit(412, ["Report 615"], "nothing here") is False


def test_name_hit_string_names_unchanged():
    assert impact_eval.name_hit("usp_JCCOApprove", ["usp_jccoapprove"], "") is True
    assert impact_eval.name_hit("usp_JCCOApprove", [], "unrelated") is False
