<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Permission Still — Specification

## Identity

**Tier:** Constraints
**Primary Question:** Who is allowed to do what, under which conditions, with what delegation paths?
**Role in the Blend:** Recovers the organizational power structure, trust boundaries, and authority hierarchy that govern access, actions, and data visibility.

---

## Purpose

The Permission Still recovers the authorization model encoded in the system — the organizational topology of actors, capabilities, contexts, and boundaries. It is distinct from the Rule Still because it captures an entire relational structure of power, not just individual constraints.

A business rule says "a refund cannot exceed the captured amount." A permission says "only a regional manager or above can approve a refund above the manager-approval threshold, and only within their own territory." The permission model defines who has authority over what, under which circumstances, with what escalation and delegation paths.

Authorization models in mature systems are among the densest encodings of business hierarchy, trust, and organizational rules. They often represent the hardest-won customer learning — added after incidents, enterprise customer requirements, regulatory audits, or security breaches.

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Role definitions | Organizational positions and their capabilities | High |
| Permission/policy tables | Explicit capability grants | High |
| Access control middleware/guards | Enforcement points | High |
| Tenant isolation logic | Data and action boundaries | High |
| Row-level security / scoping | Data visibility boundaries | High |
| Approval workflow configurations | Authority hierarchies | High |
| Admin panel access controls | Operator capability tiers | Medium-High |
| OAuth scopes / API key permissions | External access boundaries | High |
| Feature entitlements / subscription tiers | Capability gating by plan | High |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Audit logs of access decisions | Historical authorization patterns | Medium-High |
| Support escalation paths | Informal authority hierarchies | Medium |
| Enterprise sales requirements | Customer-demanded permission features | High |
| Compliance documentation (SOC2, etc.) | Required access controls | High |
| Permission-related error messages | Boundary communication | Medium |
| Tests for authorization | Protected access assertions | High |
| Customer-facing permission docs | Explained access model | High |

---

## Method

### Step 1: Role and Actor Inventory

Identify all roles, actors, and identity types the system recognizes:
- Named roles (admin, manager, member, viewer, etc.)
- System actors (background processes, integration partners, API consumers)
- Implicit roles (the owner of a resource, the creator of a record)
- Hierarchical relationships between roles (inheritance, escalation)

### Step 2: Capability Mapping

For each role/actor, map what they can do:
- What actions are permitted
- What data is visible
- What administrative functions are available
- What is explicitly denied (prohibitions)
- What requires additional conditions beyond role membership

### Step 3: Boundary Detection

Identify organizational and data boundaries:
- **Tenant boundaries:** What isolates one customer/organization from another?
- **Geographic boundaries:** What restricts access by region/country?
- **Hierarchical boundaries:** What restricts access by organizational position?
- **Ownership boundaries:** What restricts access to the creator/owner of a resource?
- **Temporal boundaries:** What permissions expire, rotate, or change with time?

### Step 4: Delegation and Escalation

Identify how authority flows:
- Can permissions be delegated? Under what constraints?
- Can users act on behalf of others? With what audit trail?
- What approval chains exist (multiple actors required)?
- How does escalation work when normal authority is insufficient?
- Are there emergency/break-glass procedures?

### Step 5: Conditional Permissions

Identify permissions that depend on context:
- Amount-based: different authority levels for different transaction sizes
- Time-based: permissions that apply only during certain periods
- State-based: permissions that depend on the current state of an object
- Context-based: permissions that depend on the relationship between actor and object

### Step 6: Entitlement and Subscription Logic

If the system has tiered access:
- What capabilities are gated by subscription level?
- What features are available to which plan/tier?
- What quotas or limits vary by entitlement?
- How does entitlement interact with role-based permissions?

### Step 7: Scoring

Score each finding:
- Business Relevance (1-5)
- Evidence Diversity (1-5)
- Purity (1-5: 1=impure, 5=pure business intent)
- Confidence (1-5)
- Convergence (1-5, assessed later)

