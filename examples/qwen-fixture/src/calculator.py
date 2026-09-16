from __future__ import annotations


def calculate_total(lines: list[dict[str, float]], discount: float = 0.0) -> float:
    """Return the discounted order total."""
    subtotal = sum(line["price"] for line in lines)
    return round(subtotal - discount, 2)
