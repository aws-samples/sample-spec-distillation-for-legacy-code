<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The State Still — Specification

## Identity

**Tier:** Behavior
**Primary Question:** What lifecycle does each business object move through?
**Role in the Blend:** Provides the temporal dimension of business objects — what states they occupy, what transitions are meaningful, and what obligations attach to each state.

---

## Purpose

The State Still recovers the lifecycle of important business objects. It asks: what states can a business object occupy, what causes movement between states, and what does each state mean to the business?

State models are among the most valuable hidden specifications in a system. Orders, tickets, invoices, shipments, projects, claims, subscriptions, accounts, approvals, and payments all have lifecycles. Those lifecycles encode business expectations, operational controls, customer promises, and compliance obligations.

The goal is to recover the meaning of state, not merely list enum values. Each state carries business significance: what obligations begin, what promises are made, what actions become available or locked.

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Status/state fields (enums, columns) | State vocabulary | High |
| State machines / transition definitions | Allowed transitions | High |
| Workflow engine configurations | Formalized lifecycle flows | High |
| Event handlers triggered by state change | Side effects of transitions | High |
| Lifecycle callbacks (before/after hooks) | Business logic at state boundaries | High |
| Guard clauses on state transitions | Transition preconditions | High |
| Database status migration scripts | State model evolution | Medium-High |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| UI labels for status | Customer-facing state names | High |
| Notification templates | What's communicated at each state | Medium-High |
| Background jobs triggered by state | Automated processing per state | Medium |
| Reporting/dashboard filters by status | Operationally meaningful states | Medium |
| Tests asserting state behavior | Protected transition rules | High |
| Audit logs of state changes | Actual transition patterns | Medium-High |

---

## Method

### Step 1: Entity-State Inventory

Identify all business objects that have lifecycle states. Look for:
- Explicit status/state fields
- Enum definitions for status values
- State machine implementations
- Workflow engine configurations
- Objects with boolean flags that function as state (is_active, is_verified, is_locked)

### Step 2: State Definition

For each state of each entity, define its business meaning:
- What is true about the business object in this state?
- What customer expectation exists?
- What obligations are active?
- What actions are available?
- What actions are locked/forbidden?
- Is this state visible to the customer? How is it communicated?

### Step 3: Transition Mapping

For each transition between states:
- What triggers the transition?
- Who/what can trigger it (permission)?
- What preconditions must be met (guards)?
- What happens as a result (side effects)?
- Is the transition reversible?
- What notifications fire?

### Step 4: Terminal and Exceptional States

Identify:
- Terminal states (no transitions out — lifecycle complete)
- Exceptional states (error, cancelled, suspended — not normal flow)
- Reversible vs. irreversible transitions
- States that reset or restart the lifecycle
- Zombie states (theoretically reachable but practically dead-end)

### Step 5: State-Dependent Behavior

Identify behaviors that change based on state:
- Different permissions per state
- Different available actions per state
- Different display/communication per state
- Different rules that apply per state
- Time-based expectations per state (SLAs, timeouts)

### Step 6: Scoring

Score each lifecycle model:
- Business Relevance (1-5)
- Evidence Diversity (1-5)
- Purity (1-5: 1=impure, 5=pure business intent)
- Confidence (1-5)
- Convergence (1-5, assessed later)

---

## Output Format

### 1. Lifecycle Registry

For each business object with a lifecycle:

```
Entity: [Business Name]
States: [List of all states]
Initial State: [Where objects begin]
Terminal States: [Where lifecycles end]
Typical Path: [Most common state progression]

State Definitions:
- [State A]: [Business meaning — what is true in this state]
  - Customer visibility: [How/whether customer sees this]
  - Available actions: [What can be done]
  - Locked actions: [What cannot be done]
  - Active obligations: [What must happen or be happening]
  - SLA/timeout: [Time expectations, if any]
...
```

### 2. Transition Specification

Transitions expressed as formal state guards per the Formal Representation Convention:

```
ALLOW transition([Entity]: [From State] → [To State])
  WHERE [guard predicate]

Where:
• [variable definitions with business meaning]

Trigger: [What causes this transition]
Actor: [Who/what can trigger it]
Side Effects: [What happens as a result — notifications, downstream triggers]
Reversible: [Yes/No — and if yes, how]
Business Meaning: [What this transition represents]
Confidence: [1-5]
```

### 3. Lifecycle Diagrams (Textual)

For each major entity, a text-based state diagram:

```
[Created] → (submit) → [Pending Review] → (approve) → [Active]
                                        → (reject) → [Rejected]
[Active] → (cancel) → [Cancelled]
[Active] → (expire) → [Expired]
[Active] → (suspend) → [Suspended] → (reactivate) → [Active]
```

### 4. Cross-Entity State Dependencies

Where one entity's state depends on or triggers another entity's state change:

```
When [Order] enters [Shipped]: [Shipment] must be in [In Transit]
When [Payment] enters [Failed]: [Order] transitions to [Payment Pending]
```

### 5. State Model Gaps

- States with unclear business meaning
- Transitions with no documented trigger or guard
- States that appear in code but never in UI or customer communication
- Missing states (expected lifecycle stages not represented)

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| Technical processing states | "queued", "processing", "retrying" | Ask: does the customer or operator care about this state, or only the system? |
| Implementation-specific states | States created for batch migrations or data fixes | Ask: is this a permanent business state or a transient technical one? |
| Overloaded status fields | Single field encoding multiple independent states | Decompose: identify which aspects are truly independent |
| Boolean states masquerading as rich lifecycles | is_active, is_verified, is_locked | Ask: are these independent binary states or part of a richer lifecycle? |
| Unreachable states | Defined in code but never transitioned to in practice | Check runtime evidence or test coverage |

---

## Validation Criteria

1. **Stakeholder recognition:** Business stakeholders can reason about the system using these state models.
2. **Customer-facing accuracy:** States visible to customers match how they're communicated.
3. **Obligation clarity:** Each state's active obligations are explicit.
4. **Transition completeness:** All valid transitions are documented with triggers and guards.
5. **Business meaning:** Every state carries meaning beyond "this is a column value."

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Workflow Still | Workflows drive state transitions; states gate workflow progression |
| Rule Still | Transition guards are rules; state-dependent rules reference state |
| Permission Still | Permissions may vary by state |
| Exception Still | Exceptional states represent recovered/failed business scenarios |
| Data Still | State fields and transition history are persisted data with business meaning |
| Test Still | State transition tests are high-value behavioral assertions |
| Runtime Still | Actual state distributions reveal which states matter in practice |
