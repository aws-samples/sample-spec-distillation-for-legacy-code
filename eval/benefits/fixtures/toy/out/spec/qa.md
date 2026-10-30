# B2/B5 Q&A: spec-from-rawcode

- Adapter: `spec`, QA set: `eval/benefits/fixtures/toy/qa.yaml`
- Context truncated: False
- Spend (all Bedrock calls incl. judge): $0.0403 of $0.10

| Metric | Value |
|---|---|
| questions | 3 |
| questions_total | 3 |
| correct_pct | 66.7 |
| score_pct | 83.3 |
| wrong | 0 |
| engineer_correct_pct | 0 |
| product_correct_pct | 100 |
| trick_correct_pct | 100 |
| trick_abstain_pct | 100 |
| plain_language_avg | 3 |
| answers_with_citations_pct | 100 |
| citation_validity_pct | 100 |
| citation_supported_pct | 66.7 |
| citation_hits_reference_pct | 100 |
| answer_tokens_in | 5090 |
| answer_tokens_out | 937 |
| answer_cost_usd | 0.0293 |
| latency_avg_s | 4.04 |
| errors | 0 |

## Per question

| Q | Audience | Correctness | Citation support | Trick | Plain lang | Cits | Hits ref lines | Latency s | Judge note |
|---|---|---|---|---|---|---|---|---|---|
| Q-E1 | engineer | partial | partial | n/a | n/a | 2 | True | 4.22 | Correctly identifies MAX_LINES=25 and stale comment, but wrongly claims ERR_TOO_MANY_LINES return value cannot be determined from material. |
| Q-P1 | product | correct | supported | n/a | 3 | 5 | True | 5.08 | Accurately covers auto-approval for ZZ vendors, 50k override, and the discrepancy between returned vs stored status. |
| Q-T1 | trick | correct | supported | abstained_correctly | n/a | 2 | True | 2.82 | Correctly identifies both inconsistent rates (8% vs 7%) and different rounding modes, concluding no single authoritative rate exists. |

## Answers

- **Q-E1** What is the maximum number of lines an invoice submitted through SubmitInvoice can have, and what is returned when it is exceeded?
  > The constant MAX_LINES is set to 25, so an invoice submitted through SubmitInvoice may have at most 25 line items. When this limit is exceeded, the material states that the validation check exists (InvoiceService.vb:L15 confirms at least one line item is required, and L7-L8 define the MAX_LINES constant at 25), but the material does not specify what is returned or thrown when the 25-line limit is exceeded — only that the rule exists. Additionally, there is an inconsistency: a comment on line 7 s
- **Q-P1** Can any supplier have invoices approved automatically without a person reviewing them?
  > Yes — but with an important caveat. Suppliers whose code begins with 'ZZ' have their invoices automatically set to 'Approved' by the system, with no human review required. However, there is a hidden rule in the database that quietly changes the status back to 'Pending Approval' for any invoice where the total exceeds 50,000. This means that in practice, only ZZ-vendor invoices with a total of 50,000 or less are truly auto-approved and stored as such. For ZZ-vendor invoices above 50,000, the syst
- **Q-T1** What exact sales tax rate does the system apply to invoice totals?
  > The system applies two different tax rates depending on which code path is used, and they are inconsistent. The SubmitInvoice method applies an 8% tax rate (multiplier 1.08), while the RecalculateTotal method applies a 7% tax rate (multiplier 1.07). Additionally, the two methods use different rounding modes (banker's rounding vs. AwayFromZero), meaning they can produce different totals even beyond the tax rate difference. There is no single authoritative tax rate — the rewrite team must decide w
