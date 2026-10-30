<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Interface Still — Specification

## Identity

**Tier:** Grounding
**Primary Question:** What promises does the system make externally?
**Role in the Blend:** Identifies the contractual surface — what the system has committed to customers, partners, internal teams, and downstream systems that must survive any rebuild.

---

## Purpose

The Interface Still identifies the promises the system makes to the outside world. External interfaces are often more stable than internal code because they carry backward-compatibility obligations. APIs, schemas, webhooks, exports, imports, file formats, SDKs, and partner integrations reveal what the business has committed to preserve.

The goal is to recover business-facing contracts, not technical plumbing. The output should make clear what the system has promised, to whom, and what the cost of breaking that promise would be.

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| REST API definitions / OpenAPI specs | External operations and data shapes | High |
| GraphQL schemas | Query capabilities and types | High |
| Event/webhook schemas | Published events and their contracts | High |
| Message queue contracts | Async communication promises | High |
| Public documentation (API docs) | Documented promises | High |
| SDK source code | Client-facing interface | High |
| Import/export format definitions | Data exchange contracts | Medium-High |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Partner integration guides | Contractual interface expectations | High |
| Backward compatibility tests | Actively protected interfaces | High |
| API versioning strategy | Evolution obligations | Medium-High |
| Deprecation notices | Sunset commitments | Medium |
| Database views exposed to external systems | Read-contract obligations | Medium |
| Public error codes and messages | Failure contract | Medium |

---

## Method

### Step 1: Interface Inventory

Catalog all external-facing interfaces:
- Public APIs (REST, GraphQL, gRPC, SOAP)
- Webhooks and event subscriptions
- File imports and exports
- SDKs and client libraries
- Database views or direct access granted to external systems
- Email/notification schemas
- Embedded widget or iframe contracts

### Step 2: Consumer Identification

For each interface, identify who depends on it:
- End-user applications (mobile, web, desktop)
- Partner integrations
- Internal services/teams
- Third-party developers (public API consumers)
- Reporting/analytics systems
- Regulatory reporting systems

### Step 3: Contract Extraction

For each interface, extract the business contract:
- What business capability does it expose?
- What data concepts are communicated?
- What operations are available?
- What guarantees are made (response format, ordering, consistency)?
- What error conditions are documented?
- What rate limits or quotas exist?
- What versioning or compatibility promises exist?

### Step 4: Obligation Assessment

For each contract:
- How many consumers depend on it?
- What is the cost of a breaking change?
- Is there a versioning strategy that allows evolution?
- Are there SLAs (availability, latency, throughput)?
- Are there contractual or regulatory obligations around this interface?

### Step 5: Business Capability Mapping

Map interfaces to business capabilities:
- Which interfaces serve the same business capability?
- Which interfaces expose internal concepts that shouldn't be external?
- Which business capabilities lack external interfaces?

### Step 6: Scoring

Score each interface:
- Business Relevance (1-5)
- Evidence Diversity (1-5)
- Purity (1-5: 1=impure, 5=pure business intent)
- Confidence (1-5)
- Convergence (1-5, assessed later)

---

## Output Format

### 1. External Contract Registry

```
Interface: [Name/path]
Type: [REST API | GraphQL | Webhook | Event | Import | Export | SDK | View]
Consumer(s): [Who depends on this]
Business Capability: [What it enables]
Operations: [What actions are available]
Data Concepts: [What business entities are exposed]
Guarantees: [Compatibility, versioning, SLA]
Breaking-Change Cost: [High | Medium | Low — and why]
Regulatory Obligation: [If any]
Evidence: [Spec file, docs, tests]
Confidence: [1-5]
```

### 2. Data Shape Contracts

For key entities exposed externally:
```
Entity: [Business Name]
External Shape: [What fields/attributes are promised]
Required Fields: [What must always be present]
Optional Fields: [What may be absent]
Deprecated Fields: [What is being phased out]
Business Meaning of Key Fields: [Why each important field exists]
Compatibility Obligation: [What cannot change without versioning]
```

### 3. Event/Webhook Contracts

```
Event: [Name]
Trigger: [What causes this event]
Payload: [What data is sent]
Consumers: [Who subscribes]
Delivery Guarantee: [At-least-once | Exactly-once | Best-effort]
Ordering Guarantee: [Ordered | Unordered]
Business Meaning: [What business occurrence this represents]
```

### 4. Backward Compatibility Obligations

```
Interface: [Which]
Obligation: [What must remain stable]
Duration: [How long — indefinite, until version sunset, contractual period]
Evidence: [Versioning policy, deprecation docs, contractual terms]
Risk of Violation: [What happens if broken]
```

### 5. Interface Gaps

Capabilities that lack proper external interfaces or have interface quality issues.

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| Internal-only APIs exposed externally by accident | Endpoints without docs, auth, or versioning | Ask: was this intentionally public? Does anyone depend on it? |
| Over-specific technical contracts | Interfaces that expose implementation details (DB IDs, internal enums) | Ask: should the external contract be this specific, or is it leaking internals? |
| Deprecated but not removed interfaces | Old versions still responding but not maintained | Check: is this actively consumed? Should it be in the spec as obligation or sunset candidate? |
| Infrastructure interfaces | Health checks, metrics endpoints, admin APIs | Ask: is this a business contract or an operational interface? |

---

## Validation Criteria

1. **Consumer clarity:** Every interface has identified consumers.
2. **Business capability mapping:** Interfaces are linked to the business capabilities they enable.
3. **Obligation awareness:** Breaking-change costs and compatibility obligations are explicit.
4. **Contract precision:** Data shapes and operations are documented at the business level.
5. **Completeness:** No significant external-facing surface is missing from the inventory.

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Domain Language | External interfaces should use consistent business vocabulary |
| Data Still | External data shapes must align with persistent data models |
| Rule Still | Interface validations enforce rules at the boundary |
| Permission Still | API scopes and entitlements are permission structures |
| Workflow Still | Interfaces enable external actors to participate in workflows |
| State Still | External state representations must be coherent |
| Runtime Still | API analytics show actual interface usage patterns |
