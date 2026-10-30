<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->
# eval/benefits/ — benefit-level evaluation of distillation tools

These scripts check whether a "software distillation" tool delivers the benefits the customer named.
They do not only check spec recall. Every script compares a tool against the same baselines through
an adapter:

| Adapter | What answers | Use |
|---|---|---|
| `raw-code` | One Bedrock call with the full line-numbered source (truncated to `--max-source-chars`) plus the task | The "no tool" baseline: a new engineer with an LLM |
| `spec` | One Bedrock call with the parts of `--spec spec.md` most relevant to the task, plus the task | Tests any spec as a knowledge base |
| `command` | Any external command (`--command "..."`) that prints one JSON object | The tool under test, e.g. `eval/lean/ask.py` |

This is an internal prototype. The judges are LLM-based and the fixture is tiny, so treat the numbers as indicative only.

## Scripts

| Benefit | Script | Input | Outputs (under `--out`) |
|---|---|---|---|
| B1 modernization insight | `insights.py` | answer key with `flags` + a pipeline run (`--run DIR` scores `DIR/issues/issues.md`, the Issues & Insights report), any other artifact (`--spec`, overrides `--run`), or `--adapter` to generate one | `insights.{json,md}`, `score.{json,md}`, `insights_spec.md` if generated |
| B2/B5 onboarding + product Q&A | `qa_eval.py` | `qa.yaml` | `qa.{json,md}` |
| B3 regression / impact analysis | `impact_eval.py` | `impact.yaml` (+ optional `--key` to resolve rule ids) | `impact.{json,md}` |
| B4 spec-driven field app | `fieldapp_eval.py` | `field-app.yaml` | `fieldapp.{json,md}` |
| Comparison | `report.py` | result JSONs or run dirs from the scripts above | one markdown table per benefit |

`common.py` holds the shared code: budget guard, Bedrock wrapper, source and citation helpers, and adapters.
`retrieval.py` is the spec retriever used by the `spec` adapter. Every Bedrock call is logged
to `<out>/bedrock_calls.jsonl`, and that includes the `score.py` judge calls made by `insights.py`.

### The `spec` adapter does not truncate

The spec is split into chunks along its own structure: one per markdown heading, which in a bottled spec
means one per requirement. Sections longer than 4,000 characters, such as the requirements index, are
split at line boundaries. For each question, change or request, the chunks are ranked with Okapi BM25.
This is pure Python with no new dependency. Identifiers are split, so `ExpFlag` also matches "exp flag",
and plurals are folded. The best chunks are packed into `--max-spec-chars`, which defaults to 60,000
characters (about 17K tokens), and shown in document order under headers like
`[spec chunk C12 | Section > Requirement | spec lines a-b]`. A spec smaller than the budget is sent
whole. If no chunk shares a word with the query, the opening chunks are sent and the record says
`fallback`. Each answer records which chunks it saw, with their headings, spec lines, sizes and scores,
in `questions[].context`, `changes[].context` and `generation_context`. Before this change the adapter
sent only the first `--max-spec-chars` characters: 150,000 of the 556,000-character JC business spec.

### Unmeasured is null, never 0

Every report carries `measurement: {status, reason, graded, total}` and `summary.measurement_status`:

- `measured`: every item was graded.
- `partial`: some items were graded. The judged metrics cover only the graded items, and the reason
  says how many.
- `not_measured`: nothing was graded. Every judged metric is `null`.

Deterministic metrics, such as citation validity and location recall, are computed over whatever was
answered. They are `null` when nothing was. `qa_eval.py` answers and grades in batches of
`--judge-batch` (default 6). Before each answer it reserves the cost of grading that batch, so a tight
cap yields fewer graded questions instead of many answers that nobody graded. On JC, the old code spent
the whole $2.45 cap on 16 answers and reported `correct_pct 0.0`. `impact_eval.py` reports
`rule_recall_pct: null` when the rule judge did not run; it no longer silently falls back to the
location method. `fieldapp_eval.py` marks key ids the judge did not return as `ungraded`, not `missed`.

### Keys

A key that loads zero items is an error. The script exits with code 2 before any call, so a mis-shaped
key can no longer come back as an empty "ok". Both schemas are accepted:

