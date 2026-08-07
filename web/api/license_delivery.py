"""
license_delivery.py — Ghost Automatic License Delivery Backend
==============================================================
Flask server — all endpoints that the Node.js API layer calls.

Endpoints
---------
POST /api/payment/confirm
    Called after payment is confirmed (by the Stripe webhook handler in
    stripe_webhook.js, or directly for free Trial activations).

    Idempotent: if the same order_id has already been processed the
    existing key is returned without generating a new one.  Duplicate
    prevention uses the PayPal capture ID (or Stripe session ID) as the
    order_id.

GET  /api/order/<order_id>
    Returns the stored order record for the dashboard / success page.

GET  /api/order/<order_id>/download
    Protected download endpoint.  Returns a signed download URL only when
    the order exists, payment_status is "verified", and delivery_status is
    "delivered".  Never returns the binary directly — returns a signed ref.

PATCH /api/order/<order_id>/status
    Called by stripe_webhook.js to mark an order as expired, refunded,
    payment_failed, or cancelled without re-delivering a key.

Storage
-------
Orders are stored in Upstash Redis when UPSTASH_REDIS_REST_URL and
UPSTASH_REDIS_REST_TOKEN are set (required for Vercel deployments — the
local filesystem is ephemeral and not shared between invocations).
Falls back to orders.json on disk for local development only.

Set these env vars on Railway/Render/Fly.io as well so that the standalone
Flask process uses the same Redis instance as Vercel, giving a single
shared order store regardless of which service handles a request.

Security notes
--------------
• The HMAC secret and key-generation logic live entirely in keygen.py.
  Nothing sensitive is sent to the browser.
• payment_token must start with "paypal:" (set by captureOrder after
  PayPal capture verification) or "stripe:" / "FREE_TRIAL".  Any other
  token is rejected.
• Orders are locked before every write; concurrent webhook replays are
  handled safely.
• Download tokens are HMAC-SHA256 signed with GHOST_CDN_SECRET and expire
  after 1 hour.  The actual binary path is read from GHOST_DOWNLOAD_PATH
  and never interpolated from user input.

Requirements: Flask, python-dotenv, requests  (pip install flask python-dotenv requests)
Run standalone:  python web/api/license_delivery.py
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
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ── Resolve project root (this file lives at  <root>/web/api/) ──────────────
_HERE         = Path(__file__).resolve().parent          # web/api/
_PROJECT_ROOT = _HERE.parent.parent                      # qa_system_config/

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import keygen  # type: ignore  # noqa: E402

# ── Data files (used by the file-based fallback storage only) ─────────────────
ORDERS_DB    = _PROJECT_ROOT / "orders.json"
DELIVERY_LOG = _PROJECT_ROOT / "delivery_log.json"


# ────────────────────────────────────────────────────────────────────────────
# Persistent storage layer
# ────────────────────────────────────────────────────────────────────────────
#
# Production (Vercel / Railway / Render / Fly.io):
#   Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN.
#   Each order is stored as a JSON string at key  ghost:order:<order_id>.
#   A sorted-set  ghost:orders:index  tracks all order IDs for list queries.
#   Upstash Redis is an HTTP-based Redis — no persistent TCP connection
#   required, works correctly in serverless environments.
#
# Local development:
#   Leave UPSTASH_REDIS_REST_URL unset.  Orders are stored in orders.json
#   next to this file exactly as before.
#
# IMPORTANT: Do NOT use the local filesystem as the order store in production.
#   Vercel serverless functions have a read-only filesystem (except /tmp) and
#   each invocation gets a fresh environment — orders written in one invocation
#   are invisible to the next.

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
    log.info("Storage backend: Upstash Redis (%s)", _REDIS_URL.split("/")[2] if "/" in _REDIS_URL else _REDIS_URL)
else:
    log.warning(
        "Storage backend: local file (%s).  "
        "Set UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN for production.",
        ORDERS_DB,
    )


def _redis_request(command: list) -> Any:
    """
    Execute a single Redis command via the Upstash REST API.
    Raises RuntimeError on HTTP failure.
    """
    url     = f"{_REDIS_URL}/{'/'.join(str(c) for c in command)}"
    req     = urllib.request.Request(
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
    """Store a single order record in Redis."""
    key   = f"ghost:order:{order_id}"
    value = json.dumps(record, default=str)
    # Use the pipeline-style POST endpoint for SET + ZADD in one round trip
    # POST /pipeline body: [[cmd], [cmd], ...]
    payload = json.dumps([
        ["SET", key, value],
        ["ZADD", "ghost:orders:index", str(int(time.time())), order_id],
    ]).encode()
    req = urllib.request.Request(
        f"{_REDIS_URL}/pipeline",
        data=payload,
        headers={
            "Authorization": f"Bearer {_REDIS_TOKEN}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(f"Redis SET failed for order {order_id}: {exc}") from exc
    # result is a list of [{result:...}, ...]; check for errors
    for item in (result if isinstance(result, list) else []):
        if isinstance(item, dict) and "error" in item:
            raise RuntimeError(f"Redis pipeline error for order {order_id}: {item['error']}")


def _redis_get_order(order_id: str) -> dict | None:
    """Retrieve a single order record from Redis, or None if not found."""
    key = f"ghost:order:{order_id}"
    try:
        raw = _redis_request(["GET", urllib.parse.quote(key, safe="")])
    except Exception as exc:
        log.error("Redis GET for order %s failed: %s", order_id, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _redis_get_all_orders() -> list[dict]:
    """Return all order records from Redis (used by _load_orders fallback)."""
    try:
        order_ids = _redis_request(["ZRANGE", "ghost:orders:index", "0", "-1"])
    except Exception as exc:
        log.error("Redis ZRANGE failed: %s", exc)
        return []
    if not order_ids:
        return []
    records = []
    for oid in order_ids:
        rec = _redis_get_order(oid)
        if rec:
            records.append(rec)
    return records


# ── Plan catalogue ────────────────────────────────────────────────────────────
#   Mirrors PLAN_CATALOGUE in api/checkout.js — kept in sync manually.
#   Backend is the authoritative source; Node passes plan slug only.
PLAN_TIER_MAP: dict[str, tuple[str, int]] = {
    "trial":    ("TRIAL", 7),
    "pro":      ("PRO",   30),
    "lifetime": ("PRO",   0),   # 0 = no expiry in keygen.generate_key
}

PLAN_PRICES: dict[str, dict[str, Any]] = {
    "trial":    {"priceUsd": 0,  "label": "Ghost Trial (free)"},
    "pro":      {"priceUsd": 7,  "label": "Ghost Pro (monthly)"},
    "lifetime": {"priceUsd": 79, "label": "Ghost Lifetime"},
}

# ── Valid payment token prefixes / literals ────────────────────────────────
#   Tokens starting with "paypal:" come from captureOrder (amount/status verified).
#   "FREE_TRIAL" is the literal used for the no-payment trial path.
#   "stripe:" is kept for backward-compatibility with any existing orders only.
_ALLOWED_TOKEN_PREFIXES = ("paypal:", "stripe:")
_ALLOWED_TOKEN_LITERALS = {"FREE_TRIAL"}

# ── Thread lock ───────────────────────────────────────────────────────────────
_orders_lock = threading.Lock()


# ────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────────

def _load_orders() -> list[dict]:
    """Load all orders from the configured storage backend."""
    if _USE_REDIS:
        return _redis_get_all_orders()
    if ORDERS_DB.exists():
        try:
            return json.loads(ORDERS_DB.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_orders(records: list[dict]) -> None:
    """
    Persist orders to the configured storage backend.

    For Redis: each record is stored individually by order_id — the
    `records` list is iterated and each record is written.  This is
    called after a new record is appended to the in-memory list, so
    only the last record in the list needs to be written; however for
    correctness (e.g. status updates) all records are synced.

    For file: atomic write via temp file.
    """
    if _USE_REDIS:
        for record in records:
            oid = record.get("order_id", "")
            if oid:
                _redis_set_order(oid, record)
        return
    tmp = ORDERS_DB.with_suffix(ORDERS_DB.suffix + ".tmp")
    tmp.write_text(
        json.dumps(records, indent=2, default=str), encoding="utf-8"
    )
    tmp.replace(ORDERS_DB)


def _load_single_order(order_id: str) -> dict | None:
    """
    Retrieve a single order record directly by order_id.

    For Redis this is O(1) and avoids loading the full order list.
    For file storage it delegates to _load_orders + linear scan.
    """
    if _USE_REDIS:
        return _redis_get_order(order_id)
    return _find_order(order_id, _load_orders())


def _log_delivery_failure(order_id: str, reason: str, detail: str = "") -> None:
    """Append a failed-delivery record to delivery_log.json (safe log — no PII beyond order_id)."""
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
            # Truncate detail to avoid storing large stack traces with PII
            "detail":    detail[:800] if detail else "",
        })
        DELIVERY_LOG.write_text(
            json.dumps(records, indent=2, default=str), encoding="utf-8"
        )
    except Exception as exc:
        log.error("Could not write delivery_log: %s", exc)


def _find_order(order_id: str, records: list[dict]) -> dict | None:
    clean = order_id.strip()
    return next((r for r in records if r.get("order_id", "") == clean), None)


def _verify_payment_token(payment_token: str) -> bool:
    """
    Verify that the payment token originated from a trusted source.

    Accepted tokens:
    • "FREE_TRIAL"         — issued only by the /api/checkout/create-session
                             handler for plan=trial, no Stripe charge.
    • "stripe:<something>" — set by stripe_webhook.js AFTER the webhook
                             signature was verified with constructEvent().
                             The Node layer guarantees this prefix is only
                             added on verified events.

    Any other token (e.g. a raw client-side string) is rejected.
    """
    if not payment_token:
        return False
    token = payment_token.strip()
    if token in _ALLOWED_TOKEN_LITERALS:
        return True
    return any(token.startswith(p) for p in _ALLOWED_TOKEN_PREFIXES)


# ────────────────────────────────────────────────────────────────────────────
# Core delivery logic  (framework-agnostic)
# ────────────────────────────────────────────────────────────────────────────

def confirm_payment_and_deliver(
    *,
    order_id:           str,
    payment_token:      str,
    plan:               str,
    email:              str,
    discord:            str,
    price_usd:          float | None = None,
    stripe_session_id:  str | None   = None,
    paypal_order_id:    str | None   = None,
    data_extra:         dict | None  = None,
) -> dict:
    """
    Confirm payment and issue a license key for the given order.

    Idempotent: calling this a second time with the same order_id returns
    the existing key without running keygen again.

    Parameters
    ----------
    order_id          : Unique order/capture ID (dedup key)
    payment_token     : Must be "FREE_TRIAL", "stripe:<…>", or "paypal:<capture_id>"
    plan              : 'trial' | 'pro' | 'lifetime'
    email             : Customer email address
    discord           : Customer Discord username
    price_usd         : Amount charged in USD (resolved from plan if absent)
    stripe_session_id : Raw Stripe session ID (stored for receipt lookup)
    paypal_order_id   : PayPal order ID (stored alongside the capture ID)
    data_extra        : Arbitrary extra fields to merge into the order record
    """
    order_id = order_id.strip() if order_id else ""
    plan     = (plan or "").strip().lower()

    # ── Input validation ──────────────────────────────────────────────────
    if not order_id:
        return {"ok": False, "error": "order_id is required"}
    if not plan or plan not in PLAN_TIER_MAP:
        return {"ok": False, "error": f"Unknown plan '{plan}'. Choose from: {list(PLAN_TIER_MAP)}"}
    if not email or "@" not in email:
        return {"ok": False, "error": "A valid email is required"}
    if not discord or len(discord.strip()) < 2:
        return {"ok": False, "error": "Discord username is required"}

    # ── Verify payment token BEFORE touching keygen ───────────────────────
    if not _verify_payment_token(payment_token):
        log.warning("Payment token rejected for order %s — token prefix not trusted", order_id)
        _log_delivery_failure(order_id, "payment_token_rejected",
                              "Token did not match allowed prefixes/literals")
        return {"ok": False, "error": "Payment could not be verified"}

    tier, expires_days = PLAN_TIER_MAP[plan]
    resolved_price = price_usd if price_usd is not None else float(PLAN_PRICES[plan]["priceUsd"])

    # ── Duplicate capture-ID check (PayPal) ──────────────────────────────────
    # order_id for PayPal payments is the capture ID — globally unique per
    # PayPal capture.  A second request with the same capture ID returns the
    # existing key without running keygen again.
    paypal_capture_id  = (payment_token or "").removeprefix("paypal:") if payment_token.startswith("paypal:") else None

    with _orders_lock:
        # For Redis use O(1) single-record lookup; for file load all records once.
        if _USE_REDIS:
            existing  = _redis_get_order(order_id)
            records   = None   # lazy — only loaded if needed for full dup scan
        else:
            records   = _load_orders()
            existing  = _find_order(order_id, records)

        # ── Idempotency: already delivered? ──────────────────────────────
        if existing and existing.get("payment_verified") and existing.get("license_key"):
            log.info("Duplicate confirm call for order %s — returning existing key", order_id)
            return {
                "ok":              True,
                "key":             existing["license_key"],
                "order_id":        existing["order_id"],
                "plan":            existing["plan"],
                "tier":            existing["tier"],
                "email":           existing["email"],
                "discord":         existing["discord"],
                "price_usd":       existing["price_usd"],
                "currency":        existing.get("currency", "USD"),
                "created_at":      existing["created_at"],
                "payment_status":  existing["payment_status"],
                "delivery_status": existing.get("delivery_status", "delivered"),
                "license_status":  existing.get("license_status", "active"),
            }

        # ── Duplicate detection by PayPal capture ID ─────────────────────
        # For Redis the capture ID IS the order_id (set in _doCaptureOrder as
        # order_id=captureId), so the idempotency check above already covers it.
        # For file storage we do a linear scan over all records.
        if paypal_capture_id:
            if _USE_REDIS:
                # order_id == captureId for PayPal orders; already checked above.
                dup = None
            else:
                if records is None:
                    records = _load_orders()
                dup = next(
                    (r for r in records if r.get("paypal_capture_id") == paypal_capture_id),
                    None,
                )
            if dup and dup.get("license_key"):
                log.warning(
                    "Duplicate PayPal capture ID %s — returning existing key for order %s",
                    paypal_capture_id, dup["order_id"],
                )
                return {
                    "ok":              True,
                    "key":             dup["license_key"],
                    "order_id":        dup["order_id"],
                    "plan":            dup["plan"],
                    "tier":            dup["tier"],
                    "email":           dup["email"],
                    "discord":         dup["discord"],
                    "price_usd":       dup["price_usd"],
                    "currency":        dup.get("currency", "USD"),
                    "created_at":      dup["created_at"],
                    "payment_status":  dup["payment_status"],
                    "delivery_status": dup.get("delivery_status", "delivered"),
                    "license_status":  dup.get("license_status", "active"),
                }

        # ── Generate a real GHOST license key ─────────────────────────────
        try:
            new_key = keygen.generate_key(expires_days=expires_days, tier=tier)
        except Exception as exc:
            log.error("keygen.generate_key failed for order %s: %s", order_id, exc)
            _log_delivery_failure(order_id, "keygen_error", traceback.format_exc())
            return {"ok": False, "error": "License key generation failed"}

        # ── Validate the freshly-generated key ────────────────────────────
        key_meta = keygen.validate_key(new_key)
        if not key_meta.get("valid"):
            err = key_meta.get("error", "unknown validation error")
            log.error("Generated key failed self-validation for order %s: %s", order_id, err)
            _log_delivery_failure(order_id, "keygen_validation_failed", err)
            return {"ok": False, "error": "Generated key failed validation — please contact support"}

        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # ── Build full order record ───────────────────────────────────────
        order_record: dict = {
            "order_id":           order_id,
            "stripe_session_id":  stripe_session_id or order_id,
            "paypal_capture_id":  paypal_capture_id or "",
            "paypal_order_id":    paypal_order_id or (data_extra.get("paypal_order_id", "") if data_extra else ""),
            "plan":               plan,
            "plan_label":         PLAN_PRICES[plan]["label"],
            "tier":               tier,
            "email":              email,
            "discord":            discord,
            "price_usd":          resolved_price,
            "currency":           "USD",
            "created_at":         created_at,
            "payment_status":     "verified",
            "payment_verified":   True,
            "delivery_status":    "delivered",
            "license_key":        new_key,
            "license_status":     "active",
            "key_expires":        str(key_meta.get("expiry") or "never"),
            "key_created":        str(key_meta.get("created")),
        }

        # ── Persist to storage ────────────────────────────────────────────
        try:
            if _USE_REDIS:
                # Write a single record directly — no need to load all orders
                _redis_set_order(order_id, order_record)
            else:
                if records is None:
                    records = _load_orders()
                records = [r for r in records if r.get("order_id", "") != order_id]
                records.append(order_record)
                _save_orders(records)
        except Exception as exc:
            log.error("Failed to save order record for %s: %s", order_id, exc)
            _log_delivery_failure(order_id, "order_save_failed", traceback.format_exc())
            return {"ok": False, "error": "Order record could not be saved — please contact support"}

        # ── Persist to issued_keys.json ───────────────────────────────────
        try:
            keygen.save_key_record(new_key, {
                "tier":    tier,
                "created": key_meta.get("created"),
                "expiry":  key_meta.get("expiry"),
                # Never store raw email in note — use order_id + plan only
                "note":    f"order:{order_id} plan:{plan} discord:{discord}",
            })
        except Exception as exc:
            # Non-fatal — order record already contains the key
            log.warning("save_key_record warning for order %s: %s", order_id, exc)

    # Log without full email to avoid PII in application logs
    log.info(
        "License delivered — order=%s plan=%s tier=%s key=%s",
        order_id, plan, tier, new_key,
    )

    return {
        "ok":              True,
        "key":             new_key,
        "order_id":        order_id,
        "plan":            plan,
        "tier":            tier,
        "email":           email,
        "discord":         discord,
        "price_usd":       resolved_price,
        "currency":        "USD",
        "created_at":      created_at,
        "payment_status":  "verified",
        "delivery_status": "delivered",
        "license_status":  "active",
    }


def get_order(order_id: str) -> dict | None:
    """
    Return the stored order record for a given order_id, or None if not found.

    Uses _load_single_order for O(1) Redis lookup when Redis is configured,
    avoiding the cost of loading all orders just to find one record.
    """
    return _load_single_order(order_id)


def update_order_status(order_id: str, status: str, extra: dict | None = None) -> bool:
    """
    Update the payment_status field of an existing order record.
    Called by the webhook handler for refunds, cancellations, and failures.
    Returns True if the record was found and updated, False otherwise.
    """
    with _orders_lock:
        # For Redis: load the single record directly, update, write back.
        # For file: load all, find, update, write all back.
        if _USE_REDIS:
            record = _redis_get_order(order_id)
            if not record:
                log.warning("update_order_status: order %s not found — status '%s' not applied", order_id, status)
                return False
            record["payment_status"] = status
            if extra:
                record.update(extra)
            try:
                _redis_set_order(order_id, record)
                log.info("Order %s status updated to '%s'", order_id, status)
                return True
            except Exception as exc:
                log.error("Failed to save status update for order %s: %s", order_id, exc)
                return False
        else:
            records = _load_orders()
            record  = _find_order(order_id, records)
            if not record:
                log.warning("update_order_status: order %s not found — status '%s' not applied", order_id, status)
                return False
            record["payment_status"] = status
            if extra:
                record.update(extra)
            try:
                _save_orders(records)
                log.info("Order %s status updated to '%s'", order_id, status)
                return True
            except Exception as exc:
                log.error("Failed to save status update for order %s: %s", order_id, exc)
                return False


# ────────────────────────────────────────────────────────────────────────────
# Flask application
# ────────────────────────────────────────────────────────────────────────────

try:
    from flask import Flask, Response, jsonify, request  # type: ignore
    _flask_available = True
except ImportError:
    _flask_available = False

if _flask_available:
    app = Flask(__name__)

    # ── CORS: restrict to the configured web origin (not wildcard in prod) ─
    _DELIVERY_ALLOWED_ORIGIN = os.environ.get(
        "GHOST_DELIVERY_ALLOWED_ORIGIN", "http://localhost:3000"
    )

    @app.after_request
    def _add_cors(response):
        response.headers["Access-Control-Allow-Origin"]  = _DELIVERY_ALLOWED_ORIGIN
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,OPTIONS"
        return response

    @app.route("/api/payment/confirm",              methods=["OPTIONS"])
    @app.route("/api/order/<order_id>",             methods=["OPTIONS"])
    @app.route("/api/order/<order_id>/status",      methods=["OPTIONS"])
    @app.route("/api/order/<order_id>/fulfill",     methods=["OPTIONS"])
    def _preflight(order_id=""):
        r = Response()
        r.headers["Access-Control-Allow-Origin"]  = "*"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type"
        r.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,OPTIONS"
        return r, 204


    # ── POST /api/payment/confirm ─────────────────────────────────────────
    @app.route("/api/payment/confirm", methods=["POST"])
    def route_confirm_payment():
        """
        POST /api/payment/confirm
        -------------------------
        Called by:
          1. stripe_webhook.js after a verified checkout.session.completed event
          2. api/checkout.js for free-trial activations

        Body (JSON):
          order_id           string  required  (Stripe session ID or GHOST-TRIAL-…)
          payment_token      string  required  "stripe:<…>" or "FREE_TRIAL"
          plan               string  required  'trial' | 'pro' | 'lifetime'
          email              string  required
          discord            string  required
          price_usd          number  optional
          stripe_session_id  string  optional  (same as order_id for Stripe payments)

        Returns 200 JSON:
          { ok, key, order_id, plan, tier, email, discord,
            price_usd, created_at, payment_status }
          or { ok: false, error }
        """
        data = request.get_json(silent=True) or {}

        result = confirm_payment_and_deliver(
            order_id          = data.get("order_id", ""),
            payment_token     = data.get("payment_token", ""),
            plan              = data.get("plan", ""),
            email             = data.get("email", ""),
            discord           = data.get("discord", ""),
            price_usd         = data.get("price_usd"),
            stripe_session_id = data.get("stripe_session_id"),
            paypal_order_id   = data.get("paypal_order_id"),
        )

        status_code = 200 if result.get("ok") else 400
        safe_result = {k: v for k, v in result.items() if k not in ("_hmac", "_seed")}
        return jsonify(safe_result), status_code


    # ── GET /api/order/<order_id> ─────────────────────────────────────────
    @app.route("/api/order/<order_id>", methods=["GET"])
    def route_get_order(order_id: str):
        """
        GET /api/order/<order_id>
        -------------------------
        Used by the checkout success page and the customer dashboard to
        surface order status and license key after payment.

        Returns the stored order record plus a synthesised download_url
        field so the frontend has a consistent field name regardless of
        payment provider.  Internal flags (payment_verified) are stripped.

        Safe response shape (all fields the success page requires):
          ok, order_id, plan, plan_label, price_usd, currency,
          created_at, payment_status, delivery_status,
          license_key, license_status, download_url,
          email, discord, tier, key_expires
        """
        record = get_order(order_id)
        if not record:
            return jsonify({"ok": False, "error": "Order not found"}), 404

        safe = {k: v for k, v in record.items()
                if k not in ("payment_verified",)}
        safe["ok"] = True

        # Synthesise download_url so the frontend has a consistent field
        # regardless of whether the order was originally created via PayPal
        # or Stripe.  Only populated when delivery is confirmed complete.
        if not safe.get("download_url") and safe.get("delivery_status") == "delivered":
            safe["download_url"] = f"/api/order/{record.get('order_id', order_id)}/download"

        log.info(
            "GET /api/order/%s — payment_status=%s delivery_status=%s license_key=%s",
            order_id,
            safe.get("payment_status"),
            safe.get("delivery_status"),
            "[present]" if safe.get("license_key") else "[missing]",
        )
        return jsonify(safe), 200


    # ── GET /api/order/<order_id>/download ────────────────────────────────
    @app.route("/api/order/<order_id>/download", methods=["GET"])
    def route_order_download(order_id: str):
        """
        GET /api/order/<order_id>/download
        ------------------------------------
        Returns a signed short-lived download reference only when:
          • The order exists in orders.json
          • payment_status == "verified"
          • delivery_status == "delivered"

        Returns JSON:
          { ok, downloadRef, ttl }   on success
          { ok: false, error }       on failure

        The actual binary path is read from GHOST_DOWNLOAD_PATH on the
        server — never from user input.  The signed reference is
        HMAC-SHA256(order_id + hour_bucket, CDN_SECRET)[:32].
        The signed-in user or order owner is the only authorised caller;
        we verify via the order record — no JWT required for this endpoint
        since the order_id itself is a capability (unguessable PayPal UUID).
        """
        record = get_order(order_id)
        if not record:
            return jsonify({"ok": False, "error": "Order not found"}), 404

        if record.get("payment_status") != "verified":
            log.warning("Download requested for unverified order %s", order_id)
            return jsonify({"ok": False, "error": "Payment not verified for this order"}), 403

        if record.get("delivery_status") != "delivered":
            log.warning("Download requested for undelivered order %s", order_id)
            return jsonify({"ok": False, "error": "Order delivery is pending — please contact support"}), 403

        # Build a time-bucketed HMAC signature (1-hour TTL)
        cdn_secret = os.environ.get("GHOST_CDN_SECRET", "REPLACE-ME").encode()
        hour_bucket = str(int(time.time()) // 3600)
        sig = _hmac_mod.new(
            cdn_secret,
            (order_id + hour_bucket).encode(),
            hashlib.sha256,
        ).hexdigest()[:32]

        download_path = os.environ.get("GHOST_DOWNLOAD_PATH", "")
        if not download_path:
            log.warning("GHOST_DOWNLOAD_PATH is not configured — download unavailable")
            return jsonify({"ok": False, "error": "Download is not available yet. Please contact support."}), 503

        log.info("Download token issued for order %s", order_id)
        return jsonify({
            "ok":          True,
            "downloadRef": f"dl:{sig}:{order_id}",
            "downloadPath": download_path,
            "ttl":         3600,
        }), 200


    # ── PATCH /api/order/<order_id>/status ────────────────────────────────
    @app.route("/api/order/<order_id>/status", methods=["PATCH"])
    def route_update_order_status(order_id: str):
        """
        PATCH /api/order/<order_id>/status
        ------------------------------------
        Called exclusively by stripe_webhook.js to mark orders as:
          expired | payment_failed | refunded | cancelled

        Body (JSON):
          status  string  required
          ...     any extra fields to merge into the record (optional)

        Never issues or revokes license keys.
        """
        data   = request.get_json(silent=True) or {}
        status = data.get("status", "").strip()

        allowed_statuses = {"expired", "payment_failed", "refunded", "cancelled"}
        if status not in allowed_statuses:
            return jsonify({
                "ok":    False,
                "error": f"status must be one of: {', '.join(sorted(allowed_statuses))}",
            }), 400

        extra = {k: v for k, v in data.items() if k != "status"}
        ok    = update_order_status(order_id, status, extra or None)

        if ok:
            return jsonify({"ok": True, "order_id": order_id, "status": status}), 200
        else:
            # Order not found is not an error for webhook replays — return 200
            # so Stripe does not keep retrying for orders that pre-date this system.
            log.warning("PATCH /status: order %s not found — acknowledged anyway", order_id)
            return jsonify({"ok": True, "order_id": order_id, "status": "not_found"}), 200


    # ── POST /api/order/<order_id>/fulfill ────────────────────────────────
    @app.route("/api/order/<order_id>/fulfill", methods=["POST"])
    def route_fulfill_order(order_id: str):
        """
        POST /api/order/<order_id>/fulfill
        ------------------------------------
        Retry fulfillment for an order whose payment was captured by PayPal
        but whose license delivery failed (delivery_status=delivery_pending).

        This endpoint NEVER re-captures or re-charges — it only generates
        a license for an order that already has a verified payment.

        The idempotency guarantee is the same as /api/payment/confirm:
        if the order already has a license_key it is returned immediately
        without running keygen again.

        Returns 200 JSON matching the shape of a successful capture-order
        response:
          { ok, paymentStatus, deliveryStatus, orderId, plan, amount,
            purchaseDate, licenseKey, licenseStatus, downloadUrl }

        Returns 404 if the order does not exist in storage.
        Returns 409 if the order's payment_status is not "verified"
          (i.e. it was never fully saved — the payment may still be
          in transit; wait and retry).
        Returns 200 with the existing key if already delivered.
        """
        # Load the order — it must already exist from the capture step
        record = get_order(order_id)
        if not record:
            log.warning(
                "POST /api/order/%s/fulfill — order not found in storage. "
                "Storage backend: %s. If UPSTASH_REDIS_REST_URL is not set "
                "this is the expected 503 cause.",
                order_id, "redis" if _USE_REDIS else "file",
            )
            return jsonify({
                "ok":    False,
                "error": "Order not found. The payment may not yet be saved to persistent storage.",
                "storage_backend": "redis" if _USE_REDIS else "local_file",
                "hint": (
                    "Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN so orders "
                    "persist across Vercel invocations." if not _USE_REDIS else
                    "Order was not saved during capture. Check delivery logs."
                ),
            }), 404

        # Already delivered — return the existing key (idempotent)
        if record.get("license_key") and record.get("delivery_status") == "delivered":
            log.info("POST /api/order/%s/fulfill — already delivered, returning existing key", order_id)
            safe = {k: v for k, v in record.items() if k not in ("payment_verified",)}
            safe["ok"] = True
            if not safe.get("download_url"):
                safe["download_url"] = f"/api/order/{order_id}/download"
            return jsonify(safe), 200

        # Payment must be verified before we can generate a license
        if record.get("payment_status") != "verified":
            log.warning(
                "POST /api/order/%s/fulfill — payment_status=%s, not 'verified'. "
                "Cannot generate license without confirmed payment.",
                order_id, record.get("payment_status"),
            )
            return jsonify({
                "ok":             False,
                "error":          "Payment is not yet verified for this order.",
                "payment_status": record.get("payment_status"),
                "delivery_status": record.get("delivery_status"),
            }), 409

        # Generate a new license (uses the same keygen logic as confirm_payment_and_deliver)
        plan = record.get("plan", "")
        tier_info = PLAN_TIER_MAP.get(plan)
        if not tier_info:
            log.error("POST /api/order/%s/fulfill — unknown plan '%s'", order_id, plan)
            return jsonify({"ok": False, "error": f"Unknown plan '{plan}' on stored order"}), 500

        tier, expires_days = tier_info

        try:
            new_key = keygen.generate_key(expires_days=expires_days, tier=tier)
        except Exception as exc:
            log.error("keygen.generate_key failed during fulfill for order %s: %s", order_id, exc)
            _log_delivery_failure(order_id, "fulfill_keygen_error", traceback.format_exc())
            return jsonify({"ok": False, "error": "License key generation failed"}), 500

        key_meta = keygen.validate_key(new_key)
        if not key_meta.get("valid"):
            err = key_meta.get("error", "unknown")
            log.error("Generated key failed self-validation during fulfill for order %s: %s", order_id, err)
            _log_delivery_failure(order_id, "fulfill_keygen_validation_failed", err)
            return jsonify({"ok": False, "error": "Generated key failed validation"}), 500

        # Update the order record with the new key
        with _orders_lock:
            # Re-load to pick up any concurrent writes (file backend)
            if not _USE_REDIS:
                records = _load_orders()
                rec2 = _find_order(order_id, records)
                # Double-check idempotency under lock
                if rec2 and rec2.get("license_key") and rec2.get("delivery_status") == "delivered":
                    safe = {k: v for k, v in rec2.items() if k not in ("payment_verified",)}
                    safe["ok"] = True
                    if not safe.get("download_url"):
                        safe["download_url"] = f"/api/order/{order_id}/download"
                    return jsonify(safe), 200
                if rec2:
                    rec2["license_key"]      = new_key
                    rec2["license_status"]   = "active"
                    rec2["delivery_status"]  = "delivered"
                    rec2["key_expires"]      = str(key_meta.get("expiry") or "never")
                    rec2["key_created"]      = str(key_meta.get("created"))
                    rec2["fulfilled_at"]     = datetime.datetime.now(datetime.timezone.utc).isoformat()
                try:
                    _save_orders(records)
                except Exception as exc:
                    log.error("Failed to save fulfill update for order %s: %s", order_id, exc)
                    _log_delivery_failure(order_id, "fulfill_save_failed", traceback.format_exc())
                    return jsonify({"ok": False, "error": "Order update could not be saved"}), 500
            else:
                # Redis path: load single, check idempotency, update, write back
                rec2 = _redis_get_order(order_id)
                if rec2 and rec2.get("license_key") and rec2.get("delivery_status") == "delivered":
                    safe = {k: v for k, v in rec2.items() if k not in ("payment_verified",)}
                    safe["ok"] = True
                    if not safe.get("download_url"):
                        safe["download_url"] = f"/api/order/{order_id}/download"
                    return jsonify(safe), 200
                if rec2:
                    rec2["license_key"]      = new_key
                    rec2["license_status"]   = "active"
                    rec2["delivery_status"]  = "delivered"
                    rec2["key_expires"]      = str(key_meta.get("expiry") or "never")
                    rec2["key_created"]      = str(key_meta.get("created"))
                    rec2["fulfilled_at"]     = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    try:
                        _redis_set_order(order_id, rec2)
                    except Exception as exc:
                        log.error("Redis fulfill write failed for order %s: %s", order_id, exc)
                        _log_delivery_failure(order_id, "fulfill_redis_write_failed", traceback.format_exc())
                        return jsonify({"ok": False, "error": "Order update could not be saved to Redis"}), 500

        # Persist to issued_keys.json (non-fatal)
        try:
            keygen.save_key_record(new_key, {
                "tier":    tier,
                "created": key_meta.get("created"),
                "expiry":  key_meta.get("expiry"),
                "note":    f"order:{order_id} plan:{plan} (fulfill-retry)",
            })
        except Exception as exc:
            log.warning("save_key_record warning during fulfill for order %s: %s", order_id, exc)

        log.info(
            "License fulfilled (retry) — order=%s plan=%s tier=%s key=%s",
            order_id, plan, tier, new_key,
        )

        updated = rec2 or record
        return jsonify({
            "ok":             True,
            "paymentStatus":  "COMPLETED",
            "deliveryStatus": "delivered",
            "orderId":        order_id,
            "plan":           updated.get("plan"),
            "planLabel":      updated.get("plan_label"),
            "amount":         str(updated.get("price_usd", "")),
            "currency":       updated.get("currency", "USD"),
            "purchaseDate":   updated.get("created_at"),
            "licenseKey":     new_key,
            "licenseStatus":  "active",
            "downloadUrl":    f"/api/order/{order_id}/download",
            "tier":           tier,
            "email":          updated.get("email"),
            "discord":        updated.get("discord"),
        }), 200


if __name__ == "__main__":
    if not _flask_available:
        print("Flask is not installed.  Run:  pip install flask")
        sys.exit(1)
    port = int(os.environ.get("GHOST_DELIVERY_PORT", 5055))
    log.info("Ghost license delivery server starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
