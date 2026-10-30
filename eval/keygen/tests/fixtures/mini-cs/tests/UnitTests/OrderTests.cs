// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Synthetic fixture for eval/keygen tests. Not real code.
using System;
using Mini.Domain;
using Xunit;

namespace Mini.UnitTests;

public class OrderAddLine
{
    [Fact]
    public void CantAddLineWithZeroQuantity()
    {
        var order = new Order("buyer-1", DateTime.Today);
        Assert.Throws<ArgumentException>(() => order.AddLine(7, 1.5m, 0));
    }

    [Theory]
    [InlineData(1)]
    [InlineData(3)]
    public void TotalSumsLineAmounts(int quantity)
    {
        var order = new Order("buyer-1", DateTime.Today);
        order.AddLine(7, 2m, quantity);
        Assert.Equal(2m * quantity, order.Total());
    }
}
