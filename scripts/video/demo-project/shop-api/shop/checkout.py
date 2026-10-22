"""The checkout endpoint.

``checkout`` behaves like a small HTTP handler: it returns ``(status, headers, body)``.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Dict, List, Optional, Tuple

from .inventory import DEFAULT_STOCK, Inventory, OutOfStock
from .pricing import UnknownItem, quote

Response = Tuple[int, Dict[str, str], str]

_inventory = Inventory(DEFAULT_STOCK)


def _json(status: int, payload: dict, headers: Optional[Dict[str, str]] = None) -> Response:
    return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(payload)


def checkout(
    customer_id: str,
    cart: List[Tuple[str, int]],
    clock: Callable[[], float] = time.time,
    inventory: Optional[Inventory] = None,
) -> Response:
    """Price the cart, reserve stock and create an order."""
    inv = inventory or _inventory
    if not customer_id:
        return _json(400, {"error": "customer_id is required"})
    if not cart:
        return _json(400, {"error": "cart is empty"})
    try:
        totals = quote(cart)
        inv.reserve(cart)
    except UnknownItem as exc:
        return _json(404, {"error": f"unknown item {exc.args[0]}"})
    except OutOfStock as exc:
        return _json(409, {"error": str(exc)})
    order = {
        "customer_id": customer_id,
        "created_at": clock(),
        "items": [{"sku": sku, "quantity": qty} for sku, qty in cart],
        "total": str(totals["total"]),
    }
    return _json(201, {"order": order})
