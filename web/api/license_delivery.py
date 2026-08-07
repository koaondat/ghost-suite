"""
license_delivery.py — Ghost License Delivery Backend (Inventory Edition)
========================================================================
Flask server that assigns pre-stocked inventory keys after payment.

NO keys are ever generated here. After PayPal returns COMPLETED:
  1. Find the first unused key matching the purchased plan.
  2. Mark it as assigned (order_id, email, discord, purchase_date).
  3. Return that key to the customer.

If no keys are available:
  - Record the order as payment_status=verified, delivery_status=out_of_stock.
  - Return a clear out-of-stock notice (never refunds automatically).

Idempotent: same order_id always returns the same key.
One order → one key. Retry delivery never assigns a second key.

Endpoints
---------
POST /api/payment/confirm
GET  /api/order/<order_id>
GET  /api/order/<order_id>/download
PATCH /api/order/<order_id>/status
POST /api/order/<order_id>/fulfill   (retry delivery for out-of-stock orders)

Storage
-------
Redis (Upstash) when UPSTASH_REDIS_REST_URL+TOKEN are set.
Falls back to orders.json on disk for local dev only.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac as _hmac_mod
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ── Project root resolution ──────────────────────────────────────────────────
_HERE         = Path(__file__).resolve().parent          # web/api/
_PROJECT_ROOT = _HERE.parent.parent                      # qa_system_config/

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import inventory as _inv  # type: ignore  # noqa: E402

# ── Data files ────────────────────────────────────────────────────────────────
ORDERS_DB    = _PROJECT_ROOT / "orders.json"
DELIVERY_LOG = _PROJECT_ROOT / "delivery_log.json"

# ── Env / Redis ───────────────────────────────────────────────────────────────
from dotenv import load_dotenv  # type: ignore
load_dotenv(_PROJECT_ROOT / ".env")

_REDIS_URL   = (os.environ.get("UPSTASH_REDIS_REST_URL")   or "").rstrip("/")
_REDIS_TOKEN = (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "")
_USE_REDIS   = bool(_REDIS_URL and _REDIS_TOKEN)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("ghost.delivery")

if _USE_REDIS:
    log.info("Storage: Upstash Redis")
else:
    log.warning("Storage: local file (%s). Set UPSTASH_REDIS_REST_URL for production.", ORDERS_DB)

# ── Plan catalogue (plan slug → tier label) ───────────────────────────────────
PLAN_PRICES: dict[str, dict[str, Any]] = {
    "trial":    {"priceUsd": 0,  "label": "Ghost Trial",            "tier": "TRIAL"},
    "pro":      {"priceUsd": 7,  "label": "Ghost Pro (monthly)",    "tier": "PRO"},
    "lifetime": {"priceUsd": 79, "label": "Ghost Lifetime",         "tier": "PRO"},
}

_ALLOWED_TOKEN_PREFIXES  = ("paypal:", "stripe:", "cashapp:", "crypto:")
_ALLOWED_TOKEN_LITERALS  = {"FREE_TRIAL"}

_orders_lock     = threading.Lock()
_inventory_lock  = threading.Lock()


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _redis_request(command: list) -> Any:
    url = f"{_REDIS_URL}/{'/'.join(str(c) for c in command)}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(f"Redis request failed: {exc}") from exc
    if "error" in body:
        raise RuntimeError(f"Redis error: {body['error']}")
    return body.get("result")


def _redis_set_order(order_id: str, record: dict) -> None:
    key     = f"ghost:order:{order_id}"
    value   = json.dumps(record, default=str)
    payload = json.dumps([
        ["SET", key, value],
        ["ZADD", "ghost:orders:index", str(int(time.time())), order_id],
    ]).encode()
    req = urllib.request.Request(
        f"{_REDIS_URL}/pipeline",
        data=payload,
        headers={"Authorization": f"Bearer {_REDIS_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(f"Redis SET failed for order {order_id}: {exc}") from exc
    for item in (result if isinstance(result, list) else []):
        if isinstance(item, dict) and "error" in item:
            raise RuntimeError(f"Redis pipeline error: {item['error']}")


def _redis_get_order(order_id: str) -> dict | None:
    key = f"ghost:order:{order_id}"
    try:
        raw = _redis_request(["GET", urllib.parse.quote(key, safe="")])
    except Exception as exc:
        log.error("Redis GET order %s failed: %s", order_id, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _redis_get_all_orders() -> list[dict]:
    try:
        order_ids = _redis_request(["ZRANGE", "ghost:orders:index", "0", "-1"])
    except Exception as exc:
        log.error("Redis ZRANGE failed: %s", exc)
        return []
    if not order_ids:
        return []
    return [r for oid in order_ids if (r := _redis_get_order(oid))]


# ── File-based order storage ──────────────────────────────────────────────────

def _load_orders() -> list[dict]:
    if _USE_REDIS:
        return _redis_get_all_orders()
    if ORDERS_DB.exists():
        try:
            return json.loads(ORDERS_DB.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_orders(records: list[dict]) -> None:
    if _USE_REDIS:
        for record in records:
            oid = record.get("order_id", "")
            if oid:
                _redis_set_order(oid, record)
        return
    tmp = ORDERS_DB.with_suffix(ORDERS_DB.suffix + ".tmp")
    tmp.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    tmp.replace(ORDERS_DB)


def _load_single_order(order_id: str) -> dict | None:
    if _USE_REDIS:
        return _redis_get_order(order_id)
    return _find_order(order_id, _load_orders())


def _find_order(order_id: str, records: list[dict]) -> dict | None:
    return next((r for r in records if r.get("order_id", "") == order_id.strip()), None)


# ── Redis inventory helpers ───────────────────────────────────────────────────
# The admin panel (server.js) stores generated keys in Redis under the key
# "ghost:inventory".  When Redis is configured, the delivery backend reads
# and writes inventory there so that both halves see the same stock.
# When Redis is not configured, we fall back to inventory.py (local JSON file).

_REDIS_INVENTORY_KEY = "ghost:inventory"


def _redis_get_inventory() -> list[dict]:
    """Load inventory array from Redis. Returns [] on any error."""
    try:
        raw = _redis_request(["GET", urllib.parse.quote(_REDIS_INVENTORY_KEY, safe="")])
    except Exception as exc:
        log.error("Redis GET inventory failed: %s", exc)
        return []
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _redis_set_inventory(records: list[dict]) -> None:
    """Persist inventory array to Redis."""
    value   = json.dumps(records, default=str)
    payload = json.dumps([["SET", _REDIS_INVENTORY_KEY, value]]).encode()
    req = urllib.request.Request(
        f"{_REDIS_URL}/pipeline",
        data=payload,
        headers={"Authorization": f"Bearer {_REDIS_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        for item in (result if isinstance(result, list) else []):
            if isinstance(item, dict) and "error" in item:
                raise RuntimeError(f"Redis pipeline error: {item['error']}")
    except Exception as exc:
        raise RuntimeError(f"Redis SET inventory failed: {exc}") from exc


def _inv_get_next_available(plan: str) -> dict | None:
    """
    Return the first available inventory record matching the canonical plan slug.
    Uses Redis when configured; falls back to local inventory.py.
    """
    canonical = _inv.normalize_plan(plan)
    if not _USE_REDIS:
        return _inv.get_next_unused(canonical)
    records = _redis_get_inventory()
    return next(
        (r for r in records
         if _inv.normalize_plan(r.get("plan", "")) == canonical
         and r.get("status") in ("available", "unused")),
        None,
    )


def _inv_assign_key(key: str, order_id: str, customer_email: str,
                    assigned_user: str, purchase_date: str) -> bool:
    """
    Mark a key as sold.  Writes to Redis when configured, local file otherwise.
    Returns True on success, False if key not found or already sold.
    """
    if not _USE_REDIS:
        return _inv.assign_key(
            key=key,
            order_id=order_id,
            customer_email=customer_email,
            assigned_user=assigned_user,
            purchase_date=purchase_date,
        )

    clean = key.strip().upper()
    with _inventory_lock:
        records = _redis_get_inventory()
        for r in records:
            if r.get("key", "").upper() == clean:
                if r.get("status") == "sold" and r.get("order_id") == order_id:
                    return True   # idempotent
                if r.get("status") not in ("available", "unused", "reserved"):
                    return False
                r["status"]         = "sold"
                r["order_id"]       = order_id
                r["customer_email"] = customer_email
                r["customer"]       = customer_email or assigned_user
                r["assigned_user"]  = assigned_user
                r["purchase_date"]  = purchase_date
                r["purchaseDate"]   = purchase_date  # legacy alias used by server.js
                _redis_set_inventory(records)
                return True
    return False


def _inv_load_all() -> list[dict]:
    """Load all inventory records. Uses Redis when configured."""
    if _USE_REDIS:
        return [_inv._migrate_record(r) for r in _redis_get_inventory()]
    return _inv._load_migrated()


def _log_delivery_failure(order_id: str, reason: str, detail: str = "") -> None:
    try:
        records: list[dict] = []
        if DELIVERY_LOG.exists():
            try:
                records = json.loads(DELIVERY_LOG.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        records.append({
            "order_id":  order_id,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "reason":    reason,
            "detail":    detail[:800] if detail else "",
        })
        DELIVERY_LOG.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        log.error("Could not write delivery_log: %s", exc)


def _verify_payment_token(payment_token: str) -> bool:
    """
    Accept tokens produced by any supported payment provider.
    Extensible: add new prefixes to _ALLOWED_TOKEN_PREFIXES without
    touching assignment logic.
    """
    if not payment_token:
        return False
    token = payment_token.strip()
    if token in _ALLOWED_TOKEN_LITERALS:
        return True
    return any(token.startswith(p) for p in _ALLOWED_TOKEN_PREFIXES)


# ── Core delivery ─────────────────────────────────────────────────────────────

def confirm_payment_and_deliver(
    *,
    order_id:           str,
    payment_token:      str,
    plan:               str,
    email:              str,
    discord:            str,
    price_usd:          float | None = None,
    paypal_order_id:    str | None   = None,
    data_extra:         dict | None  = None,
) -> dict:
    """
    Confirm payment and assign a license key from inventory.

    Idempotent — same order_id always returns the same key.
    One order may only ever receive one key.
    """
    order_id = order_id.strip() if order_id else ""
    # Normalise the plan slug — generator may store 'ghost_pro_monthly', checkout sends 'pro'.
    plan     = _inv.normalize_plan((plan or "").strip().lower())

    if not order_id:
        return {"ok": False, "error": "order_id is required"}
    if not plan or plan not in PLAN_PRICES:
        return {"ok": False, "error": f"Unknown plan '{plan}'. Choose from: {list(PLAN_PRICES)}"}
    if not email or "@" not in email:
        return {"ok": False, "error": "A valid email is required"}
    if not discord or len(discord.strip()) < 2:
        return {"ok": False, "error": "Discord username is required"}
    if not _verify_payment_token(payment_token):
        log.warning("Payment token rejected for order %s", order_id)
        _log_delivery_failure(order_id, "payment_token_rejected")
        return {"ok": False, "error": "Payment could not be verified"}

    resolved_price = price_usd if price_usd is not None else float(PLAN_PRICES[plan]["priceUsd"])
    plan_label     = PLAN_PRICES[plan]["label"]

    with _orders_lock:
        # ── Step 1: Load order record ─────────────────────────────────────
        existing = _load_single_order(order_id) if _USE_REDIS else _find_order(order_id, _load_orders())
        log.info("fulfill order=%s step=order_loaded existing=%s", order_id, bool(existing))

        # ── Idempotency: already delivered? ──────────────────────────────
        if existing:
            if existing.get("payment_verified") and existing.get("license_key"):
                log.info("fulfill order=%s step=idempotent returning_existing_key", order_id)
                return _safe_response(existing)
            if existing.get("delivery_status") == "out_of_stock":
                # Try to fulfill from inventory now that more keys may be stocked
                log.info("fulfill order=%s step=retry_out_of_stock", order_id)
                pass  # fall through to inventory lookup below

        # ── Step 2: Check available licenses in inventory ─────────────────
        all_inv        = _inv_load_all()
        inv_total      = len(all_inv)
        inv_available  = [r for r in all_inv if r.get("status") in ("available", "unused")]
        avail_total    = len(inv_available)
        plan_available = [r for r in inv_available if _inv.normalize_plan(r.get("plan", "")) == plan]
        match_count    = len(plan_available)
        log.info(
            "fulfill order=%s step=available_licenses_found plan=%s "
            "inventory_total=%d available_total=%d matching_plan=%d",
            order_id, plan, inv_total, avail_total, match_count,
        )

        # ── Step 3: Pick the next available key ───────────────────────────
        inv_record = _inv_get_next_available(plan)
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if inv_record is None:
            # ── Out of stock — never leave delivery_status=pending ────────
            log.warning(
                "fulfill order=%s step=out_of_stock plan=%s — setting delivery_status=out_of_stock",
                order_id, plan,
            )
            _log_delivery_failure(order_id, "out_of_stock", f"plan={plan}")

            order_record: dict = {
                "order_id":         order_id,
                "paypal_capture_id": (payment_token or "").removeprefix("paypal:") if payment_token.startswith("paypal:") else "",
                "paypal_order_id":  paypal_order_id or (data_extra or {}).get("paypal_order_id", ""),
                "plan":             plan,
                "plan_label":       plan_label,
                "email":            email,
                "discord":          discord,
                "price_usd":        resolved_price,
                "currency":         "USD",
                "created_at":       created_at,
                "payment_status":   "verified",
                "payment_verified": True,
                "delivery_status":  "out_of_stock",
                "license_key":      None,
                "license_status":   "pending",
            }
            _persist_order(order_id, order_record, existing)
            log.info("fulfill order=%s step=order_updated delivery_status=out_of_stock", order_id)
            return {
                "ok":               True,
                "out_of_stock":     True,
                "order_id":         order_id,
                "plan":             plan,
                "email":            email,
                "price_usd":        resolved_price,
                "created_at":       created_at,
                "payment_status":   "verified",
                "delivery_status":  "out_of_stock",
                "license_key":      None,
                "message": (
                    "Payment received. We are temporarily out of stock. "
                    "Your order has been recorded. Support has been notified."
                ),
            }

        log.info("fulfill order=%s step=selected_license key=[redacted]", order_id)

        # ── Step 4: Assign the selected key ───────────────────────────────
        assigned = _inv_assign_key(
            key=inv_record["key"],
            order_id=order_id,
            customer_email=email,
            assigned_user=discord,
            purchase_date=created_at,
        )
        if not assigned:
            # Race condition — another request grabbed the same key; retry once
            log.warning("fulfill order=%s step=assignment_race retrying", order_id)
            inv_record = _inv_get_next_available(plan)
            if inv_record is None:
                _log_delivery_failure(order_id, "out_of_stock_race", f"plan={plan}")
                # Persist as out_of_stock rather than leaving status=pending
                oos_record: dict = {
                    "order_id":         order_id,
                    "plan":             plan,
                    "plan_label":       plan_label,
                    "email":            email,
                    "discord":          discord,
                    "price_usd":        resolved_price,
                    "currency":         "USD",
                    "created_at":       created_at,
                    "payment_status":   "verified",
                    "payment_verified": True,
                    "delivery_status":  "out_of_stock",
                    "license_key":      None,
                    "license_status":   "pending",
                }
                _persist_order(order_id, oos_record, existing)
                log.warning("fulfill order=%s step=out_of_stock_race delivery_status=out_of_stock", order_id)
                return {"ok": False, "error": "Temporarily out of stock. Contact support."}
            assigned = _inv_assign_key(inv_record["key"], order_id, email, discord, created_at)

        log.info("fulfill order=%s step=assignment_committed assigned=%s", order_id, assigned)

        # Preserve payment_status='completed' if already set by the capture step;
        # otherwise fall back to 'verified' (legacy token-based path).
        existing_payment_status = (existing or {}).get("payment_status", "")
        payment_status_final = (
            "completed"
            if existing_payment_status == "completed" or payment_token.startswith("paypal:")
            else "verified"
        )

        assigned_key = inv_record["key"]
        order_record = {
            "order_id":         order_id,
            "paypal_capture_id": (payment_token or "").removeprefix("paypal:") if payment_token.startswith("paypal:") else "",
            "paypal_order_id":  paypal_order_id or (data_extra or {}).get("paypal_order_id", ""),
            "plan":             plan,
            "plan_label":       plan_label,
            "tier":             PLAN_PRICES[plan]["tier"],
            "email":            email,
            "discord":          discord,
            "price_usd":        resolved_price,
            "currency":         "USD",
            "created_at":       created_at,
            "payment_status":   payment_status_final,
            "payment_verified": True,
            "delivery_status":  "delivered",
            "license_key":      assigned_key,
            "license_status":   "active",
            "download_url":     f"/api/order/{order_id}/download",
        }

        # ── Step 5: Persist the final order record ────────────────────────
        try:
            _persist_order(order_id, order_record, existing)
            log.info(
                "fulfill order=%s step=order_updated delivery_status=delivered",
                order_id,
            )
        except Exception as exc:
            log.error("fulfill order=%s step=order_save_failed error=%s", order_id, exc)
            _log_delivery_failure(order_id, "order_save_failed", str(exc))
            # Key was already assigned — return it anyway; admin can repair the order record
            pass

    log.info("fulfill order=%s complete delivery_status=delivered key=[redacted]", order_id)
    return _safe_response(order_record)


def _persist_order(order_id: str, record: dict, existing: dict | None) -> None:
    if _USE_REDIS:
        _redis_set_order(order_id, record)
    else:
        records = _load_orders()
        records = [r for r in records if r.get("order_id", "") != order_id]
        records.append(record)
        _save_orders(records)


def _safe_response(record: dict) -> dict:
    plan = record.get("plan", "")
    tier = record.get("tier") or PLAN_PRICES.get(plan, {}).get("tier", "PRO")
    return {
        "ok":              True,
        "key":             record.get("license_key"),
        "tier":            tier,
        "order_id":        record.get("order_id"),
        "plan":            plan,
        "plan_label":      record.get("plan_label"),
        "email":           record.get("email"),
        "discord":         record.get("discord"),
        "price_usd":       record.get("price_usd"),
        "currency":        record.get("currency", "USD"),
        "created_at":      record.get("created_at"),
        "payment_status":  record.get("payment_status", "verified"),
        "delivery_status": record.get("delivery_status", "delivered"),
        "license_status":  record.get("license_status", "active"),
        "out_of_stock":    record.get("delivery_status") == "out_of_stock",
        "download_url":    record.get("download_url") or (
            f"/api/order/{record.get('order_id')}/download"
            if record.get("license_key") else None
        ),
        "instructions": [
            "Download Ghost.",
            "Extract the ZIP if required.",
            "Launch Ghost.",
            "Sign in or paste your license key.",
            "Press Activate.",
        ] if record.get("license_key") else None,
    }


def get_order(order_id: str) -> dict | None:
    return _load_single_order(order_id)


def update_order_status(order_id: str, status: str, extra: dict | None = None) -> bool:
    with _orders_lock:
        if _USE_REDIS:
            record = _redis_get_order(order_id)
            if not record:
                return False
            record["payment_status"] = status
            if extra:
                record.update(extra)
            try:
                _redis_set_order(order_id, record)
                return True
            except Exception as exc:
                log.error("Failed to save status update for order %s: %s", order_id, exc)
                return False
        else:
            records = _load_orders()
            record  = _find_order(order_id, records)
            if not record:
                return False
            record["payment_status"] = status
            if extra:
                record.update(extra)
            try:
                _save_orders(records)
                return True
            except Exception as exc:
                log.error("Failed to save status update for order %s: %s", order_id, exc)
                return False


# ── Flask application ─────────────────────────────────────────────────────────

try:
    from flask import Flask, Response, jsonify, request  # type: ignore
    _flask_available = True
except ImportError:
    _flask_available = False

if _flask_available:
    app = Flask(__name__)

    _DELIVERY_ALLOWED_ORIGIN = os.environ.get(
        "GHOST_DELIVERY_ALLOWED_ORIGIN", "http://localhost:3000"
    )

    @app.after_request
    def _add_cors(response):
        response.headers["Access-Control-Allow-Origin"]  = _DELIVERY_ALLOWED_ORIGIN
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,OPTIONS"
        return response

    @app.route("/api/payment/confirm",         methods=["OPTIONS"])
    @app.route("/api/order/<order_id>",        methods=["OPTIONS"])
    @app.route("/api/order/<order_id>/status", methods=["OPTIONS"])
    @app.route("/api/order/<order_id>/fulfill",methods=["OPTIONS"])
    @app.route("/api/admin/orders",            methods=["OPTIONS"])
    def _preflight(order_id=""):
        r = Response()
        r.headers["Access-Control-Allow-Origin"]  = "*"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type"
        r.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,OPTIONS"
        return r, 204


    # ── POST /api/payment/confirm ─────────────────────────────────────────
    @app.route("/api/payment/confirm", methods=["POST"])
    def route_confirm_payment():
        data = request.get_json(silent=True) or {}
        result = confirm_payment_and_deliver(
            order_id        = data.get("order_id", ""),
            payment_token   = data.get("payment_token", ""),
            plan            = data.get("plan", ""),
            email           = data.get("email", ""),
            discord         = data.get("discord", ""),
            price_usd       = data.get("price_usd"),
            paypal_order_id = data.get("paypal_order_id"),
        )
        return jsonify({k: v for k, v in result.items() if k not in ("_hmac", "_seed")}), \
               200 if result.get("ok") else 400


    # ── GET /api/order/<order_id> ─────────────────────────────────────────
    @app.route("/api/order/<order_id>", methods=["GET"])
    def route_get_order(order_id: str):
        # Support paypal-order:<paypalOrderId> lookup for idempotency check
        if order_id.startswith("paypal-order:"):
            paypal_oid = order_id[len("paypal-order:"):]
            # Search all orders for one matching this PayPal order ID
            all_orders = _load_orders()
            record = next((r for r in all_orders if r.get("paypal_order_id") == paypal_oid), None)
        else:
            record = get_order(order_id)
        if not record:
            return jsonify({"ok": False, "error": "Order not found"}), 404
        safe = {k: v for k, v in record.items() if k != "payment_verified"}
        safe["ok"] = True
        if not safe.get("download_url") and safe.get("delivery_status") == "delivered":
            safe["download_url"] = f"/api/order/{record.get('order_id', order_id)}/download"
        return jsonify(safe), 200


    # ── GET /api/order/<order_id>/download ────────────────────────────────
    @app.route("/api/order/<order_id>/download", methods=["GET"])
    def route_order_download(order_id: str):
        record = get_order(order_id)
        if not record:
            return jsonify({"ok": False, "error": "Order not found"}), 404
        if record.get("payment_status") != "verified":
            return jsonify({"ok": False, "error": "Payment not verified for this order"}), 403
        if record.get("delivery_status") != "delivered":
            return jsonify({"ok": False, "error": "Order delivery is pending"}), 403

        cdn_secret  = os.environ.get("GHOST_CDN_SECRET", "REPLACE-ME").encode()
        hour_bucket = str(int(time.time()) // 3600)
        sig = _hmac_mod.new(cdn_secret, (order_id + hour_bucket).encode(), hashlib.sha256).hexdigest()[:32]
        download_path = os.environ.get("GHOST_DOWNLOAD_PATH", "")
        if not download_path:
            return jsonify({"ok": False, "error": "Download not yet available. Contact support."}), 503
        return jsonify({"ok": True, "downloadRef": f"dl:{sig}:{order_id}", "downloadPath": download_path, "ttl": 3600}), 200


    # ── PATCH /api/order/<order_id>/status ────────────────────────────────
    @app.route("/api/order/<order_id>/status", methods=["PATCH"])
    def route_update_order_status(order_id: str):
        data   = request.get_json(silent=True) or {}
        status = data.get("status", "").strip()
        allowed = {"expired", "payment_failed", "refunded", "cancelled"}
        if status not in allowed:
            return jsonify({"ok": False, "error": f"status must be one of: {', '.join(sorted(allowed))}"}), 400
        extra = {k: v for k, v in data.items() if k != "status"}
        ok = update_order_status(order_id, status, extra or None)
        if ok:
            return jsonify({"ok": True, "order_id": order_id, "status": status}), 200
        log.warning("PATCH /status: order %s not found — acknowledged", order_id)
        return jsonify({"ok": True, "order_id": order_id, "status": "not_found"}), 200


    # ── POST /api/order/<order_id>/fulfill ────────────────────────────────
    @app.route("/api/order/<order_id>/fulfill", methods=["POST"])
    def route_fulfill_order(order_id: str):
        """
        Retry delivery for an out-of-stock or delivery_pending order.
        NEVER re-charges. Idempotent — returns existing key if already delivered.
        """
        record = get_order(order_id)
        if not record:
            return jsonify({"ok": False, "error": "Order not found. Payment may not be saved yet."}), 404

        # Already delivered — return existing key
        if record.get("license_key") and record.get("delivery_status") == "delivered":
            safe = {k: v for k, v in record.items() if k != "payment_verified"}
            safe["ok"] = True
            if not safe.get("download_url"):
                safe["download_url"] = f"/api/order/{order_id}/download"
            return jsonify(safe), 200

        if record.get("payment_status") not in ("verified", "completed"):
            return jsonify({
                "ok": False,
                "error": "Payment is not yet verified for this order.",
                "payment_status": record.get("payment_status"),
            }), 409

        # Try to assign from inventory
        plan = _inv.normalize_plan(record.get("plan", ""))
        if not plan or plan not in PLAN_PRICES:
            return jsonify({"ok": False, "error": f"Unknown plan '{plan}' on stored order"}), 500

        inv_record = _inv_get_next_available(plan)
        if inv_record is None:
            return jsonify({
                "ok": False,
                "error": "Still out of stock. Please contact support.",
                "delivery_status": "out_of_stock",
            }), 200

        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        assigned   = _inv_assign_key(
            key=inv_record["key"],
            order_id=order_id,
            customer_email=record.get("email", ""),
            assigned_user=record.get("discord", ""),
            purchase_date=record.get("created_at", created_at),
        )
        if not assigned:
            return jsonify({"ok": False, "error": "Could not assign key. Try again."}), 500

        with _orders_lock:
            rec2 = _load_single_order(order_id) if _USE_REDIS else _find_order(order_id, _load_orders())
            # Double-check idempotency under lock
            if rec2 and rec2.get("license_key") and rec2.get("delivery_status") == "delivered":
                safe = {k: v for k, v in rec2.items() if k != "payment_verified"}
                safe["ok"] = True
                return jsonify(safe), 200
            if rec2:
                rec2["license_key"]     = inv_record["key"]
                rec2["license_status"]  = "active"
                rec2["delivery_status"] = "delivered"
                rec2["fulfilled_at"]    = created_at
                rec2["download_url"]    = f"/api/order/{order_id}/download"
                try:
                    _persist_order(order_id, rec2, None)
                except Exception as exc:
                    log.error("Failed to save fulfill update for order %s: %s", order_id, exc)
                    return jsonify({"ok": False, "error": "Order update could not be saved"}), 500

        log.info("Fulfill — order=%s key=%s", order_id, inv_record["key"])
        safe = {k: v for k, v in (rec2 or {}).items() if k != "payment_verified"}
        safe["ok"] = True
        if not safe.get("download_url"):
            safe["download_url"] = f"/api/order/{order_id}/download"
        return jsonify(safe), 200


    # ── GET /api/admin/orders ──────────────────────────────────────────────
    @app.route("/api/admin/orders", methods=["GET"])
    def route_admin_orders():
        """Return all orders for the admin panel (Node.js server proxy)."""
        orders = _load_orders()
        safe = [{k: v for k, v in o.items() if k not in ("payment_verified",)} for o in orders]
        return jsonify({"ok": True, "orders": safe, "total": len(safe)}), 200


    if __name__ == "__main__":
        port = int(os.environ.get("GHOST_DELIVERY_PORT", 5057))
        log.info("Ghost delivery backend starting on port %d", port)
        app.run(host="0.0.0.0", port=port, debug=False)
