# eval/ — spec-distillation evaluation harness

Self-contained harness for checking whether a spec-extraction pipeline beats a simple
one-prompt baseline, against code whose answers are known in advance. Nothing here
modifies the rest of the repo. Run outputs go under `eval/runs/` (gitignored).

## Layout

| Path | What |
|---|---|
| `bedrock_util.py` | Bedrock Converse wrapper (`REGION` us-east-1, which does not follow `AWS_REGION`; override with `EVAL_BEDROCK_REGION`; model `us.anthropic.claude-sonnet-4-6`). Logs every call to `logs/local/bedrock_calls.jsonl`, which is gitignored and kept per machine, or to `$EVAL_BEDROCK_LOG` |
| `score.py` | **A. Deterministic layer** + CLI: segmentation, citation verification, source coverage, structural checks, metrics, report |
| `judge/` | **B. Judged layer**: tool-use judge, JSON Schema, verdict cache, chunked evidence, bootstrap/kappa, human-labelled golden sets |
| `tests/` | pytest suite for both layers. Makes **no AWS calls** (the Bedrock client is mocked and a real one raises) |
| `graph_eval.py`, `graph/` | Validates a run's **knowledge graph** (not its prose): structural integrity, accuracy vs the deterministic parser, concept coverage vs a key. See [Knowledge-graph validation](#knowledge-graph-validation-graph_evalpy). |
| `graph/tests/` | pytest suite for `graph_eval.py` (no AWS: Bedrock is injected as a fake client) |
| `baseline.py` | Simple baseline: one Converse call per file, concatenated to `runs/<name>/spec.md` |
| `keys/jc-changeorders/`, `../synthetic-corpus/*/ANSWER_KEY.yaml` | Answer keys for the two synthetic corpora |
| `fixtures/mini/` | Small hand-made fixture (source, key, spec with planted inventions and a bad citation) |
| `ci/` | **Suite runner**: one command, one `suite.json` + `suite.md`, budget cap, provenance, thresholds, `--baseline` regression gate, `--smoke` mode. See [CI / regression layer](#ci--regression-layer-ci) |
| `mutate/` | **Mutation harness**: inject known defects into a corpus copy and measure detection power. See [Mutation harness](#mutation-harness-mutate--detection-power) |
| `keygen/` | **Answer-key builder** for code that has no key: harvest + tests (+ docs, + bounded AI draft), merge, planted problems, human review. See [Building an answer key for new code](#building-an-answer-key-for-new-code) |
| `keys/eshoponweb/` | Draft key for the public eShopOnWeb app (`dotnet-architecture/eShopOnWeb` @ `4da8212`, MIT, source not committed; see `THIRD-PARTY-LICENSES`) and the seed-42 C# mutation ground truth |

## The two layers, and why they are separated

A spec score mixes two very different kinds of claim. Reporting them together made the
old numbers look more solid than they were.

**A. Deterministic (exact).** No model. Same inputs always produce the same bytes.
Citation validity, source coverage, structural defects and segmentation counts are
*facts* about the spec and the source tree. They are fully unit- and property-tested.

**B. Judged (estimated).** "Does this finding express key item BR-018?" is a judgement.
It is produced by Claude, run N times with a majority vote, allowed to abstain, and
reported with across-pass spread, a bootstrap 95% CI, Fleiss/Cohen kappa and
agreement with human labels. Treat a judged number as an interval, never as a point.

### What is exact vs estimated

| Reported value | Exact or estimated | Why |
|---|---|---|
| `counts.findings`, `segmentation.*` | **Exact** | Deterministic parse of the spec; strategy is named in the report |
| `citations.valid / invalid / by_category` | **Exact** | File resolved under `--source-root`, range checked against an exactly counted file |
| `citations.off_by_one` | **Exact** | Citations ending at exactly `line_count + 1` (the pager's phantom trailing line) |
| `source_coverage.*` (lines cited, files never cited, per-file density) | **Exact** | Set algebra over verified citation ranges |
| `source_coverage.key_locations` | **Exact** | Interval intersection against the key's own locations |
| `structural.*` (duplicate ids, unresolved cross-refs, uncited requirements, orphan citations, malformed sections) | **Exact** | Document scan |
| `citations.accuracy_pct` | **Mixed** | The overlap test is exact; *which* findings count is a judged match |
| `recall.*`, `precision.*`, `key_coverage`, `recall_by.*` | **Estimated** | Majority vote of N judge passes |
| `possible_inventions` | **Estimated** | Judge verdict `incorrect`, or unmatched with an invalid citation |
| `judge.across_runs` | **Measured judge noise, lower bound** | Passes differ only by the pass-number line (temperature is 0), so real-world judge variance is at least this wide |
| `judge.plan.est_cost_usd` | **Estimate of spend** | Characters / 3.6 tokens at list price, for the calls the cache will **not** serve. `est_cost_usd_uncached` is the cold-cache figure. `expected_cache_hits` is the number of calls the cache will serve |
| `judge.bootstrap_ci95` | **Estimated small-sample uncertainty** | Resamples key items / findings. Does **not** shrink by judging more times |
| `judge.agreement.fleiss_kappa_*` | **Exact arithmetic on estimated labels** | Chance-corrected agreement between passes |
| `judge.golden.*` | **Measured judge error** | Agreement with human labels on a hand-labelled subset |
| `judge_usage.cost_usd` | **Exact for calls made, list price** | Cached calls cost $0 and are counted separately |

Three things the scorer now refuses to do quietly:

* publish a percentage above 100 (`assert_percentages_sane`; this is the bug that once
  printed "precision, true of source = 104.8%");
* compare two specs whose finding counts differ by more than 2x (`--compare-to` prints a
  `WARNING` and sets `segmentation.comparability.comparable = false`);
* accept judge output that does not validate against the JSON Schema (one corrective
  retry, then it raises).

## Quick start

```bash
cd eval

# scorer self-test on the hand-made fixture (~$0.035 for 3 judge passes).
# Expect: 6 findings, 5 valid citations + 1 out_of_range, recall full 75% (D1 missed),
# 2 incorrect (the planted inventions F3/F4), Fleiss kappa 1.0 (all 3 passes unanimous).
python3 score.py --spec fixtures/mini/spec.md --key fixtures/mini/key.yaml \
  --source-root fixtures/mini/src --out-dir fixtures/mini/out --judge-runs 3

# exact layer only, no Bedrock calls, ~1 s
python3 score.py --spec SPEC.md --key KEY.yaml --source-root SRC --no-llm --out-dir OUT

# see what a judged run would cost before spending anything (cache hits count as $0)
python3 score.py --spec SPEC.md --key KEY.yaml --source-root SRC --dry-run-judge

# full run: 3 judge passes, majority vote, CIs, kappa, calibration
python3 score.py --spec SPEC.md --key KEY.yaml --source-root SRC \
  --segment requirements --judge-runs 3 \
  --golden judge/golden/ap-invoices-demo-bottled.yaml \
  --max-judge-cost-usd 6 --out-dir OUT

# re-score: every verdict comes from the cache, costs $0, and is byte-identical
python3 score.py ... --out-dir OUT2
```

## Flags

### Unchanged (backward compatible)

`--spec` `--key` `--source-root` `--out-dir` `--no-llm` `--gen-log` `--gen-tag-prefix`
`--suffix` `--exclude` `--max-source-chars` `--judge-tag` `--reuse-judgement` behave as
before. `--segment` gained a third value. `score.json` keeps all of its old top-level
keys and adds new ones.

`--no-llm` is now labelled honestly: the lexical matcher is a **smoke test**. Its bar rose
from 0.22 token containment to 0.45 (or 0.25 with a citation that lands inside the key
item's own lines), because the old bar matched on shared vocabulary alone. It still cannot
tell a rule from its negation. Never publish a recall number from this path.

### A. Deterministic layer

| Flag | Default | What it does |
|---|---|---|
| `--segment auto` | default | A heading section containing an `Evidence:`/`Citations:` line becomes one finding; everything else splits per bullet / paragraph / table row / code block. Byte-identical to the previous default. |
| `--segment lines` | | Every bullet, paragraph, table row and code block is its own finding (the original behaviour). |
| `--segment requirements` | | **New.** One finding per requirement id. Index-table rows fold into their requirement instead of inflating the count; remaining prose is kept and tagged `kind: narrative`; an index entry with no `####` definition is kept as `kind: index_only` and reported. Falls back to `auto` with a recorded note when the spec has no ids. |
| `--compare-to SCORE_JSON` | off | **New.** Compare finding counts with an earlier report. Warns loudly, on stderr and in the report, when they differ by more than 2x or use a different strategy, because recall and precision are then not comparable. |
| `--max-finding-chars N` | 20000 | **New.** Cap on text retained per finding. Citations are parsed from the full text first, and the cap keeps a head *and* a tail so a trailing `Evidence:` line survives. Bounds memory on very large specs. |
| `--max-report-findings N` | 5000 | **New.** Cap on per-finding detail written to `score.json`. Metrics always cover every finding; `findings_total` and `findings_reported` say what was written. |

### B. Judged layer

| Flag | Default | What it does |
|---|---|---|
| `--judge-runs N` | **3** | **New.** Judge every finding N times and take a majority vote. A verdict or key match needs strictly more than half the passes; otherwise the finding is reported `uncertain` / `no_majority`. Cost scales linearly — use `--judge-runs 1` for a cheap check. Use an odd N. |
| `--judge-batch N` | 25 | **New.** Findings per judge call. |
| `--judge-evidence chunked` | **default** | **New.** Send only the lines the batch's findings cite (plus `--evidence-context` lines) and the source of the lexically closest key items, instead of one ~200K-char dump of the whole tree. A manifest records the exact intervals per batch. |
| `--judge-evidence full` | | The legacy whole-tree dump, capped by `--max-source-chars`. Kept so published numbers can be reproduced. |
| `--max-evidence-chars N` | 40000 | **New.** Per-batch cap on chunked evidence. Over-budget intervals are dropped lowest-priority-first and listed in the manifest; `judge.evidence_truncated_batches` counts affected batches. |
| `--evidence-context N` | 8 | **New.** Context lines around each cited range. |
| `--evidence-key-items N` | 12 | **New.** How many lexically closest key items contribute their source lines to a batch. Every key *statement* is always in the prompt; this controls only whose *code* is shipped. |
| `--no-cache` | off | **New.** Bypass the content-addressed verdict cache (neither read nor write). |
| `--cache-dir DIR` | `judge/cache` | **New.** Where cached verdicts live. The cache key is sha256 over the prompt (with its pass header reduced to the pass **index**, see below) + system + model + tool + schema hash + max tokens + temperature, so any other change is a miss rather than a stale hit. Cache *contents* are gitignored (regenerable, machine-specific), so a fresh clone pays for the first judged run. |
| `--max-judge-cost-usd N` | off | **New.** Abort *before* calling Bedrock if the estimate of the calls the cache will **not** serve exceeds this, and abort mid-run if actual spend passes it. Exits 3. A fully cached re-score passes any cap. |
| `--dry-run-judge` | off | **New.** Print the call/token/cost plan and exit without calling Bedrock. The plan checks the cache for every (pass, batch) call: `expected_cache_hits`, `est_cost_usd` (to be spent) and `est_cost_usd_uncached`. |
| `--log-judge-prompts DIR` | off | **New.** Write every judge prompt verbatim, for auditing exactly what the judge saw. |
| `--golden FILE` | off | **New.** Human-labelled calibration file; reports judge-vs-human agreement, found-vs-missed agreement, Cohen kappa and the confusion table. |
| `--bootstrap-iters N` | 2000 | **New.** Bootstrap resamples for the 95% CIs. |
| `--seed N` | 20261006 | **New.** Bootstrap seed. Two runs with the same seed print the same CI. |

### Determinism knobs, and their limit

Temperature is 0. Key items are sorted by id, findings are in stable natural order
(`F9` before `F10`), and the prompt sections are in a fixed order. The only thing that
differs between passes is the line `Evaluation pass k of N`. That makes each pass
individually reproducible and cacheable while still giving an independent sample — but it
also means **the measured across-pass spread is a lower bound on judge noise**, not an
upper one. `judge.determinism.pass_salt` says so in the report.

**The cache is keyed on the pass index k, not on N.** The prompt sent to the model still says
"pass k of N". The cache key, however, is computed with that line reduced to "pass k". So pass 1
of a 1-pass run and pass 1 of a 3-pass run share an entry: moving from `--judge-runs 1` to 3
pays only for passes 2 and 3, and a 1-pass re-score of a 3-pass run is free. Entries written
before this rule were keyed on the full "pass k of N" prompt. They are still found, because on a
miss the runner tries the old key for every N' from k to 9, and a hit is re-filed under the new
key. On the JC business spec this turns the cost of a 1-pass re-score from $5.39 into $0, using
the pass-1 entries of the aborted 3-pass run. A 3-pass run that reuses them costs $10.36 instead
of $16.17.

## What the judge can answer

`matched` / `correct_extra` / `unsupported` / `incorrect` / **`uncertain`**.

`uncertain` is abstention. The prompt tells the judge to use it when the deciding source
lines are not in the excerpt it was given, or when the finding is too vague to evaluate,
rather than guessing. Abstentions are reported as their own bucket
(`precision.uncertain`, `judge.abstention`) and are excluded from
`precision.keyed_pct_of_decided`. They are never forced into a verdict.

## Golden calibration

`judge/golden/TEMPLATE.yaml` documents the format and the labelling rule.
`judge/golden/ap-invoices-demo-bottled.yaml` labels 15 of the 48 AP key items against the
demo bottled spec — all 7 seeded traps, the 3 Unknown exceptions, and 5 ordinary Verified
controls — each with the spec requirement ids read, the source lines checked, and a
written basis. The selection over-samples the hard cases on purpose, so agreement there is
a lower bound on agreement over the whole key.

`--golden` then reports how often the judge's majority vote matched the human label. That
is the one number in the report that *measures* judge error instead of assuming it.

## Metrics

- **Recall**: share of key items with a `full` match; also partial-credit (partial = 0.5),
  any-match, and the same for `core` items. From the majority vote.
- **Precision**: findings matched to at least one key item / all findings (`keyed`);
  `correct` also counts findings the judge rates true of the source but not in the key;
  `keyed_pct_of_decided` excludes abstentions. `incorrect` = contradicts the source;
  `unsupported` = the kind of claim source could never confirm.
- **Citation validity**: the file exists and the range is within an exactly counted file.
  Line counting splits on CR/LF/CRLF only — `str.splitlines()` also breaks on form feeds,
  which legacy COBOL and VB listings contain and which no editor counts as a line break.
  `off_by_one_end` / `phantom_line_only` separate the pager artifact (a citation ending at
  `line_count + 1`) from a real out-of-range error; `validity_pct_allowing_off_by_one`
  reports what validity would be if you forgave it.
- **Citation accuracy**: for matched findings, at least one citation overlaps the key
  item's lines. The key's location sets are not exhaustive, so this under-rates specs that
  cite other lines which also support the item.
- **Source coverage**: which key locations the spec cites anywhere, which source files and
  lines are never referenced, per-file citation density.
- **Structural**: duplicate requirement ids, unresolved cross-references (answer-key ids
  are reported separately, not as unresolved), requirements with no citation, orphan
  citations (citations outside any requirement), heading level jumps, empty sections,
  duplicate headings, unclosed code fences, thin requirements.
- **Tokens/cost**: judge usage and (with `--gen-log`) generation usage, at $3 / $15 per 1M
  input/output tokens. Cached calls are counted as `cached_calls` and cost $0.

## Supported citation formats

`file.ext:L10-L20` · `file.ext:10-20` · `file.ext#L10-L20` · `file.ext (lines 10-20)` ·
`file.ext L10` · `file.ext:L10` · `file.ext, 10-20` · `file.ext:ll. 10-20` ·
en/em dash, `to` and `..` as range separators · `file.ext:L10-L20, L30-L40` (later ranges
inherit the file) · bare `L10-L20` / `lines 10-20` (nearest cited file, or a file-named
heading, or the only file in the source root) · `lines 10-20 of file.ext` (reverse order) ·
literal names with spaces, parens, quotes or non-ASCII characters, e.g.
`AP Open Payables by Job (412).txt:L30`. Paths resolve by exact relative path first, then
by basename. `(+1)` after a citation, which the pipeline writes to mean "one more
location", is correctly *not* parsed as a citation.

## Tests

```bash
# one-off venv for hypothesis (eval/.venv is gitignored)
python3 -m venv eval/.venv && eval/.venv/bin/pip install -q hypothesis pytest PyYAML jsonschema boto3

eval/.venv/bin/python -m pytest eval/tests -q -m "not slow"   # fast subset, ~4 s
eval/.venv/bin/python -m pytest eval/tests -q                  # incl. 4 large-input, ~2 min

# every eval suite together (no AWS), ~2.5 min
python3 -m pytest -q eval/tests eval/graph/tests eval/ci/tests eval/mutate/tests eval/benefits/tests
```

`hypothesis` drives the property tests for citation classification, parse round-trips over
every supported format, interval merging, complement and overlap. When it is not
importable the same properties run as seeded generative tests, so the invariants are
checked either way.

No test makes an AWS call: an autouse fixture replaces `bedrock_util.client` with an object
that raises, and the `fake_bedrock` fixture installs a scripted `converse` stand-in.

Covered edge cases include: empty spec; whitespace-only spec; spec with no citations; all
citations invalid; citation to a non-existent file; out-of-range, off-by-one and
phantom-line ranges; reversed and zero/negative ranges; empty source file; multibyte,
CRLF, CR-only, BOM and invalid-UTF-8 source; form feeds; duplicate requirement ids;
unresolved cross-references; a 50 MB spec and an 8 MB single line (performance and
streaming); 20,000 tiny findings; a key with 0 items; a key whose locations point outside
the file or at a missing file; a key with duplicate ids; identical findings repeated 100x;
a finding matching two key items; nested and overlapping line ranges; unicode, quotes and
parens in filenames; judge returning free text, invalid JSON, a wrong-schema payload, an
empty response, two tool blocks, a truncated response, or a throttling error; cache
hit/miss/corruption/disabled; majority-vote ties and minority matches; cost-cap aborts;
and determinism (same inputs twice → identical report bytes).

## Scoring the repo's full pipeline

Point `score.py` at the pipeline's final artifact (e.g. `$DISTILL_OUTPUT/bottled_spec/bottled_spec.md`
or `master_blend.md`) with the same key and source root. Use `--segment requirements` when
the artifact has requirement ids, and pass `--compare-to` the other artifact's `score.json`
so a segmentation mismatch cannot be mistaken for a quality difference.

```bash
# from the repository root: a one-prompt-per-file baseline, then score it against the same key
python3 eval/baseline.py --source-root samples/ap-invoices --out eval/runs/baseline-ap
python3 eval/score.py --spec eval/runs/baseline-ap/spec.md --key synthetic-corpus/ap-invoices/ANSWER_KEY.yaml \
  --source-root samples/ap-invoices --gen-log eval/runs/baseline-ap/tokens.jsonl
# the pipeline's bottled spec, scored the same way
python3 eval/score.py --spec output/ap-invoices/bottled_spec/bottled_spec.md \
  --key synthetic-corpus/ap-invoices/ANSWER_KEY.yaml --source-root samples/ap-invoices \
  --segment requirements --out-dir eval/runs/pipeline-ap
```

## Programmatic use

`eval/benefits/*` import `score` and call `score.score(...)` with eleven positional
arguments. That signature and **its defaults are unchanged**: one judge pass, full source
dump. Everything new is keyword-only after `*`. The stronger defaults (3 passes, chunked
evidence) apply to the CLI only, so existing callers keep their old cost and behaviour.
`source_files`, `number_source`, `resolve_file`, `segment_markdown`, `load_findings`,
`load_key`, `parse_key_cit`, `overlap` and `to_markdown` all keep their signatures.

## Prerequisites

- Python 3 with `boto3` and `PyYAML`. `jsonschema` is optional (used only to cross-check
  the built-in validator in tests); `hypothesis` is optional (seeded fallback otherwise).
- AWS credentials with Amazon Bedrock runtime access in us-east-1 (or `EVAL_BEDROCK_REGION`),
  from any standard source: `aws configure`, `aws sso login` (IAM Identity Center), environment
  variables, or an instance role. Use a non-production account.
- Docker (optional) for the containerised .NET parser. Containers get code via `docker build`
  contexts and input via stdin rather than bind mounts, so they also work with Docker setups that
  do not share every host path.

## Knowledge-graph validation (`graph_eval.py`)

`score.py` scores the prose a run emits. `graph_eval.py` scores the **graph** it emits — the part of
the pipeline that had no test at all. It reads `<output>/knowledge_graph/` (concept graph, code graph,
concept↔code bridge) plus `<output>/facts/` (the deterministic parser) and writes
`graph_eval.json` + `graph_eval.md`.

```bash
python3 eval/graph_eval.py \
  --run output/ap-invoices-demo \
  --source-root synthetic-corpus/ap-invoices \
  --key synthetic-corpus/ap-invoices/ANSWER_KEY.yaml \
  --exclude ANSWER_KEY.yaml --exclude README.md

python3 eval/graph_eval.py ... --llm --sample 30 --max-cost 2.0   # add section 4
python3 eval/graph_eval.py --list-thresholds                      # gateable metric names
python3 -m pytest eval/graph/tests -q                             # 53 tests, no AWS
```

Default `--out-dir` is `<run>/validation/graph/`. **Three of the four sections need no LLM, no
network and no credentials**, so CI can gate on them. Exit codes: `0` clean, `1` a gate was violated,
`2` no `<output>/knowledge_graph/`, `3` refused (see *provenance* below).

### 1. Structural integrity — exact, no LLM

Anchors (does a node point at something that really exists?): every `source_files.path` resolves
under `--source-root`; every `code_objects.object`, `code_entities.code_name` and non-dynamic
`code_edges.dst` occurs in its cited file (dynamic-SQL edges are exempt by construction); every
bridge link's symbol occurs in its cited file. Results are cross-tabbed against the row's **own**
`anchor_verdict` / `verdict`, so `anchor_claims_contradicted` isolates rows the pipeline asserts are
`confirmed` but that fail an independent check — the sharpest regression signal.

Shape: no duplicate node ids, no dangling edge endpoints, no self-loops, no duplicate edges;
`same_as` merge classes stay within one still and map onto exactly one unified concept (either
violation is a *contradictory merge*); transitivity gaps, reciprocal pairs and longer `same_as`
cycles are counted; `CONTRIBUTES_TO` must be an acyclic block → unified-concept bipartite grouping
with out-degree 1; unified concept ids are **recomputed** from their contributor ids
(`sha256` of the sorted ids, per `knowledge.py`), so an id is verifiable rather than merely unique;
orphan concepts and orphan code objects are reported as rates.

Both *ends* of a bridge link are checked. The concept end is reported in tiers. The two artifacts
now **share one canonical key** (`runtimes/mcp-server/concept_key.py` strips the assigned
`[DT-NNNN] ` id and the leading `"<Type>: "` label exactly once; `concept_symbols.py`,
`knowledge.py` and `export_graph.py` all use it), so `exact` is the expected tier. The looser tiers
remain because a run made before the shared key still has to be scorable — and because
`id_stripped` / `canonical` on a *new* run is a regression signal: something in the chain stopped
writing the canonical key. `unresolved` is a genuinely dangling reference.

The symbol end is reported at five strictnesses (`full_word`, `full_sub`, **`join_word`**,
`bare_word`, `bare_sub`), plus the same rate under four plausible denominators. A single "N of M
links fail" number is ambiguous on this data; the table is not. `join_word` is the link's **own
assertion**: the identifier it claims is in the cited file (`join_name`), at identifier boundaries.
Each failure is classified:

| Class | Meaning |
|---|---|
| `ok` | the symbol occurs verbatim in the cited file |
| `qualifier_resolved` | not verbatim, but the link records which table/type its dotted qualifier resolved to and the joined identifier really is there — a **true** assertion, not a failure |
| `qualifier_dropped` | a dotted qualifier was dropped with no resolution recorded (pre-fix behaviour) |
| `substring_only` | the joined name occurs only inside a longer identifier (`cboVendor` for `Vendor`) — a plausible but **wrong** file |
| `name_absent` / `file_unresolved` | nothing to anchor to |

`bridge_link_assertion_pct` = `ok` + `qualifier_resolved`. `bridge_symbol_occurrence_pct`
(verbatim symbol only) is kept unchanged for continuity with the recorded baseline, and the two are
reported side by side — more resolvable *qualified* symbols lowers the verbatim rate while raising
the share of links that assert something true, so quoting only one of them misleads.

Links the join **refused** to emit are read from the bridge's own `dropped[]` ledger
(`bridge_links_dropped_by_the_join`), so a bridge that fails closed is distinguishable from one
that had nothing to drop.

### 2. Accuracy vs the deterministic parser — the headline metric

`<output>/facts/graph.json` comes from a Roslyn VB.NET + C# and ScriptDom T-SQL pass
(`runtimes/facts`), so for the relations it models it is ground truth, not another opinion. Both
graphs are projected into `(source file, target bare name, relation)` and scored at three
strictnesses: **pair** (right two things?), **typed** (right relation label?) and per relation / per
source language.

The **core** relation set — `table_read` / `table_write` / `proc_exec` / `trigger_on_table` — is the
headline, and its definition is **frozen** so the number stays comparable across runs.
`method_call` is scored next to it under `extended`: the VB/C# prompts could not express
method→method calls until recently, and folding ~100 newly-comparable parser edges into the headline
denominator would have silently redefined the metric.

Recall is also split **by provenance**. `code_graph.py` backfills relations it finds in
`facts.json`, tagging them `facts:*` in `code_edges.symbol`; scoring those against `facts/graph.json`
is circular, so `by_provenance.model_proposed` gives the numbers for the edges the extraction prompt
produced on its own. Quote that one when the question is "how good is the prompt".

Scope differences are reported, never absorbed: LLM `IMPORTS` edges have no parser counterpart;
a `comment_mention` edge (a table named only inside a comment) is a mention, not an access; `#temp`
tables are outside what the extraction prompt can emit. Precision and recall are computed only over
relations both graphs can express, and false positives are split into `relation_mislabelled` (the
parser connects the same pair differently) vs `pair_not_in_parser` (the only class that can be a
fabrication). If `facts/graph.json` is absent the section is skipped with a note that names the
command to produce it; its thresholds report `null`.

### 3. Concept coverage vs the answer key — deterministic

Not a semantic recall number (that is `score.py`). For each key item: is there a concept whose
citations overlap the lines the item lives on? `full` = one concept covers every location,
`partial` = some but not all, `missing` = nothing cites any location. Broken out by item type,
evidence class and trap flag, plus location-level recall. Citation parsing and key loading are
**imported** from `score.py` (not copied, not modified) with a built-in fallback if the import
breaks. Full concept bodies come from `stills/shared_vectors.db`; without it the 300-char
`body_preview` is used and the report says the coverage number is a lower bound.

### 4. Bridge quality — optional, bounded LLM sample (`--llm`)

Asks whether a concept↔code link is *semantically* right, which no regex can. Bounded by
`--sample N` (default 30) drawn with a seeded RNG from a stable sort, stratified over the
symbol-occurrence tiers so failures cannot be missed; forced Bedrock Converse **tool use** with
`toolChoice` pinned, temperature 0, the reply validated against the tool schema locally and retried
once; a content-addressed cache (`sha256` of model + system + prompt + schema) makes re-runs free;
a hard `--max-cost` budget is checked before every call. Reported with a bootstrap 95% CI, because
30 samples cannot support a bare percentage. Verdicts are `correct` / `too_generic` (the symbol is
real but is not what the concept is about) / `incorrect`.

### 5. Regression gating

`graph_eval.json` carries a flat `thresholds` block plus a `threshold_doc` saying which direction
is better for each metric. Gate with repeatable `--fail-under NAME=VALUE` (minimum) and
`--fail-over NAME=VALUE` (maximum); either exits 1 on violation. An unknown metric name or a metric
that is `null` in this run **fails** rather than passing silently.

```bash
python3 eval/graph_eval.py --run "$DISTILL_OUTPUT" --source-root "$SRC" --key "$KEY" \
  --fail-under parser_confirmed_edge_precision_pct=95 \
  --fail-under bridge_symbol_occurrence_bare_pct=99 \
  --fail-under key_item_coverage_any_pct=95 \
  --fail-over  anchor_claims_contradicted=0 \
  --fail-over  dangling_edges=0 --fail-over same_as_contradictions=0 \
  --fail-over  contributes_to_cycles=0 --fail-over unified_id_mismatches=0
```

### Provenance: refusing to score the wrong source tree

Before anything else, every `sha256` in `<output>/.enumeration/source_inventory.json` is re-checked
against `--source-root`, and `run_manifest.run_identity.source_inventory_hash` is checked against
the inventory's own hash. A mismatch means the graph was built from a different revision, where
anchor and citation failures say nothing about the graph — so `graph_eval` **exits 3 and scores
nothing** unless given `--allow-revision-mismatch` (which scores anyway and records the override).
A run with no inventory is `unknown`: a warning, not a refusal.

### Reference numbers — current

Recorded on one reference run of the pipeline over `samples/ap-invoices` (outputs are not shipped;
re-run to reproduce). **Before** is the run before the code-graph / bridge / concept-key fixes,
**after** is the same corpus and key with them, scored by the same scorer. Your numbers will vary.

| Metric | Before | After |
|---|---|---|
| Parser-confirmed edge precision (typed, core relations) | 97.56% (40/41) | **100%** (60/60) |
| Parser edge recall (typed, core relations) | 66.67% (40/60) | **100%** (60/60) |
| … SQL files / **VB.NET files** | 97.56% / **0%** | **100% / 100%** |
| … counting only edges the PROMPT proposed | n/a | **100%** |
| `method_call` precision / recall (extended set) | not expressible | **100% / 100%** (53/53) |
| False positives | 1 | **0** |
| Bridge: the link's own assertion holds | n/a | **100%** (1224/1224) |
| Bridge: `substring_only` / `qualifier_dropped` | 7 / 62 | **0 / 0** |
| Bridge concept keys matching a node **verbatim** | 0% | **100%** |
| Structural hard failures (all self-declared; `anchor_claims_contradicted` 0 in both) | 2 | 3 |
| Key items with a concept at ≥1 location / all locations | 100% / 58.3% | 100% / 58.3% |

Code graph after the fix: 15 files / 25 objects / **162 edges** / 74 entities, 1,224 bridge links,
against the unchanged 1,258-node / 3,440-edge concept graph. The code-graph pass cost **$0.15** on
Haiku 4.5 for the 15-file corpus.

---

## CI / regression layer (`ci/`)

One command runs the whole suite and emits one machine-readable report.

| Path | What |
|---|---|
| `ci/run_suite.py` | Suite runner: provenance, budget enforcement, thresholds, baseline comparison, `suite.json` + `suite.md` |
| `ci/suite_lib.py` | Config loading and validation, corpus hashing, the evaluator registry, the budget tracker, the tolerance table |
| `ci/suites/ap-invoices.yaml`, `ci/suites/jc-changeorders.yaml` | The two shipped suites |
| `ci/baselines/*.suite.json` | Committed reference runs, usable directly as `--baseline` |
| `ci/tests/` | pytest cases with local evaluator doubles, no AWS calls (`test_suite_costs.py` covers estimates, caps, judge passes, plan mode and null handling) |

```bash
# every commit: deterministic layers only, no Bedrock calls, about 5 s
python3 eval/ci/run_suite.py --config eval/ci/suites/ap-invoices.yaml --smoke \
  --out eval/ci/runs/smoke

# what would a full run cost, and what cap would each stage get? No Bedrock calls.
python3 eval/ci/run_suite.py --config eval/ci/suites/jc-changeorders.yaml \
  --var run=output/jc-changeorders --plan --out /tmp/jc-plan

# full run against a pipeline output, hard budget cap, regression gate
python3 eval/ci/run_suite.py --config eval/ci/suites/ap-invoices.yaml \
  --var run=output/ap-invoices --budget-usd 3 --out eval/ci/runs/full \
  --baseline eval/ci/baselines/ap-smoke-2026-10-06.suite.json
```

- **Evaluators are black boxes.** `run_suite.py` imports none of them. It probes each one's
  `--help` at runtime, passes only the flags that tool advertises, and records every flag it
  had to drop. A tool that is absent, or that has dropped a flag the stage needs, becomes a
  stage status (`skipped` when the stage is `optional: true`, otherwise `error`) rather than a
  traceback. `eval/graph_eval.py` and `eval/judge/` are owned by other workstreams; the suites
  already contain a `graph-eval` stage that reports `skipped: evaluator not present` until it
  lands.
- **Budget: estimates.** Before an LLM stage starts, its cost is estimated from the
  evaluator's own plan. `score.py --dry-run-judge` and `benefits/* --dry-run` are run with
  the stage's real arguments and call nothing; judge-cache hits count as $0. When a tool
  cannot plan, the suite falls back to `suite_lib.CALIBRATED_COSTS`, a table of measured
  costs:
  - score.py auto segmentation: $0.97 per 100 KB of spec per pass (AP 438 KB at $3.36/pass,
    JC 556 KB at $5.39/pass).
  - score.py `requirements` segmentation: $0.37 per 100 KB per pass ($1.60/pass on AP).
  - Q&A: $0.20 per question. Impact: $0.22 per change. Field app: $0.69 per run.

  Per-pass figures are multiplied by `judge_runs`. A stage's `est_usd` is now only a floor.
  The shipped suites no longer contain hand-written LLM estimates: the old ones put the
  whole JC suite at $3.30, about 5 times too low.
- **Budget: caps.** If `spent + estimate > cap`, the stage is **not started**. It is
  recorded `budget_exceeded`, the remaining stages `not_run`, and the suite exits 3.
  Every LLM stage gets the remaining budget passed down as its own hard cap:
  `--max-judge-cost-usd` for score.py (it had no cap before), `--max-usd` for benefits/*.
  An evaluator that declares a cap flag but stops advertising it is refused (`error`)
  rather than run uncapped. One that exits 3 because its own cap tripped is recorded
  `budget_exceeded`.
- **Budget: logs.** Every stage runs with `EVAL_BEDROCK_LOG=<stage out>/bedrock_calls.jsonl`,
  and spend is read from those logs, plus any `tokens.jsonl` and, as a backstop, the
  per-machine `logs/local/bedrock_calls.jsonl`. Records are keyed so that concurrent calls
  from another session are never charged here. `logs/bedrock_calls.jsonl` (a frozen record, if
  present) is ignored.
- **Judge passes.** `judge_runs` is set at suite level and can be overridden per stage or with
  `--judge-runs N`. It is passed to score.py (and insights.py) as `--judge-runs`. **The CI
  suites pin it to 1**, while score.py's own default is 3, so the estimate, the cap and the
  spend all describe one pass. Use `--judge-runs 3` for a number you intend to report (see
  "How to report these honestly"). On the JC run of 2026-10-06, `--plan` estimates the full
  suite at **$15.10 for 1 pass** and **$38.87 for 3 passes**. The old suite estimated $3.30.
- **`--plan`.** Runs the deterministic stages, which are free and may be needed downstream.
  For each LLM stage it records the estimate, its source and the exact command, including the
  cap that would be passed down. Nothing is spent. If the plan does not fit the cap, the stages
  a real run would never reach are still priced (`not_run` with an estimate), and the suite
  exits 3.
- **Unmeasured is not zero.** Null metrics stay null in `suite.json`, and a threshold on a null
  metric is `unmeasured`, which fails the run. An evaluator that reports
  `measurement.status: not_measured` (for example, the budget ran out before grading) puts its
  stage in status `not_measured`, which fails the run. `partial` is shown in the stage table
  and the verdict, but fails only through thresholds. A benefit key that loads zero items makes
  the evaluator exit 2, so the stage is `failed`, not `ok`.
- **Insights artifact.** `benefits-insights` scores `${run}/issues/issues.md`, the Issues &
  Insights report, by default (suite var `insights_spec`; override with
  `--var insights_spec=PATH`). It used to score the business spec, which repeated the
  `score-bottled` judge cost.
- **Provenance.** `suite.json` pins repo commit/branch/dirty flag, model id, region (read from
  `bedrock_util.REGION`, i.e. where the calls actually go; the shell's `AWS_DEFAULT_REGION` is
  recorded separately as `shell_default_region` because it was being reported as the region),
  per-corpus
  content hash (answer keys excluded from the hash input, and listed), answer-key hash, each
  evaluator's file sha256 and `--help` fingerprint, the config hash, seeds, and timestamps.
- **`--smoke`** forces every stage onto its deterministic path, passes a $0 cap to every capped
  tool, and fails with exit 4 if a single call is logged (per-stage logs plus a per-run sentinel).
  `benefits-qa`, `-impact` and `-fieldapp` answer with an LLM (impact's `--no-llm` only drops
  its rule judge), so they are skipped in smoke. `suite.canonical.json` is the same report with wall-clock values and the output path
  normalised away, so two smoke runs are byte-identical.
- **Exit codes.** 0 pass (or, with `--plan`, the plan fits); 1 threshold violation, regression,
  a threshold whose metric is absent or null, or a stage that measured nothing; 2 config or
  infrastructure error; 3 budget exhausted (or, with `--plan`, would be); 4 an LLM call
  happened during `--smoke`.

## Mutation harness (`mutate/`) — detection power

Recall says how much of a *known* spec a pipeline reproduces. It says nothing about whether the
pipeline would **notice a defect nobody told it about**. `mutate/` measures that.

```bash
# inject 5 defects into a copy of the corpus (no LLM, about 1 s)
python3 eval/mutate/mutate.py --corpus synthetic-corpus/ap-invoices \
  --out /tmp/ap-mut --mutations 5 --seed 42
# -> /tmp/ap-mut/corpus/  (feed this to the pipeline)
#    /tmp/ap-mut/mutations.yaml      ground truth, in answer-key schema
#    /tmp/ap-mut/mutate_manifest.json  hashes, rejections, provenance

# then score whatever the pipeline produced for the mutated corpus
python3 eval/mutate/score_mutations.py --mutations /tmp/ap-mut/mutations.yaml \
  --output output/ap-mut/master_blend/master_blend.md --source-root /tmp/ap-mut/corpus \
  --control output/ap-invoices-demo/master_blend/master_blend.md \
  --base-key synthetic-corpus/ap-invoices/ANSWER_KEY.yaml --out /tmp/ap-mut/detect
```

| Category | Injected defect | Flag planted |
|---|---|---|
| `threshold_divergence` | A money threshold used in two layers is changed in one of them | `inconsistency` |
| `operator_relax` | `>=` becomes `>` in one layer only | `logic_flaw` |
| `client_check_removed` | A client-side validation guard is deleted, leaving the rule database-only | `db_only` |
| `dead_code_orphan` | An unreferenced copy of an existing function is added | `dead_code` |
| `rule_to_comment` | A guard is replaced by a comment stating the same rule | `comment_only` |
| `silent_cap` | A `Decimal` return value is silently clamped, with nothing documenting it | `undocumented_feature` |
| `date_off_by_one` | A SQL date boundary stops including the boundary date | `logic_flaw` |
| `constant_alias_rename` | A second constant with the same value is added and one use site switched to it | `inconsistency` |

- **Reproducible.** Same corpus, count, seed and category list give byte-identical `corpus/`
  and `mutations.yaml`. Non-deterministic provenance (timestamp, git commit) lives only in
  `mutate_manifest.json`. Sites are discovered by pattern, never by hard-coded line number, so
  the operators work on any VB.NET + T-SQL corpus; `--list-sites` prints the candidates.
- **Syntax-preserving, enforced.** Each mutation declares the structural delta it is allowed to
  cause (for example `dead_code_orphan` declares `+1 Function, +1 End Function`). After the
  edits, `mutate/sanity.py` recomputes the file's token profile; if the actual delta differs,
  or the file fails the absolute checks (string literals terminated, parentheses balanced,
  block terminators matched), the file is rolled back and the mutation is recorded as
  `not_syntax_preserving`. Both shipped corpora pass `sanity.py` before and after mutation.
- **Non-overlapping.** Two mutations may not touch the same line of the same file; the second
  is rejected with `reason: overlaps`. Rejection order is fixed by the seed.
- **Clear of the corpus's own traps (`--avoid-key KEY.yaml`).** A mutation whose edited lines
  or ground-truth locations overlap a seeded trap is rejected with `reason:
  overlaps_answer_key`. A seeded trap is a key item that carries `flags`; use
  `--avoid-key-scope all` to protect every key item. `--avoid-key-margin N` widens each trap
  location by N lines. The key's sha256 is recorded in the manifest.

  The JC suite runs `mutate-jc` with the JC key and a margin of 2. With seed 7 and no key,
  MUT-002 (threshold_divergence at `modJCGlobals.vb:70`) sat two lines from the seeded BR-009
  inconsistency, and the control run "detected" it before anything was injected (16.7%).
  `--avoid-key-scope all` is too strict for JC: its key covers almost every site, and only 5 of
  6 mutations apply.
- **Anchors are code symbols, not path segments.** The `detected_unlocated` route credits a
  flagged finding that names the mutated symbol and the file. Path components of the site id,
  such as `ClientSide` and `modJCGlobals`, used to count as the symbol. As a result, any flagged
  finding that merely named the file "detected" the mutation, which is how the JC control floor
  reached 16.7% even after the key-site collision was removed. Since that fix, the JC seed-7
  control floor is 0.0% with or without `--avoid-key`.
- **Ground truth in answer-key schema.** `mutations.yaml` uses
  `items[{id, type, statement, locations, evidence_class, flags}]`, so `score.py` consumes it
  directly as `--key`. Locations are coordinates in the *mutated* tree. The extra `mutation:`
  block (category, before/after text, detection vocabulary) is ignored by `score.py`.
- **Detection, not description.** A mutation counts as `detected` only when a finding both
  points at the site (citation overlap, `--line-slack 3`) **and** says something is wrong,
  matched against the category's fixed vocabulary. Describing the mutated behaviour correctly
  without flagging it is reported separately as `located_not_flagged`. Artifacts with no
  citations (the bottled spec strips them) can still be credited through the
  `detected_unlocated` route, which requires the finding to name the mutated symbol and file.
- **False positives.** `--control <output for the unmutated corpus>` reruns the same detector
  over a run that never saw the injection; anything "detected" there is a false positive and is
  subtracted in the report's `net` column. `defect_claims_off_site` counts findings that flag a
  defect somewhere no mutation was injected; `--base-key` subtracts the ones that land on the
  base corpus's own seeded traps, leaving `unexplained`.
- **C# (`--lang cs`, auto-detected for a tree with `.cs` and no `.vb` files).** The same eight
  categories with C#-aware sites (`mutate/mutations_cs.py`): a threshold used in two files
  (`HasMaxLength(256)`, `const int ImageMaximumBytes`) changed in one; `>=` -> `>`; a guard clause
  deleted where the same member is still guarded elsewhere (another guard, a DataAnnotation, EF
  config); an uncalled `<Name>Legacy` clone; a guard turned into a `//` comment; `Math.Min(.., 5000)`
  on a numeric return; a date / `.Count` / `.Take(n)` boundary moved by one; a `NAME_ALT` constant
  alias. Test projects, migrations and generated files are never mutated. `sanity.py` checks C#
  literal termination and paren / bracket / brace balance, and when the facts tool can run
  (`dotnet`, `DISTILL_FACTS_CMD` or `--roslyn-cmd`) each touched file is parsed by Roslyn before and
  after; a file with new syntax errors is rolled back (`roslyn_check` in the manifest).
- `eval/mutate/examples/ap-invoices-seed42/` holds the committed reference ground truth, the
  manifest with its hashes, and the unified diff of the five injected defects.

## Building an answer key for new code

Recall and precision only mean something against a key, and a real code base has none.
`keygen/` drafts one from the code itself, in the same schema, and a human reviews it. Details:
[`keygen/README.md`](keygen/README.md).

```bash
python3 eval/keygen/build_key.py --source /tmp/eshop --commit 4da8212 \
  --repo-url https://github.com/dotnet-architecture/eShopOnWeb --source-license MIT \
  --corpus eshoponweb --out eval/keys/eshoponweb/ANSWER_KEY.draft.yaml [--facts facts.json]
python3 eval/keygen/review.py --key eval/keys/eshoponweb/ANSWER_KEY.draft.yaml --source /tmp/eshop \
  --html /tmp/eshop-key-sheet.html --csv /tmp/eshop-key-sheet.csv
# review in the browser -> decisions.json; then
python3 eval/keygen/review.py --key eval/keys/eshoponweb/ANSWER_KEY.draft.yaml --apply decisions.json \
  --out eval/keys/eshoponweb/ANSWER_KEY.reviewed.yaml
python3 eval/keygen/review.py --key eval/keys/eshoponweb/ANSWER_KEY.reviewed.yaml \
  --filter-out eval/keys/eshoponweb/ANSWER_KEY.yaml      # what score.py should be given
```

1. **Harvest** (deterministic): guard clauses, `if .. throw`, custom exceptions, enums, constants,
   literal thresholds, DataAnnotations, FluentValidation, EF Core fluent config, authorization
   attributes, routes, MediatR handlers, Ardalis specifications, domain-entity invariants, and SQL
   CHECK / UNIQUE / FK / DEFAULT / triggers / procs / RAISERROR, for C#, VB.NET and T-SQL. Statements
   come from fixed templates; every item cites exact lines and is `Verified`.
2. **Tests**: every xUnit / NUnit / MSTest method becomes a "protected behaviour" item, citing the
   test and the production code it exercises.
3. **Docs** (`--docs`, optional): feature bullets from markdown, `Inferred`.
4. **Merge**: duplicates collapse with full provenance; an item two different sources agree on is
   flagged `corroborated`.
5. **AI draft** (`--llm-draft --max-usd N`, off by default): one bounded pass proposing rules the
   harvest missed; a proposal survives only with a verified file:line quote.
6. **Planted problems** (`--plant N --seed S`): C#-aware mutations from `mutate/`, merged as
   `status: planted` items.
7. **Review**: HTML sheet with accept / edit / reject, CSV, `--apply`, and `--filter-out` so that
   `score.py` scores against reviewed items only.

**Independence rule: the key builder never reads pipeline outputs.** Its only inputs are the
source tree at a pinned commit (code, tests, docs) and, optionally, the facts tool's parse of that
tree. A key derived from pipeline output would score the pipeline's own mistakes as correct.

### What the eShopOnWeb draft is, and is not

`keys/eshoponweb/ANSWER_KEY.draft.yaml`: **241 items, all `unreviewed`**, from the 244 `.cs` files
at `4da8212` that remain once the 10 EF migration files are excluded (45 of them are tests), with
the Roslyn facts index (the regex index gives the same 241). 205 harvested, 48
from tests, 12 of them corroborated by both. By type: 95 rule, 65 interface, 42 data, 20
exception, 14 permission, 4 workflow, 1 state. The README contributes nothing: every bullet sits
under a setup, deployment or eBook-TOC heading. `keys/eshoponweb/mutations-seed42.yaml` is the
ground truth for 8 C# mutations (one per category), byte-identical across runs; the mutated tree
passes `sanity.py` and the Roslyn parse check.

Treat it as a candidate list for a reviewer, not as a key. What a harvest + tests key covers
well: input validation, persistence constraints, guards, exceptions, authorization, routes, and
behaviour the authors chose to test. What it misses, by construction:

- **Rules expressed as ordinary code.** `OrderService.CreateOrderAsync` copies each line's name and
  picture from the current catalog item but keeps the unit price captured in the basket;
  checkout uses a hard-coded shipping address; JWTs expire after 7
  days; the basket cookie lives 10 years; enabling 2FA issues exactly 10 recovery codes. None of
  them is a guard, a constant or an attribute, so no detector fires.
- **Workflows across methods** (anonymous basket transferred at login, order created from the
  basket at checkout). The harvest sees the steps one at a time; only tests, where they exist,
  describe the sequence.
- **Why a rule exists**, and which of two inconsistent copies is right. Templates state what the
  code enforces, never intent.
- **Absent or external behaviour** (the `Unknown` evidence class): identity rules inside ASP.NET
  Identity, anything in configuration or infrastructure.
- **Noise it adds**: routes (43), framework constants and UI-only items (a toast countdown
  enum) are real facts but not business rules; a reviewer should reject many of them. Template
  wording is mechanical, and `entity_rule` branch statements need rewriting.

A trial `--llm-draft` pass (judge model, cap $2, not committed) cost $0.56 for 5 calls and kept 49
of 75 proposals, all with verified citations; the other 26 duplicated existing items. Seven of the
49 corroborated harvested items. The other 42 included most of the gaps above (hard-coded shipping
address, JWT expiry, cookie lifetime, recovery-code count, image-extension rule). That suggests the
AI pass is worth its cost for coverage, provided a reviewer goes through its items. It did not
propose the order-line pricing rule.

## What these numbers mean

Not every number in this harness has the same standing. Some are arithmetic; some are an
opinion from a language model. Reporting them as one list of percentages is the easiest way to
mislead a reader, so they are separated here.

### Exact — deterministic, reproducible to the digit

These come from parsing, file/line arithmetic and bookkeeping. Re-running them on the same
inputs gives the same answer every time; a change in one of them is always a real change.

| Metric | Where | What makes it exact |
|---|---|---|
| Citation validity | `score.py` → `citations.validity_pct` | The file either exists under the source root and contains the cited lines, or it does not. Measured spread over 3 repeated runs: **0.0 points.** |
| Findings without a citation, invalid-citation count | `score.py` → `citations.*` | Counted from the parse. Measured spread: **0.** |
| Source coverage / conservation | `run_distillation.sh` → `conservation_report.json` | Every source line is paged exactly once, or the run fails. |
| Parser-confirmed graph edges | `output/*/knowledge_graph/code_graph.json` (Roslyn + ScriptDOM) | Each edge is checked against the source by a real parser, not inferred by a model. |
| Mutation detection rate | `mutate/score_mutations.py` → `overall.*`, `by_category.*` | The defect, its category and its lines are known before the output is read; the detector is a fixed vocabulary and a line-overlap test. Rerunning on the same output gives the same number. |
| Mutation reproducibility and syntax preservation | `mutate/mutate_manifest.json` | sha256 of the mutated tree and of `mutations.yaml`; the structural profile check. |
| Token counts and cost | each tool's `<out>/bedrock_calls.jsonl`, `logs/local/bedrock_calls.jsonl`, `ci/.../suite.json` | Read from the API's own usage fields. Estimates (`plan.est_cost_usd`, suite `estimated_usd`) are NOT exact: they are prompt characters / 3.6 at list price |
| Corpus content hash, evaluator hashes, commit | `ci/.../suite.json` provenance | sha256. |

Caveat that applies even to the exact numbers: citation **accuracy** (does a citation overlap
the key's lines?) depends on the key's location lists, which are not exhaustive. A correct
citation to a different supporting line counts as a miss. Treat accuracy as a lower bound.

### Estimated by an LLM judge — report with a spread, never as a single number

Recall and precision *of meaning* require deciding whether a sentence expresses the same rule
as a key item. That decision is made by Claude Sonnet 4.6 at temperature 0, which is still not
deterministic. On the reference runs (not shipped), every pair of `score.json` files that
re-judged the same artifact with the same segmentation was compared — 6 independent pairs across
three small corpora:

| Metric | Pairs | Max abs difference | Mean abs difference |
|---|---|---|---|
| `recall.strict_full_pct` | 6 | **10.5 points** | 5.2 |
| `recall.partial_credit_pct` | 6 | 5.2 points | 2.6 |
| `recall.any_match_pct` | 4 | **0.0 points** | 0.0 |
| `recall.core_full_pct` | 1 | 7.1 points | 7.1 |
| `recall.core_any_pct` | 1 | 0.0 points | 0.0 |
| `precision.keyed_pct` | 6 | 3.2 points | 1.2 |
| `precision.correct_pct` | 6 | 2.1 points | 0.8 |
| `precision.incorrect` (count) | 6 | 4 findings | 1.3 |
| `precision.unsupported` (count) | 6 | 12 findings | 5.3 |
| `citations.accuracy_pct` | 6 | 1.7 points | 0.7 |

Read that table as follows.

- **Strict full-match recall is the noisiest headline in this repo.** One artifact scored 89.6, 83.3 and 93.8 on three judge runs: a 10.5-point spread with the
  spec and the key held fixed. A 5-point difference between two pipelines on this metric is not
  evidence of anything.
- **Any-match recall is stable.** It never moved across the pairs we have. It is the metric to
  lead with when comparing approaches.
- **Counts of `unsupported` findings move by up to 12.** They are useful for direction
  ("this artifact carries a lot of boilerplate"), not for ranking.
- The LLM judge also scores the benefit evaluations in `benefits/` (Q&A correctness,
  plain-language score, rule recall in impact analysis, field-app item verdicts). Those run on
  smaller item sets than the 48-item AP key, so their per-run spread is wider still, and the
  `benefits/README.md` fixture numbers are explicitly indicative only.
- **A null is "not measured", never 0.** Every benefit report carries
  `measurement.status` (`measured` / `partial` / `not_measured`). Judged metrics cover only
  graded items, and are null when nothing was graded. Quote a `partial` number together with
  its graded count (for example, "Q&A correct 72%, 18 of 25 graded"). Never quote a
  `not_measured` stage. The JC Q&A run that reported "0.0% correct" had graded nothing.
- **Spec-adapter answers see a selection of the spec.** `--adapter spec` sends the
  BM25-retrieved chunks most relevant to each question, up to `--max-spec-chars` (60,000 by
  default), and records which ones it sent. A wrong answer can therefore mean "the right
  chunk was not retrieved" as well as "the spec is wrong". Check `questions[].context.chunks`
  before blaming the spec.

`score.py --no-llm` replaces the judge with a lexical+citation matcher. That is deterministic
and free, which is why `--smoke` uses it, but it is **not a cheap version of the same number**:
it is far more permissive. On the committed demo run it reports 93.8% any-match recall for the
master blend, the bottled spec and the `benefits-insights` stage alike — three artifacts the
judge separates clearly. Use `--no-llm` to detect breakage (a parse failure, a lost artifact, a
citation regression), never to quote recall.

### How to report these honestly

1. **Run the judge at least three times and publish the median with the observed range.**
   Two runs tell you nothing about which one was the outlier. `score.py --reuse-judgement`
   re-scores from saved verdicts with no new calls, so repeats cost only the judge calls.
2. **Never put a single judged run in a headline.** Write "any-match recall 90% (median of 3,
   range 89–91)", not "90% recall".
3. **Lead with the exact metrics.** Citation validity, source coverage, parser-confirmed edges
   and mutation detection rate carry the claims that survive scrutiny. Judged recall is
   supporting evidence.
4. **Quote detection rate with its control.** A detection rate without the false-positive floor
   from `--control` is not interpretable: a tool that calls everything suspicious scores 100%.
5. **State the corpus and the key version.** Both synthetic corpora were written by the same
   people who wrote their answer keys, and the traps are signposted by comments. Numbers do not
   transfer to a real ERP module.
6. **Quote the provenance line.** Every `suite.json` carries the commit, model id, region,
   corpus hash and seeds. A number without those cannot be reproduced and should not be used.
7. **Differences inside the measured band are not findings.** The bands are in
   `ci/suite_lib.py` → `DEFAULT_TOLERANCES`, and `--baseline` applies them automatically; it
   prefers an evaluator's own confidence interval when one is reported.

### Not measured at all

- **Generation variance.** Every published comparison ran the extraction pipeline once. The
  pipeline is at temperature 0 but is not deterministic, and its spread is unknown. A rerun
  could move the numbers by more than the judge noise above.
- **Whether the spec is useful.** Recall against a key measures agreement with a key, not
  whether an engineer could rebuild the system from the spec. This sample ships no end-to-end
  behavioural check (rebuild from the spec, then replay identical inputs against both systems).
- **Absent code.** The `Unknown` evidence class (code the corpus does not contain) is mostly
  missed by every approach tried so far.
