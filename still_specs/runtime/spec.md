<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Runtime Still — Specification

## Identity

**Tier:** Grounding
**Primary Question:** What does the system actually do in production-like use?
**Role in the Blend:** Grounds the specification in observed reality — confirming which behaviors are exercised, which are dormant, and where the system's actual behavior contradicts its apparent design.

---

## Purpose

The Runtime Still observes what the system actually does when used. Source code contains possible behavior; runtime evidence reveals actual behavior. Production-like traces, logs, analytics, and telemetry show which workflows are common, which exceptions occur, where users struggle, and which parts of the system carry operational weight.

The goal is to use observed behavior as evidence without confusing frequency with importance. A rarely-used but business-critical capability (enterprise deal closer, compliance process) must not be dismissed because of low frequency.

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Application logs | Actual execution paths and errors | High |
| Request/transaction traces | End-to-end workflow execution | High |
| Analytics events | User behavior patterns | High |
| Error/exception monitoring | Real failure modes | High |
| Audit trails | Authority-exercised behavior | Medium-High |
| Feature usage metrics | Capability adoption | High |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Performance dashboards | Load patterns and bottlenecks | Medium |
| Queue depths and processing rates | Async workflow throughput | Medium |
| Support ticket volume by area | Operational pain points | Medium-High |
| Customer journey analytics | Actual user paths | High |
| A/B test results | Behavioral preferences | Medium |
| Synthetic monitoring | Probed behavior assertions | Medium |

### Privacy and Security Note

The Runtime Still does not require exposing sensitive customer data. It requires behavioral patterns: what paths are taken, how frequently, where failures occur, and what operational interventions happen. Aggregate patterns, anonymized traces, and de-identified metrics serve the purpose.

---

## Method

### Step 1: Behavioral Pattern Identification

Map observed activity back to domain concepts:
- Which workflows are executed most frequently?
- Which workflows are rare but occur?
- Which code paths are never exercised in practice?
- What is the actual distribution of state transitions?

### Step 2: Exception and Failure Mapping

Identify actual failure modes:
- What errors occur in production?
- How frequently?
- In which workflows?
- How are they resolved?
- Do observed exceptions match the Exception Still's catalog?

### Step 3: Operational Reality Check

Identify where reality contradicts design:
- Features that exist in code but are never used
- Workflows that users abandon before completion
- Behaviors that require regular manual intervention
- Workarounds that operators perform routinely
- Capabilities that generate support volume

### Step 4: Usage Tiering

Classify system capabilities by actual usage:
- **High frequency:** Core daily operations
- **Medium frequency:** Regular but not constant
- **Low frequency:** Periodic (monthly, quarterly, annual)
- **Rare but critical:** Infrequent but high-impact (compliance, enterprise features)
- **Dormant:** Code exists but no evidence of use

### Step 5: Operational Dependency Detection

Identify operational behaviors not visible in code alone:
- Manual runbook steps
- Periodic maintenance operations
- Data correction procedures
- Monitoring and alerting patterns
- Capacity management routines

### Step 6: Scoring

Score each finding:
- Business Relevance (1-5)
- Evidence Diversity (1-5)
- Purity (1-5: 1=impure, 5=pure business intent)
- Confidence (1-5)
- Convergence (1-5, assessed later)

---

## Output Format

### 1. Observed Behavior Report

```
Workflow/Capability: [Business name]
Usage Tier: [High | Medium | Low | Rare-critical | Dormant]
Frequency: [Quantified if possible]
Actual Path: [What users actually do — may differ from designed path]
Completion Rate: [If measurable — how often the workflow succeeds end-to-end]
Common Failure Points: [Where it breaks]
Manual Intervention Required: [If any — what operators do]
Evidence: [Logs, metrics, analytics]
Confidence: [1-5]
```

### 2. Reality vs. Design Discrepancies

```
Discrepancy: [Description]
Designed behavior: [What the code/spec intends]
Actual behavior: [What really happens]
Likely explanation: [Poor UX | Missing capability | Workaround | Bug | Changed requirements]
Business impact: [What this means for the spec]
Recommendation: [Spec the actual behavior | Fix the implementation | Investigate further]
```

### 3. Operational Dependency Map

```
Operation: [What operators/support do]
Frequency: [How often]
Trigger: [What prompts this intervention]
Impact: [What it affects]
Automation opportunity: [Could this be systemic?]
Business requirement it reveals: [What need does this manual step serve?]
```

### 4. Usage Tier Summary

Overview of capabilities by actual usage level, highlighting:
- High-use capabilities that must be preserved
- Dormant capabilities that may be candidates for removal
- Rare-but-critical capabilities that must not be lost despite low frequency

### 5. Contradictions Register

Behaviors where code-level analysis and runtime evidence disagree.

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| Recency bias | Overweighting recent usage patterns, missing seasonal behavior | Ask: does usage vary by time of year, campaign, or business cycle? |
| Frequency bias | Treating low-usage as unimportant | Ask: is this a compliance, enterprise, or strategic capability that's rare but essential? |
| Bot/automated traffic | Traffic from monitoring, scrapers, or integrations distorting usage | Separate human-initiated from system-initiated activity |
| Test environment noise | Staging or QA traffic in production-like logs | Verify data source represents real customer behavior |
| Survivorship bias | Only seeing users who succeeded, missing those who gave up | Look for abandoned workflows, not just completed ones |

---

## Validation Criteria

1. **Grounded in evidence:** Every claim is supported by observable data.
2. **Usage clarity:** Capabilities are clearly tiered by actual usage.
3. **Contradiction awareness:** Design-vs-reality gaps are identified.
4. **Operational completeness:** Manual operations and interventions are documented.
5. **Balanced assessment:** Rare-but-critical capabilities are not dismissed.

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Workflow Still | Runtime confirms which workflows are actually exercised |
| Exception Still | Runtime shows which exceptions actually fire and how often |
| State Still | Runtime reveals actual state distributions and transition frequencies |
| Test Still | Runtime coverage vs. test coverage reveals protection gaps |
| Rule Still | Runtime shows which rules are triggered frequently vs. rarely |
| Interface Still | API analytics show which interfaces are actively used |
| Redistillation Still | Runtime evidence grounds the redistillation comparison |
