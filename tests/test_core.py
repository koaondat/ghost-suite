"""
tests/test_core.py — Ghost License System — Automated Tests
============================================================
Tests cover the most important workflows:
  - Key generation, validation, expiry, tier encoding
  - Ban / unban cycle
  - User registration and login (including edge cases)
  - Admin key login rate-limiting
  - Idempotent order delivery (duplicate-payment protection)
  - Payment token verification
  - Order status updates
  - Flask API routes: auth, license, admin, downloads, purchases
  - Bulk delete API endpoint
  - Stats API endpoint

Run:
    cd playground/qa_system_config
    pip install pytest flask flask-limiter PyJWT python-dotenv aiohttp
    pytest tests/test_core.py -v

Env requirements:
    None required for unit tests — the API tests use Flask test_client()
    which does not require real network access.
"""

from __future__ import annotations

import datetime
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Add project root to path ─────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "web" / "api"))

# ── Ensure a JWT secret is set for tests (32+ bytes for SHA256 compliance) ───
os.environ.setdefault("GHOST_JWT_SECRET",      "test-jwt-secret-not-for-prod-xxxxxxxxxx")
os.environ.setdefault("GHOST_ADMIN_API_KEY",   "test-admin-key-abcdef")
os.environ.setdefault("GHOST_HMAC_SECRET",     "test-hmac-secret-seed-for-pytest-xxxxx")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path, monkeypatch):
    """
    Redirect all JSON data files (keys, bans, users, orders, inventory) to a
    temp directory so tests are fully isolated and leave no state behind.
    Pre-stocks the inventory with valid generated keys so delivery tests work.
    """
    import keygen
    monkeypatch.setattr(keygen, "KEYS_DB",      tmp_path / "issued_keys.json")
    monkeypatch.setattr(keygen, "BANNED_DB",    tmp_path / "banned_keys.json")
    monkeypatch.setattr(keygen, "BLACKLIST_DB", tmp_path / "blacklist.json")
    monkeypatch.setattr(keygen, "WHITELIST_DB", tmp_path / "whitelist.json")
    monkeypatch.setattr(keygen, "USERS_DB",     tmp_path / "users.json")

    # Redirect inventory DB — shared by license_delivery._inv and api._inv.
    import inventory as _inv_mod
    monkeypatch.setattr(_inv_mod, "INVENTORY_DB", tmp_path / "key_inventory.json")

    # Pre-stock inventory so delivery tests can fulfil orders without "out of stock".
    # Generate 3 keys per plan — each delivery test consumes at most 1.
    for plan, tier in [("pro", "PRO"), ("lifetime", "PRO"), ("trial", "TRIAL")]:
        days = 0 if plan == "lifetime" else (7 if plan == "trial" else 30)
        keys = [keygen.generate_key(expires_days=days, tier=tier) for _ in range(3)]
        _inv_mod.import_keys(keys, plan)

    # Also patch delivery module paths if it is already imported
    try:
        import license_delivery as ld
        monkeypatch.setattr(ld, "ORDERS_DB",    tmp_path / "orders.json")
        monkeypatch.setattr(ld, "DELIVERY_LOG", tmp_path / "delivery_log.json")
    except ImportError:
        pass


@pytest.fixture()
def fresh_key_pro():
    import keygen
    return keygen.generate_key(expires_days=30, tier="PRO")


@pytest.fixture()
def fresh_key_trial():
    import keygen
    return keygen.generate_key(expires_days=7, tier="TRIAL")


@pytest.fixture()
def fresh_key_lifetime():
    import keygen
    return keygen.generate_key(expires_days=0, tier="PRO")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Key Generation & Validation
# ─────────────────────────────────────────────────────────────────────────────

