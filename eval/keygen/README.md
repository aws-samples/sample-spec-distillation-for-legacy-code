<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->
# `eval/keygen` — build an answer key for code that has none

Every score in `eval/` is agreement with an answer key. The synthetic corpora came with keys
written by the people who wrote the code. A real code base has no key, so this tool drafts one
from the code itself, and a human then reviews it. The output uses the same schema as
`synthetic-corpus/*/ANSWER_KEY.yaml`, so `eval/score.py`, `eval/mutate` and the CI suites read it
unchanged.

```
build_key.py   CLI: harvest -> tests -> docs -> merge -> [AI draft] -> [planted problems] -> key.yaml
kg_source.py   source tree model: blanked code, structure index (facts or regex), candidates
kg_harvest.py  1. HARVEST detectors (deterministic, no AI)
kg_testmine.py 2. MINE TESTS: one "protected behaviour" per xUnit/NUnit/MSTest method
kg_docs.py     3. DOCS (optional): markdown feature bullets
kg_merge.py    4. MERGE / DEDUPE with provenance and the `corroborated` flag
kg_llm.py      5. AI DRAFT (optional, off by default, cost-capped, citations verified)
               6. PLANTED PROBLEMS: eval/mutate (C# operators in mutate/mutations_cs.py)
review.py      7. REVIEW: HTML sheet + CSV, apply decisions, filter reviewed items for score.py
tests/         pytest, no AWS, no dotnet; fixtures/mini-{cs,vb,sql} are synthetic
```

## Quick start

```bash
git clone https://github.com/dotnet-architecture/eShopOnWeb /tmp/eshop
git -C /tmp/eshop checkout 4da8212

# draft key (deterministic, ~1 s; --facts is optional, see "Structure index")
python3 eval/keygen/build_key.py --source /tmp/eshop --commit 4da8212 \
  --repo-url https://github.com/dotnet-architecture/eShopOnWeb --source-license MIT \
  --corpus eshoponweb --out eval/keys/eshoponweb/ANSWER_KEY.draft.yaml \
  [--facts /tmp/eshop-facts.json] [--docs README.md] [--date 2026-10-08]

# review sheet + CSV
python3 eval/keygen/review.py --key eval/keys/eshoponweb/ANSWER_KEY.draft.yaml --source /tmp/eshop \
  --html eval/keys/eshoponweb/ANSWER_KEY.draft.review.html \
  --csv  eval/keys/eshoponweb/ANSWER_KEY.draft.review.csv

# open the HTML, accept / edit / reject, "Export decisions" -> decisions.json, then
python3 eval/keygen/review.py --key eval/keys/eshoponweb/ANSWER_KEY.draft.yaml \
  --apply decisions.json --out eval/keys/eshoponweb/ANSWER_KEY.reviewed.yaml
python3 eval/keygen/review.py --key eval/keys/eshoponweb/ANSWER_KEY.reviewed.yaml \
  --filter-out eval/keys/eshoponweb/ANSWER_KEY.yaml          # accepted + edited (+ planted)
python3 eval/score.py --spec SPEC.md --key eval/keys/eshoponweb/ANSWER_KEY.yaml --source-root /tmp/eshop
```

A filled-in CSV works as `--apply` input too (`decision` = accept / edit / reject, plus
`edited_statement`, `edited_type`, `note`).

## The rule that keeps the key independent

**The key builder never reads pipeline outputs.** Its inputs are the source tree at a pinned
commit, its tests, optionally its docs, and optionally the facts tool's parse of that tree. It
reads no spec, page summary, knowledge graph or anything else `run_distillation.sh` produced. A
key built from pipeline output would share the pipeline's mistakes and score them as correct.
The AI draft follows the same rule, and uses a prompt that audits the key for gaps rather than
the pipeline's "describe this code" extraction prompts.

## What each stage produces

Every item has the answer-key fields (`id, type, statement, locations, evidence_class, flags`)
plus `status`, `source` (every contributor), `provenance` (each contributor's own statement and
locations), and a one-line `snippet`. Ids are stable hashes (`BR-4cc172`), so review decisions
survive a rebuild. Types: `rule | data | state | workflow | exception | interface | permission`.

