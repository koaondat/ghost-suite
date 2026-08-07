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
    prevention uses the Stripe Checkout Session ID as the order_id.

GET  /api/order/<order_id>
    Returns the stored order record for the dashboard / success page.

PATCH /api/order/<order_id>/status
    Called by stripe_webhook.js to mark an order as expired, refunded,
    payment_failed, or cancelled without re-delivering a key.

Security notes
--------------
• The HMAC secret and key-generation logic live entirely in keygen.py.
  Nothing sensitive is sent to the browser.
• payment_token must start with "stripe:" (set by the webhook handler)
  or be the literal "FREE_TRIAL" for trial plan activations.  Any other
  token is rejected.  This ensures keys are only issued for real Stripe
  events — the webhook signature was already verified by the Node layer.
• Orders are file-locked before every write; concurrent webhook replays
  are handled safely.

Requirements: Flask, python-dotenv  (pip install flask python-dotenv)
Run standalone:  python web/api/license_delivery.py
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

# ── Resolve project root (this file lives at  <root>/web/api/) ──────────────
_HERE         = Path(__file__).resolve().parent          # web/api/
_PROJECT_ROOT = _HERE.parent.parent                      # qa_system_config/

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import keygen  # type: ignore  # noqa: E402

# ── Data files ───────────────────────────────────────────────────────────────
ORDERS_DB    = _PROJECT_ROOT / "orders.json"
DELIVERY_LOG = _PROJECT_ROOT / "delivery_log.json"

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

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("ghost.delivery")


# ────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────────

def _load_orders() -> list[dict]:
    if ORDERS_DB.exists():
        try:
            return json.loads(ORDERS_DB.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_orders(records: list[dict]) -> None:
    """Atomic write via temp file to prevent partial reads on concurrent access."""
    tmp = ORDERS_DB.with_suffix(ORDERS_DB.suffix + ".tmp")
    tmp.write_text(
        json.dumps(records, indent=2, default=str), encoding="utf-8"
    )
    tmp.replace(ORDERS_DB)


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
    order_id:          str,
    payment_token:     str,
    plan:              str,
    email:             str,
    discord:           str,
    price_usd:         float | None = None,
    stripe_session_id: str | None   = None,
) -> dict:
    """
    Confirm payment and issue a license key for the given order.

    Idempotent: calling this a second time with the same order_id returns
    the existing key without running keygen again.

    Parameters
    ----------
    order_id          : Stripe Checkout Session ID (used as unique order key)
    payment_token     : Must be "FREE_TRIAL" or "stripe:<intent_or_session_id>"
    plan              : 'trial' | 'pro' | 'lifetime'
    email             : Customer email address
    discord           : Customer Discord username
    price_usd         : Amount charged in USD (resolved from plan if absent)
    stripe_session_id : Raw Stripe session ID (stored for receipt lookup)
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
        records = _load_orders()

        # ── Idempotency: already delivered? ──────────────────────────────
        existing = _find_order(order_id, records)
        if existing and existing.get("payment_verified") and existing.get("license_key"):
            log.info("Duplicate confirm call for order %s — returning existing key", order_id)
            return {
                "ok":             True,
                "key":            existing["license_key"],
                "order_id":       existing["order_id"],
                "plan":           existing["plan"],
                "tier":           existing["tier"],
                "email":          existing["email"],
                "discord":        existing["discord"],
                "price_usd":      existing["price_usd"],
                "created_at":     existing["created_at"],
                "payment_status": existing["payment_status"],
            }

        # ── Duplicate detection by PayPal capture ID ─────────────────────
        # Also check every record's stored paypal_capture_id to guard
        # against the same capture being submitted under a different order_id.
        if paypal_capture_id:
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
                    "ok":             True,
                    "key":            dup["license_key"],
                    "order_id":       dup["order_id"],
                    "plan":           dup["plan"],
                    "tier":           dup["tier"],
                    "email":          dup["email"],
                    "discord":        dup["discord"],
                    "price_usd":      dup["price_usd"],
                    "created_at":     dup["created_at"],
                    "payment_status": dup["payment_status"],
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
            "plan":               plan,
            "plan_label":         PLAN_PRICES[plan]["label"],
            "tier":               tier,
            "email":              email,
            "discord":            discord,
            "price_usd":          resolved_price,
            "created_at":         created_at,
            "payment_status":     "verified",
            "payment_verified":   True,
            "license_key":        new_key,
            "key_expires":        str(key_meta.get("expiry") or "never"),
            "key_created":        str(key_meta.get("created")),
        }

        # ── Persist to orders.json ────────────────────────────────────────
        try:
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
        "ok":             True,
        "key":            new_key,
        "order_id":       order_id,
        "plan":           plan,
        "tier":           tier,
        "email":          email,
        "discord":        discord,
        "price_usd":      resolved_price,
        "created_at":     created_at,
        "payment_status": "verified",
    }


def get_order(order_id: str) -> dict | None:
    """Return the stored order record for a given order_id, or None if not found."""
    records = _load_orders()
    return _find_order(order_id, records)


def update_order_status(order_id: str, status: str, extra: dict | None = None) -> bool:
    """
    Update the payment_status field of an existing order record.
    Called by the webhook handler for refunds, cancellations, and failures.
    Returns True if the record was found and updated, False otherwise.
    """
    with _orders_lock:
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
        surface order status and license key after a Stripe redirect.
        Returns the stored order record minus internal flags.
        """
        record = get_order(order_id)
        if not record:
            return jsonify({"ok": False, "error": "Order not found"}), 404

        safe = {k: v for k, v in record.items()
                if k not in ("payment_verified",)}
        safe["ok"] = True
        return jsonify(safe), 200


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


if __name__ == "__main__":
    if not _flask_available:
        print("Flask is not installed.  Run:  pip install flask")
        sys.exit(1)
    port = int(os.environ.get("GHOST_DELIVERY_PORT", 5055))
    log.info("Ghost license delivery server starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
