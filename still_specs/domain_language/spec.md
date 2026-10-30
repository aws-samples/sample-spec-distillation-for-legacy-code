<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Domain Language Still — Specification

## Identity

**Tier:** Foundation
**Primary Question:** What business concepts does the system recognize?
**Role in the Blend:** Provides the foundational vocabulary that all other stills reference. Without a clean domain language cut, subsequent stills lack anchoring terms.

---

## Purpose

The Domain Language Still identifies the business vocabulary embedded in the system. It recovers the meaningful nouns, verbs, roles, relationships, and events that the system appears to understand — regardless of whether those concepts were ever formally documented.

This still is typically run first because language is the most accessible surface for latent business intent. Class names, database tables, API resources, route names, UI labels, enums, error messages, and configuration keys all reveal the conceptual model of the product.

The goal is not to produce a technical data model or an ER diagram. The goal is to recover the business language that should anchor the reason-centered specification. The output should be recognizable to a product manager, domain expert, or customer — not only to an engineer.

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Model/entity class names | Core business nouns | High |
| Database table and column names | Persistent concepts and attributes | High |
| API resource paths and operation names | Externally visible concepts and actions | High |
| UI labels, headings, and navigation | Customer-facing vocabulary | High |
| Enum values and constants | Constrained concept categories | Medium-High |
| Route definitions | Action vocabulary | Medium |
| Event names (domain events, webhooks) | Business-significant occurrences | High |
| Error messages | Failure-mode vocabulary | Medium |
| Configuration keys | Operational concepts | Low-Medium |
| Translation/i18n files | Localized business terms | High |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Test names and descriptions | Behavioral vocabulary | Medium |
| Fixture and seed data | Example instances of concepts | Medium |
| Release notes and changelogs | Evolution of terminology | Medium |
| Issue labels and categories | Organizational vocabulary | Low-Medium |
| Public documentation | Explained concepts | High |
| Support/help content | User-facing language | High |
| Code comments | Developer understanding of domain | Low-Medium |

---

## Method

### Step 1: Term Extraction

Scan primary sources and extract all candidate terms that could represent business concepts. Cast a wide net. Include:

- Nouns that appear as class names, table names, API resources
- Verbs that appear as method names, API operations, event triggers
- Adjectives/qualifiers that appear as status values, type discriminators, flags
- Compound terms that represent specific domain concepts (e.g., "line_item", "shipping_method", "approval_chain")

### Step 2: Clustering

Group extracted terms into semantic clusters. Terms that refer to the same concept (even with different names) should be clustered together. For example:

- `Cart`, `Basket`, `shopping_cart` → same concept
- `User`, `Customer`, `Account`, `Buyer` → potentially different concepts or potentially the same concept in different contexts

Each cluster becomes a candidate domain concept.

### Step 3: Classification

For each cluster, classify as one of:

| Classification | Definition | Example |
|---|---|---|
| **Core Entity** | A primary business object | Order, Customer, Product |
| **Supporting Entity** | An entity that exists in service of a core entity | LineItem, Address, PaymentMethod |
| **Actor/Role** | A person or system that interacts with the domain | Buyer, Admin, FulfillmentPartner |
| **Action** | A business-meaningful operation | Purchase, Refund, Approve |
| **Event** | A business-significant occurrence | OrderPlaced, PaymentCaptured |
| **Attribute** | A meaningful property of an entity | Status, Priority, Amount |
| **Relationship** | A named connection between entities | BelongsTo, Manages, Fulfills |
| **Implementation Term** | A technical concept without business meaning | Repository, Serializer, Middleware |

### Step 4: Separation

Separate business concepts from implementation terms. Ask for each candidate:

- Would a product manager, customer, or domain expert recognize this term?
- Does this term appear in customer-facing contexts (UI, docs, API)?
- Does this term survive if the system is rebuilt with different technology?
- Is this term necessary to explain what the system does (not how)?

Terms that fail all four questions are likely implementation residue.

