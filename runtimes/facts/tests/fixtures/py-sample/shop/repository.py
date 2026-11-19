# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import sqlite3
import requests as http

from .models import Order


class OrderRepository:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)

    def load(self, order_id: int) -> Order:
        row = self.conn.execute("SELECT id, total, status FROM orders WHERE id = ?", (order_id,)).fetchone()
        return Order(row[0], row[1], row[2])

    def save_status(self, order: Order) -> None:
        self.conn.execute("UPDATE orders SET status = ? WHERE id = ?", (order.status, order.order_id))
        self.conn.execute("INSERT INTO order_audit (order_id, status) VALUES (?, ?)",
                          (order.order_id, order.status))

    def notify(self, order: Order) -> None:
        http.post("https://example.invalid/notify", json={"id": order.order_id})
