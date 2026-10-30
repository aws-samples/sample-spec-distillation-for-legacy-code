<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->
# eval/lean: lean spec-distillation pipeline, v1 (layers 1-5 + query tools)

This pipeline sits between `eval/baseline.py` (one LLM call per file, no cross-file context) and the
heavier repo pipeline. It combines cheap deterministic structure with a small number of LLM calls:
one per source file, one system-level call, and one consolidation call. The main `spec.md` keeps the
baseline's table layout, so `eval/score.py` scores it the same way.

It is shaped around five goals:

| Goal | What serves it |
|---|---|
| 1. Specs that improve modernisation | layer 1b candidates (duplicate logic, cross-layer comparisons, db checks without a client counterpart) and the layer 5 sections *Duplicated logic*, *Inconsistencies*, *Possible logic flaws*, *Undocumented behaviours* (`modernization.md` holds the recommendations) |
| 2. Onboarding | layer 3 *System overview* (architecture, persistence, lifecycles, interfaces), `glossary.md`, `ask.py` |
| 3. Less regression testing | `graph.json` and `impact.py`: a change maps to affected code, rules to re-test, reports and contracts |
| 4. Spec-driven new apps that must stay compatible | layer 1b `contracts` and the layer 5 *External contract* section (proc signatures, return codes, result sets, code values, report parameters, payloads) |
| 5. Business-language questions | `ask.py` (retrieval over the spec and the glossary, plus cited snippets on demand) |

## Layers

| Layer | What | LLM? |
|---|---|---|
| 1. Structure | `structure/` is a .NET 8 console tool that runs in Docker. It parses VB.NET with Roslyn (`Microsoft.CodeAnalysis.VisualBasic`, syntax only). It parses T-SQL with ScriptDom (`TSql160Parser`), covering the `.sql` files and also the SQL text inside VB string literals. Output: `facts.json`. | no |
| 1b. Derived candidates | `facts_post.py` post-processes `facts.json` into `facts["v1"]` and writes `graph.json`. | no |
| 2. Per-file extraction | `lean.py` makes one Bedrock call per source file. Each call carries the line-numbered file, a neighbour slice of the facts, the pinned constant definitions and the layer 1b candidates that touch the file. | 1 call/file |
| 3. System pass | One call over a compact summary of the facts (files, types, call flow, procs, triggers, tables, code values, status writes and their guards, interfaces, header comments). It produces *Architecture*, *Persistence*, *Lifecycles*, *External Interfaces* and a glossary. | 1 call |
| 5. Consolidation | One call over every layer 2 finding (id, file, statement, citations, flags, and whether a comment documents it) plus the layer 1b candidates. It returns merges (exact duplicates only) and the five cross-cutting sections. Python applies the merges and assembles the spec. | 1 call |

(Layer 4, a rebuild from the spec, is not part of this pipeline or this sample.)

### facts.json (layer 1)

Every fact has `file` plus either `start`/`end` or `line` (1-based). Everything from v0 is still here:

- **VB**: `types`, `methods`, `members`, `calls`, `proc_refs`, `inline_sql`, `constants`, `literals`, `const_uses`.
- **SQL**: `procs` (also functions and views, with `kind`), `triggers`, `tables`, `seed_rows`, `scripts`, `numeric_literals`.
- `comments` and `derived` (`proc_index`, `table_index`, `cross_layer_name_matches`, `shared_numeric_values`, `comment_mentions`, `commented_out_code`, ...).

New in v1 (schema `lean-facts/v1`):

- **SQL units** gain:
  - `columns`: column references with table resolved through aliases, `inserted`/`deleted` mapped to the trigger table, and unqualified columns resolved per line against the DDL. Each has mode `read` or `write` (SET or INSERT lists).
  - `var_from_column` (`SELECT @x = Col`), `string_literals` and `result_sets` (top-level SELECTs that return rows).
  - `fingerprints`: token fingerprints of IF/WHERE/EXISTS/IN predicates and of UPDATE/INSERT statements. Aliases and schemas are dropped. `k1` keeps variable names and `k2` abstracts them.
  - `IN (...)` predicates as comparisons. Every comparison now has its `left`/`right` operand text.
- **VB** gains:
  - `comparisons`: every comparison, plus each `Select Case` clause as a comparison against the selector. Operands are split into identifiers, string-indexer names (`row("Col")`) and the literal or constant value.
  - `fingerprints`: normalised If/Return/assignment/initialiser/Select Case/method-body fingerprints. Constants are replaced by their values. `k2` abstracts locals.
  - `column_refs`: string literals that name a DB column or a proc parameter.
  - `local_aliases`: `Dim x = f("Col", 5000D)` gives `x ~ Col`, default 5000.
  - `unit_literals`, `attributes`, `enums`.
  - `io_calls`: HTTP, JSON/XML serialisation, file, mail, config, process, messaging and report APIs, plus URL, JSON or file-name literals.

### facts["v1"] and graph.json (layer 1b, `facts_post.py`)

