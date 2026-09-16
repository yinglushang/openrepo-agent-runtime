from __future__ import annotations


def build_order_summary(lines: list[dict[str, float]], discount: float = 0.0) -> str:
    """Return ``items=<quantity> total=<amount>`` for an order."""
    raise NotImplementedError("order summary is not implemented")
