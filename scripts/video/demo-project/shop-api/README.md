# shop-api

A tiny checkout service: price a cart, reserve stock, create an order.

```
shop/pricing.py     catalog, tax, cart totals
shop/inventory.py   in-memory stock with all-or-nothing reservations
shop/checkout.py    the checkout handler — returns (status, headers, body)
```

Run the tests with `python3 -m unittest`.
