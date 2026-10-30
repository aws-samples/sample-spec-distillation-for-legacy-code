<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->
# `runtimes/facts` — the deterministic parser layer

Everything here runs **without an LLM**. It parses the enumerated source with real compiler
front-ends and emits structural facts that carry `file` plus `start`/`end` (1-based, inclusive) or
`line`, so every downstream claim can be cited back to a source line.

Two things consume it:

1. **The map prompt.** `facts_block.py` renders a bounded per-page block that `run_distillation.sh`
   appends to each page, so the extraction model sees *who calls whom, which procedures and tables
   are read or written, and what the constants are* rather than having to infer it.
2. **Ground truth for scoring.** `facts_post.py` writes `graph.json`, which `eval/graph_eval.py`
   scores the LLM-built code graph against (`eval/graph/parity.py`), and which
   `runtimes/mcp-server/code_graph.py` uses to corroborate and backfill its own edges.

```
build_facts.py                 Phase 1.2 driver: stage source -> run the tool -> enrich -> page blocks
structure/Program.cs           the tool: Roslyn (VB.NET, C#) + ScriptDom (T-SQL) -> raw facts.json
structure/LeanStructure.csproj .NET 8, Microsoft.CodeAnalysis.{VisualBasic,CSharp} + TransactSql.ScriptDom
structure/Dockerfile           build/run the tool without a local .NET SDK
facts_post.py                  layer 1b: units, comparisons, cross-layer + duplicate candidates, graph.json
facts_block.py                 the bounded per-page "Structural facts" block
identifier_evidence.py         Phase 1.3 (pure Python, no dotnet): abbreviated column/variable names
                               + evidence of their meaning (comments, UI labels, definitions, where
                               and how the value is set/tested) -> bounded per-page block appended
                               to the map prompt ("expand only when supported, else Unknown")
tests/                         pytest coverage; fixtures/cs-sample is the C# fixture corpus
```

## Language support

| Language | Extensions | Front end | Facts section | What is extracted |
|---|---|---|---|---|
| **T-SQL** | `.sql` | `Microsoft.SqlServer.TransactSql.ScriptDom` 161 (`TSql160Parser`) | `sql` | procedures / functions / views / triggers / tables, params, table reads & writes, column reads & writes with the owning table resolved, `EXEC` calls, `RAISERROR`/`THROW`, return codes, `CHECK` constraints, seed rows, comparisons, numeric literals, expression fingerprints, comments |
| **VB.NET** | `.vb` | `Microsoft.CodeAnalysis.VisualBasic` 4.8 (syntax only) | `vb` | types (incl. WinForms detection), methods & properties, fields, constants, method→method calls (receiver-type resolved), stored-procedure references, inline SQL (literals + `&=` fragments, parsed with ScriptDom), literals in a decision context, constant uses, comparisons with operands split, expression fingerprints, DB column references via row indexers, local aliases, attributes, enums, external-I/O calls, comments |
| **C#** | `.cs` | `Microsoft.CodeAnalysis.CSharp` 4.8 (syntax only) | `cs` | **the same facts, same keys as VB** — classes/structs/records/interfaces, methods, constructors, local functions, properties, fields, `const` **and `static readonly`** thresholds, method→method calls, stored-procedure references (`SqlCommand("usp_X")`, `CommandType.StoredProcedure`, proc-name literals), inline SQL (literals, `+` concatenation, interpolated and raw strings, `+=` fragments), literals in a decision context, constant uses, comparisons plus `switch`/switch-expression arms, expression fingerprints, column references via `row["Col"]` / `AddWithValue("@Col")`, local aliases, attributes, enums, external-I/O calls, `//` `/* */` and `///` comments |
| anything else | — | not parsed | `files` only | the file is still inventoried (with its line count) and its quoted strings feed the shared-literal detector, so an unparsed language is **declared, never silently dropped** |

Notes on the C# rig:

- It is a **parallel set of passes**, not a fold into the VB ones: Roslyn's VB and C# syntax trees
  share no node types. Everything *after* the parse — `SqlCollector`, the comment-block builder,
  the fact dictionaries — is shared, so there is one definition of each fact.