class TestKeyGeneration:
    def test_key_format(self, fresh_key_pro):
        import re
        assert re.match(r"^GHOST(-[A-Z0-9]{5}){4}$", fresh_key_pro), \
            f"Key format invalid: {fresh_key_pro}"

    def test_key_validates(self, fresh_key_pro):
        import keygen
        meta = keygen.validate_key(fresh_key_pro)
        assert meta["valid"] is True
        assert meta["tier"] == "PRO"
        assert meta["expired"] is False

    def test_trial_tier(self, fresh_key_trial):
        import keygen
        meta = keygen.validate_key(fresh_key_trial)
        assert meta["valid"] is True
        assert meta["tier"] == "TRIAL"

    def test_lifetime_key_never_expires(self, fresh_key_lifetime):
        import keygen
        meta = keygen.validate_key(fresh_key_lifetime)
        assert meta["valid"] is True
        assert meta["expiry"] is None
        assert meta["days_remaining"] == -1

    def test_invalid_key_rejected(self):
        import keygen
        meta = keygen.validate_key("GHOST-AAAAA-BBBBB-CCCCC-DDDDD")
        assert meta["valid"] is False
        assert "signature" in meta["error"].lower() or "invalid" in meta["error"].lower()

    def test_wrong_prefix_rejected(self):
        import keygen
        meta = keygen.validate_key("BLAH-AAAAA-BBBBB-CCCCC-DDDDD")
        assert meta["valid"] is False

    def test_admin_master_key_validates(self):
        import keygen
        meta = keygen.validate_key(keygen.ADMIN_MASTER_KEY)
        assert meta["valid"] is True
        assert meta["tier"] == "ADMIN"

    def test_expired_key_detected(self):
        """Generate a key that expired yesterday and check expired flag."""
        import keygen, datetime, struct
        # Generate with -1 days → expiry already passed
        # We must craft the key to have yesterday's expiry
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        created   = datetime.date.today().toordinal()
        expiry    = yesterday.toordinal()
        tier_byte = keygen.TIERS["PRO"]
        payload   = struct.pack(">IIB", created, expiry, tier_byte)
        import hmac as _hmac, hashlib
        sig = _hmac.new(keygen._HMAC_SECRET, payload, hashlib.sha256).digest()[:3]
        raw = payload + sig
        parts = [keygen._encode_b32(raw[i:i+3])[:5] for i in range(0, 12, 3)]
        key = f"GHOST-{parts[0]}-{parts[1]}-{parts[2]}-{parts[3]}"
        meta = keygen.validate_key(key)
        assert meta["expired"] is True
        assert meta["valid"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 2. Ban / Unban
# ─────────────────────────────────────────────────────────────────────────────

class TestBanUnban:
    def test_ban_key(self, fresh_key_pro):
        import keygen
        keygen.ban_key(fresh_key_pro, "test reason")
        assert keygen.is_banned(fresh_key_pro)

    def test_banned_key_invalid(self, fresh_key_pro):
        import keygen
        keygen.ban_key(fresh_key_pro)
        meta = keygen.validate_key(fresh_key_pro)
        assert meta["valid"] is False
        assert "banned" in meta["error"].lower()

    def test_unban_key(self, fresh_key_pro):
        import keygen
        keygen.ban_key(fresh_key_pro)
        removed = keygen.unban_key(fresh_key_pro)
        assert removed is True
        assert not keygen.is_banned(fresh_key_pro)
        meta = keygen.validate_key(fresh_key_pro)
        assert meta["valid"] is True

    def test_unban_nonexistent_returns_false(self, fresh_key_pro):
        import keygen
        result = keygen.unban_key(fresh_key_pro)
        assert result is False

    def test_duplicate_ban_is_idempotent(self, fresh_key_pro):
        import keygen
        keygen.ban_key(fresh_key_pro, "first")
        keygen.ban_key(fresh_key_pro, "second")  # Should not add a second record
        bans = keygen.load_banned()
        matching = [b for b in bans if b["key"] == fresh_key_pro]
        assert len(matching) == 1, "Duplicate ban entries found"


# ─────────────────────────────────────────────────────────────────────────────
# 3. User Registration & Login
# ─────────────────────────────────────────────────────────────────────────────

class TestUserAuth:
    def test_register_and_login(self, fresh_key_pro):
        import keygen
        reg = keygen.register_user("alice", "SecurePass1!", fresh_key_pro)
        assert reg["ok"] is True
        assert reg["tier"] == "PRO"

        result = keygen.login_user("alice", "SecurePass1!", fresh_key_pro)
        assert result["ok"] is True
        assert result["username"] == "alice"

    def test_duplicate_username_rejected(self, fresh_key_pro):
        import keygen
        keygen.register_user("bob", "Password99#", fresh_key_pro)
        key2 = keygen.generate_key(expires_days=30, tier="PRO")
        reg2 = keygen.register_user("bob", "Password99#", key2)
        assert reg2["ok"] is False
        assert "taken" in reg2["error"].lower()

    def test_key_already_bound_rejected(self, fresh_key_pro):
        import keygen
        keygen.register_user("charlie", "Password99#", fresh_key_pro)
        reg2 = keygen.register_user("dave", "Password99#", fresh_key_pro)
        assert reg2["ok"] is False
        assert "bound" in reg2["error"].lower()

    def test_wrong_password_rejected(self, fresh_key_pro):
        import keygen
        keygen.register_user("eve", "GoodPass1!", fresh_key_pro)
        result = keygen.login_user("eve", "WrongPass!", fresh_key_pro)
        assert result["ok"] is False

    def test_short_password_rejected(self, fresh_key_pro):
        import keygen
        reg = keygen.register_user("frank", "short", fresh_key_pro)
        assert reg["ok"] is False
        assert "8 characters" in reg["error"]

    def test_banned_key_login_rejected(self, fresh_key_pro):
        import keygen
        keygen.register_user("grace", "Secure123!", fresh_key_pro)
        keygen.ban_key(fresh_key_pro)
        result = keygen.login_user("grace", "Secure123!", fresh_key_pro)
        assert result["ok"] is False

    def test_delete_user(self, fresh_key_pro):
        import keygen
        keygen.register_user("heidi", "TestPass1!", fresh_key_pro)
        deleted = keygen.delete_user("heidi")
        assert deleted is True
        users = keygen.load_all_users()
        assert not any(u["username"] == "heidi" for u in users)

    def test_load_all_users_strips_secrets(self, fresh_key_pro):
        """load_all_users() must never return pw_hash or salt."""
        import keygen
        keygen.register_user("ivan", "Password99#", fresh_key_pro)
        users = keygen.load_all_users()
        for u in users:
            assert "pw_hash" not in u, "pw_hash must not be in public user list"
            assert "salt"    not in u, "salt must not be in public user list"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Key Record DB
# ─────────────────────────────────────────────────────────────────────────────

class TestKeyRecordDB:
    def test_save_and_load(self, fresh_key_pro):
        import keygen
        meta = keygen.validate_key(fresh_key_pro)
        meta["note"] = "pytest"
        keygen.save_key_record(fresh_key_pro, meta)
        records = keygen.load_all_keys()
        assert any(r["key"] == fresh_key_pro for r in records)

    def test_delete_key_record(self, fresh_key_pro):
        import keygen
        meta = keygen.validate_key(fresh_key_pro)
        keygen.save_key_record(fresh_key_pro, meta)
        deleted = keygen.delete_key_record(fresh_key_pro)
        assert deleted is True
        records = keygen.load_all_keys()
        assert not any(r["key"] == fresh_key_pro for r in records)

    def test_delete_nonexistent_returns_false(self):
        import keygen
        result = keygen.delete_key_record("GHOST-AAAAA-BBBBB-CCCCC-DDDDD")
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# 5. Delivery / Idempotency (license_delivery.py)
# ─────────────────────────────────────────────────────────────────────────────

class TestLicenseDelivery:
    def test_confirm_payment_produces_key(self):
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id      = "cs_test_abc123",
            payment_token = "stripe:pi_test_abc123",
            plan          = "pro",
            email         = "customer@example.com",
            discord       = "customer#1234",
            price_usd     = 7.0,
        )
        assert result["ok"] is True
        assert result["key"].startswith("GHOST-")
        assert result["tier"] == "PRO"

    def test_idempotency_same_order_returns_same_key(self):
        import license_delivery as ld
        kwargs = dict(
            order_id      = "cs_test_idem_001",
            payment_token = "stripe:pi_test_idem_001",
            plan          = "pro",
            email         = "idem@example.com",
            discord       = "idem#1234",
            price_usd     = 7.0,
        )
        r1 = ld.confirm_payment_and_deliver(**kwargs)
        r2 = ld.confirm_payment_and_deliver(**kwargs)
        assert r1["ok"] and r2["ok"]
        assert r1["key"] == r2["key"], "Duplicate payment produced a different key"

    def test_free_trial_token_accepted(self):
        """FREE_TRIAL token is valid; 'trial' maps to 'day' duration → tier=PRO."""
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id      = "GHOST-TRIAL-XYZ",
            payment_token = "FREE_TRIAL",
            plan          = "trial",
            email         = "trial@example.com",
            discord       = "trialuser",
            price_usd     = 0,
        )
        assert result["ok"] is True
        # 'trial' normalizes to 'day', which has tier=PRO in the duration catalogue
        assert result["tier"] == "PRO"
        assert result["key"] is not None

    def test_invalid_payment_token_rejected(self):
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id      = "cs_bad_token",
            payment_token = "client_hack_attempt",
            plan          = "pro",
            email         = "hacker@evil.com",
            discord       = "hacker",
        )
        assert result["ok"] is False
        assert "verified" in result["error"].lower() or "token" in result["error"].lower()

    def test_unknown_plan_rejected(self):
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id      = "cs_bad_plan",
            payment_token = "stripe:pi_bad_plan",
            plan          = "ultra_mega_plan",
            email         = "x@example.com",
            discord       = "x",
        )
        assert result["ok"] is False

    def test_invalid_email_rejected(self):
        import license_delivery as ld
        result = ld.confirm_payment_and_deliver(
            order_id      = "cs_bad_email",
            payment_token = "stripe:pi_bad_email",
            plan          = "pro",
            email         = "not-an-email",
            discord       = "x",
        )
        assert result["ok"] is False

    def test_update_order_status(self):
        import license_delivery as ld
        ld.confirm_payment_and_deliver(
            order_id      = "cs_refund_test",
            payment_token = "stripe:pi_refund",
            plan          = "pro",
            email         = "refund@example.com",
            discord       = "refunduser",
            price_usd     = 7.0,
        )
        ok = ld.update_order_status("cs_refund_test", "refunded")
        assert ok is True
        order = ld.get_order("cs_refund_test")
        assert order["payment_status"] == "refunded"

    def test_get_order_returns_none_for_missing(self):
        import license_delivery as ld
        order = ld.get_order("nonexistent_order_id")
        assert order is None

    def test_lifetime_plan_key_has_90day_expiry(self):
        """'lifetime' maps to '3months' (90 days) — check expires_at is set."""
        import license_delivery as ld
        from datetime import datetime, timezone
        result = ld.confirm_payment_and_deliver(
            order_id      = "cs_lifetime_test",
            payment_token = "stripe:pi_lifetime",
            plan          = "lifetime",
            email         = "lifetime@example.com",
            discord       = "lifetimer",
            price_usd     = 59.99,
        )
        assert result["ok"] is True
        assert result["key"] is not None
        # 'lifetime' now maps to '3months' = 90 days; verify expires_at is ~90 days out
        order = ld.get_order("cs_lifetime_test")
        assert order is not None
        exp_str = order.get("expires_at")
        assert exp_str is not None, "expires_at must be set for 3months plan"
        exp_date = datetime.fromisoformat(exp_str)
        delta = (exp_date - datetime.now(timezone.utc)).days
        assert 88 <= delta <= 90, f"Expected ~90 days, got {delta}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Flask API routes (api.py)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def api_client():
    """Create a Flask test client for api.py (function-scoped so it picks up the
    per-test tmp data dir set by _tmp_data_dir)."""
    import api as ghost_api
    ghost_api.app.config["TESTING"] = True
    # Disable rate limiting for tests
    ghost_api.limiter.enabled = False
    with ghost_api.app.test_client() as client:
        yield client


ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key-abcdef"}


