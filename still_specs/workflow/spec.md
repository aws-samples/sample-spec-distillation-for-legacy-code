<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Workflow Still — Specification

## Identity

**Tier:** Behavior
**Primary Question:** What work is the system helping users complete?
**Role in the Blend:** Provides the process backbone that connects domain concepts, rules, states, and permissions into coherent user journeys.

---

## Purpose

The Workflow Still identifies the work the system enables. It recovers user journeys, business processes, task sequences, handoffs, approvals, and operational loops — the movement through the system that creates value.

Where the Domain Language Still focuses on nouns and the Rule Still focuses on constraints, the Workflow Still focuses on flow. It answers: what are people (and systems) trying to accomplish, and what path does the system provide?

The goal is to move beyond feature lists and screen inventories into a specification of the work the system supports and why that work matters.

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Route maps / controllers | Entry points, action sequences, response paths | High |
| UI screens and page transitions | User journey structure | High |
| Frontend components and forms | Decision points, required inputs | Medium-High |
| Command/service handlers | Business operation orchestration | High |
| State transitions and workflow engines | Formalized process flows | High |
| Background/async jobs | System-initiated process steps | Medium-High |
| Sequence/integration tests | End-to-end behavior paths | High |
| Acceptance/BDD tests | Scenario-level workflow descriptions | High |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Analytics funnels | Observed user paths | Medium-High |
| Help documentation | Documented task instructions | High |
| Support tickets | Workflow failure points and confusion areas | Medium |
| Onboarding docs | Happy-path workflow descriptions | Medium-High |
| Email/notification templates | Handoff and status communication points | Medium |
| Admin panels | Operator workflows distinct from user workflows | Medium |

---

## Method

### Step 1: Entry Point Identification

Identify all ways work begins in the system:

- User-initiated actions (clicks, form submissions, API calls)
- System-initiated triggers (scheduled jobs, event handlers, webhooks)
- External triggers (partner callbacks, incoming integrations)
- Operator interventions (admin actions, support tools)

### Step 2: Path Reconstruction

For each entry point, trace the path forward:

- What is the user/system trying to accomplish?
- What steps must occur in sequence?
- What decisions are made along the way?
- What business object changes state?
- Where are there branches (alternate paths)?
- Where does the workflow terminate (success, failure, abandonment)?

### Step 3: Actor Identification

For each workflow, identify:

- Who initiates it?
- Who participates at each step?
- Where do handoffs occur between actors?
- Are there approval gates (one actor waits for another)?
- Are there system-actor steps (automated processing between human steps)?

### Step 4: Outcome Classification

For each workflow, classify outcomes:

- **Success:** The intended value was delivered
- **Partial success:** Some value delivered, some steps incomplete
- **Failure:** The workflow could not complete; what happens next?
- **Abandonment:** The user left mid-workflow; what is the system state?
- **Exception:** An unusual case required non-standard handling

### Step 5: Business Reason Annotation

For each workflow, articulate:

- Why does this workflow exist? What business value does it produce?
- What would happen if this workflow were removed?
- Is this workflow customer-facing, operator-facing, or system-internal?
- Is this a primary workflow (core to the product) or supporting (administrative, corrective)?

### Step 6: Implementation Filtering

Strip implementation-specific details from workflow descriptions:

- Remove references to specific UI components, page names, or URL structures
- Remove references to specific technical mechanisms (queues, caches, HTTP methods)
- Preserve the business sequence, decision points, and outcomes
- Test: would this workflow description remain valid if the system were rebuilt with entirely different technology?

### Step 7: Scoring

Score each workflow on:
- Business Relevance (1-5)
- Evidence Diversity (1-5)
- Purity (1-5: 1=impure, 5=pure business intent)
- Confidence (1-5)
- Convergence with other stills (1-5, assessed later)

---

## Output Format

### 1. Workflow Registry

For each identified workflow:

```
Workflow: [Business Name]
Type: [User-initiated | System-initiated | Operator | External-triggered]
Primary Actor: [Who starts it]
Business Purpose: [Why it exists — what value it produces]
Trigger: [What starts the workflow]
Preconditions: [What must be true before the workflow can begin]

Main Path:
1. [Step — actor — action — outcome]
2. [Step — actor — action — outcome]
...

Alternate Paths:
- [Condition] → [Different sequence]
...

Failure Paths:
- [Failure condition] → [What happens]
...

Success Outcome: [What state exists when the workflow completes successfully]
Business Objects Affected: [What changes]
Handoffs: [Where one actor passes to another]
Notifications: [What communications occur and when]
Estimated Frequency: [If inferable — high/medium/low usage]
Confidence: [1-5]
Open Questions: [Ambiguities]
```

### 2. Workflow Dependency Map

Which workflows depend on, trigger, or follow other workflows, expressed as formal sequence notation:

```
completion(Workflow_A) → triggers → initiation(Workflow_B)
initiation(Workflow_C) REQUIRES completion(Workflow_D)
Workflow_E = exception_path_within(Workflow_F)
```

Decision points within workflows expressed as formal predicates:
```
branch = { path_A    where [predicate]
         { path_B    where [predicate]

Where:
• [variable definitions]
```

### 3. Actor-Workflow Matrix

Which actors participate in which workflows, and in what capacity:

```
| Actor | Workflow | Role in Workflow |
|---|---|---|
| Customer | Checkout | Initiator, primary actor |
| Payment Provider | Checkout | Processing step (system handoff) |
| Fulfillment Team | Order Processing | Receives handoff from checkout |
```

### 4. Gap Analysis

Workflows that appear incomplete, undocumented, or suspicious:
- Entry points with no clear path forward
- Workflows that dead-end without clear outcome
- Actor handoffs with no notification mechanism
- High-complexity areas with minimal test coverage

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| UI-specific sequencing | Steps that exist because of current page structure, not business need | Ask: would this step exist in a CLI version? A mobile version? An API-only version? |
| Technical processing steps | "Job enqueued," "cache invalidated," "webhook dispatched" | Ask: does the user or business care about this step, or only about its outcome? |
| Framework-imposed patterns | Steps that exist because the framework requires them (CSRF confirmation, session handling) | Ask: does this step represent a business decision or a technical mechanism? |
| Current-UX bias | Confusing "how users currently do it" with "what users need to accomplish" | Ask: if we redesigned the UI completely, would this step still be necessary? |
| Missing off-system workflows | Steps that happen outside the application (phone calls, spreadsheets, emails) | Look for workflow gaps: where does the system hand off and not track completion? |

---

## Validation Criteria

1. **Stakeholder recognition:** Users and operators would recognize these as their actual workflows.
2. **Implementation neutrality:** Workflows remain valid regardless of technology stack.
3. **Completeness signal:** Every major screen, endpoint, and feature exists to serve at least one workflow.
4. **Business justification:** Every workflow has a stated business purpose.
5. **Exception coverage:** Failure and alternate paths are documented, not just happy paths.

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Domain Language | Workflows use vocabulary from the Domain Language Still |
| Rule Still | Rules constrain and gate workflow steps |
| Permission Still | Permissions determine who can perform which workflow steps |
| State Still | Workflows drive state transitions; states gate workflow progression |
| Exception Still | Exception paths are alternate workflow branches |
| Test Still | Tests often encode complete workflow scenarios |
| History Still | Workflow changes reveal evolving business processes |
| Runtime Still | Runtime data shows which workflows are actually used |
