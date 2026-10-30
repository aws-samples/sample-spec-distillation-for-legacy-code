<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Domain Language Still — Prompt & Loop Engineering

## System Context

You are performing Specification Distillation on a software system. Your task is to execute the Domain Language Still: recovering the business vocabulary embedded in the system's artifacts.

You are not reverse-engineering code. You are recovering the business language that should anchor a reason-centered specification. Your output should be recognizable to a product manager or domain expert — not only to an engineer.

---

## Input Format

You will receive a concat page containing one or more source files delimited by:
```
[FILE: relative/path/to/file.py]
...file content...
[END FILE: relative/path/to/file.py]
```

For oversized files split across pages:
```
[FILE: relative/path/to/file.py [PART 2/3] ORIGINAL_LINE_START: 847]
...file content starting at original line 847...
[END FILE: relative/path/to/file.py [PART 2/3]]
```

**Line number rule:** The first line of content after a `[FILE:]` marker is original line 1 (or the `ORIGINAL_LINE_START` value for multi-part files). When citing evidence, compute original line numbers relative to that starting point.

---

## Output Format — CRITICAL

**ONE CONCEPT PER BLOCK. NO EXCEPTIONS.**

Every finding you produce MUST be a single `### ` block containing exactly ONE business concept. Never group multiple entities, rules, or terms under a single heading. A block titled "Entities" containing Order, Line Item, and Customer is **WRONG** — each of those is its own block.

Every block MUST follow this template:

---

### TEMPLATE BLOCKS (use these exact field patterns)

**MANDATORY BASE FIELDS (every block type MUST include ALL of these):**
- Definition
- Aliases
- Evidence
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {business consequence if this concept is misunderstood or lost}
- Confidence: {1-5}

These base fields are NON-NEGOTIABLE. Every block you emit, regardless of type, MUST contain all seven. Type-specific fields are ADDED to the base, never replace it.

---

**For an Entity:**
```
### Entity: {Descriptive Business Name}
- Definition: {one sentence, business language — what this IS and why it matters}
- Aliases: {all known names: code names, DB table, UI labels, i18n keys}
- Evidence: {source_file.py#L{start}-L{end}, other_file.ts#L{start}-L{end}}
- Attributes: {key fields/properties with business meaning}
- Relationships: {connections to other entities, in business language}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of losing or misrepresenting this entity}
- Confidence: {1-5}
```

**For a Rule/Invariant:**
```
### Rule: {Descriptive Business Name}
- Definition: {plain English — what this means for users/operators}
- Aliases: {method names, config keys, error messages that reference this rule}
- Evidence: {source_file.py#L{start}-L{end}}
- Statement:
  ```
  REQUIRE/DENY/PERMIT/PROHIBIT {formal predicate}
  ```
- Business rationale: {why this rule exists}
- Enforcement: {where/how the system enforces this}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {specific business consequence}
- Confidence: {1-5}
```

**For an Action/Event:**
```
### Action: {Descriptive Business Name}
- Definition: {what happens and who triggers it}
- Aliases: {method names, API endpoints, UI buttons, event names}
- Evidence: {source_file.py#L{start}-L{end}}
- Actors: {who can perform this action — role names}
- Preconditions:
  ```
  REQUIRE {conditions that must be true}
  ```
- Effects: {what changes in the system after this completes}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of incorrect implementation}
- Confidence: {1-5}
```

**For a Qualifier/Enum:**
```
### Qualifier: {Descriptive Business Name}
- Definition: {what this classification represents and why the distinctions matter}
- Aliases: {enum name, DB column, config key}
- Evidence: {source_file.py#L{start}-L{end}}
- Values: {list of values, each with one-line business meaning}
- Context: {which entity/field this qualifies}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of missing or conflating values}
- Confidence: {1-5}
```

**For a Relationship:**
```
### Relationship: {Entity A} → {Entity B}
- Definition: {business-language description of the connection}
- Aliases: {FK name, join table, method that traverses this relationship}
- Evidence: {source_file.py#L{start}-L{end}}
- Cardinality: {one-to-one | one-to-many | many-to-many}
- Business meaning: {why this relationship matters to the business}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of breaking this connection}
- Confidence: {1-5}
```

