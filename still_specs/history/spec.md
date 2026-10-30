<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The History Still — Specification

## Identity

**Tier:** Evidence
**Primary Question:** Why did the system change over time?
**Role in the Blend:** Provides the temporal dimension — recovered rationale for why the system became what it is, revealing discovered requirements and market learning.

---

## Purpose

The History Still recovers why the system changed. The current codebase is only the final visible layer. Commit history, pull requests, issues, tickets, release notes, and design discussions reveal the path by which the system became what it is. That path often contains the missing business rationale.

The goal is to recover discovered requirements — the business learning that happened after initial implementation and may never have been written back into formal specifications.

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Commit messages | Change rationale | Medium (often terse) |
| Pull request descriptions and discussions | Detailed change reasoning | High |
| Issue/ticket threads | Problem statements and requirements | High |
| Release notes / changelogs | Customer-facing change summaries | High |
| Incident reports / postmortems | Operational learning | High |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Design documents | Architectural decisions and trade-offs | High (if findable) |
| Customer request records | Market-driven requirements | High |
| Support escalation history | Operational pain points | Medium-High |
| Migration/upgrade notes | System evolution decisions | Medium |
| Deprecated code with comments | Abandoned directions | Medium |
| Product decision records (ADRs) | Explicit rationale | High |

---

## Method

### Step 1: Change Stream Identification

Identify available historical sources and their quality:
- Git log availability and message quality
- PR/MR discussion presence
- Issue tracker linkage
- Release note availability
- Document archive accessibility

### Step 2: Thematic Grouping

Group historical changes by business theme, not by file or technical area:
- What business problem triggered this change?
- Who experienced the problem?
- What business rule, workflow, or capability changed?
- Was this change corrective, strategic, regulatory, operational, or defensive?

### Step 3: Discovered Requirement Extraction

For each meaningful change cluster:
- What was the original state?
- What was the triggering event (customer complaint, incident, market change, regulation)?
- What was the resulting requirement?
- Is the requirement durable or was it situational?
- Did it introduce a new concept, modify an existing one, or remove something?

### Step 4: Decision Archaeology

Recover explicit decisions where possible:
- Why was approach A chosen over approach B?
- What constraints shaped the decision?
- What trade-offs were made?
- Are those constraints still valid today?

### Step 5: Pattern Recognition

Identify patterns in the change history:
- Areas that change frequently (under-specified or evolving requirements)
- Changes that were reverted (failed assumptions)
- Repeated similar changes (recurring problems not solved at root)
- Changes clustering around specific events (regulatory deadlines, customer demands)

### Step 6: Obsolescence Detection

Identify decisions and requirements that may no longer be valid:
- Constraints from expired partnerships
- Rules from deprecated pricing models
- Workarounds for resolved infrastructure limitations
- Features for discontinued customer segments

### Step 7: Scoring

Score each discovered requirement:
- Business Relevance (1-5)
- Evidence Diversity (1-5)
- Purity (1-5: 1=impure, 5=pure business intent)
- Confidence (1-5)
- Convergence (1-5, assessed later)

---

## Output Format

### 1. Discovered Requirements Timeline

```
Period: [Date range or release]
Theme: [Business area]
Trigger: [What caused the change — incident, customer, regulation, strategy]
Problem Statement: [What was wrong or needed]
Resulting Requirement: [What the system now must do]
Affected Entities/Workflows: [What changed]
Evidence: [PRs, issues, commits, docs]
Durability Assessment: [Still relevant | Potentially obsolete | Context-dependent]
Confidence: [1-5]
```

### 2. Decision Record

```
Decision: [What was decided]
Date: [When]
Context: [What prompted the decision]
Options Considered: [If visible in discussion]
Rationale: [Why this choice was made]
Trade-offs: [What was sacrificed]
Constraints: [What forced the decision]
Still Valid: [Yes | Probably | Uncertain | Likely obsolete]
Evidence: [Where documented]
```

### 3. Evolution Patterns

```
Pattern: [Description of recurring change pattern]
Frequency: [How often this pattern appears]
Interpretation: [What it likely means — evolving requirement, technical debt, etc.]
Affected Area: [Domain concepts involved]
```

### 4. Superseded Requirements

Requirements that were once valid but have been replaced or may no longer apply:
```
Original Requirement: [What was true before]
Replaced By: [What is true now]
Trigger for Change: [Why it was superseded]
Risk if Preserved: [What goes wrong if the old requirement is kept]
```

### 5. Rationale Gaps

Areas where changes were made but the business rationale is not recoverable:
```
Change: [What happened]
Date: [When]
Available Evidence: [What we have]
Missing Context: [What we can't determine]
Recommendation: [Verify with stakeholders | Accept uncertainty | Investigate further]
```

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| Refactoring noise | Commits that restructure without changing behavior | Ask: did business behavior change, or only code structure? |
| Dependency updates | Version bumps, security patches | Ask: did this change product capability or only infrastructure? |
| Formatting/style changes | Linting, code style enforcement | Skip unless they reveal naming/terminology decisions |
| Incomplete commit messages | "fix bug", "wip", "update" | Look to associated PR description, issue, or test changes for context |
| Conflated commits | Multiple unrelated changes in one commit | Decompose by examining file changes and tests |

---

## Validation Criteria

1. **Rationale recovery:** Most major system capabilities have traceable origins.
2. **Business language:** Discovered requirements are expressed as business needs, not code changes.
3. **Durability assessment:** Each requirement is assessed for current relevance.
4. **Pattern insight:** Recurring changes reveal evolving or under-specified areas.
5. **Decision clarity:** Major architectural and product decisions are documented with available rationale.

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Rule Still | History reveals why rules were added |
| Exception Still | Exception additions often trace to specific incidents |
| State Still | State model changes reveal evolving business processes |
| Data Still | Schema evolution reveals business model changes |
| Permission Still | Permission additions often trace to customer requests or incidents |
| Test Still | Regression tests align with historical bug discoveries |
| Domain Language Still | Terminology changes reveal concept evolution |
