import json
import unittest

from shop.checkout import checkout
from shop.inventory import Inventory


def fresh_inventory() -> Inventory:
    return Inventory({"sku-espresso": 10, "sku-grinder": 1})


class CheckoutTest(unittest.TestCase):
    def test_creates_order(self):
        status, headers, body = checkout("c-1", [("sku-espresso", 2)], clock=lambda: 1000.0, inventory=fresh_inventory())
        self.assertEqual(status, 201)
        self.assertEqual(headers["Content-Type"], "application/json")
        order = json.loads(body)["order"]
        self.assertEqual(order["total"], "7.58")
        self.assertEqual(order["created_at"], 1000.0)

    def test_requires_customer(self):
        status, _, _ = checkout("", [("sku-espresso", 1)], inventory=fresh_inventory())
        self.assertEqual(status, 400)

    def test_rejects_empty_cart(self):
        status, _, _ = checkout("c-1", [], inventory=fresh_inventory())
        self.assertEqual(status, 400)

    def test_unknown_item(self):
        status, _, body = checkout("c-1", [("sku-teapot", 1)], inventory=fresh_inventory())
        self.assertEqual(status, 404)
        self.assertIn("sku-teapot", body)

    def test_out_of_stock(self):
        status, _, body = checkout("c-1", [("sku-grinder", 2)], inventory=fresh_inventory())
        self.assertEqual(status, 409)
        self.assertIn("only 1 left", body)


if __name__ == "__main__":
    unittest.main()
