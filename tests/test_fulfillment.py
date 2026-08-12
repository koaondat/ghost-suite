"""
tests/test_fulfillment.py — Ghost Fulfillment Pipeline Tests
=============================================================
Validates the exact order lifecycle described in the fulfillment spec.

New flow (auto-keygen):
  Plan selected → payment captured → server verifies → key auto-generated →
  key saved to inventory → order status = completed, licenseKey filled,
  deliveryStatus = delivered → returned to customer.

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


# ── No longer need pre-stocked inventory fixtures since keys are auto-generated.
# Kept for plan normalization tests that still need any plan context.

@pytest.fixture()
def _check_all_plans(tmp_path):
    """No-op — auto-keygen does not need pre-stocked keys."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. Auto-keygen delivery
# ─────────────────────────────────────────────────────────────────────────────

class TestAutoKeygenDelivery:
    """Auto-keygen: each successful payment generates exactly one new key."""

    def test_delivery_creates_key_without_inventory(self):
        """Key is generated even with no pre-stocked inventory."""
        import inventory as _inv_mod, license_delivery as ld
        # Confirm inventory starts empty
        assert _inv_mod.stats()["total"] == 0

        result = ld.confirm_payment_and_deliver(
            order_id="order-autogen-001",
            payment_token="paypal:capture-autogen-001",
            plan="month",
            email="auto@example.com",
            discord="autouser",
            price_usd=24.99,
        )
        assert result["ok"] is True, f"Delivery failed: {result}"
        assert result["key"] is not None
        assert result["key"].startswith("GHOST-"), "Key must have GHOST- prefix"
        assert result["delivery_status"] == "delivered"
        assert result["tier"] == "PRO"

    def test_generated_key_saved_to_inventory(self):
        """After purchase, the generated key appears in inventory as sold."""
        import inventory as _inv_mod, license_delivery as ld

        result = ld.confirm_payment_and_deliver(
            order_id="order-inv-001",
            payment_token="paypal:capture-inv-001",
            plan="week",
            email="invcheck@example.com",
            discord="invuser",
            price_usd=9.99,
        )
        assert result["ok"] is True

        sold_keys = _inv_mod.list_keys(status="sold")
        assert len(sold_keys) == 1, f"Expected 1 sold key in inventory, got {len(sold_keys)}"
        assert sold_keys[0]["key"] == result["key"]
        assert sold_keys[0]["customer_email"] == "invcheck@example.com"

    def test_generated_key_has_correct_expiry(self):
        """The key record in inventory must have a non-None expiration."""
        import inventory as _inv_mod, license_delivery as ld

        result = ld.confirm_payment_and_deliver(
            order_id="order-expiry-001",
            payment_token="paypal:capture-expiry-001",
            plan="day",
            email="expiry@example.com",
            discord="expiryuser",
            price_usd=2.99,
        )
        assert result["ok"] is True

        sold_keys = _inv_mod.list_keys(status="sold")
        assert sold_keys, "No sold keys found"
        key_rec = sold_keys[0]
        assert key_rec.get("expiration") is not None, "Expiration must be set"

    def test_each_duration_has_correct_expiry_days(self):
        """day=1, 3days=3, week=7, month=30, 3months=90."""
        import license_delivery as ld
        from datetime import datetime, timezone

        plan_days = [("day", 1), ("3days", 3), ("week", 7), ("month", 30), ("3months", 90)]
        for plan, expected_days in plan_days:
            result = ld.confirm_payment_and_deliver(
                order_id=f"order-expiry-{plan}",
                payment_token=f"paypal:capture-{plan}-001",
                plan=plan,
                email=f"{plan}@example.com",
                discord=f"{plan}user",
                price_usd=0.01,
            )
            assert result["ok"] is True, f"Plan {plan!r} failed: {result}"
            assert result.get("key") is not None
            # expires_at should be ~expected_days from now
            order = ld.get_order(f"order-expiry-{plan}")
            assert order is not None
            exp_str = order.get("expires_at")
            assert exp_str is not None, f"expires_at must be set for plan {plan!r}"
            exp_date = datetime.fromisoformat(exp_str)
            delta = (exp_date - datetime.now(timezone.utc)).days
            assert expected_days - 1 <= delta <= expected_days, \
                f"Plan {plan!r}: expected ~{expected_days} days, got {delta}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Plan normalization
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanNormalization:
    """
    Verify that every plan alias normalizes to a canonical slug before delivery.
    Legacy slugs like 'pro' → 'month', 'lifetime' → '3months'.
    """

    @pytest.mark.parametrize("alias,expected_plan", [
        ("month",   "month"),
        ("30day",   "month"),
        ("pro",     "month"),
        ("monthly", "month"),
        ("ghost_pro_monthly", "month"),
        ("ghost pro monthly", "month"),
        ("ghost pro (monthly)", "month"),
    ])
    def test_pro_aliases_resolve_to_month(self, alias, expected_plan):
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id=f"order-alias-{alias.replace(' ', '_').replace('(', '').replace(')', '')}",
            payment_token="paypal:capture-alias-001",
            plan=alias,
            email="alias@example.com",
            discord="aliasuser",
            price_usd=24.99,
        )
        assert result["ok"] is True, \
            f"Plan alias {alias!r} failed delivery: {result.get('error')}"
        assert result["key"] is not None
        assert result["tier"] == "PRO"
        order = ld.get_order(
            f"order-alias-{alias.replace(' ', '_').replace('(', '').replace(')', '')}"
        )
        assert order["plan"] == expected_plan, \
            f"Expected plan={expected_plan!r}, got {order['plan']!r}"

    @pytest.mark.parametrize("alias,expected_plan", [
        ("lifetime",        "3months"),
        ("ghost lifetime",  "3months"),
        ("ghost_lifetime",  "3months"),
        ("90day",           "3months"),
        ("3months",         "3months"),
    ])
    def test_lifetime_aliases_resolve_to_3months(self, alias, expected_plan):
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id=f"order-lt-alias-{alias.replace(' ', '_')}",
            payment_token=f"paypal:capture-lt-{alias.replace(' ', '_')}",
            plan=alias,
            email="lifetime@example.com",
            discord="lifetimeuser",
            price_usd=59.99,
        )
        assert result["ok"] is True, f"Lifetime alias {alias!r} failed: {result.get('error')}"
        order = ld.get_order(f"order-lt-alias-{alias.replace(' ', '_')}")
        assert order["plan"] == expected_plan

    @pytest.mark.parametrize("alias", ["day", "3days", "week", "month", "3months"])
    def test_canonical_slugs_work_directly(self, alias):
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id=f"order-canonical-{alias}",
            payment_token=f"paypal:capture-{alias}",
            plan=alias,
            email=f"{alias}@example.com",
            discord=f"{alias}user",
            price_usd=9.99,
        )
        assert result["ok"] is True, f"Canonical slug {alias!r} failed: {result.get('error')}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Full fulfillment pipeline
