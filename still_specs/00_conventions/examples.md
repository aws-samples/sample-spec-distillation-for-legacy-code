<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# Formal Representation — Examples Library

## Purpose

This document provides reference examples of the formal representation convention applied to different functional logic types. Each example shows the progression from natural language or implementation-style expression to proper formal notation.

---

## 1. Calculation Examples

### Example 1A: Simple Calculation

**Natural language:** "The line item subtotal is the unit price times the quantity"

**Formal:**
```
S = P × Q

Where:
• S = line item subtotal (currency)
• P = unit price of the product (currency)
• Q = quantity ordered (positive integer)
```

---

### Example 1B: Piecewise Calculation

**Natural language:** "Shipping is free for orders over a threshold, otherwise a flat standard fee applies"

**Formal:**
```
shipping_cost = { 0        where order_total ≥ free_threshold
                { base_fee  where order_total < free_threshold

Where:
• shipping_cost = amount charged for standard shipping (currency)
• order_total = sum of all line item subtotals before shipping (currency)
• free_threshold = minimum order value for free shipping (currency parameter)
• base_fee = standard shipping charge (currency parameter)
```

---

### Example 1C: Aggregation Calculation

**Natural language:** "The order total is the sum of all line items plus tax plus shipping minus any discounts applied"

**Formal:**
```
T = ∑(S) + tax + shipping - D

Where:
• T = order total (currency, ≥ 0)
• ∑(S) = sum of all line item subtotals in the order
• tax = calculated tax amount (see Tax Rule)
• shipping = calculated shipping cost (see Shipping Rule)
• D = total discount amount applied (currency, ≥ 0)
```

---

## 2. Constraint / Invariant Examples

### Example 2A: Simple Constraint

**Natural language:** "A refund can't be more than what was originally paid"

**Formal:**
```
REQUIRE refund_amount ≤ captured_amount

Where:
• refund_amount = amount being refunded to customer (currency, > 0)
• captured_amount = total payment successfully captured for this order (currency)
```

---

### Example 2B: Uniqueness Constraint

**Natural language:** "No two users in the same organization can have the same email"

**Formal:**
```
REQUIRE ∀ users u₁, u₂ in organization O:
  u₁ ≠ u₂ → u₁.email ≠ u₂.email

Where:
• O = an organization (tenant boundary)
• u₁, u₂ = any two distinct user accounts within O
• email = user's login email address
```

---

### Example 2C: Temporal Constraint

**Natural language:** "An invoice must be paid within 30 days of issue or it becomes overdue"

**Formal:**
```
REQUIRE payment_date ≤ issue_date + grace_period
  ELSE status transitions to "overdue"

Where:
• payment_date = date full payment is received
• issue_date = date the invoice was issued to customer
• grace_period = maximum allowed payment window (30 days)
```

---

## 3. Eligibility / Composed Condition Examples

### Example 3A: Simple Eligibility

**Natural language:** "Customers qualify for the loyalty program if they've been active for at least a year and have spent over a spend threshold"

**Formal:**
```
loyalty_eligible = tenure_met ∧ spend_met

Where:
• loyalty_eligible = customer qualifies for loyalty program enrollment
• tenure_met = customer.account_age ≥ min_tenure
• spend_met = customer.lifetime_spend > min_spend_threshold
```

---

### Example 3B: Factored Complex Eligibility

**Natural language:** "A promotional offer applies if the customer is in good standing, the product is eligible, the promotion period is active, and the customer hasn't already used this promotion"

**Formal:**
```
promo_applicable = customer_qualified ∧ product_qualified ∧ timing_valid ∧ ¬already_used

Where:
• promo_applicable = this promotion can be applied to this order
• customer_qualified = customer.status = "active" ∧ customer.account_age ≥ 30 days
• product_qualified = product.category ∈ promotion.eligible_categories
• timing_valid = current_date ≥ promotion.start_date ∧ current_date ≤ promotion.end_date
• already_used = ∃ prior redemption of this promotion by this customer
```

---

### Example 3C: Deeply Factored Eligibility

**Natural language:** "An employee can submit an expense claim if they are an active employee, the expense category is allowed for their department, the amount is within their approval tier, and the receipt is attached"

**Formal:**
```
can_submit = employee_active ∧ category_allowed ∧ within_limit ∧ documentation_complete

Where:
• can_submit = claim is ready for submission
• employee_active = employee.status = "active" ∧ employee.termination_date = null
• category_allowed = expense.category ∈ department.permitted_categories
• within_limit = expense.amount ≤ tier_limit(employee.level)
• documentation_complete = receipt_attached ∧ description.length ≥ min_description_length

Supporting definitions:
• tier_limit(level) = { limit_low    where level ∈ {junior, mid}
                      { limit_mid    where level ∈ {senior, lead}
                      { limit_high   where level ∈ {director, vp}
  (each limit_* is a currency parameter recovered from the system, not a fixed literal)
```

---

## 4. Permission Examples

### Example 4A: Simple Permission

**Natural language:** "Only admins can delete customer accounts"

**Formal:**
```
PERMIT delete(customer_account)
  BY actor.role = "admin"

Where:
• customer_account = a registered customer record
• actor = the user attempting the operation
• actor.role = the actor's assigned system role
```

---

### Example 4B: Contextual Permission

**Natural language:** "Team members can edit projects they belong to, but only managers can edit any project in their department"

