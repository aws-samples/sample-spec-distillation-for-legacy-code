<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# Domain Partitioning Convention

## Purpose

This document defines how large codebases are split into bounded contexts (domains) for independent distillation. Partitioning enables the framework to handle systems that exceed single-context-window capacity by decomposing them into manageable units that can be distilled independently and then blended at the system level.

---

## Operating Principle

Large systems are rarely one coherent domain. An ERP might contain project management, accounting, procurement, HR, and reporting — each with its own vocabulary, rules, workflows, and state machines. Distilling the entire system as one unit forces stills to sample rather than comprehensively extract.

Domain partitioning splits the system along natural business boundaries, runs the full still pipeline independently per domain, then merges domain-level master blends into a system-level specification that captures cross-domain interactions.

---

## What Constitutes a Domain

A domain is a coherent subset of the codebase that:

1. **Has its own vocabulary** — Core entities and concepts that are primarily used within this subset
2. **Has internal cohesion** — Files within the domain reference each other more than they reference files outside it
3. **Has identifiable boundaries** — There are clear interface points where this domain interacts with others
4. **Is independently meaningful** — A product person could describe what this domain does without referencing the internals of other domains

---

## Sizing Guidelines

| Metric | Minimum | Target | Maximum |
|---|---|---|---|
| Files of signal (post-selection) | 20 | 50-150 | 200 |
| Estimated business concepts | 5 | 15-40 | 60 |
| State machines | 1 | 3-8 | 15 |

**Below minimum:** Domain is too thin to justify independent distillation — merge with a related domain.

**Above maximum:** Domain is too large for comfortable single-pass distillation — look for internal sub-boundaries.

---

## Partitioning Signals

The domain partitioner uses these signals to identify boundaries:

### Strong Signals (High Confidence)
- **Module/package/namespace boundaries** — Explicit developer-intended separation
- **Database schema groupings** — Tables that share foreign keys form a cluster
- **Separate route/API groups** — Distinct API namespaces often indicate domain boundaries
- **Independent deployment units** — Services, workers, or modules that deploy independently

### Medium Signals (Requires Validation)
- **Directory structure** — Top-level directories named for business concepts
- **Distinct entity groups** — Models that only reference each other, not the broader system
- **Team ownership boundaries** — CODEOWNERS patterns or git blame clustering

### Weak Signals (Suggestive Only)
- **File naming patterns** — Consistent prefixes suggesting grouping
- **Import/dependency graphs** — Clusters of tightly-coupled files
- **Test organization** — Test suites that group by feature area

---

## Cross-Domain Concepts

Some concepts span multiple domains. These are handled explicitly:

### Shared Entities
Entities referenced by multiple domains (e.g., "User" or "Organization") are:
- Included in **every domain that references them** for extraction purposes
- Reconciled during cross-domain synthesis to produce a single authoritative definition
- Annotated with "shared" status in the domain map

### Cross-Domain Workflows
Workflows that span domains (e.g., "procurement creates a purchase order, accounting pays it") are:
- Partially extracted in each participating domain (the steps that live there)
- Stitched together during cross-domain synthesis
- The synthesis agent specifically looks for workflow fragments that connect across boundaries

### Cross-Domain Rules
Rules that reference entities from multiple domains:
- Extracted in the domain where they are enforced
- Cross-referenced during synthesis with the entities they constrain

---

## Domain Map Output Format

```json
{
  "system": "MySystem",
  "generated_at": "2026-07-15T10:00:00Z",
  "source_root": "./src",
  "total_files": 2847,
  "domains": [
    {
      "name": "project_management",
      "description": "Core project lifecycle, planning, scheduling, and team management",
      "files": ["src/Entity/Project.php", "src/Service/ProjectService.php", ...],
      "file_count": 87,
      "key_entities": ["Project", "Task", "Schedule", "Team"],
      "boundary_interfaces": ["shares User with identity", "provides Project to financial"]
    },
    {
      "name": "financial",
      "description": "Invoicing, payments, cost tracking, budgeting",
      "files": ["src/Entity/Invoice.php", "src/Service/PaymentService.php", ...],
      "file_count": 112,
      "key_entities": ["Invoice", "Payment", "Budget", "CostItem"],
      "boundary_interfaces": ["consumes Project from project_management", "shares User with identity"]
    }
  ],
  "shared_files": [
    {
      "path": "src/Entity/User.php",
      "used_by": ["project_management", "financial", "identity"],
      "primary_domain": "identity"
    }
  ],
  "cross_domain_workflows": [
    {
      "name": "order_to_fulfillment",
      "domains": ["project_management", "financial"],
      "description": "An order triggers invoice generation and payment processing"
    }
  ]
}
```

---

## Human Override

The domain map is generated as a proposal, not a final decision. The recommended workflow:

1. Partitioner produces `domain_map.json`
2. Human reviews: Are boundaries sensible? Are domains too large/small? Are shared entities correct?
3. Human edits `domain_map.json` if needed (move files between domains, merge/split domains)
4. Orchestrator proceeds with the reviewed map

For automated/CI runs, the map can be used as-is with a warning that it was not human-reviewed.

---

## Cross-Domain Synthesis

After all domains are independently distilled, a cross-domain synthesis step:

1. **Vocabulary reconciliation** — Do domains use the same term for different concepts? Different terms for the same concept?
2. **Workflow stitching** — Connect partial workflows that span boundaries
3. **Rule conflict detection** — Do rules in Domain A conflict with rules in Domain B?
4. **State alignment** — Do shared entities have consistent state machines across domains?
5. **Authorization boundary mapping** — How does permission cross domain boundaries?
6. **Interface contract identification** — What promises does each domain make to others?

The output is a unified system-level master blend that preserves domain provenance (which domain each finding originated from).

---

## When Partitioning Is Not Needed

Skip partitioning when:
- Artifact pre-selection produces fewer than 200 files total across all stills
- The system is a single coherent domain (one purpose, one vocabulary)
- A human has already scoped the distillation to a specific area

The orchestrator should detect post-selection file count and skip partitioning for smaller codebases.

---

## Relationship to Existing Tier Structure

Domain partitioning does not change the tier structure within a domain. Each domain runs the full:

```
Foundation → Behavior → Constraints → Evidence → Grounding → Synthesis
```

The partitioning adds a layer **around** this:

```
Tier 0:   Artifact Selection (whole tree)
Tier 0.5: Domain Partitioning

Per Domain (parallelizable):
  Tier 1-5 + Synthesis (scoped to domain files)

Cross-Domain Synthesis → System Master Blend → Bottling
```
