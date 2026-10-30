# B1 modernization insight: raw-code

- Spec: `eval/benefits/fixtures/toy/out/raw-code/insights_spec.md`, key: `eval/benefits/fixtures/toy/key.yaml`
- Matcher: llm, flag classifier: llm
- Spend: $0.0706 of $0.40

| Metric | Value |
|---|---|
| key_items | 7 |
| findings | 17 |
| issue_items | 5 |
| issue_described_any_pct | 100 |
| issue_flagged_same_pct | 80 |
| issue_flagged_any_pct | 80 |
| findings_flagging_issues | 12 |
| flag_precision_keyed_pct | 50 |
| flag_precision_true_pct | 100 |
| overall_recall_full_pct | 71.4 |
| overall_recall_any_pct | 100 |
| incorrect_findings | 0 |
| duplicated_logic_described_pct | 100 |
| duplicated_logic_flagged_pct | 100 |
| logic_flaw_described_pct | 100 |
| logic_flaw_flagged_pct | 0 |
| undocumented_feature_described_pct | 100 |
| undocumented_feature_flagged_pct | 100 |
| inconsistency_described_pct | 100 |
| inconsistency_flagged_pct | 100 |
| dead_code_described_pct | 100 |
| dead_code_flagged_pct | 100 |
| comment_only_described_pct | 100 |
| db_only_described_pct | 100 |
| answer_tokens_in | 1510 |
| answer_tokens_out | 2271 |
| answer_cost_usd | 0.038595 |
| latency_s | 22.54 |

## Per flag category

| Flag | Items | Described any % | Described full % | Flagged (same) % | Flagged (any issue) % | Findings flagging | Flag precision keyed % | Flag precision true % |
|---|---|---|---|---|---|---|---|---|
| duplicated_logic | 1 | 100 | 0 | 100 | 100 | 1 | 100 | 100 |
| logic_flaw | 1 | 100 | 100 | 0 | 0 | 3 | 0 | 100 |
| undocumented_feature | 1 | 100 | 100 | 100 | 100 | 4 | 25 | 100 |
| inconsistency | 2 | 100 | 50 | 100 | 100 | 3 | 100 | 100 |
| dead_code | 1 | 100 | 100 | 100 | 100 | 1 | 100 | 100 |
| comment_only | 1 | 100 | 100 | n/a | 100 | n/a | n/a | n/a |
| db_only | 2 | 100 | 50 | n/a | 100 | n/a | n/a | n/a |

## Key items

| Item | Key flags | Coverage | Flagged by tool as | Statement |
|---|---|---|---|---|
| T-01 | comment_only, inconsistency | full | inconsistency | SubmitInvoice rejects an invoice with more than MAX_LINES = 25 lines (ERR_TOO_MANY_LINES); the comment claimin |
| T-02 | duplicated_logic, inconsistency | partial | duplicated_logic, inconsistency | Invoice total is computed in two places with different tax multipliers and rounding (SubmitInvoice uses 1.08 w |
| T-03 | undocumented_feature | full | undocumented_feature | Vendors whose code starts with "ZZ" are auto-approved (status APPROVED) and skip the approval step; this bypas |
| T-04 | db_only | full | logic_flaw, undocumented_feature | usp_SaveInvoice downgrades an APPROVED invoice with total over 50000 back to PENDING_APPROVAL, so ZZ auto-appr |
| T-05 | db_only | partial | logic_flaw | The database requires invoice Total > 0 and (VendorCode, InvoiceNo) unique; the application does not check eit |
| T-06 | logic_flaw | full | - | IsOverdue uses Days > 30, so an invoice exactly 30 days past due is not overdue (off-by-one against a 30-day p |
| T-07 | dead_code | full | dead_code | LegacyDiscount (2% discount) is private and never called (dead code). |
