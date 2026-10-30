# B1 modernization insight: raw-code-nollm

- Spec: `eval/benefits/fixtures/toy/out/raw-code/insights_spec.md`, key: `eval/benefits/fixtures/toy/key.yaml`
- Matcher: deterministic, flag classifier: keywords
- Spend: $0.0000 of $1.00

| Metric | Value |
|---|---|
| key_items | 7 |
| findings | 17 |
| issue_items | 5 |
| issue_described_any_pct | 100 |
| issue_flagged_same_pct | 100 |
| issue_flagged_any_pct | 100 |
| findings_flagging_issues | 13 |
| flag_precision_keyed_pct | 53.8 |
| flag_precision_true_pct | n/a |
| overall_recall_full_pct | 100 |
| overall_recall_any_pct | 100 |
| incorrect_findings | 0 |
| duplicated_logic_described_pct | 100 |
| duplicated_logic_flagged_pct | 100 |
| logic_flaw_described_pct | 100 |
| logic_flaw_flagged_pct | 100 |
| undocumented_feature_described_pct | 100 |
| undocumented_feature_flagged_pct | 100 |
| inconsistency_described_pct | 100 |
| inconsistency_flagged_pct | 100 |
| dead_code_described_pct | 100 |
| dead_code_flagged_pct | 100 |
| comment_only_described_pct | 100 |
| db_only_described_pct | 100 |

## Per flag category

| Flag | Items | Described any % | Described full % | Flagged (same) % | Flagged (any issue) % | Findings flagging | Flag precision keyed % | Flag precision true % |
|---|---|---|---|---|---|---|---|---|
| duplicated_logic | 1 | 100 | 100 | 100 | 100 | 2 | 50 | n/a |
| logic_flaw | 1 | 100 | 100 | 100 | 100 | 4 | 25 | n/a |
| undocumented_feature | 1 | 100 | 100 | 100 | 100 | 4 | 25 | n/a |
| inconsistency | 2 | 100 | 100 | 100 | 100 | 6 | 66.7 | n/a |
| dead_code | 1 | 100 | 100 | 100 | 100 | 1 | 100 | n/a |
| comment_only | 1 | 100 | 100 | n/a | 100 | n/a | n/a | n/a |
| db_only | 2 | 100 | 100 | n/a | 100 | n/a | n/a | n/a |

## Key items

| Item | Key flags | Coverage | Flagged by tool as | Statement |
|---|---|---|---|---|
| T-01 | comment_only, inconsistency | full | inconsistency | SubmitInvoice rejects an invoice with more than MAX_LINES = 25 lines (ERR_TOO_MANY_LINES); the comment claimin |
| T-02 | duplicated_logic, inconsistency | full | duplicated_logic, inconsistency | Invoice total is computed in two places with different tax multipliers and rounding (SubmitInvoice uses 1.08 w |
| T-03 | undocumented_feature | full | undocumented_feature | Vendors whose code starts with "ZZ" are auto-approved (status APPROVED) and skip the approval step; this bypas |
| T-04 | db_only | full | inconsistency, logic_flaw, undocumented_feature | usp_SaveInvoice downgrades an APPROVED invoice with total over 50000 back to PENDING_APPROVAL, so ZZ auto-appr |
| T-05 | db_only | full | duplicated_logic, logic_flaw | The database requires invoice Total > 0 and (VendorCode, InvoiceNo) unique; the application does not check eit |
| T-06 | logic_flaw | full | logic_flaw | IsOverdue uses Days > 30, so an invoice exactly 30 days past due is not overdue (off-by-one against a 30-day p |
| T-07 | dead_code | full | dead_code | LegacyDiscount (2% discount) is private and never called (dead code). |
