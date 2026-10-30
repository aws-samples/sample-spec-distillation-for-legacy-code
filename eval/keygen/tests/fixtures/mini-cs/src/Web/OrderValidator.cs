// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Synthetic fixture for eval/keygen tests. Not real code.
using FluentValidation;

namespace Mini.Web;

public class OrderRequestValidator : AbstractValidator<OrderRequest>
{
    public OrderRequestValidator()
    {
        RuleFor(x => x.Quantity).GreaterThan(0).LessThanOrEqualTo(100)
            .WithMessage("Quantity must be between 1 and 100");
        RuleFor(x => x.Email).NotEmpty().EmailAddress();
    }
}
