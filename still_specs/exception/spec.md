<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Exception Still — Specification

## Identity

**Tier:** Evidence
**Primary Question:** What edge cases has the business learned to handle?
**Role in the Blend:** Captures the operationally-discovered requirements, compensations, and recovery paths that represent some of the most defensible IP in a mature system.

---

## Purpose

The Exception Still extracts the unusual cases the business has learned to handle. It recovers edge cases, overrides, compensations, recovery paths, and operational workarounds that exist because the business encountered real customers, real failures, real partners, real regulations, and real operational constraints.

Exceptions are where the most valuable latent IP often lives. Competitors can copy happy paths quickly. They are unlikely to know the exception landscape without years of operational experience.

The goal is not to preserve every hack. The goal is to identify which exceptions represent durable business learning and which are implementation scars that should not be carried forward.

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Error handling / rescue blocks | Failure recovery behaviors | High |
| Fallback logic | Graceful degradation paths | High |
| Admin override tools | Manual intervention capabilities | High |
| Retry and compensation logic | Recovery mechanisms | Medium-High |
| Support/admin panels | Operational tools for exception handling | High |
| Manual adjustment mechanisms | Human-in-the-loop corrections | High |
| Special-case conditionals | Business edge-case handling | High |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Issue/ticket history | Discovered edge cases | High |
| Incident reports / postmortems | Operational learning | High |
| Comments explaining workarounds | Developer-documented exceptions | Medium-High |
| Code with "hack" / "workaround" / "TODO" comments | Technical debt signals | Medium |
| Support playbooks / runbooks | Operational procedure knowledge | High |
| Release notes mentioning fixes | Resolved edge cases | Medium |

---

## Method

### Step 1: Exception Inventory

Identify all exception-handling code and classify each as:

| Classification | Definition | Example |
|---|---|---|
| **Customer exception** | Edge case a customer encounters | Cart with mixed tax jurisdictions |
| **Operational exception** | Issue operators must handle | Failed payment needing manual retry |
| **Partner/integration exception** | External system failure handling | Shipping provider timeout recovery |
| **Compliance exception** | Regulatory edge case | Data residency requirement override |
| **Data quality exception** | Handling bad or inconsistent data | Legacy records with null required fields |
| **Historical migration exception** | Workaround for migrated data | Old accounts without email verification |
| **Technical failure exception** | System resilience handling | Database connection retry |
| **Manual override** | Intentional human bypass of normal flow | Support agent waiving a fee |
| **Business-approved workaround** | Official non-standard path | Enterprise customer custom pricing |
| **Obsolete residue** | No longer needed exception handling | Code for a deprecated integration |

### Step 2: Business vs. Technical Separation

For each exception, determine:
- Does this reflect a durable business requirement? (Customer will always encounter this scenario)
- Does this reflect a temporary technical constraint? (Would disappear with better implementation)
- Does this reflect historical debt? (Exists because of past decisions, not current needs)

### Step 3: Rationale Recovery

For each business-relevant exception:
- What scenario triggers it?
- What would happen without it?
- Who does it protect (customer, business, partner)?
- How frequently does it occur?
- Was it discovered through an incident, customer report, or proactive design?

### Step 4: Override and Compensation Mapping

Map the system's manual intervention capabilities:
- Who can override normal rules?
- Under what conditions?
- With what audit trail?
- What is the compensation mechanism (undo, credit, manual adjustment)?
- What constraints apply to overrides?

### Step 5: Scoring and Recommendation

For each exception:
- Score on standard dimensions (Business Relevance, Evidence, Purity, Confidence, Convergence)
- Recommend disposition: **Preserve** | **Revise** | **Automate** | **Discard**

---

## Output Format

### 1. Exception Catalog

```
Exception: [Business-language description of the scenario]
Trigger (formal): [Predicate expression for when this exception fires]
Where: [Variable legend for trigger predicate]
Classification: [Customer | Operational | Partner | Compliance | Data | Migration | Technical | Override | Workaround | Obsolete]
Affected Workflow: [Which workflow it interrupts or modifies]
Recovery Behavior: [What the system does]
Actor Responsible: [Who handles it — system, operator, customer?]
Business Reason: [Why this exception must be handled]
Frequency: [High | Medium | Low | Rare-but-critical | Unknown]
Customer Impact: [If not handled, what would the customer experience?]
Evidence: [Code, tickets, incidents, support docs]
Confidence: [1-5]
Recommendation: [Preserve | Revise | Automate | Discard]
Recommendation Rationale: [Why]
```

### 2. Override and Compensation Map

```
Override: [What normal behavior can be bypassed]
Mechanism: [How — admin tool, support action, config change]
Authority: [Who can trigger it]
Audit: [What trail is left]
Constraints: [Limitations — amount caps, approval requirements]
Frequency: [How often used]
Business Justification: [Why this override exists]
```

### 3. Exception Landscape Summary

A high-level view of where exceptions cluster:
- Which workflows have the most exception handling (complexity hotspots)
- Which areas have surprisingly few exceptions (potential gaps or very clean design)
- Patterns in exception types (are most customer-driven? operational? integration?)

### 4. Obsolescence Candidates

Exceptions recommended for discard, with evidence:
```
Exception: [Description]
Reason for obsolescence: [Referenced feature deprecated | Integration removed | Constraint no longer exists]
Evidence: [How we know it's obsolete]
Risk of removal: [What could go wrong if we're wrong]
```

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| Generic error handling | Catch-all exception handlers, 500 pages | Ask: does this handle a specific business scenario or just prevent crashes? |
| Retry mechanisms for infrastructure | Connection pool retries, HTTP retry middleware | Ask: is this business resilience or technical resilience? |
| Developer convenience code | Debug paths, test backdoors, development shortcuts | Ask: would this exist in production? Does it serve customers? |
| Over-engineering | Exception handling for scenarios that never occur | Check: is there evidence this was ever triggered? |
| Defensive programming | Guards against theoretically possible but practically impossible states | Ask: is there a business scenario that reaches this code? |

---

## Validation Criteria

1. **Business justification:** Each preserved exception has a clear business reason.
2. **Scenario clarity:** Each exception describes a concrete scenario, not an abstract failure mode.
3. **Actor identification:** Who handles each exception is explicit.
4. **Disposition decisiveness:** Each exception has a clear recommendation for the future.
5. **Competitive awareness:** Exceptions representing hardest-won operational knowledge are flagged as high-value IP.

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Workflow Still | Exceptions are alternate paths within workflows |
| Rule Still | Exceptions often represent rule violation recovery |
| State Still | Exceptional states are lifecycle branches for abnormal scenarios |
| Permission Still | Overrides are permission-mediated exception paths |
| History Still | Exception additions often trace to specific incidents or discoveries |
| Test Still | Exception paths should have test coverage; gaps are findings |
| Runtime Still | Runtime evidence shows which exceptions actually fire |