| Stage | `source` | `evidence_class` | What becomes an item |
|---|---|---|---|
| 1. Harvest | `harvest:<detector>` | Verified | `guard_ardalis` (Guard.Against.*, incl. custom guard extensions), `if_throw` (`if (..) throw new X`, `?? throw`, VB `If .. Then Throw New`), `exception_type` (custom exceptions + where thrown, handled, mapped to HTTP), `enum`, `constant` (const / static readonly / VB Const), `literal_threshold`, `data_annotation`, `fluent_validation`, `ef_config` (IsRequired, HasMaxLength, HasPrecision, HasColumnType, HasConversion, HasDefaultValue, HasIndex().IsUnique(), HasForeignKey, OwnsOne), `authorization` ([Authorize(Roles, Policy)], [AllowAnonymous] overrides, RequireRole, IsInRole, AddPolicy), `route` (MVC attribute routes, minimal API Map*, Razor Pages handlers), `mediatr`, `specification` (Ardalis.Specification queries), `entity_rule` (computed members, branches, accumulations, RemoveAll in domain entities), `sql_check/unique/fk/default/trigger/proc/raise` |
| 2. Tests | `tests` | Verified | each `[Fact] [Theory] [Test] [TestCase] [TestMethod]` method: test name + key assertions + the production methods it exercises |
| 3. Docs | `docs` | Inferred | markdown bullets under feature-like headings (install/setup/licence/TOC sections skipped) |
| 4. Merge | all | strongest | duplicates collapse; 2+ different sources set `corroborated` |
| 5. AI draft | `llm-draft` | Inferred | rules the harvest missed, each with a verified file:line quote |
| 6. Planted | `planted:<category>` | per mutation | `--plant N --seed S`: mutation items, `status: planted` |

Statements come from fixed templates, for example
`Basket item quantity must not be negative (0 or more) (Guard.Against.OutOfRange in
BasketItem.AddQuantity, BasketItem.SetQuantity).` They say what the code enforces and where; they
never guess why. Weak templates (`entity_rule` branches) are marked `template: weak` so the
reviewer knows to rewrite them.

### Structure index

Detectors need to know which class and method a line belongs to, and test mining needs to
resolve calls to definitions. With `--facts <facts.json>` (raw output of
`runtimes/facts/structure`, Roslyn + ScriptDom) the index uses the parser's exact type and method
spans and its call graph. Without it, a brace/keyword scanner over comment- and string-blanked
code builds the same index. On eShopOnWeb both paths give the same 241 items; only the
"exercises ..." list of two mocked-repository tests differs (the facts call graph resolves
fewer of their calls). The facts tool needs the .NET 8 SDK, locally or in Docker; see `runtimes/facts/README.md`.

### Test mining and corroboration

A test candidate joins **one** harvest item: the one whose location lies inside the code the
test reaches (the test, the methods it calls, two call hops further, and the properties it
reads) and whose statement shares the test's behaviour words (test name minus test-class name,
stemmed). Example: `CantAddItemWithNegativeQuantity` corroborates the quantity guard in
`BasketItem.AddQuantity`, reached through `Basket.AddItem`. Mock and assertion calls
(`Received`, `Returns`, `Assert.*`, ...) and framework types never count as the system under test.

### AI draft (`--llm-draft --max-usd N`)

Off by default. One pass over the non-test source files in a fixed order, in chunks of about
60 KB, each prompt listing the items already in the key. Before every call the worst-case cost
(prompt chars / 2.5 tokens at the input price plus the full output budget at the output price)
is checked against the remaining cap; `--llm-max-calls` bounds the pass as well. The model is
the eval judge model (`bedrock_util.MODEL_ID`) unless `--llm-model` overrides it. A proposal is
dropped unless its file exists, its line range is inside the file and its quote occurs in those
lines; one that restates an existing item is dropped too. Calls are logged next to the key in
`<key>.llm_calls.jsonl`. The report (calls, spend, kept, drop reasons) is recorded in
`metadata.inputs.llm_draft`.

### Planted problems (`--plant N --seed S`)

Runs `eval/mutate/mutate.py` (C# operators for a C# tree) into `--plant-out`, harvests the
*mutated* tree (so every location is a mutated-tree coordinate), and appends the mutation items
(`status: planted`, trap flags such as `inconsistency`, `logic_flaw`, `comment_only`, `db_only`,
`dead_code`, `undocumented_feature`). `--mutations-out` copies `mutations.yaml`. Same seed gives
byte-identical output. When the facts tool can run (`dotnet` on PATH, `DISTILL_FACTS_CMD`, or
`--roslyn-cmd`), every touched C# file is also parsed by Roslyn before and after mutation and
rolled back if it gained syntax errors.

## Metadata header

`metadata:` records `repo_url`, `commit`, `source_license`, `builder {name, version}`,
`generated`, `reviewer`, `reviewed`, the independence rule, the inputs (structure index, facts
sha256, test glob, docs globs, file counts, candidate counts, AI-draft report, planted report)
and `counts` by type, status, source, detector and evidence class, plus the number of
corroborated items. `--date` fixes `generated`, which makes the whole file byte-identical for
the same inputs.

## Third-party code

The key cites the source tree by URL and commit; the source itself is never committed. Items
carry one-line `snippet` quotes and the HTML sheet shows the cited lines only (no context, at
most 8 lines from at most 2 locations per item), with the source licence in the header.

## Tests

```bash
python3 -m pytest -q eval/keygen eval/mutate/tests eval/tests
```

No AWS calls (the AI draft is tested with an injected call function) and no `dotnet` (the Roslyn
check is tested with a stand-in tool). `tests/fixtures/` holds minimal synthetic C#, VB.NET and
T-SQL corpora.
