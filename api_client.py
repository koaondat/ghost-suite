"""
api_client.py — Async HTTP client for the Ghost shared API
===========================================================
Used by bot.py (Discord bot) and any other async consumer.
All admin endpoints require the GHOST_ADMIN_API_KEY env var.

This module replaces direct keygen.py imports in the bot;
keygen.py is now only called by api.py on the server.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

log = logging.getLogger("ghost.api_client")

# Default to the shared API running on the same host as the bot
API_BASE = os.environ.get("GHOST_API_URL", "http://localhost:5056").rstrip("/")
ADMIN_KEY = os.environ.get("GHOST_ADMIN_API_KEY", "").strip()


def _admin_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Admin-Key":  ADMIN_KEY,
    }


class APIError(Exception):
    """Raised when the shared API returns ok=False or a non-2xx status."""
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


async def _request(method: str, path: str, **kwargs) -> dict:
    """
    Make an async HTTP request to the shared API.
    Raises APIError if the response indicates failure.
    """
    url = f"{API_BASE}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, **kwargs) as resp:
            try:
                data: dict = await resp.json()
            except Exception:
                data = {}
            if not data.get("ok") or resp.status >= 400:
                error = data.get("error") or data.get("message") or f"HTTP {resp.status}"
                raise APIError(error, resp.status)
            return data


# ─────────────────────────────────────────────────────────────────────────────
# Admin helpers called by bot.py
# ─────────────────────────────────────────────────────────────────────────────

async def generate_keys(tier: str, days: int, note: str, quantity: int) -> list[str]:
    data = await _request(
        "POST", "/api/admin/license/generate",
        json={"tier": tier, "days": days, "note": note, "quantity": quantity},
        headers=_admin_headers(),
    )
    return data.get("keys", [])


async def key_info(key: str) -> dict:
    return await _request("GET", f"/api/admin/license/{key}",
                           headers=_admin_headers())


async def ban_key(key: str, reason: str = "") -> dict:
    return await _request("POST", f"/api/admin/license/{key}/ban",
                           json={"reason": reason},
                           headers=_admin_headers())


async def unban_key(key: str) -> dict:
    return await _request("POST", f"/api/admin/license/{key}/unban",
                           headers=_admin_headers())


async def delete_key(key: str) -> dict:
    return await _request("DELETE", f"/api/admin/license/{key}",
                           headers=_admin_headers())


async def extend_key(key: str, days: int) -> dict:
    return await _request("POST", f"/api/admin/license/{key}/extend",
                           json={"days": days},
                           headers=_admin_headers())


async def reset_activation(key: str) -> dict:
    return await _request("POST", f"/api/admin/license/{key}/reset",
                           headers=_admin_headers())


async def list_keys(tier: str = "ALL", limit: int = 50) -> list[dict]:
    data = await _request("GET", f"/api/admin/keys?tier={tier}&limit={limit}",
                           headers=_admin_headers())
    return data.get("keys", [])


async def list_users() -> list[dict]:
    data = await _request("GET", "/api/admin/users",
                           headers=_admin_headers())
    return data.get("users", [])


async def user_info(username: str) -> dict | None:
    users = await list_users()
    return next((u for u in users if u.get("username", "").lower() == username.lower()), None)


async def delete_user(username: str) -> dict:
    return await _request("DELETE", f"/api/admin/users/{username}",
                           headers=_admin_headers())


async def validate_license(key: str) -> dict:
    return await _request("POST", "/api/license/validate",
                           json={"key": key},
                           headers={"Content-Type": "application/json"})


async def stats() -> dict:
    """Aggregate stats by fetching keys and users lists."""
    keys_data  = await _request("GET", "/api/admin/keys?limit=500",
                                 headers=_admin_headers())
    users_data = await _request("GET", "/api/admin/users",
                                 headers=_admin_headers())
    keys  = keys_data.get("keys", [])
    users = users_data.get("users", [])

    tiers   = {"TRIAL": 0, "PRO": 0, "ADMIN": 0}
    active  = 0
    expired = 0
    banned  = 0
    for k in keys:
        t = str(k.get("tier", "")).upper()
        if t in tiers:
            tiers[t] += 1
        if k.get("banned"):
            banned += 1
        elif k.get("expired"):
            expired += 1
        elif k.get("valid"):
            active += 1

    return {
        "total_keys": len(keys),
        "active":     active,
        "expired":    expired,
        "banned":     banned,
        "tiers":      tiers,
        "users":      len(users),
    }
