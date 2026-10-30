<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->
# runtimes/pipeline — deterministic paging and viewer scripts

Helper scripts that `run_distillation.sh` calls around the model-backed stages. None of them calls
Amazon Bedrock.

| File | Used by | What it does |
|---|---|---|
| `concat-batch.sh` | Phase 1 | Groups enumerated source files into line-numbered pages under a token budget. Oversized files are split into resumable parts, so every line is paged exactly once. |
| `check-page-conservation.py` | Phase 1.1 | Fails the run unless every enumerated source unit, part and line appears exactly once in the pages (or is listed as excluded with a reason). |
| `manifests/*.txt` | Phases 2–4 | Expected output files per extraction pass ("still"), used by the completeness checks. |
| `generate-viewer-v3.py` | Phase 6 | Builds the static HTML viewer (spec, issues, knowledge graph, evaluation tab). |
| `viewer_evaluation.html` | Phase 6, launcher | Template for the viewer's Evaluation tab. |
| `cytoscape.min.js` | viewer | Bundled third-party graph library (MIT; see `THIRD-PARTY-LICENSES`). |
| `ingest-helpers/rpt/RptExtract.java` | optional | Extracts text from Crystal Reports `.rpt` files when a licensed Java runtime library is configured (`RPT_JRC_JAVA` / `RPT_JRC_CLASSPATH`). Without it `.rpt` files are listed in `excluded.tsv`. |
| `tests/` | pytest | Paging conservation and viewer tests (no AWS). |

Run the tests from the repository root:

```bash
python3 -m pytest -q runtimes/pipeline/tests
```
