// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Synthetic fixture for eval/keygen tests. Not real code.
using System;
using Mini.Domain;
using NUnit.Framework;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace Mini.OtherTests;

public class OrderSubmitNUnit
{
    [Test]
    public void SubmitMovesDraftToSubmitted()
    {
        var order = new Order("buyer-2", DateTime.Today);
        order.Submit();
        Assert.AreEqual(OrderStatus.Submitted, order.Status);
    }
}

[TestClass]
public class OrderLateMsTest
{
    [TestMethod]
    public void IsLateWhenPlacedBeforeCutoff()
    {
        var order = new Order("buyer-3", new DateTime(2026, 1, 1));
        Assert.IsTrue(order.IsLate(new DateTime(2026, 2, 1)));
    }
}
