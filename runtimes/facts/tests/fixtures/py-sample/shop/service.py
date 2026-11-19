# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import logging
import shop.models
from shop.repository import OrderRepository
from shop import models as m

log = logging.getLogger(__name__)


def approve(order):
    order.status = "APPROVED"
    return order


class OrderService:
    def __init__(self, repo: OrderRepository):
        self.repo = repo
        self.audit = AuditTrail()

    def place(self, order_id: int):
        order = self.repo.load(order_id)
        if order.is_large():
            self._hold(order)
        else:
            approve(order)
        self.repo.save_status(order)
        self.audit.record(order)
        print(order)
        return shop.models.Order(order_id, 0)

    def _hold(self, order: "m.Order"):
        order.status = "HOLD"
        self.repo.notify(order)


class AuditTrail:
    def record(self, order):
        log.info("recorded %s", order)


def main():
    svc = OrderService(OrderRepository(":memory:"))
    svc.place(1)