# ─────────────────────────────────────────────────────────────────────────────

class TestFulfillmentPipeline:
    ORDER_ID = "08R4498F47545414E"

    def _make_pending_record(self, order_id: str) -> dict:
        """Simulate what paypal.js writes before calling delivery."""
        return {
            "order_id":          order_id,
            "paypal_order_id":   "PAYPAL-ORDER-XYZ",
            "paypal_capture_id": order_id,
            "invoice_id":        "GHOST-INV-12345678",
            "plan":              "month",
            "plan_label":        "Phantom 1 Month",
            "tier":              "PRO",
            "email":             "buyer@example.com",
            "discord":           "buyer#0001",
            "price_usd":         24.99,
            "currency":          "USD",
            "created_at":        "2024-01-01T00:00:00+00:00",
            "payment_status":    "completed",
            "payment_verified":  True,
            "delivery_status":   "pending",
            "license_key":       None,
            "license_status":    "pending",
        }

    def test_order_created_then_fulfilled(self, tmp_path):
        import license_delivery as ld

        # Step 1 – simulate paypal.js pre-save
        pending = self._make_pending_record(self.ORDER_ID)
        ld._persist_order(self.ORDER_ID, pending, None)

        saved = ld.get_order(self.ORDER_ID)
        assert saved is not None
        assert saved["delivery_status"] == "pending"
        assert saved["license_key"] is None

        # Step 2 – call delivery
        result = ld.confirm_payment_and_deliver(
            order_id=self.ORDER_ID,
            payment_token=f"paypal:{self.ORDER_ID}",
            plan="month",
            email="buyer@example.com",
            discord="buyer#0001",
            price_usd=24.99,
            paypal_order_id="PAYPAL-ORDER-XYZ",
        )

        # Step 3 – assertions
        assert result["ok"] is True, f"Fulfillment failed: {result}"
        assert result["key"] is not None
        assert result["key"].startswith("GHOST-")
        assert result["delivery_status"] == "delivered"
        assert result["tier"] == "PRO"

        order = ld.get_order(self.ORDER_ID)
        assert order["delivery_status"] == "delivered"
        assert order["license_key"] == result["key"]
        assert order["license_status"] == "active"
        assert order["payment_status"] == "completed"

    def test_payment_status_completed_preserved(self, tmp_path):
        """payment_status from PayPal capture must be 'completed', not overwritten."""
        import license_delivery as ld

        pending = self._make_pending_record("order-pstatus-001")
        ld._persist_order("order-pstatus-001", pending, None)

        result = ld.confirm_payment_and_deliver(
            order_id="order-pstatus-001",
            payment_token="paypal:capture-pstatus-001",
            plan="month",
            email="buyer@example.com",
            discord="buyer#0001",
            price_usd=24.99,
        )
        assert result["ok"] is True
        order = ld.get_order("order-pstatus-001")
        assert order["payment_status"] == "completed"

    def test_idempotency_same_order_returns_same_key(self, tmp_path):
        """Calling confirm_payment_and_deliver twice returns the same key."""
        import license_delivery as ld

        pending = self._make_pending_record("order-idem-002")
        ld._persist_order("order-idem-002", pending, None)

        r1 = ld.confirm_payment_and_deliver(
            order_id="order-idem-002",
            payment_token="paypal:capture-idem-002",
            plan="month",
            email="buyer@example.com",
            discord="buyer#0001",
            price_usd=24.99,
        )
        r2 = ld.confirm_payment_and_deliver(
            order_id="order-idem-002",
            payment_token="paypal:capture-idem-002",
            plan="month",
            email="buyer@example.com",
            discord="buyer#0001",
            price_usd=24.99,
        )

        assert r1["ok"] and r2["ok"]
        assert r1["key"] == r2["key"], "Idempotent call returned different key"

    def test_no_duplicate_keys_on_refresh(self, tmp_path):
        """Multiple calls for the same order never create more than one inventory record."""
        import inventory as _inv_mod, license_delivery as ld

        for _ in range(3):
            ld.confirm_payment_and_deliver(
                order_id="order-nodup-001",
                payment_token="paypal:capture-nodup-001",
                plan="week",
                email="nodup@example.com",
                discord="nodupuser",
                price_usd=9.99,
            )

        sold = _inv_mod.list_keys(status="sold")
        assert len(sold) == 1, f"Only 1 key should be created, got {len(sold)}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Failed payment creates no key
