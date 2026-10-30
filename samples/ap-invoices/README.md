<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->
# Sample: AP invoices (synthetic)

Synthetic VB.NET WinForms + SQL Server accounts-payable module for a fictional construction
contractor. 16 files, about 2,320 lines. Every name, table, procedure, role and comment is invented;
it contains no real customer or product code, and it is not meant to compile or run.

Run the pipeline on it (copy it first so this README is not part of the input):

```bash
cp -r samples/ap-invoices /tmp/ap-src && rm /tmp/ap-src/README.md
DISTILL_TARGET=/tmp/ap-src DISTILL_OUTPUT=output/ap-invoices DISTILL_MAX_USD=20 ./run_distillation.sh
```

The answer key used to score the output is deliberately kept out of this folder, so the pipeline
never sees it: it lives in `synthetic-corpus/ap-invoices/ANSWER_KEY.yaml` next to an identical copy
of the source.
