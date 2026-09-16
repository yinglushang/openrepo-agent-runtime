from __future__ import annotations

import unittest
from typing import Any

from src.order_service import find_order, reserve_stock


class _Cursor:
    def fetchone(self) -> dict[str, str]:
        return {"id": "order-1"}


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, parameters: Any = None) -> _Cursor:
        self.calls.append((query, parameters))
        return _Cursor()


class FindOrderTests(unittest.TestCase):
    def test_uses_parameterized_query(self) -> None:
        connection = _Connection()
        result = find_order(connection, "x' OR 1=1 --")

        self.assertEqual(result, {"id": "order-1"})
        query, parameters = connection.calls[0]
        self.assertNotIn("OR 1=1", query)
        self.assertEqual(parameters, ("x' OR 1=1 --",))


class ReserveStockTests(unittest.TestCase):
    def test_rejects_zero_quantity_without_mutation(self) -> None:
        inventory = {"SKU-1": 3}
        self.assertFalse(reserve_stock(inventory, "SKU-1", 0))
        self.assertEqual(inventory, {"SKU-1": 3})

    def test_rejects_negative_quantity_without_mutation(self) -> None:
        inventory = {"SKU-1": 3}
        self.assertFalse(reserve_stock(inventory, "SKU-1", -2))
        self.assertEqual(inventory, {"SKU-1": 3})

    def test_reserves_available_stock(self) -> None:
        inventory = {"SKU-1": 3}
        self.assertTrue(reserve_stock(inventory, "SKU-1", 2))
        self.assertEqual(inventory, {"SKU-1": 1})


if __name__ == "__main__":
    unittest.main()
