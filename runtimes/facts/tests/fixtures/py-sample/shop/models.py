# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from dataclasses import dataclass
from decimal import Decimal

MAX_ORDER_TOTAL = Decimal("5000")


@dataclass
class Order:
    order_id: int
    total: Decimal
    status: str = "NEW"

    def is_large(self) -> bool:
        return self.total > MAX_ORDER_TOTAL


class PriorityOrder(Order):
    def escalate(self):
        return self.is_large()
