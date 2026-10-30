// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Synthetic fixture for eval/keygen tests. Not real code.
using System;
using System.ComponentModel.DataAnnotations;
using System.Linq;
using System.Threading.Tasks;
using MediatR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Mini.Domain;

namespace Mini.Web;

public static class Roles
{
    public const string ADMINISTRATORS = "Administrators";
}

[Authorize]
[Route("[controller]/[action]")]
public class OrdersController : Controller
{
    private readonly IMediator _mediator;

    public OrdersController(IMediator mediator)
    {
        _mediator = mediator;
    }

    [HttpGet]
    public async Task<IActionResult> MyOrders()
    {
        var orders = await _mediator.Send(new GetMyOrders(User.Identity.Name));
        return View(orders.Take(20));
    }

    [HttpPost("{orderId}")]
    [Authorize(Roles = Roles.ADMINISTRATORS)]
    public IActionResult Cancel(int orderId)
    {
        if (orderId <= 0) throw new ArgumentOutOfRangeException(nameof(orderId), "Order id must be positive");
        return Ok();
    }

    [AllowAnonymous]
    [HttpGet]
    public IActionResult Help()
    {
        return View();
    }
}

public record GetMyOrders(string BuyerId) : IRequest<int[]>;

public class GetMyOrdersHandler : IRequestHandler<GetMyOrders, int[]>
{
    public Task<int[]> Handle(GetMyOrders request, System.Threading.CancellationToken ct)
    {
        return Task.FromResult(new int[0]);
    }
}

public class OrderRequest
{
    [Required]
    [StringLength(256, MinimumLength = 3, ErrorMessage = "Buyer id must be 3 to 256 characters")]
    public string BuyerId { get; set; }

    [Range(1, 100)]
    public int Quantity { get; set; }

    [EmailAddress]
    public string Email { get; set; }
}
