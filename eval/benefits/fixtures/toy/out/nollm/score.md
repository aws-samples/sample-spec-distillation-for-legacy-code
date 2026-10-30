# Score: `eval/benefits/fixtures/toy/out/raw-code/insights_spec.md`

- Key: `eval/benefits/fixtures/toy/key.yaml` (7 items, 0 core)
- Matcher: **deterministic**
- Findings parsed: 17, citations: 30

| Metric | Value |
|---|---|
| Recall (full matches) | 100.0% |
| Recall (partial = 0.5) | 100.0% |
| Recall (any match) | 100.0% |
| Core recall (full) | n/a |
| Core recall (any) | n/a |
| Precision (matched to key) | 70.6% |
| Precision (correct incl. true extras) | n/a |
| Incorrect findings | 0 |
| Unsupported findings | 0 |
| Citation validity | 100.0% |
| Citation accuracy (overlaps key lines) | 91.7% |
| Findings without citation | 0 |

## Recall by type

| Group | Items | Full | Partial | Missed | Full % | Any % |
|---|---|---|---|---|---|---|
| calculation | 3 | 3 | 0 | 0 | 100.0% | 100.0% |
| validation | 2 | 2 | 0 | 0 | 100.0% | 100.0% |
| workflow | 2 | 2 | 0 | 0 | 100.0% | 100.0% |

## Recall by evidence class

| Group | Items | Full | Partial | Missed | Full % | Any % |
|---|---|---|---|---|---|---|
| code | 5 | 5 | 0 | 0 | 100.0% | 100.0% |
| db | 2 | 2 | 0 | 0 | 100.0% | 100.0% |

## Recall by trap flag

| Group | Items | Full | Partial | Missed | Full % | Any % |
|---|---|---|---|---|---|---|
| comment_only | 1 | 1 | 0 | 0 | 100.0% | 100.0% |
| db_only | 2 | 2 | 0 | 0 | 100.0% | 100.0% |
| dead_code | 1 | 1 | 0 | 0 | 100.0% | 100.0% |
| duplicated_logic | 1 | 1 | 0 | 0 | 100.0% | 100.0% |
| inconsistency | 2 | 2 | 0 | 0 | 100.0% | 100.0% |
| logic_flaw | 1 | 1 | 0 | 0 | 100.0% | 100.0% |
| undocumented_feature | 1 | 1 | 0 | 0 | 100.0% | 100.0% |

## Key coverage

| Key item | Type | Core | Evidence | Flags | Coverage |
|---|---|---|---|---|---|
| T-01 | validation |  | code | comment_only, inconsistency | full |
| T-02 | calculation |  | code | duplicated_logic, inconsistency | full |
| T-03 | workflow |  | code | undocumented_feature | full |
| T-04 | workflow |  | db | db_only | full |
| T-05 | validation |  | db | db_only | full |
| T-06 | calculation |  | code | logic_flaw | full |
| T-07 | calculation |  | code | dead_code | full |

## Possible inventions / incorrect findings

- **F1** (unmatched) top score 0.18 
  > - Business rule: An invoice must have a non-empty vendor code. (InvoiceService.vb:L13-L13)
- **F2** (unmatched) top score 0.08 
  > - Business rule: An invoice must have a non-empty invoice number. (InvoiceService.vb:L14-L14)
- **F3** (unmatched) top score 0.08 
  > - Business rule: An invoice must have at least one line item. (InvoiceService.vb:L15-L15)
- **F16** (unmatched) top score 0.20 
  > - [ISSUE: undocumented_feature] Undocumented feature: The VendorCode column in the database is VARCHAR(10), but there is no length validation in the application. A vendorCode longer than 10 characters
- **F17** (unmatched) top score 0.20 
  > - [ISSUE: undocumented_feature] Undocumented feature: The InvoiceNo column in the database is VARCHAR(20), but there is no length validation in the application. An invoiceNo longer than 20 characters 