- `comparisons`: VB and SQL comparisons and CHECK constraints, normalised to (subject ids, operator, reference ids, value). The literal or threshold always sits on the right. Ids are normalised identifiers, with Hungarian prefixes stripped and expanded through local, variable and column aliases.
- `cross_layer_candidates`: relational comparisons on the same subject and threshold (shared reference id or equal literal) in different units, where the boundary (`>=`/`<` vs `>`/`<=`) or the threshold value differs.
- `duplicate_candidates`:
  - `same-expression` / `same-shape`: equal fingerprints in different units.
  - `same-message`: the same message text of 15 or more characters in several files.
  - `shared-literals`: literal clusters (Jaccard ≥ 0.2, at least 3 shared salient values; column names and type sizes ignored), across VB methods, SQL units and report-definition files.
  - `same-threshold-check`: equal relational checks in several units.
- `status_domains`: code columns and every value seen, with where each was seen. Values come from CHECKs, literal SETs, seed rows, comparisons, and VB constants whose group comment names `Table.Column`.
- `contracts`: everything other layers or systems depend on.
  - Procs: signature, return codes with their messages, RAISERRORs, result sets, callers, and an `entry_point` flag.
  - VB-to-proc `param_bindings`: parameters sent versus declared, flagging missing ones that have no default and undeclared ones.
  - Reports (`{?Param}`, `{Command.Field}`, data-source file), table schemas, `io_calls`, serialisation attributes, enums, DTO-like classes and the public API of non-form classes.
- `db_checks`: each DB comparison with its client-side counterparts (same subject and the same value or reference). Key joins and control-flow variables are skipped. An empty list is a `db-only` candidate.
- `concept_index`: identifier -> every comparison that uses it.
- `graph.json`: nodes are files, VB types and methods, constants, procs, functions and views, triggers, scripts, report files, tables, columns, UI events and concepts. Edges:
  - `calls`, `exec`, `reads`, `writes`, `reads_col`, `writes_col` (`ambiguous` when the column name exists in several tables)
  - `fires` (table -> trigger), `uses_const`, `compares` (unit -> concept), `mirrors` (VB constant -> SQL column or parameter of the same name)
  - `handled_by` (UI event -> handler), `contains`, `member`, `has_column`, `defines`, `comment_mentions`, `report_source`

### Layer 2 context (per file)

The v0 neighbour slice is unchanged: callers, callees, procs reached directly or through a DAL wrapper, tables with their DDL, triggers and other writers, constants, cross-layer name/value matches, comment mentions, and snippets within a 320-line budget. v1 adds:

- **Pinned constant definitions.** Every constant the file uses is shown with its value and group comment, outside the snippet budget. This fixes the v0 bug where definitions were dropped.
- **Layer 1b candidates that touch the file.** These are the same concept compared differently, repeated logic, DB checks with their client-side counterpart (or `NONE`), client checks with their DB counterpart, code values, contracts, parameter bindings, I/O calls, attributes, enums and reports.
- **Prompt rules.**
  - Every citation must start with the file name including its extension.
  - `db-only` is allowed only when the layer 1b list shows no client counterpart.
  - New flags: `duplicated-logic`, `undocumented`, `logic-flaw` and `contract`.
- **Post-processing.**
  - All citations are canonicalised to `name.ext:Lx-Ly`. A stem, path or bare `Lx` is resolved and clamped to the file length.
  - `verify_db_only` removes a `db-only` flag when a cited DB comparison has a client counterpart, or when the same message text is raised client-side, and records that it did so in the flag cell.

## Outputs (`--out`)

- `spec.md` (scored). It has three parts:
  - `# System overview` (layer 3).
  - `# Cross-cutting findings` (layer 5): *Duplicated logic*, *Inconsistencies*, *Possible logic flaws*, *Undocumented behaviours*, *External contract*.
  - One `# File: <rel>` section per file, with the deduplicated layer 2 findings.

  Every finding is a table row `| ID | Statement | Citations | Evidence | Flags |`. Evidence is one of Verified / Inferred / Unknown / Confirmation Required.
- `findings.json`: all findings in structured form (global id, section, file, citations, evidence, flags, origin, `merged_from`, `based_on`, `modernization`). It is the input to `ask.py` and `impact.py`.
- `glossary.md`: business term -> meaning -> code identifiers -> citations.
- `modernization.md`: one recommendation per cross-cutting finding. It is kept out of `spec.md` because recommendations are not checkable claims.
- `facts.json`, `graph.json`, `tokens.jsonl`, `generation_usage.json`, `run.log`.
- `contexts/`: the exact prompts and raw answers. `<file>.md` / `<file>.out.md` for layer 2, `_system.*` for layer 3, `_consolidate.*` for layer 5.
- `source/`: the staged corpus copy. It is also the scoring `--source-root`.

## Usage

