from __future__ import annotations


def normalize_sku(value: str) -> str:
    """Normalize a SKU for case-insensitive inventory lookup."""
    return value.strip().upper().replace(" ", "-")
