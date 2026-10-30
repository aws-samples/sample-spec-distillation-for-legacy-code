# B4 field-app compatibility spec: raw-code

- Adapter: `raw-code`, key: `eval/benefits/fixtures/toy/field-app.yaml`
- Spend: $0.0633 of $0.30

| Metric | Value |
|---|---|
| key_items | 6 |
| requirements_produced | 19 |
| recall_full_pct | 100 |
| recall_any_pct | 100 |
| key_items_contradicted | 0 |
| wrong_requirements | 0 |
| invented_requirements | 0 |
| precision_keyed_pct | 84.2 |
| precision_correct_pct | 100 |
| citation_validity_pct | 100 |
| requirements_without_citation | 0 |
| recall_limit_pct | 100 |
| recall_payload_pct | 100 |
| recall_required_field_pct | 100 |
| recall_server_validation_pct | 100 |
| recall_status_pct | 100 |
| answer_tokens_in | 1541 |
| answer_tokens_out | 2349 |
| answer_cost_usd | 0.039858 |
| latency_s | 19.67 |
| errors | 0 |

## Key items

| Item | Category | Coverage | Requirement |
|---|---|---|---|
| FA-1 | required_field | full | vendorCode and invoiceNo are required (ERR_VENDOR_REQUIRED / ERR_INVOICE_NO_REQUIRED) and at least one line is required  |
| FA-2 | limit | full | At most 25 lines per invoice (ERR_TOO_MANY_LINES), not 10 as the comment says. |
| FA-3 | limit | full | Computed total including tax must not exceed 100000 (ERR_AMOUNT_LIMIT). |
| FA-4 | status | full | The call returns status PENDING_APPROVAL or APPROVED (ZZ vendors), or an ERR_* code; the stored status may be downgraded |
| FA-5 | server_validation | full | (VendorCode, InvoiceNo) must be unique and total must be > 0; enforced only by the database, so the app must handle an i |
| FA-6 | payload | full | Field lengths - vendorCode max 10 chars, invoiceNo max 20 chars (database column sizes). |

## Produced requirements

| Req | Category | Verdict | Key ids | Note | Statement |
|---|---|---|---|---|---|
| R1 | payload | matched | FA-6 |  | The request payload must include a vendorCode field of type string, mapping to VARCHAR(10) in the database. |
| R2 | payload | matched | FA-6 |  | The request payload must include an invoiceNo field of type string, mapping to VARCHAR(20) in the database. |
| R3 | payload | correct_extra |  |  | The request payload must include a lines field that is a list/array of InvoiceLine objects, each containing at minimum a Qty (quantity) and  |
| R4 | required_field | matched | FA-1 |  | vendorCode is required; if it is null or empty the server returns the error string 'ERR_VENDOR_REQUIRED' and does not persist the record. |
| R5 | required_field | matched | FA-1 |  | invoiceNo is required; if it is null or empty the server returns the error string 'ERR_INVOICE_NO_REQUIRED' and does not persist the record. |
| R6 | required_field | matched | FA-1 |  | The lines list is required and must contain at least one element; if it is null or empty the server returns the error string 'ERR_NO_LINES'  |
| R7 | limit | matched | FA-2 |  | The lines list must not exceed 25 items; if lines.Count > 25 the server returns the error string 'ERR_TOO_MANY_LINES' and does not persist t |
| R8 | server_validation | correct_extra |  |  | The server computes the invoice total as the sum of (Qty * UnitPrice * 1.08), with each line's contribution rounded to 2 decimal places befo |
| R9 | limit | matched | FA-3 |  | The computed total (after 8% tax) must not exceed 100,000.00; if total > 100000 the server returns the error string 'ERR_AMOUNT_LIMIT' and d |
| R10 | status | matched | FA-4 |  | If vendorCode starts with the prefix 'ZZ', the server assigns status 'APPROVED' before saving. |
| R11 | status | matched | FA-4 |  | If vendorCode does not start with 'ZZ', the server assigns status 'PENDING_APPROVAL' before saving. |
| R12 | status | matched | FA-4 |  | The SubmitInvoice entry point returns the status string that was assigned ('APPROVED' or 'PENDING_APPROVAL') on success. |
| R13 | server_validation | matched | FA-4 |  | The stored procedure usp_SaveInvoice overrides the status to 'PENDING_APPROVAL' if the computed total exceeds 50,000 AND the incoming status |
| R14 | status | matched | FA-4 |  | The only two status values that can be stored in the Invoice table are 'APPROVED' and 'PENDING_APPROVAL', constrained to VARCHAR(20). |
| R15 | server_validation | matched | FA-5 |  | The combination of (VendorCode, InvoiceNo) must be unique in the database; submitting a duplicate pair will cause a database-level unique co |
| R16 | server_validation | matched | FA-5 |  | The stored total must be greater than 0; a database CHECK constraint rejects any insert where Total <= 0. |
| R17 | payload | matched | FA-6 |  | vendorCode must be no longer than 10 characters to satisfy the VARCHAR(10) column constraint; values exceeding this length will cause a data |
| R18 | payload | matched | FA-6 |  | invoiceNo must be no longer than 20 characters to satisfy the VARCHAR(20) column constraint; values exceeding this length will cause a datab |
| R19 | payload | correct_extra |  |  | The Total stored in the database is typed DECIMAL(12,2), meaning the maximum storable total is 9,999,999,999.99 with exactly 2 decimal place |