---

## Output Format

### 1. Role and Actor Registry

```
Role: [Business Name]
Business Definition: [What this role represents organizationally]
Inherits From: [Parent role, if hierarchical]
Primary Capabilities: [What this role can do]
Primary Restrictions: [What this role cannot do]
Data Visibility: [What this role can see]
Typical Human Context: [Who in the real world holds this role]
Evidence: [Where defined in system]
Confidence: [1-5]
```

### 2. Permission Matrix

A structured matrix of roles × capabilities × conditions, expressed formally:

```
PERMIT [capability]
  BY [role conditions]
  WHERE [additional context]

Where:
• [variable definitions]
```

Example structure:
```
PERMIT approve(refund)
  BY actor.role ≥ "manager"
  WHERE actor.region = refund.region ∧ refund.amount ≤ actor.approval_limit

Where:
• actor.role = actor's organizational role (hierarchical: agent < manager < director)
• actor.region = geographic region assigned to actor
• refund.region = region where the refund originated
• actor.approval_limit = maximum refund value this actor can approve (currency)
```

### 3. Boundary Specification

```
Boundary: [Description]
Type: [Tenant | Geographic | Hierarchical | Ownership | Temporal]
Enforcement: [How maintained]
What it isolates: [What data/actions are scoped]
What crosses it: [Exceptions that bridge the boundary]
Business Reason: [Why this separation exists]
Failure Mode: [What goes wrong if boundary is breached]
Confidence: [1-5]
```

### 4. Delegation and Escalation Map

```
Delegation: [Description]
From Role: [Delegator]
To Role: [Delegate]
Constraints: [Limitations on delegated authority]
Audit: [What tracking exists]
Expiration: [Time-bounded or permanent]
Business Reason: [Why delegation is needed]
```

### 5. Entitlement Model (if applicable)

```
Tier: [Plan/subscription level]
Capabilities Unlocked: [What this tier provides]
Limits: [Quotas, rate limits, storage caps]
Upgrade Triggers: [What drives customers to the next tier]
Business Rationale: [Why this tiering exists]
```

### 6. Permission Evolution Notes

Permissions that were added over time in response to specific business events:
- Enterprise customer requirements
- Security incidents
- Regulatory audits
- Customer complaints about data visibility
- Partner integration requirements

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| Framework-level auth | Session management, CSRF, cookie handling | Ask: does this govern business capability or prevent technical exploits? |
| Infrastructure access | Server SSH keys, database passwords, deployment permissions | Ask: is this about what the product allows, or what the infrastructure requires? |
| Over-granular technical permissions | `read:model:field` level permissions that reflect code structure | Ask: would a business user understand this permission unit? Consolidate to capability level. |
| Legacy permission artifacts | Roles from merged systems or deprecated features | Check: is this role assigned to active users? Is it referenced in current workflows? |
| Overly permissive defaults | Admin roles that can do everything | This IS a finding — note it as a potential business risk, not an impurity to filter |

---

## Validation Criteria

1. **Organizational recognition:** Business stakeholders would recognize the role hierarchy as reflecting reality.
2. **Boundary completeness:** All data and action boundaries are identified and justified.
3. **Delegation clarity:** How authority flows is explicit, not implicit.
4. **Business justification:** Every permission boundary has a stated business reason.
5. **Failure mode awareness:** The consequences of boundary violation are articulated.

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Rule Still | Permission rules are a specialized subset; the Permission Still goes deeper into authority topology |
| Workflow Still | Permission gates workflow steps; approval chains are permission-mediated workflows |
| State Still | Permissions may be state-dependent (different permissions in different lifecycle stages) |
| Exception Still | Override and escalation paths are permission exceptions |
| Data Still | Data visibility boundaries are permission-enforced |
| Interface Still | API scopes and entitlements are externally visible permission structures |
| History Still | Permission additions often trace to specific incidents or customer requests |