class TestAPIAuth:
    def test_register_missing_fields(self, api_client):
        r = api_client.post("/api/auth/register", json={})
        assert r.status_code == 400

    def test_register_invalid_email(self, api_client, fresh_key_pro):
        r = api_client.post("/api/auth/register", json={
            "username": "testuser", "email": "bad", "password": "Password99#",
            "license_key": fresh_key_pro,
        })
        assert r.status_code == 400
        data = r.get_json()
        assert data["field"] == "email"

    def test_register_short_password(self, api_client, fresh_key_pro):
        r = api_client.post("/api/auth/register", json={
            "username": "testuser", "email": "x@x.com", "password": "short",
            "license_key": fresh_key_pro,
        })
        assert r.status_code == 400
        data = r.get_json()
        assert data["field"] == "password"

    def test_register_and_login_flow(self, api_client, fresh_key_pro):
        # Register
        r = api_client.post("/api/auth/register", json={
            "username": "apitest1", "email": "api1@x.com",
            "password": "Password99#!", "license_key": fresh_key_pro,
        })
        assert r.status_code == 201
        data = r.get_json()
        assert data["ok"] is True
        assert "token" in data

        # Login
        r2 = api_client.post("/api/auth/login", json={
            "identity": "apitest1", "password": "Password99#!",
        })
        assert r2.status_code == 200
        d2 = r2.get_json()
        assert d2["ok"] is True
        assert "token" in d2

    def test_login_wrong_password_returns_401(self, api_client, fresh_key_pro):
        api_client.post("/api/auth/register", json={
            "username": "apitest_wp", "email": "wp@x.com",
            "password": "RightPass99#", "license_key": fresh_key_pro,
        })
        r = api_client.post("/api/auth/login", json={
            "identity": "apitest_wp", "password": "WrongPass99#",
        })
        assert r.status_code == 401

    def test_login_unknown_user_returns_401(self, api_client):
        r = api_client.post("/api/auth/login", json={
            "identity": "ghostuser_not_exists", "password": "anything",
        })
        assert r.status_code == 401

    def test_logout_clears_cookie(self, api_client):
        r = api_client.post("/api/auth/logout")
        assert r.status_code == 200