- impact: `{changes: [...]}` as above, or the shipped synthetic-corpus form
  `{scenarios: [{id, change, affected_locations: {must_change: [...], must_review: [...]},
  affected_rules, affected_reports, affected_external_contract: "Yes. ..."}]}`.
  - Bucketed locations are flattened. Each keeps its `bucket`, and `why` becomes `reason`.
  - `location_recall_must_change_pct` is reported next to the overall recall.
  - The contract prose becomes `affected_contracts`: the interface identifiers it names, such as
    camelCase fields (`requiresExecApproval`) and CODE_LIKE values (`JOB_CLOSED`). The prose itself
    is kept as `contract_note`.
- field app: `{items: [{id, category, requirement, ...}]}`, or the shipped
  `{requirements: [{id, kind, requirement, ...}]}`, where `kind` is read as `category`.

These loaders normalise the shipped key formats inside the evaluators, so no hand-converted copy
of a key is needed; a test checks that the loaders produce the expected locations.

### `--dry-run`

Every script takes `--dry-run`. It builds the same prompts a real run would send, including the
retrieved spec chunks, prices them, writes `<stem>.json` with `dry_run: true` and
`plan.est_cost_usd`, and exits without calling Bedrock. `insights.py --dry-run` includes the
`score.py` judge plan for the artifact, where cache hits are free. `eval/ci/run_suite.py` uses these
plans as its stage estimates.

### Budget guard

Every script takes `--max-usd` (default 1.0). Before each Bedrock call the script estimates its cost
(prompt chars / 3.5 input tokens, plus an expected output size, at $3 / $15 per 1M tokens). If spend so far
plus that estimate, plus any amount reserved for grading what was already answered, would exceed the
cap, the run stops. The script still writes partial results and
records the reason in `budget.stopped`. Cost for `command` tools is counted only when the tool reports
`input_tokens`/`output_tokens`/`cost_usd`, either at the top level or under `usage`. A command is not
started once the cap is reached.

### Command adapter contract

The template is split with `shlex`, and placeholders are then substituted inside each argument. Values
containing spaces or quotes are therefore safe and need no extra quoting. Available placeholders are
`{question}`, `{change}`, `{request}`, `{audience}`, `{asked_by}`, `{id}`, `{task}`, and `{payload_file}`
(a JSON file holding the payload and the instructions). The command runs from the repo root. Its stdout must
contain one JSON object:

- qa: `{answer, citations[], evidence_class[, cannot_determine]}`
- impact: `{affected_locations[{file,start_line,end_line,reason}], affected_rules[], affected_reports[], affected_contracts[]}`
- fieldapp: `{requirements[{category, statement, citations[]}]}`, or `{answer: "<markdown>"}`, which is segmented with `score.py`
- insights: `{findings[{statement, issue_type, citations[]}]}` or `{answer: "<markdown>"}`

Citations can be `{file,start_line,end_line}`, `{file,lines:"10-20"}`, or a string such as `"X.vb:L10-L20"`.

## Metrics

**B1 `insights.py`.** By default it scores a run's Issues & Insights report (`--run DIR` reads
`DIR/issues/issues.md`), because B1 asks whether issues are *flagged* and that report is the pipeline's
flagging artifact. The CI suites used to score the business spec here, which repeated the
`score-bottled` judge work at the same cost. Matching is done by `score.score()`: an LLM judge (one
pass, or `--judge-runs N`, under a `--max-judge-cost-usd` cap equal to what is left of `--max-usd`),
or the deterministic matcher with `--no-llm`.
Each finding is then classified for the issue categories it **explicitly flags**: by an LLM classifier,
or by keyword rules with `--no-llm`. A finding that only describes the behaviour does not count. For each
key flag (`duplicated_logic`, `logic_flaw`, `undocumented_feature`, `inconsistency`, `dead_code`, `comment_only`, `db_only`):

- described recall: the item is matched at any degree, or at full degree
- flagged recall: the item is matched by a finding that flags the same category (`flagged_any_issue` is the lenient version)
- flag precision: of the findings that flag the category, the share matched to a key item carrying that flag
  (`keyed`), or matched to any item or judged a true extra (`true`, LLM only)

`comment_only` and `db_only` describe where the evidence sits, not the kind of issue, so only described
recall and "flagged as any issue" apply to them.

**B2/B5 `qa_eval.py`.** For `qa.yaml` (`{questions:[{id, audience: engineer|product|trick, question,
reference_answer, locations[]}]}`), the batched judge reports:

- correctness (correct / partial / wrong)
- citation support (supported / partial / unsupported / none)
- trick handling (abstained_correctly / guessed)
- for product questions only, a plain-language score from 1 to 5 plus the jargon terms used

The script also computes citation validity and whether a citation overlaps the reference lines without
any LLM, and records tokens, latency and cost per answer. Product questions tell the answerer that a
product manager is asking.

