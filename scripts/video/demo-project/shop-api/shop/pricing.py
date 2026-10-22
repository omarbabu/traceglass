"""Price calculation for carts."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Tuple

CATALOG: Dict[str, Decimal] = {
    "sku-espresso": Decimal("3.50"),
    "sku-croissant": Decimal("2.75"),
    "sku-beans-1kg": Decimal("24.00"),
    "sku-grinder": Decimal("89.00"),
}

TAX_RATE = Decimal("0.0825")


class UnknownItem(KeyError):
    """Raised when a cart references a SKU that is not in the catalog."""


def line_total(sku: str, quantity: int) -> Decimal:
    """Price of ``quantity`` units of ``sku``."""
    if sku not in CATALOG:
        raise UnknownItem(sku)
    return CATALOG[sku] * quantity


def quote(cart: List[Tuple[str, int]]) -> Dict[str, Decimal]:
    """Subtotal, tax and total for a cart of ``(sku, quantity)`` pairs."""
    subtotal = sum((line_total(sku, qty) for sku, qty in cart), Decimal("0"))
    tax = (subtotal * TAX_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"subtotal": subtotal, "tax": tax, "total": subtotal + tax}