- `facts_post.clr()` merges the `vb` and `cs` sections into one language-neutral view and
  `unit_prefix()` derives a unit's id prefix from its file, so a C# method is `cs:Class.Method`
  next to VB's `vb:Class.Method`. Consequence: **duplicate-logic and cross-layer comparison
  candidates span VB, C# and T-SQL with no per-language branches.**
- `const` and `static readonly` are both treated as constants, because both state a fixed
  threshold. C# literal suffixes (`5000m`, `5000u`) normalise to the same value as `5000`, which is
  what lets a C# threshold match a T-SQL one.
- A corpus with no `.cs` files produces the same output as before the C# rig existed: the merge is
  a no-op and `cs` is an empty section.

Known limitation (pre-existing, applies to VB too): `facts_post.concept_keys()` keys a comparison
against a *named constant* on the constant's identity and a comparison against a *literal* on its
value, so `grossAmt >= ApprovalThreshold` (C#) and `@GrossAmt > 5000` (T-SQL) are not grouped even
though `ApprovalThreshold` is 5000. The shared value is still reported, in
`derived.shared_numeric_values`.

## Running it

In the pipeline (Phase 1.2) `run_distillation.sh` calls:

```bash
python3 runtimes/facts/build_facts.py build \
  --source-root "$SRC" --manifest "$OUT/.enumeration/plated_manifest.txt" \
  --pages "$OUT/.pages" --out "$OUT/facts" --page-budget "$DISTILL_PAGE_TOKENS"
```

Facts are an **enhancement, never a gate**: if `dotnet` is missing or the tool fails,
`facts_status.json` records `{"status": "skipped", "reason": ...}`, nothing is written under
`pages/`, and the exit code is still 0 so the run continues without facts.

Standalone, with a local .NET 8 SDK:

```bash
cd runtimes/facts/structure && dotnet publish -c Release -o out
dotnet out/LeanStructure.dll <source-root> > facts.json     # raw
python3 runtimes/facts/facts_post.py --run <dir-with-facts.json-and-source/>   # enrich + graph.json
```

Without a local SDK, build and run it in Docker. The corpus goes in as a tar stream on stdin rather
than a bind mount, so this also works where Docker does not share every host path:

```bash
docker build -t specdistill-lean-structure runtimes/facts/structure
tar -C <corpus> -cf - . | docker run --rm -i specdistill-lean-structure > facts.json
```


Environment:

| Variable | Meaning |
|---|---|
| `DISTILL_FACTS` | `0` disables Phase 1.2 entirely (`run_distillation.sh`) |
| `DISTILL_FACTS_TOOL` | path to a published `LeanStructure.dll` (default `structure/out/LeanStructure.dll`) |
| `DISTILL_FACTS_CMD` | full command taking the source root as its last argument and printing raw facts JSON (overrides `dotnet`; used by the tests) |
| `DISTILL_FACTS_BUILD` | `1` (default) = `dotnet publish` the tool when the dll is missing |
| `DISTILL_FACTS_MAX_FRACTION` | share of the page budget a facts block may use (default `0.15`) |

## Tests

```bash
python3 -m pytest -q runtimes/facts/tests
```

No AWS, no network, and no `dotnet` needed: the structure tool is replaced by a stand-in that
prints a recorded `facts.json` (`tests/fixtures/ap_invoices_facts_raw.json.gz`). Everything else is
real — paging, the conservation check, `facts_post` enrichment, block rendering, the map-prompt
composition and `run_distillation.sh`.

The C# fixture corpus lives in `tests/fixtures/cs-sample/` (synthetic; a class, a method calling
another, a `SqlCommand` carrying a proc name, a `const` threshold used in a comparison, inline SQL
over two tables, a column read through a row indexer, and a rule stated only in a comment). The
tests cover it two ways: `test_cs_facts.py` asserts on a recorded run of the real tool
(`tests/fixtures/cs_sample_facts_raw.json.gz`), and the same test re-runs the real `dotnet` tool
when one is on `PATH` so the recording cannot drift from the parser unnoticed.