# ─────────────────────────────────────────────────────────────────────────────

class TestFailedPaymentNoKey:
    def test_invalid_payment_token_creates_no_key(self):
        """An invalid/missing payment token must not create any key."""
        import inventory as _inv_mod, license_delivery as ld

        result = ld.confirm_payment_and_deliver(
            order_id="order-fail-001",
            payment_token="invalid-token",
            plan="month",
            email="fail@example.com",
            discord="failuser",
            price_usd=24.99,
        )
        assert result["ok"] is False
        assert _inv_mod.stats()["total"] == 0, "No key must be saved for failed payment"

    def test_missing_email_creates_no_key(self):
        import inventory as _inv_mod, license_delivery as ld

        result = ld.confirm_payment_and_deliver(
            order_id="order-noemail-001",
            payment_token="paypal:capture-valid",
            plan="month",
            email="",
            discord="validuser",
            price_usd=24.99,
        )
        assert result["ok"] is False
        assert _inv_mod.stats()["total"] == 0

    def test_unknown_plan_creates_no_key(self):
        import inventory as _inv_mod, license_delivery as ld

        result = ld.confirm_payment_and_deliver(
            order_id="order-badplan-001",
            payment_token="paypal:capture-valid",
            plan="bogusplan",
            email="badplan@example.com",
            discord="badplanuser",
            price_usd=24.99,
        )
        assert result["ok"] is False
        assert _inv_mod.stats()["total"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. Flask route integration
# ─────────────────────────────────────────────────────────────────────────────

class TestFulfillmentRoutes:
    @pytest.fixture()
    def delivery_app(self):
        import license_delivery as ld
        ld.app.config["TESTING"] = True
        return ld.app.test_client()

    def test_confirm_route_returns_key(self, delivery_app):
        resp = delivery_app.post(
            "/api/payment/confirm",
            json={
                "order_id":      "order-route-001",
                "payment_token": "paypal:capture-route-001",
                "plan":          "month",
                "email":         "route@example.com",
                "discord":       "routeuser",
                "price_usd":     24.99,
            },
            content_type="application/json",
        )
        data = resp.get_json()
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {data}"
        assert data["ok"] is True
        assert data["key"] is not None
        assert data["key"].startswith("GHOST-")
        assert data["delivery_status"] == "delivered"

    def test_fulfill_route_retry_delivers_key(self, delivery_app, tmp_path):
        """POST /api/order/<id>/fulfill must deliver a key for a pending-payment order."""
        import license_delivery as ld

        pending = {
            "order_id":       "order-retry-route-001",
            "plan":           "month",
            "plan_label":     "Phantom 1 Month",
            "email":          "retry@example.com",
            "discord":        "retryuser",
            "price_usd":      24.99,
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
        assert data.get("license_key") is not None
        assert data.get("delivery_status") == "delivered"

    def test_confirm_route_idempotent(self, delivery_app):
        """Same order_id must always return the same key."""
        payload = {
            "order_id":      "order-idem-route-001",
            "payment_token": "paypal:capture-idem-001",
            "plan":          "week",
            "email":         "idem@example.com",
            "discord":       "idemuser",
            "price_usd":     9.99,
        }
        r1 = delivery_app.post("/api/payment/confirm", json=payload,
                               content_type="application/json").get_json()
        r2 = delivery_app.post("/api/payment/confirm", json=payload,
                               content_type="application/json").get_json()
        assert r1["ok"] and r2["ok"]
        assert r1["key"] == r2["key"], "Key must be the same on retry"
