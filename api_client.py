"""
api_client.py — Async HTTP client for the Ghost shared API
===========================================================
Used by bot.py (Discord bot) and any other async consumer.
All admin endpoints require the GHOST_ADMIN_API_KEY env var.

CURRENT ENDPOINT MAP (api.py — verified):
  POST /api/admin/inventory/generate       generate inventory keys (current system)
  GET  /api/admin/inventory/<key>          look up an inventory key
  POST /api/admin/inventory/<key>/revoke   revoke an inventory key
  PATCH /api/admin/inventory/<key>         update key fields (status, notes, hwid, etc.)
  POST /api/admin/inventory/<key>/extend   extend expiry
  GET  /api/admin/inventory                list all inventory keys
  POST /api/admin/license/generate         generate HMAC-signed keys (legacy / staff)
  GET  /api/admin/license/<key>            view HMAC key info
  POST /api/admin/license/<key>/ban        ban a key
  POST /api/admin/license/<key>/unban      unban a key
  DELETE /api/admin/license/<key>          delete HMAC key record
  POST /api/admin/license/<key>/reset      reset HWID binding
  GET  /api/admin/keys                     list HMAC issued keys
  GET  /api/admin/users                    list registered users
  DELETE /api/admin/users/<username>       delete user account
  GET  /api/admin/orders                   list all orders
  GET  /api/admin/orders/<order_id>        single order detail
  GET  /api/admin/customers                list customers (derived from orders)
  GET  /api/admin/stats                    aggregate stats (real backend endpoint)
  GET  /api/admin/downloads                app version / download info
  GET  /api/releases/latest                current release metadata (no auth)
  GET  /api/admin/pending-customer-roles   orders pending Discord role assignment
  POST /api/admin/orders/<id>/role-granted mark Customer role granted
  GET  /health                             API liveness probe
  GET  /status                             API readiness / depth probe
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

log = logging.getLogger("ghost.api_client")

# ---------------------------------------------------------------------------
# Config helpers — read lazily so load_dotenv() in bot.py runs first
# ---------------------------------------------------------------------------

def _api_base() -> str:
    """Return the Ghost API base URL, read from env each call."""
    raw = os.environ.get("GHOST_API_URL", "").strip()
    if not raw:
        log.warning(
            "GHOST_API_URL is not set. Set it to your deployed API URL "
            "(e.g. https://api.yourdomain.com). Falling back to localhost:5056."
        )
        return "http://localhost:5056"
    return raw.rstrip("/")


def _admin_key() -> str:
    """Return the Ghost admin API key, read from env each call."""
    return os.environ.get("GHOST_ADMIN_API_KEY", "").strip()


def _admin_headers() -> dict[str, str]:
    """
    Return headers for server-to-server admin API calls.

    Sends BOTH the legacy X-Admin-Key and the standard Authorization: Bearer
    header so the bot works against both the Flask (api.py) and Node (server.js)
    backends regardless of which one handles the request.
    The key is never exposed to browser/frontend JavaScript.
    """
    key = _admin_key()
    return {
        "Content-Type":  "application/json",
        "X-Admin-Key":   key,
        "Authorization": f"Bearer {key}",
    }


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class APIError(Exception):
    """Raised when the shared API returns ok=False or a non-2xx status."""
    def __init__(self, message: str, status: int = 0, endpoint: str = ""):
        super().__init__(message)
        self.status   = status
        self.endpoint = endpoint


# ---------------------------------------------------------------------------
# Core request helper
# ---------------------------------------------------------------------------

async def _request(method: str, path: str, **kwargs) -> dict:
    """
    Make an async HTTP request to the shared API.
    Raises APIError with endpoint, HTTP status, and safe error message.
    """
    url = f"{_api_base()}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, **kwargs) as resp:
            try:
                data: dict = await resp.json()
            except Exception:
                data = {}
            if not data.get("ok") or resp.status >= 400:
                error = data.get("error") or data.get("message") or f"HTTP {resp.status}"
                log.error("API error: %s %s → %d: %s", method, path, resp.status, error)
                raise APIError(error, resp.status, path)
            return data


# ---------------------------------------------------------------------------
# Inventory key system  (CURRENT — used by website / desktop / bot)
# Plans: day | 3days | week | month | 3months
# ---------------------------------------------------------------------------

# Map human-readable duration labels to canonical plan slugs used by the backend
PLAN_LABELS: dict[str, str] = {
    "1 Day":     "day",
    "3 Days":    "3days",
    "1 Week":    "week",
    "30 Days":   "month",
    "3 Months":  "3months",
}

# Reverse map for display
PLAN_DISPLAY: dict[str, str] = {v: k for k, v in PLAN_LABELS.items()}

# Duration in days for each plan (used to compute expiry dates for display)
PLAN_DAYS: dict[str, int] = {
    "day":     1,
    "3days":   3,
    "week":    7,
    "month":   30,
    "3months": 90,
}


async def generate_inventory_keys(
    plan: str,
    quantity: int,
    notes: str = "",
    expiration_days: int | None = None,
) -> list[str]:
    """
    Generate inventory keys via POST /api/admin/inventory/generate.
    plan must be a canonical slug: day | 3days | week | month | 3months
    This is the current key system used by website purchases, admin panel,
    and desktop app activation.
    """
    payload: dict[str, Any] = {
        "plan":     plan,
        "quantity": quantity,
        "notes":    notes,
        "format":   "seg4x4",
    }
    if expiration_days is not None:
        payload["expiration"] = str(expiration_days)
    else:
        days = PLAN_DAYS.get(plan)
        if days:
            payload["expiration"] = str(days)

    data = await _request(
        "POST", "/api/admin/inventory/generate",
        json=payload,
        headers=_admin_headers(),
    )
    return data.get("keys", [])


async def inventory_key_info(key: str) -> dict:
    """
    Look up a single inventory key record.

    Uses GET /api/admin/inventory?search=<key> and returns the first exact match.
    Handles both response shapes:
      • server.js returns  { ok, items: [...], total }
      • api.py    returns  { ok, keys:  [...], total }
    Raises APIError(status=404) if the key is not found in inventory.
    """
    clean = key.strip().upper()
    data  = await _request(
        "GET", "/api/admin/inventory",
        params={"search": clean},
        headers=_admin_headers(),
    )
    # server.js uses "items"; api.py uses "keys" — accept both
    records = data.get("items") or data.get("keys") or []
    # Exact match only
    record = next((r for r in records if r.get("key", "").upper() == clean), None)
    if record is None:
        raise APIError(f"Key {clean} not found in inventory", 404, "/api/admin/inventory")
    return record


async def revoke_inventory_key(key: str) -> dict:
    """POST /api/admin/inventory/<key>/revoke — revoke an inventory key."""
    return await _request(
        "POST", f"/api/admin/inventory/{key.strip().upper()}/revoke",
        headers=_admin_headers(),
    )


async def reset_inventory_hwid(key: str) -> dict:
    """PATCH /api/admin/inventory/<key> { hwid: '' } — clear HWID binding."""
    return await _request(
        "PATCH", f"/api/admin/inventory/{key.strip().upper()}",
        json={"hwid": ""},
        headers=_admin_headers(),
    )


async def extend_inventory_key(key: str, days: int) -> dict:
    """POST /api/admin/inventory/<key>/extend { days }"""
    return await _request(
        "POST", f"/api/admin/inventory/{key.strip().upper()}/extend",
        json={"days": days},
        headers=_admin_headers(),
    )


async def list_inventory_keys(
    status: str | None = None,
    plan: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """GET /api/admin/inventory — list inventory keys with optional filters.
    Handles both response shapes: server.js → 'items', api.py → 'keys'.
    """
    params: dict[str, str] = {}
    if status:
        params["status"] = status
    if plan:
        params["plan"] = plan
    if search:
        params["search"] = search
    data = await _request(
        "GET", "/api/admin/inventory",
        params=params,
        headers=_admin_headers(),
    )
    return data.get("items") or data.get("keys") or []


# ---------------------------------------------------------------------------
# HMAC-signed key system  (staff/admin generation via api.py)
# These keys (GHOST-XXXXX-XXXXX-XXXXX-XXXXX, 5-char segments) are generated
# by the HMAC keygen and stored in issued_keys.json.
# ---------------------------------------------------------------------------

async def generate_keys(tier: str, days: int, note: str, quantity: int) -> list[str]:
    """POST /api/admin/license/generate — generate HMAC-signed keys."""
    data = await _request(
        "POST", "/api/admin/license/generate",
        json={"tier": tier, "days": days, "note": note, "quantity": quantity},
        headers=_admin_headers(),
    )
    return data.get("keys", [])


async def key_info(key: str) -> dict:
    """GET /api/admin/license/<key> — look up HMAC key info."""
    return await _request(
        "GET", f"/api/admin/license/{key.strip().upper()}",
        headers=_admin_headers(),
    )


async def ban_key(key: str, reason: str = "") -> dict:
    """POST /api/admin/license/<key>/ban"""
    return await _request(
        "POST", f"/api/admin/license/{key.strip().upper()}/ban",
        json={"reason": reason},
        headers=_admin_headers(),
    )


async def unban_key(key: str) -> dict:
    """POST /api/admin/license/<key>/unban"""
    return await _request(
        "POST", f"/api/admin/license/{key.strip().upper()}/unban",
        headers=_admin_headers(),
    )


async def delete_key(key: str) -> dict:
    """DELETE /api/admin/license/<key>"""
    return await _request(
        "DELETE", f"/api/admin/license/{key.strip().upper()}",
        headers=_admin_headers(),
    )


async def extend_key(key: str, days: int) -> dict:
    """POST /api/admin/license/<key>/extend"""
    return await _request(
        "POST", f"/api/admin/license/{key.strip().upper()}/extend",
        json={"days": days},
        headers=_admin_headers(),
    )


async def reset_activation(key: str) -> dict:
    """POST /api/admin/license/<key>/reset"""
    return await _request(
        "POST", f"/api/admin/license/{key.strip().upper()}/reset",
        headers=_admin_headers(),
    )


async def list_keys(tier: str = "ALL", limit: int = 50) -> list[dict]:
    """GET /api/admin/keys — list HMAC issued keys."""
    data = await _request(
        "GET", f"/api/admin/keys?tier={tier}&limit={limit}",
        headers=_admin_headers(),
    )
    return data.get("keys", [])


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def list_users() -> list[dict]:
    """GET /api/admin/users"""
    data = await _request("GET", "/api/admin/users", headers=_admin_headers())
    return data.get("users", [])


async def user_info(username: str) -> dict | None:
    users = await list_users()
    return next((u for u in users if u.get("username", "").lower() == username.lower()), None)


async def delete_user(username: str) -> dict:
    """DELETE /api/admin/users/<username>"""
    return await _request(
        "DELETE", f"/api/admin/users/{username}",
        headers=_admin_headers(),
    )


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def list_orders() -> list[dict]:
    """GET /api/admin/orders"""
    data = await _request("GET", "/api/admin/orders", headers=_admin_headers())
    return data.get("orders", [])


async def get_order(order_id: str) -> dict | None:
    """GET /api/admin/orders/<order_id>"""
    try:
        data = await _request(
            "GET", f"/api/admin/orders/{order_id}",
            headers=_admin_headers(),
        )
        return data.get("order")
    except APIError:
        return None


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

async def list_customers(search: str = "") -> list[dict]:
    """GET /api/admin/customers?search="""
    params = {}
    if search:
        params["search"] = search
    data = await _request(
        "GET", "/api/admin/customers",
        params=params,
        headers=_admin_headers(),
    )
    return data.get("customers", [])


# ---------------------------------------------------------------------------
# Stats  (real backend endpoint — GET /api/admin/stats)
# ---------------------------------------------------------------------------

async def stats() -> dict:
    """GET /api/admin/stats — real aggregate stats from the backend."""
    return await _request("GET", "/api/admin/stats", headers=_admin_headers())


# ---------------------------------------------------------------------------
# App version / downloads
# ---------------------------------------------------------------------------

async def get_latest_release() -> dict:
    """GET /api/releases/latest — no auth required. Current app version info."""
    try:
        return await _request("GET", "/api/releases/latest")
    except APIError:
        return {}


async def get_download_info() -> dict:
    """GET /api/admin/downloads — admin downloads metadata."""
    try:
        return await _request("GET", "/api/admin/downloads", headers=_admin_headers())
    except APIError:
        return {}


# ---------------------------------------------------------------------------
# API health probes
# ---------------------------------------------------------------------------

async def health_check() -> dict:
    """GET /health — shallow liveness probe."""
    return await _request("GET", "/health")


async def status_check() -> dict:
    """GET /status — deep readiness probe (keys/users counts, uptime)."""
    return await _request("GET", "/status")


# ---------------------------------------------------------------------------
# License validation (public — used by desktop app)
# ---------------------------------------------------------------------------

async def validate_license(key: str) -> dict:
    """POST /api/license/activate — validate a key (desktop activation path)."""
    return await _request(
        "POST", "/api/license/activate",
        json={"license_key": key},
        headers={"Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Customer-role assignment helpers (used by bot.py background task)
# ---------------------------------------------------------------------------

async def get_pending_customer_roles() -> list[dict]:
    """
    GET /api/admin/pending-customer-roles
    Returns orders that are paid, have a verified OAuth-linked Discord ID,
    and have not yet had the Customer role granted.

    The server resolves the Discord ID from the user's OAuth-linked account
    record — never from client-submitted data.  Only numeric 17–19 digit
    snowflakes pass through.

    The bot must only use discord_id from this response to locate members.
    Never use display names or usernames.
    """
    try:
        data = await _request(
            "GET", "/api/admin/pending-customer-roles",
            headers=_admin_headers(),
        )
        return data.get("orders", [])
    except APIError:
        return []


async def mark_customer_role_granted(order_id: str) -> dict:
    """
    POST /api/admin/orders/<order_id>/role-granted
    Tell the server that CUSTOMER_ROLE_ID has been successfully granted.
    The server marks discord_role_granted=True and clears discord_role_pending
    on the linked user account.
    """
    return await _request(
        "POST", f"/api/admin/orders/{order_id}/role-granted",
        headers=_admin_headers(),
    )


# ---------------------------------------------------------------------------
# Discord sync helper — look up a customer by Discord ID across orders/users
# ---------------------------------------------------------------------------

async def find_customer_by_discord_id(discord_id: int) -> dict | None:
    """
    Search orders for a record where discord_id matches.
    Returns the most-recent matching order or None.
    """
    try:
        orders = await list_orders()
        sid    = str(discord_id)
        matches = [o for o in orders if str(o.get("discord_id", "") or "") == sid]
        if not matches:
            return None
        return max(matches, key=lambda o: o.get("created_at", ""))
    except APIError:
        return None
