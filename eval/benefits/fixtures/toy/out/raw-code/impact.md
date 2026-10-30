# B3 impact analysis: raw-code

- Adapter: `raw-code`, set: `eval/benefits/fixtures/toy/impact.yaml`
- Rule matching: llm
- Spend: $0.0501 of $0.30

| Metric | Value |
|---|---|
| changes | 2 |
| changes_total | 2 |
| location_recall_pct | 75 |
| location_precision_pct | 33.3 |
| file_recall_pct | 100 |
| file_precision_pct | 50 |
| rule_recall_pct | 100 |
| rule_recall_by_location_pct | 100 |
| report_recall_pct | n/a |
| contract_recall_pct | 100 |
| all_copies_found_pct | 100 |
| duplicate_groups_complete_pct | 100 |
| invalid_locations | 0 |
| predicted_locations | 9 |
| answer_tokens_in | 3056 |
| answer_tokens_out | 2311 |
| answer_cost_usd | 0.0438 |
| latency_avg_s | 17.61 |
| errors | 0 |

## Per change

| Change | Expected locs hit | Predicted locs correct | Files hit | Rules | All copies | Missed contracts | Latency s |
|---|---|---|---|---|---|---|---|
| I-1 | 2/3 | 2/5 | 1/1 | 1/1 | True | - | 19.5 |
| I-2 | 1/1 | 1/4 | 1/1 | 2/2 | n/a | - | 15.72 |

## Missed expected locations

- I-1: `InvoiceService.vb:22-22`
