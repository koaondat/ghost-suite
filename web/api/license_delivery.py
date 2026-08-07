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
ORDERS_DB           = _PROJECT_ROOT / "orders.json"
DELIVERY_LOG        = _PROJECT_ROOT / "delivery_log.json"
FULFILLMENT_DIAG_LOG = _PROJECT_ROOT / "fulfillment_diag.json"

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

_orders_lock       = threading.Lock()
_inventory_lock    = threading.Lock()
_fulfillment_lock  = threading.Lock()


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


def _log_fulfillment_attempt(
    order_id:        str,
    plan:            str,
    available_keys:  int,
    selected_key:    bool,
    outcome:         str,           # "delivered" | "failed" | "out_of_stock" | "idempotent"
    error:           str = "",
) -> None:
    """
    Append one structured entry to fulfillment_diag.json (capped at 20 records).
    Called from every terminal path in confirm_payment_and_deliver and
    route_fulfill_order so the admin diagnostics page always has a fresh trail.
    """
    entry = {
        "timestamp":      datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "order_id":       order_id,
        "plan":           plan,
        "available_keys": available_keys,
        "selected_key":   selected_key,
        "outcome":        outcome,
        "error":          error[:500] if error else "",
        "stages": [],   # filled in by the caller when available
    }
    with _fulfillment_lock:
        try:
            records: list[dict] = []
            if FULFILLMENT_DIAG_LOG.exists():
                try:
                    records = json.loads(FULFILLMENT_DIAG_LOG.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
            records.append(entry)
            # keep last 20
            FULFILLMENT_DIAG_LOG.write_text(
                json.dumps(records[-20:], indent=2, default=str), encoding="utf-8"
            )
        except Exception as exc:
            log.error("[fulfillment] Could not write fulfillment_diag: %s", exc)


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
    stages:  list[str] = []

    # ── Normalise the plan slug up front ─────────────────────────────────────
    raw_plan  = (plan or "").strip()
    plan      = _inv.normalize_plan(raw_plan.lower())

    log.info("[fulfillment] started order=%s", order_id)
    log.info("[fulfillment] normalized_plan=%r (raw=%r)", plan, raw_plan)
    stages.append("started")

    if not order_id:
        return {"ok": False, "error": "order_id is required"}
    if not plan or plan not in PLAN_PRICES:
        msg = f"Unknown plan '{plan}'. Choose from: {list(PLAN_PRICES)}"
        log.error("[fulfillment] error=%s order=%s", msg, order_id)
        _log_fulfillment_attempt(order_id, plan, 0, False, "failed", msg)
        return {"ok": False, "error": msg}
    if not email or "@" not in email:
        return {"ok": False, "error": "A valid email is required"}
    if not discord or len(discord.strip()) < 2:
        return {"ok": False, "error": "Discord username is required"}
    if not _verify_payment_token(payment_token):
        log.warning("[fulfillment] error=payment_token_rejected order=%s", order_id)
        _log_delivery_failure(order_id, "payment_token_rejected")
        _log_fulfillment_attempt(order_id, plan, 0, False, "failed", "payment_token_rejected")
        return {"ok": False, "error": "Payment could not be verified"}

    resolved_price = price_usd if price_usd is not None else float(PLAN_PRICES[plan]["priceUsd"])
    plan_label     = PLAN_PRICES[plan]["label"]

    # Outer try/except ensures the order is NEVER left permanently pending
    # if an unexpected exception escapes the inner logic.
    order_record: dict = {}
    try:
        with _orders_lock:
            # ── Step 1: Load order record ─────────────────────────────────────
            existing = _load_single_order(order_id) if _USE_REDIS else _find_order(order_id, _load_orders())
            log.info("[fulfillment] order_loaded order=%s existing=%s", order_id, bool(existing))
            stages.append("order_loaded")

            if existing:
                # Log order.plan vs inventory.plan vs request plan for mismatch diagnosis
                stored_plan = existing.get("plan", "")
                stored_plan_canonical = _inv.normalize_plan(stored_plan)
                log.info(
                    "[fulfillment] plan_check order=%s "
                    "order.plan=%r order.plan_canonical=%r "
                    "request.plan_raw=%r request.plan_canonical=%r "
                    "inventory_match=%s",
                    order_id,
                    stored_plan, stored_plan_canonical,
                    raw_plan, plan,
                    stored_plan_canonical == plan,
                )

            # ── Idempotency: already delivered? ──────────────────────────────
            if existing:
                if existing.get("license_key") and existing.get("delivery_status") == "delivered":
                    log.info("[fulfillment] idempotent order=%s returning_existing_key", order_id)
                    _log_fulfillment_attempt(order_id, plan, 0, True, "idempotent")
                    return _safe_response(existing)
                if existing.get("delivery_status") == "out_of_stock":
                    log.info("[fulfillment] retry_out_of_stock order=%s", order_id)
                    pass  # fall through to inventory lookup below
                # pending / delivery_pending — fall through to assign a key

            # ── Step 2: Check available licenses in inventory ─────────────────
            all_inv        = _inv_load_all()
            inv_total      = len(all_inv)
            inv_available  = [r for r in all_inv if r.get("status") in ("available", "unused")]
            avail_total    = len(inv_available)

            # Log plan values on every inventory record so mismatches are obvious
            inv_plans_raw       = [r.get("plan", "") for r in all_inv]
            inv_plans_canonical = [_inv.normalize_plan(p) for p in inv_plans_raw]
            plan_available      = [r for r in inv_available if _inv.normalize_plan(r.get("plan", "")) == plan]
            match_count         = len(plan_available)

            log.info(
                "[fulfillment] available_keys=%d order=%s "
                "inventory_total=%d available_total=%d matching_plan=%d "
                "inventory_plans_raw=%r inventory_plans_canonical=%r searching_for=%r",
                match_count, order_id,
                inv_total, avail_total, match_count,
                inv_plans_raw, inv_plans_canonical, plan,
            )
            stages.append(f"available_keys={match_count}")

            # ── Step 3: Pick the next available key ───────────────────────────
            inv_record = _inv_get_next_available(plan)
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

            log.info("[fulfillment] selected_key=%s order=%s", inv_record is not None, order_id)
            stages.append(f"selected_key={'true' if inv_record else 'false'}")

            if inv_record is None:
                # ── Out of stock — never leave delivery_status=pending ────────
                log.warning("[fulfillment] error=out_of_stock order=%s plan=%r", order_id, plan)
                _log_delivery_failure(order_id, "out_of_stock", f"plan={plan}")

                existing_payment_status = (existing or {}).get("payment_status", "")
                oos_payment_status = (
                    "completed"
                    if existing_payment_status == "completed" or payment_token.startswith("paypal:")
                    else "verified"
                )
                order_record = {
                    **(existing or {}),
                    "order_id":          order_id,
                    "paypal_capture_id": (payment_token or "").removeprefix("paypal:") if payment_token.startswith("paypal:") else (existing or {}).get("paypal_capture_id", ""),
                    "paypal_order_id":   paypal_order_id or (data_extra or {}).get("paypal_order_id", "") or (existing or {}).get("paypal_order_id", ""),
                    "plan":              plan,
                    "plan_label":        plan_label,
                    "email":             email,
                    "discord":           discord,
                    "price_usd":         resolved_price,
                    "currency":          "USD",
                    "created_at":        (existing or {}).get("created_at", created_at),
                    "payment_status":    oos_payment_status,
                    "payment_verified":  True,
                    "delivery_status":   "out_of_stock",
                    "license_key":       None,
                    "license_status":    "pending",
                    "failure_reason":    "out_of_stock",
                }
                _persist_order(order_id, order_record, existing)
                log.info("[fulfillment] order_saved order=%s delivery_status=out_of_stock", order_id)
                _log_fulfillment_attempt(order_id, plan, match_count, False, "out_of_stock")
                return {
                    "ok":               True,
                    "out_of_stock":     True,
                    "order_id":         order_id,
                    "plan":             plan,
                    "email":            email,
                    "price_usd":        resolved_price,
                    "created_at":       order_record["created_at"],
                    "payment_status":   oos_payment_status,
                    "delivery_status":  "out_of_stock",
                    "license_key":      None,
                    "message": (
                        "Payment received. We are temporarily out of stock. "
                        "Your order has been recorded. Support has been notified."
                    ),
                }

            log.info(
                "[fulfillment] selected_key_value=%s order=%s plan=%r",
                inv_record["key"], order_id, inv_record.get("plan", ""),
            )

            # ── Step 4a: Assign the selected key (atomic) ─────────────────────
            log.info("[fulfillment] updating_license order=%s key=%s", order_id, inv_record["key"])
            stages.append("updating_license")
            assigned = _inv_assign_key(
                key=inv_record["key"],
                order_id=order_id,
                customer_email=email,
                assigned_user=discord,
                purchase_date=created_at,
            )
            if not assigned:
                # Race condition — another request grabbed the same key; retry once
                log.warning("[fulfillment] assignment_race order=%s — retrying", order_id)
                inv_record = _inv_get_next_available(plan)
                if inv_record is None:
                    _log_delivery_failure(order_id, "out_of_stock_race", f"plan={plan}")
                    existing_payment_status = (existing or {}).get("payment_status", "")
                    oos_payment_status2 = (
                        "completed"
                        if existing_payment_status == "completed" or payment_token.startswith("paypal:")
                        else "verified"
                    )
                    oos_record: dict = {
                        **(existing or {}),
                        "order_id":          order_id,
                        "plan":              plan,
                        "plan_label":        plan_label,
                        "email":             email,
                        "discord":           discord,
                        "price_usd":         resolved_price,
                        "currency":          "USD",
                        "created_at":        (existing or {}).get("created_at", created_at),
                        "payment_status":    oos_payment_status2,
                        "payment_verified":  True,
                        "delivery_status":   "out_of_stock",
                        "license_key":       None,
                        "license_status":    "pending",
                        "failure_reason":    "out_of_stock_race",
                    }
                    _persist_order(order_id, oos_record, existing)
                    log.warning("[fulfillment] out_of_stock_race order=%s delivery_status=out_of_stock", order_id)
                    _log_fulfillment_attempt(order_id, plan, 0, False, "out_of_stock", "race")
                    return {"ok": False, "error": "Temporarily out of stock. Contact support."}
                log.info("[fulfillment] updating_license (retry) order=%s key=%s", order_id, inv_record["key"])
                assigned = _inv_assign_key(inv_record["key"], order_id, email, discord, created_at)

            log.info("[fulfillment] license_updated order=%s key=%s assigned=%s",
                     order_id, inv_record["key"], assigned)

            # ── Step 4b: Build and persist the final order record ─────────────
            existing_payment_status = (existing or {}).get("payment_status", "")
            payment_status_final = (
                "completed"
                if existing_payment_status == "completed" or payment_token.startswith("paypal:")
                else "verified"
            )

            assigned_key = inv_record["key"]
            order_record = {
                **(existing or {}),
                "order_id":          order_id,
                "paypal_capture_id": (payment_token or "").removeprefix("paypal:") if payment_token.startswith("paypal:") else (existing or {}).get("paypal_capture_id", ""),
                "paypal_order_id":   paypal_order_id or (data_extra or {}).get("paypal_order_id", "") or (existing or {}).get("paypal_order_id", ""),
                "plan":              plan,
                "plan_label":        plan_label,
                "tier":              PLAN_PRICES[plan]["tier"],
                "email":             email,
                "discord":           discord,
                "price_usd":         resolved_price,
                "currency":          "USD",
                "created_at":        (existing or {}).get("created_at", created_at),
                "payment_status":    payment_status_final,
                "payment_verified":  True,
                "delivery_status":   "delivered",
                "license_key":       assigned_key,
                "license_status":    "active",
                "status":            "completed",
                "download_url":      f"/api/order/{order_id}/download",
            }

            log.info("[fulfillment] updating_order order=%s delivery_status=delivered", order_id)
            stages.append("updating_order")
            _persist_order(order_id, order_record, existing)
            log.info(
                "[fulfillment] order_saved order=%s delivery_status=delivered license_key=%s",
                order_id, assigned_key,
            )
            stages.append("completed")

    except Exception as exc:
        # ── Safety net: never leave the order permanently pending ─────────────
        err_msg = str(exc)
        log.exception("[fulfillment] error=%s order=%s", err_msg, order_id)
        _log_delivery_failure(order_id, "exception", err_msg)
        _log_fulfillment_attempt(order_id, plan, 0, False, "failed", err_msg)

        # Attempt to save the order as failed so it shows up in admin view
        try:
            failed_record: dict = {
                **(order_record or {}),
                "order_id":        order_id,
                "plan":            plan,
                "email":           email,
                "discord":         discord,
                "delivery_status": "failed",
                "license_key":     None,
                "license_status":  "pending",
                "failure_reason":  err_msg[:500],
                "status":          "failed",
            }
            _persist_order(order_id, failed_record, None)
            log.info("[fulfillment] order_saved order=%s delivery_status=failed", order_id)
        except Exception as save_exc:
            log.error("[fulfillment] could not save failed order record order=%s: %s", order_id, save_exc)

        return {"ok": False, "error": f"Internal fulfillment error: {err_msg}"}

    log.info("[fulfillment] completed order=%s", order_id)
    _log_fulfillment_attempt(order_id, plan, 0, True, "delivered")
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
        # Accept both "verified" (legacy/Stripe) and "completed" (PayPal capture path)
        if record.get("payment_status") not in ("verified", "completed"):
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
        Retry delivery for a pending, delivery_pending, or out_of_stock order.
        NEVER re-charges. Idempotent — returns existing key if already delivered.
        """
        log.info("[fulfillment] started order=%s (retry)", order_id)

        record = get_order(order_id)
        if not record:
            log.warning("[fulfillment] error=order_not_found order=%s", order_id)
            _log_fulfillment_attempt(order_id, "", 0, False, "failed", "order_not_found")
            return jsonify({"ok": False, "error": "Order not found. Payment may not be saved yet."}), 404

        log.info("[fulfillment] order_loaded order=%s delivery_status=%s license_key=%s",
                 order_id, record.get("delivery_status"),
                 "[present]" if record.get("license_key") else "[missing]")

        # Already delivered — return existing key (idempotent)
        if record.get("license_key") and record.get("delivery_status") == "delivered":
            safe = {k: v for k, v in record.items() if k != "payment_verified"}
            safe["ok"] = True
            if not safe.get("download_url"):
                safe["download_url"] = f"/api/order/{order_id}/download"
            log.info("[fulfillment] idempotent order=%s already_delivered", order_id)
            _log_fulfillment_attempt(order_id, record.get("plan", ""), 0, True, "idempotent")
            return jsonify(safe), 200

        if record.get("payment_status") not in ("verified", "completed"):
            return jsonify({
                "ok": False,
                "error": "Payment is not yet verified for this order.",
                "payment_status": record.get("payment_status"),
            }), 409

        # Normalize plan from the stored order record
        raw_plan = record.get("plan", "")
        plan     = _inv.normalize_plan(raw_plan)
        log.info("[fulfillment] normalized_plan=%r (raw=%r) order=%s", plan, raw_plan, order_id)

        if not plan or plan not in PLAN_PRICES:
            err = f"Unknown plan '{plan}' on stored order"
            log.error("[fulfillment] error=%s order=%s", err, order_id)
            _log_fulfillment_attempt(order_id, plan, 0, False, "failed", err)
            return jsonify({"ok": False, "error": err}), 500

        # Validate inventory and log what is available
        all_inv       = _inv_load_all()
        inv_available = [r for r in all_inv if r.get("status") in ("available", "unused")]
        plan_avail    = [r for r in inv_available if _inv.normalize_plan(r.get("plan", "")) == plan]
        match_count   = len(plan_avail)
        log.info(
            "[fulfillment] available_keys=%d order=%s "
            "inventory_total=%d available_total=%d matching_plan=%d plan=%r",
            match_count, order_id, len(all_inv), len(inv_available), match_count, plan,
        )

        inv_record = _inv_get_next_available(plan)
        log.info("[fulfillment] selected_key=%s order=%s", inv_record is not None, order_id)

        if inv_record is None:
            # Update the order to out_of_stock so it never stays pending
            with _orders_lock:
                rec_upd = _load_single_order(order_id) if _USE_REDIS else _find_order(order_id, _load_orders())
                if rec_upd:
                    rec_upd["delivery_status"] = "out_of_stock"
                    rec_upd["failure_reason"]  = "out_of_stock"
                    try:
                        _persist_order(order_id, rec_upd, None)
                        log.info("[fulfillment] order_saved order=%s delivery_status=out_of_stock", order_id)
                    except Exception as exc:
                        log.error("[fulfillment] error=%s order=%s (saving out_of_stock)", exc, order_id)
            _log_fulfillment_attempt(order_id, plan, match_count, False, "out_of_stock")
            return jsonify({
                "ok":              False,
                "error":           "Still out of stock. Please contact support.",
                "delivery_status": "out_of_stock",
            }), 200

        log.info("[fulfillment] selected_key_value=%s order=%s plan=%r",
                 inv_record["key"], order_id, inv_record.get("plan", ""))

        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        log.info("[fulfillment] updating_license order=%s key=%s", order_id, inv_record["key"])
        assigned   = _inv_assign_key(
            key=inv_record["key"],
            order_id=order_id,
            customer_email=record.get("email", ""),
            assigned_user=record.get("discord", ""),
            purchase_date=record.get("created_at", created_at),
        )
        if not assigned:
            err = "Could not assign key. Try again."
            log.error("[fulfillment] error=%s order=%s", err, order_id)
            _log_fulfillment_attempt(order_id, plan, match_count, True, "failed", err)
            return jsonify({"ok": False, "error": err}), 500

        log.info("[fulfillment] license_updated order=%s key=%s", order_id, inv_record["key"])
        log.info("[fulfillment] updating_order order=%s delivery_status=delivered", order_id)

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
                rec2["status"]          = "completed"
                rec2["fulfilled_at"]    = created_at
                rec2["download_url"]    = f"/api/order/{order_id}/download"
                try:
                    _persist_order(order_id, rec2, None)
                    log.info("[fulfillment] order_saved order=%s delivery_status=delivered license_key=%s",
                             order_id, inv_record["key"])
                except Exception as exc:
                    err_msg = str(exc)
                    log.error("[fulfillment] error=%s order=%s (saving delivered)", err_msg, order_id)
                    _log_fulfillment_attempt(order_id, plan, match_count, True, "failed", err_msg)
                    return jsonify({"ok": False, "error": "Order update could not be saved"}), 500

        log.info("[fulfillment] completed order=%s", order_id)
        _log_fulfillment_attempt(order_id, plan, match_count, True, "delivered")
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


    # ── GET /api/admin/fulfillment-log ─────────────────────────────────────
    @app.route("/api/admin/fulfillment-log", methods=["GET"])
    def route_admin_fulfillment_log():
        """
        Return the last 20 fulfillment attempts from fulfillment_diag.json.
        Used by the Fulfillment Diagnostics admin panel tab.
        Query: ?limit=20
        """
        try:
            limit = min(int(request.args.get("limit", 20)), 50)
        except (ValueError, TypeError):
            limit = 20
        records: list[dict] = []
        if FULFILLMENT_DIAG_LOG.exists():
            try:
                records = json.loads(FULFILLMENT_DIAG_LOG.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                records = []
        return jsonify({"ok": True, "attempts": records[-limit:], "total": len(records)}), 200


    if __name__ == "__main__":
        port = int(os.environ.get("GHOST_DELIVERY_PORT", 5057))
        log.info("Ghost delivery backend starting on port %d", port)
        app.run(host="0.0.0.0", port=port, debug=False)