**Formal:**
```
PERMIT edit(project)
  WHERE member_access ∨ manager_access

Where:
• member_access = actor ∈ project.team_members
• manager_access = actor.role = "manager" ∧ actor.department = project.department
```

---

### Example 4C: Threshold Permission with Delegation

**Natural language:** "Small purchase orders can be approved by any manager. Mid-range orders require director approval. Large orders require VP approval or a delegated approval from a VP."

**Formal:**
```
PERMIT approve(purchase_order)
  WHERE authority_sufficient

authority_sufficient = { actor.role ≥ "manager"                              where amount < manager_limit
                       { actor.role ≥ "director"                             where manager_limit ≤ amount < director_limit
                       { actor.role ≥ "vp" ∨ delegation.from_role ≥ "vp"    where amount ≥ director_limit

Where:
• amount = purchase_order.total_value (currency)
• manager_limit, director_limit = currency band thresholds recovered from the system
• actor.role = role with hierarchical ordering: manager < director < vp
• delegation = active delegation grant from a higher authority to actor
• delegation.from_role = the role of the person who delegated authority
```

---

## 5. Sequence Examples

### Example 5A: Simple Sequence

**Natural language:** "Payment must be captured before the order can be shipped"

**Formal:**
```
capture(payment) → before → initiate(shipment)

Where:
• capture(payment) = successful payment authorization and capture for the order
• initiate(shipment) = creating a shipment record and notifying fulfillment
```

---

### Example 5B: Multi-Step Sequence

**Natural language:** "Account verification requires email confirmation, then identity check, then approval"

**Formal:**
```
confirm(email) → verify(identity) → approve(account)

Where:
• confirm(email) = user clicks verification link; email marked as confirmed
• verify(identity) = identity documents reviewed and accepted
• approve(account) = account status transitions to "active"; full access granted

Constraint: each step REQUIRES previous step status = "complete"
```

---

### Example 5C: Parallel Prerequisites

**Natural language:** "An order can only ship when both payment is captured AND inventory is allocated"

**Formal:**
```
initiate(shipment) REQUIRES payment_captured ∧ inventory_allocated

Where:
• payment_captured = payment.status = "captured" for this order
• inventory_allocated = all line items have reserved inventory in warehouse
• initiate(shipment) = shipment created and ready for fulfillment
```

---

## 6. State Guard Examples

### Example 6A: Simple Guard

**Natural language:** "An order can only be cancelled if it hasn't been shipped yet"

**Formal:**
```
ALLOW transition(order: any_state → "cancelled")
  WHERE order.shipment_status ∉ {"shipped", "delivered"}

Where:
• any_state = any non-terminal order state
• shipment_status = current fulfillment status of the order
```

---

### Example 6B: Permission + State Guard

**Natural language:** "Only the assigned reviewer or an admin can move a document from 'in review' to 'approved'"

**Formal:**
```
ALLOW transition(document: "in_review" → "approved")
  WHERE actor = document.assigned_reviewer ∨ actor.role = "admin"

Where:
• document.assigned_reviewer = the user responsible for reviewing this document
• actor = the user attempting the state change
• actor.role = the actor's system role
```

---

### Example 6C: Compound State Guard

**Natural language:** "A subscription can renew only if payment method is valid, the customer hasn't requested cancellation, and it's within the renewal window"

**Formal:**
```
ALLOW transition(subscription: "active" → "renewed")
  WHERE renewal_eligible

renewal_eligible = payment_valid ∧ ¬cancellation_requested ∧ in_renewal_window

Where:
• payment_valid = customer.default_payment_method.status = "valid"
• cancellation_requested = ∃ pending cancellation request for this subscription
• in_renewal_window = current_date ≥ subscription.renewal_date - 7 days
                    ∧ current_date ≤ subscription.renewal_date + 3 days
```

---

## 7. Threshold Examples

### Example 7A: Simple Threshold

**Natural language:** "Orders over a review threshold require manual fraud review"

**Formal:**
```
fraud_review_required = order.total > review_threshold

Where:
• order.total = total order value including tax and shipping (currency)
• review_threshold = minimum value triggering manual review (currency parameter, recovered — not a fixed literal)
```

---

### Example 7B: Tiered Threshold

**Natural language:** "Commission rate depends on quarterly sales volume"

**Formal:**
```
commission_rate = { 0.05    where quarterly_sales < tier_1
                 { 0.08    where tier_1 ≤ quarterly_sales < tier_2
                 { 0.12    where quarterly_sales ≥ tier_2

Where:
• commission_rate = percentage applied to each sale (decimal)
• quarterly_sales = total revenue attributed to salesperson in current quarter (currency)
• tier_1 = standard performance threshold (currency parameter, recovered)
• tier_2 = high performance threshold (currency parameter, recovered)
```

---

## Summary of Patterns

| When You Have... | Use This Pattern | Key Marker |
|---|---|---|
| A computed business value | Calculation with formula | `result = expression` |
| Something that must always be true | Constraint/invariant | `REQUIRE predicate` |
| A qualification check with multiple conditions | Factored eligibility | `eligible = A ∧ B ∧ C` with each named |
| An authorization decision | Permission predicate | `PERMIT action WHERE conditions` |
| Required ordering of operations | Sequence notation | `A → before → B` |
| A condition for state change | State guard | `ALLOW transition(S₁ → S₂) WHERE predicate` |
| A numeric boundary changing behavior | Threshold (piecewise) | `result = { x where condition }` |
| A complex condition | Factor into sub-expressions | Named sub-expressions with "Where" definitions |