### Step 5: Conflict Detection

Identify naming conflicts:

- **Synonyms:** Multiple terms for the same concept (signals organizational history or ambiguity)
- **Homonyms:** Same term used for different concepts in different contexts (signals potential bugs or architectural boundaries)
- **Drift:** Terms whose meaning has shifted over time (signals product evolution)
- **Stale terms:** Names that no longer match current business meaning (signals tech debt)

### Step 6: Relationship Mapping

For each core and supporting entity, identify:

- What other entities does it relate to?
- What is the nature of the relationship (ownership, containment, reference, dependency)?
- What business meaning does the relationship carry?

### Step 7: Scoring

Score each domain concept on the five standard dimensions (see Scoring the Stills in the strategy memo):

- Business Relevance (1-5)
- Evidence Diversity (1-5)
- Purity (1-5: 1=impure, 5=pure business intent)
- Confidence (1-5)
- Convergence with other stills (1-5, assessed after other stills run)

---

## Output Format

The primary output is a **Domain Vocabulary Document** containing:

### 1. Concept Registry

For each identified business concept:

```
Concept: [Business Name]
Type: [Core Entity | Supporting Entity | Actor | Action | Event | Attribute | Relationship]
Definition: [One-sentence business definition]
Aliases: [Other names found in the system]
Evidence: [Where this concept appears: code, UI, API, tests, docs]
Confidence: [1-5]
Business Relevance: [1-5]
Relationships: [List of related concepts]
Open Questions: [Ambiguities requiring human review]
```

### 2. Naming Conflict Register

For each detected conflict:

```
Conflict Type: [Synonym | Homonym | Drift | Stale]
Terms: [The conflicting terms]
Contexts: [Where each term appears]
Likely Explanation: [Why this conflict exists]
Recommendation: [Which term should anchor the spec]
```

### 3. Excluded Terms

A list of terms classified as implementation-only, with brief rationale for exclusion. This serves as an audit trail and a check against over-filtering.

### 4. Domain Map

A high-level visual or structured representation of how core concepts relate to each other. This becomes the skeleton that other stills flesh out.

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| Framework vocabulary | Repository, Controller, Service, Factory | Ask: does this term exist for the customer or only for the code? |
| ORM artifacts | HasMany, BelongsTo, Polymorphic | Ask: is the relationship business-meaningful or only structural? |
| Infrastructure terms | Queue, Cache, Worker, Job | Ask: does this affect business behavior or only system performance? |
| Over-prominent technical terms | Terms appearing frequently due to framework patterns, not business importance | Weight by diversity of appearance (code + UI + tests + docs) over raw frequency |
| Stale names | Old product terminology preserved in code | Cross-reference with current UI, docs, and customer-facing language |
| Inherited vocabulary | Terms from acquired products or abandoned features | Check whether the concept is still active in current behavior |

---

## Validation Criteria

A high-quality Domain Language cut satisfies:

1. **Stakeholder recognition:** A product manager or domain expert would recognize 80%+ of the core concepts without explanation.
2. **Customer alignment:** The vocabulary aligns with language used in the product UI, help docs, and customer communications.
3. **Completeness signal:** Every major screen, API endpoint, and workflow in the system can be described using the recovered vocabulary.
4. **Clean separation:** Implementation terms are excluded unless they carry genuine business meaning.
5. **Convergence readiness:** The vocabulary provides clean anchoring terms for the Workflow, Rule, State, and other stills to reference.

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Data Still | Shares entity identification; Data Still goes deeper into persistence meaning |
| Workflow Still | Uses domain vocabulary to name actors, actions, and objects in workflows |
| Rule Still | Rules reference domain concepts; vocabulary must be stable before rules are expressed |
| Permission Still | Actors and roles identified here feed directly into permission modeling |
| State Still | State models attach to entities identified here |
| Interface Still | External-facing vocabulary should converge with internal domain language |
| Redistillation Still | The recovered vocabulary is a primary comparison target |
