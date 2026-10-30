<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Data Still — Specification

## Identity

**Tier:** Foundation
**Primary Question:** What does the business consider worth remembering, and why?
**Role in the Blend:** Reveals business obligations around persistence, retention, auditability, and data relationships that other stills may reference but not fully surface.

---

## Purpose

The Data Still identifies what the system commits to preserving and the rules that govern that preservation. It moves beyond the Domain Language Still's vocabulary recovery into the deeper question of *why* certain information is persisted, *what obligations* attach to that persistence, and *what business invariants* are enforced through data design.

Database schemas, migrations, retention policies, audit trails, soft deletes, and field-level constraints reveal what the business treats as durable truth. When a system chooses to never delete certain records, to version certain objects, or to enforce certain referential constraints, those choices encode business requirements — often undocumented ones.

This still is especially important for systems subject to regulatory, compliance, or contractual obligations around data.

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Database schemas (current) | Entity structure, constraints, relationships | High |
| Migration history | Schema evolution and business-model changes | High |
| Field constraints (NOT NULL, UNIQUE, CHECK) | Business invariants enforced at data level | High |
| Referential integrity (foreign keys) | Mandatory business relationships | High |
| Soft-delete patterns | Data the business refuses to lose | High |
| Audit columns (created_at, updated_by, etc.) | Accountability requirements | Medium-High |
| Versioning/history tables | Change-tracking obligations | High |
| Retention/archival logic | Regulatory or contractual data obligations | High |
| Indexes with business meaning | Query patterns revealing business priorities | Medium |
| Data classification labels | Sensitivity and handling requirements | High |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Data access layers / scopes | Visibility and access boundaries | Medium |
| Reporting queries | What the business measures and tracks | Medium |
| Analytics schemas | Business KPIs encoded in data | Medium |
| Backup/recovery configurations | Data criticality tiers | Medium |
| API response shapes | What data is exposed externally | Medium-High |
| Seed/fixture data | Example instances revealing data patterns | Low-Medium |
| Data dictionaries / ERD diagrams | Documented data intent | High (if current) |
| GDPR/compliance annotations | Regulatory obligations | High |

---

## Method

### Step 1: Schema Inventory

Map all persistent data structures. For each table/collection/entity:
- What business concept does it represent?
- Is it a primary business object, a supporting/junction object, or a technical artifact?
- What is its relationship to the Domain Language Still's concept registry?

### Step 2: Constraint Analysis

For each entity, examine constraints and ask what business rule they protect:

- **NOT NULL:** What would it mean for this field to be absent? Is the constraint a business rule ("every order must have a customer") or a technical convenience?
- **UNIQUE:** What business invariant does uniqueness protect? (e.g., "no two customers may share an email" = identity rule)
- **CHECK constraints:** What value ranges or conditions does the business require?
- **Foreign keys:** What relationship is mandatory vs. optional, and what does that mean for the business?
- **Composite constraints:** What multi-field invariants exist?

### Step 3: Evolution Analysis

Review migration history to understand how the data model changed:

- What entities were added and when? (New business capabilities)
- What fields were added to existing entities? (Discovered requirements)
- What constraints were added after initial creation? (Learned invariants)
- What was renamed? (Concept drift or clarification)
- What was deprecated but never removed? (Abandoned features)
- What data was migrated or transformed? (Business model changes)

### Step 4: Persistence Obligation Analysis

Identify data the system treats specially regarding lifecycle:

- **Never deleted:** What data uses soft-delete, archival, or tombstoning? Why can't it be destroyed?
- **Versioned:** What data maintains history? Who needs to see prior versions?
- **Audited:** What data tracks who-changed-what-when? What accountability requirement drives this?
- **Time-bounded:** What data has explicit retention periods? What regulation or policy dictates the window?
- **Immutable:** What data cannot be modified after creation? What business promise does immutability protect?

### Step 5: Relationship Significance

For each relationship between entities, determine:

- Is this a business-meaningful relationship or a technical normalization artifact?
- What happens to the business if this relationship is broken?
- Is the relationship mandatory or optional, and what does that mean operationally?
- Are there cascade behaviors (delete, update) that encode business rules?

### Step 6: Data Boundary Detection

Identify boundaries in the data that signal business segmentation:

- Tenant isolation (multi-tenancy)
- Geographic partitioning
- Organizational boundaries (departments, teams, business units)
- Temporal boundaries (fiscal periods, contract terms)
- Classification boundaries (data sensitivity tiers)

### Step 7: Scoring

Score each finding on:
- Business Relevance (1-5)
- Evidence Diversity (1-5)
- Purity (1-5: 1=impure, 5=pure business intent)
- Confidence (1-5)
- Convergence with other stills (1-5, assessed later)

---

## Output Format

### 1. Persistent Business Object Registry

For each business-meaningful entity:

```
Entity: [Business Name]
Persistence Rationale: [Why does the business need to remember this?]
Lifecycle: [Created when? Modified how? Deleted/archived when? Never deleted?]
Key Constraints: [Business invariants enforced at data level]
Audit Requirements: [What accountability exists?]
Retention Obligations: [How long must it be kept? Why?]
Sensitivity: [Classification level, if any]
Relationships: [To other entities, with business meaning]
Evolution Notes: [How this entity changed over time and why]
Confidence: [1-5]
```

### 2. Business Invariant Registry (Data-Level)

For each constraint that encodes a business rule, expressed as formal notation per the Formal Representation Convention:

```
Invariant: [Formal expression — REQUIRE predicate]
Where: [Variable legend with business definitions]
Entity: [What it applies to]
Evidence: [Schema, migration, test, or documentation]
Business Rationale: [Why this must be true]
Confidence: [1-5]
```

### 3. Data Obligation Map

```
Obligation: [What the business must do with this data]
Type: [Retention | Deletion | Audit | Immutability | Versioning | Isolation]
Regulatory Driver: [If applicable]
Affected Entities: [List]
Evidence: [Where this obligation is encoded]
Confidence: [1-5]
```

### 4. Evolution Timeline

Key data-model changes ordered chronologically, each annotated with the business reason (where inferable):

```
Date/Migration: [identifier]
Change: [What happened]
Business Reason: [Why, if determinable]
Impact: [What capability was added, changed, or lost]
```

### 5. Excluded Structures

Technical tables/structures with no business meaning, with rationale for exclusion.

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| ORM artifacts | Join tables with no business name, polymorphic type columns | Ask: does this structure have a business name, or only a technical one? |
| Normalization choices | Tables split for performance or storage, not business reasons | Ask: would a denormalized version change business meaning? |
| Caching/materialized views | Tables that duplicate data for speed | Ask: is this data authoritative or derived? |
| Framework tables | Session stores, job queues, migration trackers | Ask: does this exist for the business or for the infrastructure? |
| Legacy debris | Unused tables from deprecated features | Check for recent writes; if dormant, flag as potential residue |
| Over-indexing on indexes | Indexes reveal access patterns but not always business priorities | Distinguish query optimization from business-meaningful access patterns |

---

## Validation Criteria

1. **Business justification:** Every included entity has a stated business reason for existence.
2. **Obligation clarity:** Data retention, deletion, and audit requirements are explicit.
3. **Evolution insight:** The timeline reveals how the business model changed, not just how the schema changed.
4. **Constraint meaning:** Every significant constraint is translated into a business-language invariant.
5. **Clean separation:** Technical persistence artifacts (caches, queues, ORM internals) are excluded.

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Domain Language | Shares entity identification; Data Still provides persistence depth |
| Rule Still | Many business rules are enforced at the data layer; this still surfaces them |
| State Still | State fields and transitions are visible in schema; Data Still provides the structural view |
| History Still | Migration history overlaps with the History Still's evolution analysis |
| Interface Still | API response shapes reveal which data is externally promised |
| Permission Still | Data visibility boundaries feed into permission modeling |
| Redistillation Still | Data structure is a primary comparison surface |
