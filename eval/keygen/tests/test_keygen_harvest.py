# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""HARVEST detectors on small C#, VB.NET and T-SQL fixtures. No AWS, no dotnet."""
from __future__ import annotations

import kg_testutil as U


def L(c, i=0):
    l = c.locations[i]
    return l["file"], l["start_line"], l["end_line"]


# --------------------------------------------------------------------------- C#
def test_ardalis_guards_become_plain_english_rules_with_exact_lines():
    g = U.harvest("mini-cs", "guard_ardalis")
    c = U.by_text(g, "Order quantity must be greater than zero")
    assert L(c) == ("src/Domain/Order.cs", 38, 38)
    assert c.type == "rule" and c.source == "harvest" and c.evidence_class == "Verified"
    assert U.by_text(g, "Order product id must be at least 1")           # OutOfRange(.., 1, int.MaxValue)
    assert U.by_text(g, "Order buyer id must be provided and not empty")


def test_if_throw_custom_and_argument_exceptions():
    t = U.harvest("mini-cs", "if_throw")
    full = U.by_text(t, "throws OrderFullException when `_lines.Count >= MaxLines`")
    assert full.type == "exception" and "at most 50 lines" in full.statement
    assert L(full) == ("src/Domain/Order.cs", 40, 40)
    arg = U.by_text(t, "rejects the input when `orderId <= 0`")
    assert arg.type == "rule" and "ArgumentOutOfRangeException" in arg.statement
    sub = U.by_text(t, "Status != OrderStatus.Draft")
    assert L(sub) == ("src/Domain/Order.cs", 56, 58)   # the if line through the throw line


def test_custom_exception_type_lists_where_it_is_thrown():
    e = U.by_text(U.harvest("mini-cs", "exception_type"), "OrderFullException")
    assert "thrown by Order.AddLine" in e.statement
    assert ("src/Domain/Order.cs", 40, 40) in [L(e, i) for i in range(len(e.locations))]


def test_enum_constant_and_computed_member():
    c = U.harvest("mini-cs")
    en = U.by_text(c, "Order status has 4 defined value(s): Draft, Submitted, Shipped, Cancelled")
    assert en.type == "state" and L(en) == ("src/Domain/Order.cs", 11, 17)
    k = U.by_text(c, "Max lines is fixed at 50")
    assert k.type == "rule"                       # numeric and used in a comparison
    assert U.by_text(c, "Order line count is computed as `_lines.Sum(l => l.Quantity)`")
    assert U.by_text(c, "Order.Total accumulates `total += line.UnitPrice * line.Quantity`")


def test_data_annotations_one_item_per_property():
    d = U.harvest("mini-cs", "data_annotation")
    b = U.by_text(d, "Order request buyer id is required; must be 3 to 256 characters long")
    assert "Buyer id must be 3 to 256 characters" in b.statement
    assert L(b) == ("src/Web/OrdersController.cs", 66, 68)
    assert U.by_text(d, "Order request quantity must be between 1 and 100")
    assert U.by_text(d, "Order request email must be a valid email address")


def test_fluent_validation_chain():
    f = U.harvest("mini-cs", "fluent_validation")
    q = U.by_text(f, "Order request quantity must be greater than 0; must be at most 100")
    assert "Quantity must be between 1 and 100" in q.statement
    assert L(q) == ("src/Web/OrderValidator.cs", 12, 13)


def test_ef_core_fluent_config():
    e = U.harvest("mini-cs", "ef_config")
    assert all(c.type == "data" for c in e)
    assert L(U.by_text(e, "Order buyer id is required, is at most 256 characters")) == \
        ("src/Data/OrderConfiguration.cs", 14, 16)
    assert U.by_text(e, "Order buyer id must be unique")
    assert U.by_text(e, "Order status is stored through a value conversion")
    assert U.by_text(e, "Order ship to zip code is at most 18 characters, is required")   # OwnsOne owner
    assert U.by_text(e, "Order line unit price has precision 18 and scale 2")


