# Score: `eval/benefits/fixtures/toy/out/raw-code/insights_spec.md`

- Key: `eval/benefits/fixtures/toy/key.yaml` (7 items, 0 core)
- Matcher: **llm**
- Findings parsed: 17, citations: 30

| Metric | Value |
|---|---|
| Recall (full matches) | 71.4% |
| Recall (partial = 0.5) | 85.7% |
| Recall (any match) | 100.0% |
| Core recall (full) | n/a |
| Core recall (any) | n/a |
| Precision (matched to key) | 64.7% |
| Precision (correct incl. true extras) | 100.0% |
| Incorrect findings | 0 |
| Unsupported findings | 0 |
| Citation validity | 100.0% |
| Citation accuracy (overlaps key lines) | 100.0% |
| Findings without citation | 0 |
| Judge tokens in/out (cost) | 3477/746 ($0.0216) |

## Recall by type

| Group | Items | Full | Partial | Missed | Full % | Any % |
|---|---|---|---|---|---|---|
| calculation | 3 | 2 | 1 | 0 | 66.7% | 100.0% |
| validation | 2 | 1 | 1 | 0 | 50.0% | 100.0% |
| workflow | 2 | 2 | 0 | 0 | 100.0% | 100.0% |

## Recall by evidence class

| Group | Items | Full | Partial | Missed | Full % | Any % |
|---|---|---|---|---|---|---|
| code | 5 | 4 | 1 | 0 | 80.0% | 100.0% |
| db | 2 | 1 | 1 | 0 | 50.0% | 100.0% |

## Recall by trap flag

| Group | Items | Full | Partial | Missed | Full % | Any % |
|---|---|---|---|---|---|---|
| comment_only | 1 | 1 | 0 | 0 | 100.0% | 100.0% |
| db_only | 2 | 1 | 1 | 0 | 50.0% | 100.0% |
| dead_code | 1 | 1 | 0 | 0 | 100.0% | 100.0% |
| duplicated_logic | 1 | 0 | 1 | 0 | 0.0% | 100.0% |
| inconsistency | 2 | 1 | 1 | 0 | 50.0% | 100.0% |
| logic_flaw | 1 | 1 | 0 | 0 | 100.0% | 100.0% |
| undocumented_feature | 1 | 1 | 0 | 0 | 100.0% | 100.0% |

## Key coverage

| Key item | Type | Core | Evidence | Flags | Coverage |
|---|---|---|---|---|---|
| T-01 | validation |  | code | comment_only, inconsistency | full |
| T-02 | calculation |  | code | duplicated_logic, inconsistency | partial |
| T-03 | workflow |  | code | undocumented_feature | full |
| T-04 | workflow |  | db | db_only | full |
| T-05 | validation |  | db | db_only | partial |
| T-06 | calculation |  | code | logic_flaw | full |
| T-07 | calculation |  | code | dead_code | full |
