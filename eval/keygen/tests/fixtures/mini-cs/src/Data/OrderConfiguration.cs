// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Synthetic fixture for eval/keygen tests. Not real code.
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Metadata.Builders;
using Mini.Domain;

namespace Mini.Data;

public class OrderConfiguration : IEntityTypeConfiguration<Order>
{
    public void Configure(EntityTypeBuilder<Order> builder)
    {
        builder.Property(o => o.BuyerId)
            .IsRequired()
            .HasMaxLength(256);

        builder.Property(o => o.Status)
            .HasConversion<string>();

        builder.HasIndex(o => o.BuyerId).IsUnique();

        builder.OwnsOne(o => o.ShipTo, a =>
        {
            a.Property(x => x.ZipCode)
                .HasMaxLength(18)
                .IsRequired();
        });
    }
}

public class OrderLineConfiguration : IEntityTypeConfiguration<OrderLine>
{
    public void Configure(EntityTypeBuilder<OrderLine> builder)
    {
        builder.Property(l => l.UnitPrice)
            .HasPrecision(18, 2);
    }
}