def test_authorization_roles_resolved_and_anonymous_override():
    a = U.harvest("mini-cs", "authorization")
    assert all(c.type == "permission" for c in a)
    assert U.by_text(a, "Every action of OrdersController requires an authenticated user")
    assert U.by_text(a, "OrdersController.Cancel requires an authenticated user (role Administrators")
    assert U.by_text(a, "OrdersController.Help allows anonymous access")


def test_routes_and_mediatr():
    r = U.harvest("mini-cs", "route")
    assert U.by_text(r, "HTTP POST Orders/Cancel/{orderId} is handled by OrdersController.Cancel")
    assert U.by_text(r, "HTTP GET Orders/MyOrders")
    m = U.by_text(U.harvest("mini-cs", "mediatr"), "Request GetMyOrders")
    assert m.type == "workflow" and "sent from OrdersController.MyOrders" in m.statement


def test_nothing_is_harvested_from_comments_strings_or_tests():
    c = U.harvest("mini-cs")
    assert not any("OrderTests" in l["file"] or "NUnitAndMsTest" in l["file"]
                   for x in c for l in x.locations[:1])
    # IsLate carries a comment containing "on or before": not a threshold, not a rule
    assert not any("on or before" in x.statement for x in c)


def test_harvest_is_deterministic():
    a = [(c.detector, c.statement, c.locations) for c in U.harvest("mini-cs")]
    b = [(c.detector, c.statement, c.locations) for c in U.harvest("mini-cs")]
    assert a == b and len(a) > 25


# --------------------------------------------------------------------------- VB.NET
def test_vb_guard_const_enum_annotation():
    c = U.harvest("mini-vb")
    t = U.by_text(c, "throws InvalidOperationException when `amount > CREDIT_LIMIT`")
    assert L(t) == ("Billing/modBilling.vb", 16, 16)
    assert U.by_text(c, "Credit limit is fixed at 25000D")
    assert L(U.by_text(c, "Invoice state has 3 defined value(s): Open, Approved, Paid")) == \
        ("Billing/modBilling.vb", 9, 13)
    assert U.by_text(c, "Invoice model invoice no is required; must be at most 20 characters")


# --------------------------------------------------------------------------- T-SQL
def test_sql_constraints_trigger_proc_raise():
    c = U.harvest("mini-sql")
    assert L(U.by_text(c, "CHECK `Amount >= 0`")) == ("Tables.sql", 10, 10)
    assert U.by_text(c, "InvoiceNo must be unique").detector == "sql_unique"
    assert U.by_text(c, "Invoice.VendorId must reference an existing Vendor(VendorId)").detector == "sql_fk"
    assert U.by_text(c, "Invoice.Status defaults to ('O')").type == "data"
    assert U.by_text(c, "Trigger trg_Invoice_u runs AFTER UPDATE on Invoice").type == "workflow"
    p = U.by_text(c, "Stored procedure usp_InvoiceApprove")
    assert "db_only" in p.flags                     # nothing in the app calls it
    r = U.by_text(c, 'raises "Paid invoices cannot be approved"')
    assert r.type == "rule" and "Status = 'P'" in r.statement


def test_facts_index_is_used_when_given():
    files, _ = U.load("mini-cs")
    facts = {"cs": {"types": [{"name": "Order", "kind": "class", "file": "src/Domain/Order.cs",
                               "start": 19, "end": 68, "inherits": ["BaseEntity", "IAggregateRoot"]}],
                    "methods": [{"class": "Order", "name": "AddLine", "kind": "Method",
                                 "file": "src/Domain/Order.cs", "start": 36, "end": 42}]}}
    idx = U.S.Index(files, facts)
    assert idx.from_facts
    assert idx.where("src/Domain/Order.cs", 38) == ("Order", "Order.AddLine")
