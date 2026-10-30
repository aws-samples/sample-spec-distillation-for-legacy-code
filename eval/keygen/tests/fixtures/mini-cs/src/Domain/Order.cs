// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Synthetic fixture for eval/keygen tests. Not real code.
using System;
using System.Collections.Generic;
using System.Linq;
using Ardalis.GuardClauses;

namespace Mini.Domain;

public enum OrderStatus
{
    Draft,
    Submitted,
    Shipped = 5,
    Cancelled
}

public class Order : BaseEntity, IAggregateRoot
{
    public const int MaxLines = 50;
    private readonly List<OrderLine> _lines = new List<OrderLine>();

    public string BuyerId { get; private set; }
    public DateTime OrderDate { get; private set; }
    public OrderStatus Status { get; private set; }
    public int LineCount => _lines.Sum(l => l.Quantity);

    public Order(string buyerId, DateTime orderDate)
    {
        Guard.Against.NullOrEmpty(buyerId, nameof(buyerId));
        BuyerId = buyerId;
        OrderDate = orderDate;
    }

    public void AddLine(int productId, decimal unitPrice, int quantity)
    {
        Guard.Against.NegativeOrZero(quantity, nameof(quantity));
        Guard.Against.OutOfRange(productId, nameof(productId), 1, int.MaxValue);
        if (_lines.Count >= MaxLines) throw new OrderFullException("An order holds at most 50 lines");
        _lines.Add(new OrderLine(productId, unitPrice, quantity));
    }

    public decimal Total()
    {
        var total = 0m;
        foreach (var line in _lines)
        {
            total += line.UnitPrice * line.Quantity;
        }
        return total;
    }

    public void Submit()
    {
        if (Status != OrderStatus.Draft)
        {
            throw new InvalidOperationException("Only draft orders can be submitted");
        }
        Status = OrderStatus.Submitted;
    }

    public bool IsLate(DateTime cutoff)
    {
        // "late" means placed on or before the cutoff and still not shipped
        return OrderDate <= cutoff && Status != OrderStatus.Shipped;
    }
}

public class OrderLine : BaseEntity
{
    public int ProductId { get; private set; }
    public decimal UnitPrice { get; private set; }
    public int Quantity { get; private set; }

    public OrderLine(int productId, decimal unitPrice, int quantity)
    {
        ProductId = productId;
        UnitPrice = unitPrice;
        Quantity = quantity;
    }

    public decimal LineTotal()
    {
        return UnitPrice * Quantity;
    }
}

public class OrderFullException : Exception
{
    public OrderFullException(string message) : base(message)
    {
    }
}
