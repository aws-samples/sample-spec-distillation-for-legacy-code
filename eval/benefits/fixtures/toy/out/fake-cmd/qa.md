# B2/B5 Q&A: fake-cmd

- Adapter: `command`, QA set: `eval/benefits/fixtures/toy/qa.yaml`
- Context truncated: n/a
- Spend (all Bedrock calls incl. judge): $0.0040 of $0.05

| Metric | Value |
|---|---|
| questions | 1 |
| questions_total | 1 |
| correct_pct | 0 |
| score_pct | 0 |
| wrong | 1 |
| engineer_correct_pct | 0 |
| product_correct_pct | n/a |
| trick_correct_pct | n/a |
| trick_abstain_pct | n/a |
| plain_language_avg | n/a |
| answers_with_citations_pct | 100 |
| citation_validity_pct | 50 |
| citation_supported_pct | 0 |
| citation_hits_reference_pct | 0 |
| answer_tokens_in | 100 |
| answer_tokens_out | 10 |
| answer_cost_usd | 0.0004 |
| latency_avg_s | 0.03 |
| errors | 0 |

## Per question

| Q | Audience | Correctness | Citation support | Trick | Plain lang | Cits | Hits ref lines | Latency s | Judge note |
|---|---|---|---|---|---|---|---|---|---|
| Q-E1 | engineer | wrong | partial | n/a | n/a | 2 | False | 0.03 | Tool cited stale comment saying 10; actual MAX_LINES=25 and ERR_TOO_MANY_LINES return value both missed. |

## Answers

- **Q-E1** What is the maximum number of lines an invoice submitted through SubmitInvoice can have, and what is returned when it is exceeded?
  > 10 lines per invoice (asked: What is the maximum )
