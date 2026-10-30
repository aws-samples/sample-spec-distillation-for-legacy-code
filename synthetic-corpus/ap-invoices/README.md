<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->
# Synthetic corpus: AP Invoices (construction contractor)

A small, made-up legacy Accounts Payable module for testing spec distillation (extracting business
rules, states, workflows and exceptions from legacy code). Every name, table, proc, role and comment is
invented; nothing comes from a real customer or product. It is meant to look like a typical
VB.NET WinForms, SQL Server stored procedure and Crystal Reports stack, including the usual mess:
rules duplicated across layers, hardcoded copies of DB parameters, commented-out code and initials in
history comments. It is not meant to compile or run.

## Layout (follows the path conventions in `runtimes/mcp-server/story_module.py`)

```
ClientSide/AP/AP/            VB.NET WinForms client (module code "AP")
  frmAPInvoiceEntry.vb (+ .Designer.vb stub)   invoice entry / post / void
  frmAPInvoiceApproval.vb                      approval queue
  frmAPPaymentHold.vb                          payment holds
  clsAPInvoiceBL.vb                            client-side business rules
  clsAPInvoiceDAL.vb                           data access (APCo, Mth, APTrans, UIMth, APRef ...)
  modAPGlobals.vb                              status/role/hold constants, hardcoded limits
Scripts/Tables/AP_Tables.sql                   DDL + seed rows (APApprLim, APHoldCode)
Scripts/StoredProcedures/                      usp_APInvPost, usp_APInvApprove, usp_APHoldRelease,
                                               usp_APRetgCompute, usp_APDupCheck
Scripts/Triggers/trg_APInvHdr_u.sql            status-transition guard + audit
Scripts/Reports/412.sql                        report command SQL
Reports/AP Accounts Payable/...(412).txt       Crystal report definition stand-in
ANSWER_KEY.yaml                                ground truth
```

## Answer key

`ANSWER_KEY.yaml` has a top-level `items:` list. Each item has `id`, `type`
(rule/state/workflow/exception), `statement`, `locations[{file, start_line, end_line}]` (paths relative
to this folder, 1-based, inclusive), `evidence_class` (Verified / Inferred / Unknown / Confirmation
Required) and `flags[]` (trap flags). All line ranges were checked against the final files.
**If you edit any source file, re-check the ranges.**

## Seeded traps

| Trap | Item | What a good extractor should notice |
|---|---|---|
| db_only | BR-018 | A 1099 vendor with no TIN cannot post. Only `usp_APInvPost` checks this. |
| dead_code | BR-019 | The 2%/10 early-pay discount: no callers, and the proc it calls does not exist. |
| inconsistency | BR-020 | Approval threshold: the UI uses `>= 10000`, the proc uses `> ApprThreshold`. |
| comment_only | BR-021 | Utility vendors are exempt from retainage. This exists only as a VB comment. |

Secondary traps: BR-028 (hardcoded UI copies of DB limits, latent drift), BR-029 (removed MGR
auto-release and bulk approve). Gaps that need human confirmation: BR-027 (the PM job filter is
UI-only), EX-005, EX-006 and EX-007 (code that is not in the corpus).
