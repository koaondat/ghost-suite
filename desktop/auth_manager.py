"""
auth_manager.py — Authentication & session management
======================================================
Handles login against the Ghost API, secure session token persistence,
and on-launch session restore so customers aren't re-prompted each run.

Session token is stored encrypted using a machine-derived key so it cannot
simply be copied between machines.  The encryption is best-effort (defense
in depth) — server-side JWT expiry is the authoritative guard.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from app_config import API_BASE_URL, APP_VERSION, SESSION_FILE

log = logging.getLogger("ghost.auth")


# ── Machine-derived obfuscation key (non-cryptographic, best-effort) ─────────

def _machine_key() -> bytes:
    """Derive a 32-byte key from stable machine identifiers."""
    parts = [
        os.environ.get("COMPUTERNAME", ""),
        os.environ.get("USERNAME", ""),
        os.environ.get("PROCESSOR_IDENTIFIER", ""),
    ]
    seed = "|".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha256(seed).digest()


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    key_len = len(key)
    return bytes(b ^ key[i % key_len] for i, b in enumerate(data))


def _encode_token(token: str) -> str:
    raw = token.encode("utf-8")
    obf = _xor_bytes(raw, _machine_key())
    return base64.b64encode(obf).decode("ascii")


def _decode_token(encoded: str) -> str:
    obf = base64.b64decode(encoded.encode("ascii"))
    raw = _xor_bytes(obf, _machine_key())
    return raw.decode("utf-8")


# ── Session file I/O ──────────────────────────────────────────────────────────

def save_session(token: str, username: str, license_key: str = "") -> None:
    """Persist the session to disk.  token may be a JWT or a license key."""
    try:
        payload = json.dumps({
            "token":       _encode_token(token),
            "username":    username,
            "license_key": _encode_token(license_key) if license_key else "",
            "saved_at":    int(time.time()),
        })
        SESSION_FILE.write_text(payload, "utf-8")
    except Exception as exc:
        log.warning("save_session failed: %s", exc)


def save_key_session(license_key: str) -> None:
    """Persist a license-key-only session (no JWT)."""
    save_session(token="", username="", license_key=license_key)


def load_session() -> tuple[str, str] | None:
    """
    Load a previously saved session.
    Returns (token, username) if the file exists and is parseable, else None.
    For key-only sessions token is empty and username is empty.
    """
    try:
        if not SESSION_FILE.exists():
            return None
        data = json.loads(SESSION_FILE.read_text("utf-8"))
        token    = _decode_token(data["token"]) if data.get("token") else ""
        username = data.get("username", "")
        return token, username
    except Exception as exc:
        log.debug("load_session failed: %s", exc)
        return None


def load_key_session() -> str | None:
    """
    Load the stored license key (key-only session).
    Returns the license key string, or None if not available.
    """
    try:
        if not SESSION_FILE.exists():
            return None
        data = json.loads(SESSION_FILE.read_text("utf-8"))
        encoded = data.get("license_key", "")
        if not encoded:
            return None
        return _decode_token(encoded)
    except Exception as exc:
        log.debug("load_key_session failed: %s", exc)
        return None


def clear_session() -> None:
    """Delete the persisted session."""
    try:
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
    except Exception:
        pass


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(path: str, payload: dict, token: str | None = None,
          timeout: int = 10) -> dict:
    url     = f"{API_BASE_URL}{path}"
    headers = {"Content-Type": "application/json",
               "User-Agent":   f"GhostDesktop/{APP_VERSION}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8"))
            return {"ok": False, "error": err_body.get("error", str(exc)),
                    "status": exc.code}
        except Exception:
            return {"ok": False, "error": str(exc), "status": exc.code}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "status": 0}


def _get(path: str, token: str | None = None, timeout: int = 10) -> dict:
    url     = f"{API_BASE_URL}{path}"
    headers = {"User-Agent": f"GhostDesktop/{APP_VERSION}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8"))
            return {"ok": False, "error": err_body.get("error", str(exc)),
                    "status": exc.code}
        except Exception:
            return {"ok": False, "error": str(exc), "status": exc.code}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "status": 0}


# ── Public API ────────────────────────────────────────────────────────────────

def activate_license(license_key: str) -> dict:
    """
    Activate a license key via the key-only endpoint.
    No username or password required.

    Returns {"ok": True, "status": "active", "product": ..., "expires_at": ...,
             "remaining_seconds": ..., "plan": ..., "key_masked": ...}
    or      {"ok": False, "error": <customer-friendly message>}
    """
    return _post("/api/license/activate", {"license_key": license_key})


def load_activation(license_key: str) -> dict:
    """
    Re-validate a stored license key (used on restart to confirm it is still active).
    Returns the same shape as activate_license().
    """
    return _post("/api/license/activate", {"license_key": license_key})


def login(username: str, password: str, license_key: str) -> dict:
    """
    Authenticate with the Ghost API (account-based login).
    Returns {"ok": True, "token": ..., "username": ..., "tier": ...}
    or      {"ok": False, "error": ...}
    """
    if not API_BASE_URL:
        return {"ok": False, "error": "API URL not configured."}
    return _post("/api/auth/login",
                 {"identity": username, "password": password, "license_key": license_key})


def validate_stored_session(token: str) -> dict:
    """
    Check that a stored session is still valid by calling /api/license/info.
    Returns the license info dict on success or {"ok": False} on failure.
    """
    if not API_BASE_URL:
        return {"ok": False, "error": "offline"}
    return _get("/api/license/info", token=token, timeout=8)


def logout_remote(token: str) -> None:
    """Best-effort remote logout — ignores errors."""
    if not API_BASE_URL:
        return
    try:
        _post("/api/auth/logout", {}, token=token, timeout=5)
    except Exception:
        pass