**B3 `impact_eval.py`.** For `impact.yaml` (`{changes:[{id, change, affected_locations[{file,start_line,end_line[,
duplicate_group]}], affected_rules[], affected_reports[], affected_contracts[]}]}`, or the shipped
`scenarios:` form, see Keys above), it reports:

- location recall and precision, where a hit means the same file and overlapping line ranges
- file-level recall and precision
- rule recall, from an LLM judge, or with `--no-llm` from predicted locations overlapping the rule's `--key` locations
- report and contract recall, by name matching
- `all_copies_found_pct`: the share of changes where every location in each `duplicate_group` was hit

**B4 `fieldapp_eval.py`.** The adapter is asked for a compatibility spec of the external API. The default
request (override it with `--request`) covers payload, required fields, server-side validations, statuses
and limits. A judge that sees the numbered source then rates:

- each `field-app.yaml` item (`{items:[{id, category, requirement, locations[]}]}`): full / partial / missed / contradicted
- each produced requirement: matched / correct_extra / invented / wrong

Reported metrics are recall (full and any), recall per category, wrong and invented counts, precision, and citation validity.

## Self-test fixture (`fixtures/toy/`)

The fixture is a synthetic 2-file corpus (`src/InvoiceService.vb`, `src/schema.sql`) with planted traps:

- a stale comment (10 vs 25 lines)
- duplicated total logic with different tax rates and rounding
- an undocumented `ZZ` auto-approve bypass
- a database-only downgrade above 50000
- database-only uniqueness and `Total > 0` checks
- an off-by-one overdue check
- dead code

It also includes `key.yaml` (7 items with flags), `qa.yaml` (3 questions, one per audience), `impact.yaml`
(2 changes, one with a duplicate group), and `field-app.yaml` (6 items).

```bash
# from the repository root
F=eval/benefits/fixtures/toy; O=$F/out/raw-code
python3 eval/benefits/insights.py  --adapter raw-code --key $F/key.yaml --source-root $F/src --out $O --max-usd 0.4
python3 eval/benefits/qa_eval.py   --adapter raw-code --qa $F/qa.yaml --source-root $F/src --out $O --max-usd 0.3
python3 eval/benefits/impact_eval.py --adapter raw-code --impact $F/impact.yaml --key $F/key.yaml --source-root $F/src --out $O --max-usd 0.3
python3 eval/benefits/fieldapp_eval.py --adapter raw-code --key $F/field-app.yaml --source-root $F/src --out $O --max-usd 0.3
python3 eval/benefits/report.py --glob "$F/out/*" --out $F/out/comparison.md

# free (no Bedrock): deterministic matcher + keyword flags on an existing spec
python3 eval/benefits/insights.py --spec $O/insights_spec.md --no-llm --key $F/key.yaml --source-root $F/src --out $F/out/nollm
# command adapter (any tool printing JSON)
python3 eval/benefits/qa_eval.py --adapter command --tool-name lean \
  --command "python3 eval/lean/ask.py --run-dir RUN --question {question}" --qa QA.yaml --source-root SRC --out OUT
```

Results from the 2026-10-05 fixture run (Sonnet 4.6; total about $0.26 including judges):

| Benefit | raw-code baseline result |
|---|---|
| B1 | all 5 issue items described; 4/5 explicitly flagged (the off-by-one in `IsOverdue` was described as a boundary to confirm, not flagged as a flaw); flag precision 50% keyed, 100% true; $0.07 |
| B2/B5 | 3/3 correct, trick question abstained, plain-language score 4/5, citations 100% valid and supported; $0.03 |
| B3 | location recall 75% (missed the `MAX_AMOUNT` check affected by a tax change), precision 33%, file recall 100%, rule recall 100%, all duplicate copies found; $0.05 |
| B4 | 6/6 items full, 0 wrong, 0 invented, 19 requirements (16 keyed, 3 true extras); $0.06 |

Plumbing checks: the `spec` adapter, given the generated spec, scored 2/3 correct on Q&A. The spec lacked
the `ERR_TOO_MANY_LINES` code and the answer correctly said so. A fake `command` tool that returned a
stale-comment answer plus one nonexistent file was graded wrong, with 50% citation validity. With
`--max-usd 0.005` the run stopped before the first call and still wrote outputs.

The toy corpus is far too easy to tell tools apart, because the raw-code baseline nearly maxes it out.
Real comparisons need the larger hidden corpus and keys.
