"""
tests/test_fulfillment.py — Ghost Fulfillment Pipeline Tests
=============================================================
Validates the exact order lifecycle described in the fulfillment spec:

  Order created → plan normalized → inventory searched → key found →
  key reserved atomically → key status = sold → order status = completed,
  licenseKey filled, deliveryStatus = delivered → Redis saved →
  updated order returned.

Required inventory test:
  Start:  available = 5
  After:  available = 4, completed orders +1, customer licenses +1

Run:
    cd playground/qa_system_config
    pytest tests/test_fulfillment.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "web" / "api"))

os.environ.setdefault("GHOST_JWT_SECRET",    "test-jwt-secret-not-for-prod-xxxxxxxxxx")
os.environ.setdefault("GHOST_ADMIN_API_KEY", "test-admin-key-abcdef")
os.environ.setdefault("GHOST_HMAC_SECRET",   "test-hmac-secret-seed-for-pytest-xxxxx")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    """Redirect all data files to tmp_path so tests are fully isolated."""
    import keygen
    monkeypatch.setattr(keygen, "KEYS_DB",      tmp_path / "issued_keys.json")
    monkeypatch.setattr(keygen, "BANNED_DB",    tmp_path / "banned_keys.json")
    monkeypatch.setattr(keygen, "BLACKLIST_DB", tmp_path / "blacklist.json")
    monkeypatch.setattr(keygen, "WHITELIST_DB", tmp_path / "whitelist.json")
    monkeypatch.setattr(keygen, "USERS_DB",     tmp_path / "users.json")

    import inventory as _inv_mod
    monkeypatch.setattr(_inv_mod, "INVENTORY_DB", tmp_path / "key_inventory.json")

    import license_delivery as ld
    monkeypatch.setattr(ld, "ORDERS_DB",    tmp_path / "orders.json")
    monkeypatch.setattr(ld, "DELIVERY_LOG", tmp_path / "delivery_log.json")


@pytest.fixture()
def pro_inventory_5(tmp_path):
    """Pre-stock exactly 5 available pro keys and return their values."""
    import keygen, inventory as _inv_mod
    # Use varied expiry days to ensure distinct HMAC-based keys (keygen is deterministic
    # for the same tier+days within the same second, so vary the inputs).
    keys = [keygen.generate_key(expires_days=30 + i, tier="PRO") for i in range(5)]
    _inv_mod.import_keys(keys, "pro")
    return keys


@pytest.fixture()
def _check_all_plans(tmp_path):
    """Pre-stock one key per canonical plan (pro, lifetime, trial)."""
    import keygen, inventory as _inv_mod
    for plan, tier, days in [("pro", "PRO", 30), ("lifetime", "PRO", 0), ("trial", "TRIAL", 7)]:
        k = keygen.generate_key(expires_days=days, tier=tier)
        _inv_mod.import_keys([k], plan)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Inventory counting
# ─────────────────────────────────────────────────────────────────────────────

class TestInventoryCounting:
    def test_initial_available_is_5(self, pro_inventory_5):
        import inventory as _inv_mod
        s = _inv_mod.stats()
        assert s["available"] == 5, f"Expected 5 available, got {s['available']}"

    def test_available_decreases_by_one_after_purchase(self, pro_inventory_5):
        import inventory as _inv_mod, license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id="order-count-001",
            payment_token="paypal:capture-count-001",
            plan="pro",
            email="count@example.com",
            discord="countuser",
            price_usd=7.0,
        )
        assert result["ok"] is True, f"Delivery failed: {result}"
        s = _inv_mod.stats()
        assert s["available"] == 4, f"Expected 4 available after purchase, got {s['available']}"
        assert s["sold"] == 1, f"Expected 1 sold, got {s['sold']}"

    def test_no_pending_orders_remain_after_successful_delivery(self, pro_inventory_5):
        import license_delivery as ld
        ld.confirm_payment_and_deliver(
            order_id="order-pending-001",
            payment_token="paypal:capture-pending-001",
            plan="pro",
            email="pending@example.com",
            discord="pendinguser",
            price_usd=7.0,
        )
        order = ld.get_order("order-pending-001")
        assert order is not None
        assert order.get("delivery_status") == "delivered", \
            f"Expected delivered, got {order.get('delivery_status')}"
        assert order.get("license_key") is not None, "license_key must not be None"

    def test_customer_licenses_plus_one(self, pro_inventory_5):
        import inventory as _inv_mod, license_delivery as ld
        ld.confirm_payment_and_deliver(
            order_id="order-custlic-001",
            payment_token="paypal:capture-custlic-001",
            plan="pro",
            email="custlic@example.com",
            discord="cuslicuser",
            price_usd=7.0,
        )
        # Customer licenses = inventory records with status=sold
        all_keys = _inv_mod.list_keys(status="sold")
        assert len(all_keys) == 1, f"Expected 1 sold key (customer license), got {len(all_keys)}"
        assert all_keys[0]["customer_email"] == "custlic@example.com"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Plan normalization
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanNormalization:
    """
    Verify that every plan alias normalizes to the canonical slug before
    the inventory search, so 'Ghost Pro (monthly)' and 'pro' both match
    inventory records stored as 'pro'.
    """

    @pytest.mark.parametrize("alias", [
        "pro",
        "monthly",
        "ghost_pro_monthly",
        "ghost pro monthly",
        "ghost pro (monthly)",
        "ghost_pro",
        "ghost pro",
        "Ghost Pro",
        "GHOST PRO (MONTHLY)",
        "Ghost Pro (monthly)",
    ])
    def test_pro_alias_resolves_to_pro(self, alias, pro_inventory_5):
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id=f"order-alias-{alias.replace(' ', '_').replace('(', '').replace(')', '')}",
            payment_token="paypal:capture-alias-001",
            plan=alias,
            email="alias@example.com",
            discord="aliasuser",
            price_usd=7.0,
        )
        assert result["ok"] is True, \
            f"Plan alias {alias!r} failed delivery: {result.get('error')}"
        assert result["key"] is not None
        assert result["tier"] == "PRO"

    def test_lifetime_alias_resolves(self, _check_all_plans):
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id="order-lifetime-alias-001",
            payment_token="paypal:capture-lifetime-001",
            plan="ghost lifetime",
            email="lifetime@example.com",
            discord="lifetimeuser",
            price_usd=79.0,
        )
        assert result["ok"] is True, f"Lifetime alias failed: {result.get('error')}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Full fulfillment pipeline for a PayPal order
# ─────────────────────────────────────────────────────────────────────────────

class TestFulfillmentPipeline:
    ORDER_ID = "08R4498F47545414E"   # The exact order ID from the bug report

    def _make_pending_record(self, order_id: str) -> dict:
        """Simulate what paypal.js writes to Redis/file before calling delivery."""
        return {
            "order_id":          order_id,
            "paypal_order_id":   "PAYPAL-ORDER-XYZ",
            "paypal_capture_id": order_id,
            "invoice_id":        "GHOST-INV-12345678",
            "plan":              "pro",
            "plan_label":        "Ghost Pro (monthly)",
            "tier":              "PRO",
            "email":             "buyer@example.com",
            "discord":           "buyer#0001",
            "price_usd":         7.0,
            "currency":          "USD",
            "created_at":        "2024-01-01T00:00:00+00:00",
            "payment_status":    "completed",
            "payment_verified":  True,
            "delivery_status":   "pending",
            "license_key":       None,
            "license_status":    "pending",
        }

    def test_order_created_in_redis_then_fulfilled(self, pro_inventory_5, tmp_path):
        """
        Simulate the full flow:
        1. paypal.js saves a pending order (payment_status=completed, delivery_status=pending)
        2. confirm_payment_and_deliver is called
        3. Order becomes delivery_status=delivered with a license_key
        """
        import license_delivery as ld

        # Step 1 – simulate Redis pre-save by the Node.js capture handler
        pending = self._make_pending_record(self.ORDER_ID)
        ld._persist_order(self.ORDER_ID, pending, None)

        # Verify it is there and pending
        saved = ld.get_order(self.ORDER_ID)
        assert saved is not None, "Pre-saved order must be retrievable"
        assert saved["delivery_status"] == "pending"
        assert saved["license_key"] is None

        # Step 2 – call delivery (same as POST /api/payment/confirm)
        result = ld.confirm_payment_and_deliver(
            order_id=self.ORDER_ID,
            payment_token=f"paypal:{self.ORDER_ID}",
            plan="pro",
            email="buyer@example.com",
            discord="buyer#0001",
            price_usd=7.0,
            paypal_order_id="PAYPAL-ORDER-XYZ",
        )

        # Step 3 – assertions
        assert result["ok"] is True, f"Fulfillment failed: {result}"
        assert result["key"] is not None, "license_key must be set"
        assert result["key"].startswith("GHOST-"), "Key must have GHOST- prefix"
        assert result["delivery_status"] == "delivered"
        assert result["tier"] == "PRO"

        # Verify persisted order
        order = ld.get_order(self.ORDER_ID)
        assert order is not None
        assert order["delivery_status"] == "delivered"
        assert order["license_key"] == result["key"]
        assert order["license_status"] == "active"
        assert order["payment_status"] == "completed"   # preserved from pre-save

    def test_payment_status_completed_preserved(self, pro_inventory_5, tmp_path):
        """payment_status from the PayPal capture must be 'completed', not overwritten with 'verified'."""
        import license_delivery as ld

        pending = self._make_pending_record("order-pstatus-001")
        ld._persist_order("order-pstatus-001", pending, None)

        result = ld.confirm_payment_and_deliver(
            order_id="order-pstatus-001",
            payment_token="paypal:capture-pstatus-001",
            plan="pro",
            email="buyer@example.com",
            discord="buyer#0001",
            price_usd=7.0,
        )
        assert result["ok"] is True
        order = ld.get_order("order-pstatus-001")
        assert order["payment_status"] == "completed", \
            f"payment_status should be 'completed', got {order['payment_status']!r}"

    def test_idempotency_pre_saved_pending_order(self, pro_inventory_5, tmp_path):
        """
        Calling confirm_payment_and_deliver twice for the same order must return
        the SAME key and must NOT double-consume from inventory.
        """
        import inventory as _inv_mod, license_delivery as ld

        pending = self._make_pending_record("order-idem-002")
        ld._persist_order("order-idem-002", pending, None)

        r1 = ld.confirm_payment_and_deliver(
            order_id="order-idem-002",
            payment_token="paypal:capture-idem-002",
            plan="pro",
            email="buyer@example.com",
            discord="buyer#0001",
            price_usd=7.0,
        )
        r2 = ld.confirm_payment_and_deliver(
            order_id="order-idem-002",
            payment_token="paypal:capture-idem-002",
            plan="pro",
            email="buyer@example.com",
            discord="buyer#0001",
            price_usd=7.0,
        )

        assert r1["ok"] and r2["ok"]
        assert r1["key"] == r2["key"], "Idempotent call returned different key"

        s = _inv_mod.stats()
        assert s["sold"] == 1, f"Only 1 key should be consumed, got {s['sold']}"
        assert s["available"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# 4. Out-of-stock safety
# ─────────────────────────────────────────────────────────────────────────────

class TestOutOfStock:
    def test_no_inventory_sets_out_of_stock_not_pending(self):
        """
        When inventory is empty, the order must become out_of_stock —
        never remain pending indefinitely.
        """
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id="order-oos-001",
            payment_token="paypal:capture-oos-001",
            plan="pro",
            email="oos@example.com",
            discord="oosuser",
            price_usd=7.0,
        )
        # ok=True (payment recorded) but out_of_stock=True and no key
        assert result.get("ok") is True
        assert result.get("out_of_stock") is True
        assert result.get("license_key") is None
        assert result.get("delivery_status") == "out_of_stock"

        # The order record in storage must also reflect out_of_stock
        order = ld.get_order("order-oos-001")
        assert order is not None
        assert order["delivery_status"] == "out_of_stock", \
            f"Order must be out_of_stock, not {order['delivery_status']!r}"

    def test_out_of_stock_order_fulfilled_when_stock_added(self, tmp_path):
        """Once an out-of-stock order is saved, adding inventory and retrying must succeed."""
        import keygen, inventory as _inv_mod, license_delivery as ld

        # First: out-of-stock
        result = ld.confirm_payment_and_deliver(
            order_id="order-oos-refill-001",
            payment_token="paypal:capture-oos-refill-001",
            plan="pro",
            email="refill@example.com",
            discord="refilluser",
            price_usd=7.0,
        )
        assert result.get("delivery_status") == "out_of_stock"

        # Now: add stock
        new_key = keygen.generate_key(expires_days=30, tier="PRO")
        _inv_mod.import_keys([new_key], "pro")

        # Retry via /fulfill endpoint logic
        order = ld.get_order("order-oos-refill-001")
        assert order is not None

        plan = _inv_mod.normalize_plan(order.get("plan", ""))
        inv_rec = ld._inv_get_next_available(plan)
        assert inv_rec is not None, "After adding stock, inv_record must be found"

        assigned = ld._inv_assign_key(
            key=inv_rec["key"],
            order_id="order-oos-refill-001",
            customer_email=order.get("email", ""),
            assigned_user=order.get("discord", ""),
            purchase_date=order.get("created_at", ""),
        )
        assert assigned is True

        order["license_key"]     = inv_rec["key"]
        order["delivery_status"] = "delivered"
        order["license_status"]  = "active"
        ld._persist_order("order-oos-refill-001", order, None)

        final = ld.get_order("order-oos-refill-001")
        assert final["delivery_status"] == "delivered"
        assert final["license_key"] == inv_rec["key"]


# ─────────────────────────────────────────────────────────────────────────────
# 5. Flask route integration
# ─────────────────────────────────────────────────────────────────────────────

class TestFulfillmentRoutes:
    @pytest.fixture()
    def delivery_app(self):
        import license_delivery as ld
        ld.app.config["TESTING"] = True
        return ld.app.test_client()

    def test_confirm_route_returns_key(self, delivery_app, pro_inventory_5):
        resp = delivery_app.post(
            "/api/payment/confirm",
            json={
                "order_id":      "order-route-001",
                "payment_token": "paypal:capture-route-001",
                "plan":          "pro",
                "email":         "route@example.com",
                "discord":       "routeuser",
                "price_usd":     7.0,
            },
            content_type="application/json",
        )
        data = resp.get_json()
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {data}"
        assert data["ok"] is True
        assert data["key"] is not None
        assert data["delivery_status"] == "delivered"

    def test_fulfill_route_retry_delivers_key(self, delivery_app, pro_inventory_5, tmp_path):
        """POST /api/order/<id>/fulfill must deliver a key for a pending-payment order."""
        import license_delivery as ld

        # Pre-save pending order (like paypal.js does)
        pending = {
            "order_id":       "order-retry-route-001",
            "plan":           "pro",
            "plan_label":     "Ghost Pro (monthly)",
            "email":          "retry@example.com",
            "discord":        "retryuser",
            "price_usd":      7.0,
            "currency":       "USD",
            "created_at":     "2024-01-01T00:00:00+00:00",
            "payment_status": "completed",
            "payment_verified": True,
            "delivery_status": "pending",
            "license_key":    None,
        }
        ld._persist_order("order-retry-route-001", pending, None)

        resp = delivery_app.post(
            "/api/order/order-retry-route-001/fulfill",
            json={},
            content_type="application/json",
        )
        data = resp.get_json()
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {data}"
        assert data.get("ok") is True
        assert data.get("license_key") is not None, "license_key must be set after fulfill"
        assert data.get("delivery_status") == "delivered"

    def test_get_order_route_returns_stored_record(self, delivery_app, pro_inventory_5):
        import license_delivery as ld
        ld.confirm_payment_and_deliver(
            order_id="order-get-001",
            payment_token="paypal:capture-get-001",
            plan="pro",
            email="get@example.com",
            discord="getuser",
            price_usd=7.0,
        )
        resp = delivery_app.get("/api/order/order-get-001")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data.get("ok") is True
        assert data.get("delivery_status") == "delivered"
        assert data.get("license_key") is not None

    def test_download_route_accepts_completed_payment_status(self, delivery_app, pro_inventory_5, tmp_path):
        """
        /api/order/<id>/download must accept payment_status='completed'
        (PayPal capture path), not only 'verified' (Stripe/legacy).
        """
        import license_delivery as ld, os

        # Deliver an order so it has payment_status=completed
        ld.confirm_payment_and_deliver(
            order_id="order-dl-001",
            payment_token="paypal:capture-dl-001",
            plan="pro",
            email="dl@example.com",
            discord="dluser",
            price_usd=7.0,
        )

        # Set GHOST_DOWNLOAD_PATH so the route doesn't 503
        os.environ["GHOST_DOWNLOAD_PATH"] = "/downloads/GhostConfig.exe"
        try:
            resp = delivery_app.get("/api/order/order-dl-001/download")
            # Must NOT be 403 (payment_status check failure)
            assert resp.status_code != 403, \
                f"Download blocked with 403 — payment_status check is rejecting 'completed'"
        finally:
            del os.environ["GHOST_DOWNLOAD_PATH"]
