"""In-memory stock levels."""

from __future__ import annotations

from typing import Dict, List, Tuple


class OutOfStock(Exception):
    """Raised when a cart asks for more units than are available."""

    def __init__(self, sku: str, requested: int, available: int) -> None:
        super().__init__(f"{sku}: requested {requested}, only {available} left")
        self.sku = sku
        self.requested = requested
        self.available = available


class Inventory:
    """Tracks how many units of each SKU are on hand."""

    def __init__(self, stock: Dict[str, int]) -> None:
        self._stock = dict(stock)

    def available(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def reserve(self, cart: List[Tuple[str, int]]) -> None:
        """Atomically reserve every line in ``cart`` or nothing at all."""
        for sku, qty in cart:
            if self.available(sku) < qty:
                raise OutOfStock(sku, qty, self.available(sku))
        for sku, qty in cart:
            self._stock[sku] -= qty


DEFAULT_STOCK = {"sku-espresso": 500, "sku-croissant": 40, "sku-beans-1kg": 25, "sku-grinder": 3}
