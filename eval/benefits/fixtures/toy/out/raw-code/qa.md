# B2/B5 Q&A: raw-code

- Adapter: `raw-code`, QA set: `eval/benefits/fixtures/toy/qa.yaml`
- Context truncated: False
- Spend (all Bedrock calls incl. judge): $0.0310 of $0.30

| Metric | Value |
|---|---|
| questions | 3 |
| questions_total | 3 |
| correct_pct | 100 |
| score_pct | 100 |
| wrong | 0 |
| engineer_correct_pct | 100 |
| product_correct_pct | 100 |
| trick_correct_pct | 100 |
| trick_abstain_pct | 100 |
| plain_language_avg | 4 |
| answers_with_citations_pct | 100 |
| citation_validity_pct | 100 |
| citation_supported_pct | 100 |
| citation_hits_reference_pct | 100 |
| answer_tokens_in | 4571 |
| answer_tokens_out | 582 |
| answer_cost_usd | 0.0224 |
| latency_avg_s | 3.32 |
| errors | 0 |

## Per question

| Q | Audience | Correctness | Citation support | Trick | Plain lang | Cits | Hits ref lines | Latency s | Judge note |
|---|---|---|---|---|---|---|---|---|---|
| Q-E1 | engineer | correct | supported | n/a | n/a | 2 | True | 2.26 | Correctly states 25 lines max and ERR_TOO_MANY_LINES return value, supported by cited lines. |
| Q-P1 | product | correct | supported | n/a | 4 | 2 | True | 5.2 | Accurately covers ZZ prefix auto-approval and $50k override; mostly plain but uses some status code terms. |
| Q-T1 | trick | correct | supported | abstained_correctly | n/a | 2 | True | 2.51 | Correctly identifies inconsistency between 8% and 7% rates in two different functions, matching reference. |

## Answers

- **Q-E1** What is the maximum number of lines an invoice submitted through SubmitInvoice can have, and what is returned when it is exceeded?
  > An invoice submitted through SubmitInvoice can have a maximum of 25 lines. If that limit is exceeded, the function returns the string "ERR_TOO_MANY_LINES".
- **Q-P1** Can any supplier have invoices approved automatically without a person reviewing them?
  > Yes, but only under a specific condition. Suppliers whose supplier code starts with the letters 'ZZ' can have their invoices automatically set to 'Approved' by the system — with one important catch: if the invoice total exceeds $50,000, the database will override that approval and put the invoice back into 'Pending Approval' status, requiring a human review. So in practice, ZZ-prefix suppliers get automatic approval only when their invoice total is $50,000 or less. All other suppliers always go 
- **Q-T1** What exact sales tax rate does the system apply to invoice totals?
  > The system applies an 8% sales tax rate (multiplier of 1.08) to invoice line totals when submitting an invoice. However, the RecalculateTotal function uses a 7% rate (multiplier of 1.07) instead. These two functions are inconsistent with each other.