```bash
cd eval
# full run: stage only source files -> docker layer 1 -> layer 1b -> Bedrock layers 2, 3, 5
python3 lean/lean.py --corpus ../synthetic-corpus/ap-invoices --out runs/lean-ap \
  --include '*.vb' --include '*.sql' --include '*.txt'
# contexts only, no Bedrock (also writes contexts/_system.md)
python3 lean/lean.py --corpus ../synthetic-corpus/ap-invoices --out runs/lean-ap --include '*.vb' \
  --include '*.sql' --include '*.txt' --dry-run
# resume after a failure without paying again for answers already in contexts/
python3 lean/lean.py --corpus ... --out runs/lean-ap --skip-structure --reuse
# layer 1 + 1b by hand (the corpus goes in as a tar stream on stdin, never a bind mount)
docker build -t specdistill-lean-structure lean/structure
tar -C runs/lean-ap/source -cf - . | docker run --rm -i specdistill-lean-structure > runs/lean-ap/facts.json
python3 lean/facts_post.py --run runs/lean-ap
# score (same flags as the v0 rescore)
python3 score.py --spec runs/lean-ap/spec.md --key <key.yaml> --source-root runs/lean-ap/source \
  --out-dir runs/lean-ap --gen-log runs/lean-ap/tokens.jsonl \
  --max-source-chars 200000 --exclude ANSWER_KEY.yaml --exclude README.md

# questions (business or technical); --snippets puts cited source lines in the first prompt
python3 lean/ask.py --run runs/lean-ap "Can a project manager approve a 30,000 dollar invoice?"
# change impact: LLM maps the change to graph seeds, then a deterministic walk (or pass --seed ids)
python3 lean/impact.py --run runs/lean-ap "Raise the approval threshold to 15,000"
python3 lean/impact.py --run runs/lean-ap "x" --seed col:APInvHdr.Status --json
```

### ask.py

1. Retrieval: BM25 over `findings.json`, with business words expanded through `glossary.md`, plus `graph.json` summaries of the units that the top findings cite and of the nodes whose names match the question.
2. One LLM call answers from that material. It may ask once for specific cited line ranges, which are then sent as numbered snippets (at most 6 ranges, 40 lines each). The corpus is never dumped.
3. Output: `answer`, `citations` (checked against the files; citations outside the retrieved material are listed as unsupported), `evidence` (Verified / Inferred / Unknown / Confirmation Required), `cannot_determine`, and the finding ids used.
4. Business-phrased questions get a plain-language answer.

### impact.py

1. **Seeds.** One LLM call picks seed node ids from a shortlist: retrieval hits plus every constant, table, proc, trigger, method, script and concept. `--seed` skips the call.
2. **Walk (deterministic).**
   - Upstream through `calls`, `exec`, `uses_const`, `reads`/`writes` (table and column), `handled_by` and `defines`, up to the UI events.
   - Side effects through `fires` and `mirrors`.
   - Data consumers of what changed units write (one hop).
   - Same-concept checks: every other unit that compares an identifier the seeds define or compare.
3. **Mapping.** Affected units are mapped to spec findings (rules to re-test, by tier), reports and contracts (procs, parameter bindings, tables, code values, I/O).

Queries log tokens to `<run>/query_tokens.jsonl`, separate from generation.

Requirements: Docker, Python 3 with boto3, and Bedrock access through `eval/bedrock_util.py`. No Python packages are installed. The generation run stops before a call once 80% of `--budget` (default $5) is spent.

## v1 on the AP corpus (one reference run, one judge run; outputs not shipped)

- **Generation:** 18 calls, $2.08. The judge run cost $2.91.
- **Spec size:** 680 findings. That is 561 per-file rows after layer 5 dropped 50 duplicates, plus 69 system rows and 50 cross-cutting rows. Layer 1b removed 16 `db-only` flags.
- **Recall:** full 91.7%, any 95.8%.
- **Precision:** keyed 77.1%, correct 99.4%. 3 findings were judged incorrect and 3 unsupported.
- **Citations:** validity 100%, accuracy 77.8%.

## Known limits (v1)

- Roslyn runs syntax-only, so calls into framework types go unresolved and so do ambiguous method names.
- Concept matching is lexical. It works on normalised identifiers, aliases from `Dim x = f("Col")` and `SELECT @x = Col`, and VB-constant-to-column name matches. Thresholds with unrelated names (`lim` vs `@MaxAmt`) are not linked; the LLM layers have to spot those.
- Unqualified column references in multi-table statements are resolved to the nearest table that has the column. Ambiguous ones are marked `ambiguous` in the graph. VB row-indexer column references (`row("Status")`) link to every table with that column name.
- Literal clusters and same-message candidates are hints. Status codes shared by unrelated units can produce noise. Layer 2/5 are told to confirm against the code.
- Layer 5 sees findings in compact form (statement cut at 280 characters, first 4 citations) and returns JSON. When its answer is not valid JSON, the spec is assembled without merges or cross-cutting sections and a warning is printed.
- The `db-only` check only covers DB checks that are comparisons. CHECK constraints and RAISERROR-only paths keep the model's flag.
- `ask.py` retrieval is lexical with glossary expansion. Questions that use words found in neither the spec nor the glossary can miss relevant findings. In that case the answer should come back as `cannot_determine`.
