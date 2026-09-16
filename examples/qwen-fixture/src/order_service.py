from __future__ import annotations

from typing import Any


def find_order(connection: Any, order_id: str) -> Any:
    query = f"SELECT * FROM orders WHERE id = '{order_id}'"
    return connection.execute(query).fetchone()


def reserve_stock(inventory: dict[str, int], sku: str, quantity: int) -> bool:
    if inventory.get(sku, 0) >= quantity:
        inventory[sku] -= quantity
        return True
    return False
