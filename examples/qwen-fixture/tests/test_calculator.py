from __future__ import annotations

import unittest

from src.calculator import calculate_total


class CalculateTotalTests(unittest.TestCase):
    def test_applies_quantity_and_percentage_discount(self) -> None:
        lines = [{"price": 10.0, "quantity": 2.0}, {"price": 5.0, "quantity": 3.0}]
        self.assertEqual(calculate_total(lines, discount=0.1), 31.5)

    def test_accepts_empty_order(self) -> None:
        self.assertEqual(calculate_total([], discount=0.0), 0.0)

    def test_rejects_invalid_discount(self) -> None:
        with self.assertRaises(ValueError):
            calculate_total([{"price": 10.0, "quantity": 1.0}], discount=1.5)


if __name__ == "__main__":
    unittest.main()
