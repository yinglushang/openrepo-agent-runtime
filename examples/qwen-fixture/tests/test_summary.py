from __future__ import annotations

import unittest

from src.summary import build_order_summary


class BuildOrderSummaryTests(unittest.TestCase):
    def test_reports_item_quantity_and_discounted_total(self) -> None:
        lines = [{"price": 10.0, "quantity": 2.0}, {"price": 5.0, "quantity": 3.0}]
        self.assertEqual(build_order_summary(lines, discount=0.1), "items=5 total=31.50")

    def test_handles_empty_order(self) -> None:
        self.assertEqual(build_order_summary([], discount=0.0), "items=0 total=0.00")


if __name__ == "__main__":
    unittest.main()