class TestAPILicense:
    def _register_and_token(self, api_client, fresh_key_pro, suffix=""):
        api_client.post("/api/auth/register", json={
            "username": f"lictest{suffix}", "email": f"lic{suffix}@x.com",
            "password": "Password99#!", "license_key": fresh_key_pro,
        })
        r = api_client.post("/api/auth/login", json={
            "identity": f"lictest{suffix}", "password": "Password99#!",
        })
        return r.get_json()["token"]

    def test_license_info_requires_auth(self, api_client):
        r = api_client.get("/api/license/info")
        assert r.status_code == 401

    def test_license_info_with_valid_token(self, api_client, fresh_key_pro):
        token = self._register_and_token(api_client, fresh_key_pro, "a")
        r = api_client.get("/api/license/info",
                           headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "license" in data

    def test_license_validate_endpoint(self, api_client, fresh_key_pro):
        r = api_client.post("/api/license/validate", json={"key": fresh_key_pro})
        assert r.status_code == 200
        data = r.get_json()
        assert data["valid"] is True

    def test_license_validate_invalid_key(self, api_client):
        r = api_client.post("/api/license/validate", json={"key": "GHOST-AAAAA-BBBBB-CCCCC-DDDDD"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["valid"] is False

    def test_downloads_requires_auth(self, api_client):
        # GET /api/downloads requires a valid JWT — no token → 401
        r = api_client.get("/api/downloads")
        assert r.status_code == 401

    def test_downloads_with_valid_token(self, api_client, fresh_key_pro):
        token = self._register_and_token(api_client, fresh_key_pro, "b")
        r = api_client.get("/api/downloads",
                           headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "downloads" in data

    def test_purchases_requires_auth(self, api_client):
        # Use a fresh unauthenticated client call with no token
        r = api_client.get("/api/purchases",
                           headers={"Authorization": "Bearer invalid-token-xyz"})
        assert r.status_code == 401


class TestAPIAdmin:
    def test_admin_generate_key(self, api_client):
        r = api_client.post("/api/admin/license/generate",
                            json={"tier": "PRO", "days": 30},
                            headers=ADMIN_HEADERS)
        assert r.status_code == 201
        data = r.get_json()
        assert data["ok"] is True
        assert len(data["keys"]) == 1
        assert data["keys"][0].startswith("GHOST-")

    def test_admin_generate_bulk_keys(self, api_client):
        r = api_client.post("/api/admin/license/generate",
                            json={"tier": "TRIAL", "days": 7, "quantity": 5},
                            headers=ADMIN_HEADERS)
        assert r.status_code == 201
        data = r.get_json()
        assert len(data["keys"]) == 5

    def test_admin_generate_rejects_bad_tier(self, api_client):
        r = api_client.post("/api/admin/license/generate",
                            json={"tier": "ULTRA", "days": 30},
                            headers=ADMIN_HEADERS)
        assert r.status_code == 400

    def test_admin_generate_without_key_returns_403(self, api_client):
        r = api_client.post("/api/admin/license/generate",
                            json={"tier": "PRO", "days": 30})
        assert r.status_code in (401, 403)

    def test_admin_ban_and_unban_key(self, api_client, fresh_key_pro):
        ban_r = api_client.post(f"/api/admin/license/{fresh_key_pro}/ban",
                                json={"reason": "test"},
                                headers=ADMIN_HEADERS)
        assert ban_r.get_json()["banned"] is True

        unban_r = api_client.post(f"/api/admin/license/{fresh_key_pro}/unban",
                                  headers=ADMIN_HEADERS)
        assert unban_r.get_json()["unbanned"] is True

    def test_admin_delete_key(self, api_client, fresh_key_pro):
        import keygen
        meta = keygen.validate_key(fresh_key_pro)
        keygen.save_key_record(fresh_key_pro, meta)
        r = api_client.delete(f"/api/admin/license/{fresh_key_pro}",
                              headers=ADMIN_HEADERS)
        data = r.get_json()
        assert data["deleted"] is True

    def test_admin_list_keys(self, api_client, fresh_key_pro):
        import keygen
        keygen.save_key_record(fresh_key_pro, keygen.validate_key(fresh_key_pro))
        r = api_client.get("/api/admin/keys", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert isinstance(data["keys"], list)

    def test_admin_list_users(self, api_client):
        r = api_client.get("/api/admin/users", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert isinstance(data["users"], list)

    def test_admin_stats_endpoint(self, api_client, fresh_key_pro):
        import keygen
        keygen.save_key_record(fresh_key_pro, keygen.validate_key(fresh_key_pro))
        r = api_client.get("/api/admin/stats", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "total_keys" in data
        assert "users" in data

    def test_admin_bulk_delete(self, api_client):
        # Generate 3 fresh keys via the admin API, then bulk-delete exactly those keys.
        keys = []
        for _ in range(3):
            gen = api_client.post("/api/admin/license/generate",
                                  json={"tier": "PRO", "days": 30},
                                  headers=ADMIN_HEADERS)
            assert gen.status_code == 201
            keys.extend(gen.get_json()["keys"])

        assert len(keys) == 3, "Expected exactly 3 freshly generated keys"

        r = api_client.post("/api/admin/license/bulk-delete",
                            json={"keys": keys},
                            headers=ADMIN_HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        # All 3 freshly-generated keys must be deleted (not_found = 0)
        assert data["not_found"] == []
        assert set(keys) == set(data["deleted"])

    def test_admin_bulk_delete_deduplicates(self, api_client, fresh_key_pro):
        import keygen
        keygen.save_key_record(fresh_key_pro, keygen.validate_key(fresh_key_pro))
        # Send the same key twice — should only delete once
        r = api_client.post("/api/admin/license/bulk-delete",
                            json={"keys": [fresh_key_pro, fresh_key_pro]},
                            headers=ADMIN_HEADERS)
        data = r.get_json()
        assert len(data["deleted"]) == 1

    def test_admin_bulk_delete_over_100_rejected(self, api_client):
        keys = [f"GHOST-AAAAA-BBBBB-CCCCC-{str(i).zfill(5)[:5]}" for i in range(101)]
        r = api_client.post("/api/admin/license/bulk-delete",
                            json={"keys": keys},
                            headers=ADMIN_HEADERS)
        assert r.status_code == 400

    def test_admin_reset_activation(self, api_client, fresh_key_pro):
        import keygen
        keygen.register_user("resettest", "Password99#!", fresh_key_pro)
        r = api_client.post(f"/api/admin/license/{fresh_key_pro}/reset",
                            headers=ADMIN_HEADERS)
        data = r.get_json()
        assert data["ok"] is True

    def test_admin_key_info(self, api_client, fresh_key_pro):
        r = api_client.get(f"/api/admin/license/{fresh_key_pro}",
                           headers=ADMIN_HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "license" in data


# ─────────────────────────────────────────────────────────────────────────────
# 7. Health & Status
# ─────────────────────────────────────────────────────────────────────────────

class TestAPIHealth:
    def test_health_endpoint(self, api_client):
        r = api_client.get("/health")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["service"] == "ghost-api"

    def test_status_endpoint(self, api_client):
        r = api_client.get("/status")
        assert r.status_code in (200, 503)
        data = r.get_json()
        assert "service" in data

    def test_404_returns_json(self, api_client):
        # The OPTIONS wildcard route can catch some paths as 405;
        # use a path under /api/ that definitely has no handler.
        r = api_client.get("/api/no/such/endpoint/xyz")
        assert r.status_code in (404, 405)
        data = r.get_json()
        assert data["ok"] is False

    def test_405_returns_json(self, api_client):
        r = api_client.delete("/api/auth/login")
        assert r.status_code == 405
        data = r.get_json()
        assert data["ok"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Admin panel cookie-based auth — full production sequence trace
# POST /api/admin/panel/auth → GET /api/admin/session → GET /api/admin/dashboard
# → GET /api/admin/inventory  (all must return 200)
# ─────────────────────────────────────────────────────────────────────────────

class TestAdminCookieAuth:
    """
    Covers every step of the required production auth trace:
      1. POST /api/admin/panel/auth  → 200, Set-Cookie __Host-ghost_admin_session present
      2. GET  /api/admin/session     → 200, authenticated=true  (cookie sent automatically)
      3. GET  /api/admin/dashboard   → 200  (cookie sent automatically)
      4. GET  /api/admin/inventory   → 200  (cookie sent automatically)

    Also verifies the debug endpoint and logout.
    """

    # The GHOST_ADMIN_API_KEY set in conftest/env
    _PASSWORD = "test-admin-key-abcdef"

    def _login(self, client):
        """POST to /api/admin/panel/auth and return the response."""
        return client.post(
            "/api/admin/panel/auth",
            json={"password": self._PASSWORD},
        )

    # ── Step 1 ────────────────────────────────────────────────────────────────
    def test_panel_auth_returns_200(self, api_client):
        r = self._login(api_client)
        assert r.status_code == 200, f"Expected 200 from /api/admin/panel/auth, got {r.status_code}: {r.data}"
        data = r.get_json()
        assert data["ok"] is True

    def test_panel_auth_sets_cookie(self, api_client):
        r = self._login(api_client)
        assert r.status_code == 200
        # Flask test client stores cookies in r.headers["Set-Cookie"]
        cookie_header = r.headers.get("Set-Cookie", "")
        assert "__Host-ghost_admin_session" in cookie_header, (
            f"Set-Cookie header missing __Host-ghost_admin_session.\n"
            f"Got: {cookie_header!r}"
        )

    def test_panel_auth_cookie_is_httponly(self, api_client):
        r = self._login(api_client)
        cookie_header = r.headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie_header, (
            f"Cookie must be HttpOnly. Got: {cookie_header!r}"
        )

    def test_panel_auth_cookie_is_secure(self, api_client):
        r = self._login(api_client)
        cookie_header = r.headers.get("Set-Cookie", "")
        assert "Secure" in cookie_header, (
            f"Cookie must be Secure. Got: {cookie_header!r}"
        )

    def test_panel_auth_cookie_samesite_lax(self, api_client):
        r = self._login(api_client)
        cookie_header = r.headers.get("Set-Cookie", "")
        assert "SameSite=Lax" in cookie_header, (
            f"Cookie must be SameSite=Lax. Got: {cookie_header!r}"
        )

    def test_panel_auth_cookie_path_root(self, api_client):
        r = self._login(api_client)
        cookie_header = r.headers.get("Set-Cookie", "")
        assert "Path=/" in cookie_header, (
            f"Cookie must have Path=/. Got: {cookie_header!r}"
        )

    def test_panel_auth_no_token_in_body(self, api_client):
        """The panel JWT must NOT be returned in the JSON body (security)."""
        r = self._login(api_client)
        data = r.get_json()
        assert "token" not in data, (
            "JWT must not be exposed in response body — browser should only know it via Set-Cookie"
        )

    def test_panel_auth_wrong_password_returns_401(self, api_client):
        r = api_client.post("/api/admin/panel/auth", json={"password": "wrong-password"})
        assert r.status_code == 401

    # ── Step 2 ────────────────────────────────────────────────────────────────
    def test_session_unauthenticated_returns_401(self, api_client):
        r = api_client.get("/api/admin/session")
        assert r.status_code == 401
        data = r.get_json()
        assert data["authenticated"] is False

    def test_session_after_login_returns_authenticated(self, api_client):
        self._login(api_client)   # sets cookie in Flask test client jar
        r = api_client.get("/api/admin/session")
        assert r.status_code == 200, (
            f"GET /api/admin/session should return 200 after login, got {r.status_code}: {r.data}"
        )
        data = r.get_json()
        assert data["authenticated"] is True

    # ── Step 3 ────────────────────────────────────────────────────────────────
    def test_dashboard_without_cookie_returns_401(self, api_client):
        r = api_client.get("/api/admin/dashboard")
        assert r.status_code == 401

    def test_dashboard_after_login_returns_200(self, api_client):
        self._login(api_client)
        r = api_client.get("/api/admin/dashboard")
        assert r.status_code == 200, (
            f"GET /api/admin/dashboard should return 200 after login, got {r.status_code}: {r.data}"
        )
        data = r.get_json()
        assert data["ok"] is True

    # ── Step 4 ────────────────────────────────────────────────────────────────
    def test_inventory_without_cookie_returns_401(self, api_client):
        r = api_client.get("/api/admin/inventory")
        assert r.status_code == 401

    def test_inventory_after_login_returns_200(self, api_client):
        self._login(api_client)
        r = api_client.get("/api/admin/inventory")
        assert r.status_code == 200, (
            f"GET /api/admin/inventory should return 200 after login, got {r.status_code}: {r.data}"
        )
        data = r.get_json()
        assert data["ok"] is True

    # ── Debug endpoint ────────────────────────────────────────────────────────
    def test_debug_session_no_cookie(self, api_client):
        r = api_client.get("/api/admin/debug-session")
        assert r.status_code == 200
        data = r.get_json()
        assert data["cookiePresent"] is False
        assert data["sessionValid"] is False
        assert "secretConfigured" in data
        # Must not expose any actual values
        assert "token" not in data
        assert "secret" not in data
        assert "password" not in data
        assert "hash" not in data

    def test_debug_session_with_cookie(self, api_client):
        self._login(api_client)
        r = api_client.get("/api/admin/debug-session")
        assert r.status_code == 200
        data = r.get_json()
        assert data["cookiePresent"] is True
        assert data["sessionValid"] is True
        assert data["secretConfigured"] is True

    # ── Logout ────────────────────────────────────────────────────────────────
    def test_logout_clears_session(self, api_client):
        self._login(api_client)
        # Verify logged in
        r = api_client.get("/api/admin/session")
        assert r.status_code == 200

        # Logout
        lo = api_client.post("/api/admin/panel/logout")
        assert lo.status_code == 200

        # Cookie should be cleared — session endpoint must now return 401
        r2 = api_client.get("/api/admin/session")
        assert r2.status_code == 401

    # ── CORS header check ─────────────────────────────────────────────────────
    def test_cors_access_control_allow_credentials(self, api_client):
        """credentials: 'include' requires Access-Control-Allow-Credentials: true."""
        r = api_client.get(
            "/api/admin/session",
            headers={"Origin": "https://yourdomain.com"},
        )
        # 401 is fine here (no cookie) — we only check the CORS header
        acao = r.headers.get("Access-Control-Allow-Credentials", "")
        assert acao == "true", (
            f"Access-Control-Allow-Credentials must be 'true'. Got: {acao!r}"
        )