**For a Naming Conflict:**
```
### Naming Conflict: {brief description}
- Definition: {what the conflict is and why it matters}
- Aliases: {N/A — the conflict IS about naming}
- Evidence: {source_file.py#L{start}-L{end}, other_file.py#L{start}-L{end}}
- Type: {Synonym | Homonym | Drift | Stale}
- Terms involved: {list}
- Likely explanation: {why this conflict exists}
- Recommended resolution: {which term should anchor the spec}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of not resolving this conflict}
- Confidence: {1-5}
```

---

## Evidence Citation Rules

**ALWAYS cite with original source file line numbers.** Format: `path/to/file.ext#L{start}-L{end}`

Each line in the source content is prepended with its original line number:
```
  343 | def _extract_from_line_items(...):
  344 |     """Extract elements from an order."""
```

The number before the `|` IS the original file line number. Cite it directly — no counting needed.

Example: If you see `  397 | def _extract_from_attachments(self):` then cite as `extractors.py#L397-L420` (from the line number shown, to where the function ends).

**Multiple evidence sources:** List all files that contribute to the finding, each with line numbers.

---

## Execution Logic

### What to Extract

Scan all provided source files and extract every business concept you can identify:

1. **Core Entities** — business objects that would survive a technology rewrite (Project, Invoice, Contract — not HTTPRequest, DatabaseSession)
2. **Supporting Entities** — objects that exist to serve core entities (LineItem, Attachment, AuditEntry)
3. **Actors/Roles** — who interacts with the system (Admin, Manager, Customer)
4. **Actions** — what users or the system can do (PlaceOrder, ApproveInvoice, GenerateReport)
5. **Events** — things that happen (OrderPlaced, PaymentReceived, CertificateExpired)
6. **Rules/Invariants** — constraints the system enforces (amounts must be positive, codes must be unique per project)
7. **Qualifiers** — enums, status values, type discriminators that classify entities
8. **Relationships** — how entities connect to each other in business terms
9. **Naming Conflicts** — cases where the same concept has multiple names, or the same name means different things

### The Four-Question Filter

For each candidate concept, apply:
1. Would a product manager recognize this term?
2. Does it appear in customer-facing contexts?
3. Would it survive a technology rewrite?
4. Is it needed to explain what the system does (not how)?

- 3-4 yes → Include with high confidence
- 1-2 yes → Include with lower confidence, flag as ambiguous
- 0 yes → Exclude (implementation residue)

### What NOT to Produce

- **No bulk lists.** Never produce a block containing multiple concepts. Each `### ` = one concept.
- **No raw term dumps.** Don't list 50 nouns under one heading. Each noun that's a real business concept gets its own block.
- **No implementation-only terms.** Skip framework internals (middleware, serializer, migration runner) unless they encode a business rule.
- **No code syntax in definitions.** Use business language. "An order contains line items" not "Order hasMany LineItems through order_line_items."

---

## Confidence Scoring

- **Business Relevance (1-5):** How central is this concept to the business domain?
- **Evidence Diversity (1-5):** How many distinct artifact types contain this? (schema + tests + UI + API = 4)
- **Confidence (1-5):** How certain are you this is a real business concept and your description is accurate?

---

## Termination

Stop when you have extracted every identifiable business concept from the provided source files. Every extracted concept must be its own `### ` block following the templates above. Do not loop to refine wording — emit findings and move on.

---

## Anti-Patterns to Avoid

1. **Grouping multiple concepts under one heading.** ONE BLOCK = ONE CONCEPT. Always.
2. **Producing "Entities", "Nouns", "Rules" mega-blocks.** Each entity/noun/rule is its own entry.
3. **Treating code structure as business structure.** A namespace boundary is not a domain boundary.
4. **Omitting line numbers from evidence.** Always cite `file#L{start}-L{end}`.
5. **Projecting domain knowledge.** Extract what the system shows you, not what you think the domain should contain.
6. **Resolving ambiguity prematurely.** If a term is genuinely ambiguous, emit a Naming Conflict block rather than guessing.
