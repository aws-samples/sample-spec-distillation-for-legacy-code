# Output Readability Convention

Every still output file that contains scored entries MUST include a "How to Read This Document" section near the top. This section serves two audiences:

1. **A business stakeholder** who needs to understand what the document IS and why they'd care
2. **A technical reader** who needs to understand what the scores mean without consulting external references

---

## Required: Document-Level Introduction

Each major output file (concept_registry, business_rule_catalog, workflow_registry, etc.) must begin with:

```
## How to Read This Document

### What this is
[One paragraph: what this document contains, in plain language]

### Who should read this
[Who benefits: product managers, architects, new engineers, AI agents, etc.]

### How to use it
[Concrete use cases: "Use this when you need to...", "Reference this before..."]

### Relationship to other outputs
[How this connects to other still outputs in the distillation]
```

---

## Required: Scoring Legend (for any document with scored entries)

When a document contains entries with numeric scores, include this legend BEFORE the first scored entry:

```
## Scoring Guide

| Score | Business Relevance | Evidence Diversity | Purity | Confidence |
|-------|---|---|---|---|
| 5 | Critical to the business — removing it breaks customer value | Found in 4+ independent source types (code, tests, UI, docs, runtime) | Pure business language — no technology references needed to understand it | Near-certain this is durable business truth |
| 4 | Important business capability — impacts revenue or operations | Found in 3 source types | Mostly clean — minor tech references that don't obscure meaning | High confidence — strong evidence from multiple paths |
| 3 | Useful business function — supports but doesn't drive value | Found in 2 source types | Mixed — business intent visible but expressed partly in tech terms | Moderate — reasonable candidate, may need stakeholder validation |
| 2 | Peripheral — exists but unclear business justification | Found in 1 source type only | Mostly technical — business meaning requires interpretation | Low — thin evidence, possibly outdated or misinterpreted |
| 1 | Questionable — may be implementation artifact, not business need | Single ambiguous reference | Implementation language dominates — hard to state without referencing code | Uncertain — may not represent durable business truth |
```

### Additional field explanations

**Aliases**: Other names this concept goes by in the source code, UI, documentation, or team vocabulary. These surface naming drift and help newcomers recognize the same concept across different parts of the system.

**Evidence**: Where in the source artifacts this finding was observed. Multiple distinct sources increase confidence that this is genuine business truth, not an artifact of one developer's naming choice.

**Pages**: Which analysis pages (concat batches) this finding appeared in. Broad page coverage = the concept is pervasive across the system.

**Filter score**: How many of the four quality dimensions scored ≥ 4. A filter score of 4/4 means high confidence across all dimensions — this is unambiguous, well-evidenced, clean business truth.

---

## Required: Still-Specific Context

Each still should add interpretation guidance specific to its output type:

### Domain Language Still
- **Core Entity**: A primary business object that drives the system's value. If this disappeared, a core workflow would break.
- **Supporting Entity**: Exists to serve a Core Entity. Important but doesn't independently drive value.
- **Actor/Role**: A person, system, or organizational function that performs actions.
- **Flagged (F-xxx)**: Concepts that appeared in code but may be implementation artifacts rather than business vocabulary. Requires human judgment.

### Rule Still
- **REQUIRE**: Something that MUST be true — a hard constraint. Violation = system refuses the operation.
- **PERMIT**: Something that MAY happen under stated conditions. An authorization or eligibility gate.
- **PROHIBIT**: Something that must NOT happen. The system actively prevents this.
- **Type classifications**: Eligibility (qualifies/disqualifies), Calculation (computes a value), Permission (authorizes), Obligation (must happen), Threshold (numeric boundary), Sequence (required order), Compliance (legal/regulatory), Temporal (time-based).

### State Still
- **Terminal state**: No transitions out — the entity's lifecycle is complete.
- **Guard**: A condition that must be true before a transition is allowed.
- **Side effect**: Something that happens automatically when a transition occurs (notifications, audit entries, downstream state changes).

### Workflow Still
- **Precondition**: What must be true before the workflow can start.
- **Postcondition**: What is guaranteed to be true after the workflow completes successfully.
- **Actor**: Who or what performs each step.
- **Handoff**: Where responsibility transfers from one actor to another.

### Exception Still
- **Disposition: Preserve**: This exception represents real business learning — it must survive into any rebuild.
- **Disposition: Revise**: The exception exists but the handling is outdated or suboptimal.
- **Disposition: Discard**: This was a workaround for a constraint that no longer exists.

---

## Anti-pattern

Do NOT produce output files that require the reader to consult a separate scoring guide, conventions document, or distillation methodology reference to understand what they're reading. The document must be self-contained for interpretation.

---

## Required: Plain-English Companions for Formal Notation

Every formal expression (REQUIRE, PERMIT, PROHIBIT, calculation) MUST be immediately followed by a plain-English sentence that a non-technical business user can understand without knowing mathematical notation.

**Example — correct:**
```
### RULE-001: Booking Date Ordering
REQUIRE check_out > check_in

*In plain terms: A booking's checkout date must come after its check-in date.*
```

**Example — incorrect (missing companion):**
```
### RULE-001: Booking Date Ordering
REQUIRE check_out > check_in
```

The formal notation exists for precision and implementation. The companion exists for comprehension. Both are required.

---

## Required: Named Evidence Sources (not just counts)

Evidence MUST name the actual sources, not just count them. A business reader wants to know HOW you know this is real.

**Correct:**
```
Evidence: Schema validation (BookingCreate) + Service layer (assert_room_bookable) — 2 sources
```

**Incorrect:**
```
Evidence diversity: 2
```

The count is still useful for scoring, but the names must be visible inline.

---

## Required: No Internal Processing References in Reader-Facing Output

Remove or translate internal processing artifacts that mean nothing to a reader:

| Remove/translate | Replace with |
|---|---|
| "Sources: page_000" | The module or domain area name (e.g., "Source: Accommodation module") |
| "Pages: 000-039" | "Found across: core modules, all domains" or simply omit |
| Field/column names as-is | Business-language equivalents (e.g., `planned_end_date` → "planned end date") |

The bottled spec and master blend must NEVER reference page numbers, batch IDs, or internal processing structure.

---

## Required: Gap Analysis in Business Language

Gap analysis entries must be expressed in language a product manager would use, not in field-name references:

**Correct:**
> There's no safeguard preventing someone from setting a project's end date before its start date. This means scheduling reports could show impossible timelines.

**Incorrect:**
> No rule prevents `planned_end_date` from being before `planned_start_date`.

The gap should explain WHAT could go wrong in business terms, not just what's technically missing.
