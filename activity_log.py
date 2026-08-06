"""
activity_log.py — Login & Registration Activity Logger
=======================================================
Records every authentication event (login success, login failure,
registration success, registration failure) to a JSON-lines file.

Captured fields (never stores passwords, tokens, or HMAC secrets):
  event_type  : "login_success" | "login_fail" | "register_success" | "register_fail"
  username    : string (or "" when not supplied)
  user_id     : SHA-256(username.lower())[:12]  — opaque, not reversible by itself
  timestamp   : ISO-8601 UTC  e.g. "2025-01-15T14:32:07Z"
  ip          : best-effort local/remote IP (127.0.0.1 when offline)
  geo         : approximate city / country resolved from public IP (best-effort)
  device_name : computer hostname
  os_info     : Windows build string
  browser     : "GhostConfig/<build>"  (this is a desktop app, not a browser)
  license_key : first 8 chars + "…" — enough to identify tier, never full key
  result      : human-readable outcome string
  error       : error message on failure, "" on success

This module is intentionally dependency-free (stdlib only) and is safe
to import on non-Windows machines (geo lookup simply returns "N/A").
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import socket
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


ACTIVITY_LOG_PATH = _data_dir() / "activity_log.json"

# Thread-lock so concurrent auth attempts don't corrupt the JSON.
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_id(username: str) -> str:
    """Opaque 12-char hex derived from username — not reversible without salt."""
    return hashlib.sha256(username.strip().lower().encode()).hexdigest()[:12]


def _mask_key(key: str) -> str:
    """Show only first 8 chars of the license key followed by '…'."""
    clean = key.strip()
    if len(clean) <= 8:
        return clean
    return clean[:8] + "…"


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# Cache the geo lookup result for the session so we only hit the API once.
_geo_cache: Optional[str] = None
_geo_lock = threading.Lock()


def _geo_lookup(ip: str) -> str:
    """Return approximate city/country string for *ip* (best-effort, may be blank)."""
    global _geo_cache
    with _geo_lock:
        if _geo_cache is not None:
            return _geo_cache
        try:
            url = f"http://ip-api.com/json/{ip}?fields=city,country,status"
            req = urllib.request.Request(url, headers={"User-Agent": "GhostConfig/4.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
            if data.get("status") == "success":
                city    = data.get("city", "")
                country = data.get("country", "")
                result  = ", ".join(p for p in (city, country) if p) or "N/A"
            else:
                result = "N/A"
        except Exception:
            result = "N/A"
        _geo_cache = result
        return result


def _os_info() -> str:
    try:
        ver = platform.version()
        rel = platform.release()
        return f"Windows {rel} ({ver})"
    except Exception:
        return platform.system() or "Unknown"


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "Unknown"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record(
    event_type: str,          # "login_success" | "login_fail" | "register_success" | "register_fail"
    username:   str,
    license_key: str,
    result_msg: str,
    error_msg:  str = "",
) -> None:
    """
    Append one activity record to ACTIVITY_LOG_PATH.
    This function is non-blocking in normal usage but may take up to ~3 s on
    first call if the geo API is reachable.  Call from a background thread
    if you need to keep the UI responsive during authentication.
    """
    ip       = _local_ip()
    geo      = _geo_lookup(ip)
    hostname = _hostname()
    os_str   = _os_info()

    entry = {
        "event_type":  event_type,
        "username":    username.strip(),
        "user_id":     _user_id(username) if username.strip() else "",
        "timestamp":   datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ip":          ip,
        "geo":         geo,
        "device_name": hostname,
        "os_info":     os_str,
        "browser":     "GhostConfig/4.0",
        "license_key": _mask_key(license_key),
        "result":      result_msg,
        "error":       error_msg,
    }

    with _lock:
        try:
            records = _load_all()
            records.append(entry)
            ACTIVITY_LOG_PATH.write_text(
                json.dumps(records, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass  # Never let logging crash the auth flow


def _load_all() -> list[dict]:
    """Return all stored activity records (newest last)."""
    if not ACTIVITY_LOG_PATH.exists():
        return []
    try:
        return json.loads(ACTIVITY_LOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def load_all() -> list[dict]:
    """Thread-safe public accessor — returns a copy."""
    with _lock:
        return list(_load_all())


def load_for_user(username: str) -> list[dict]:
    """Return all records for a specific username (case-insensitive)."""
    lo = username.strip().lower()
    with _lock:
        return [r for r in _load_all() if r.get("username", "").lower() == lo]


def load_devices_for_user(username: str) -> list[dict]:
    """
    Return the unique (device_name, os_info, ip) combinations seen
    for *username*, most-recently-seen first.
    """
    seen: list[tuple[str, str, str]] = []
    devices: list[dict] = []
    for r in reversed(load_for_user(username)):
        key = (r.get("device_name", ""), r.get("os_info", ""), r.get("ip", ""))
        if key not in seen:
            seen.append(key)
            devices.append({
                "device_name": r.get("device_name", ""),
                "os_info":     r.get("os_info", ""),
                "ip":          r.get("ip", ""),
                "geo":         r.get("geo", ""),
                "last_seen":   r.get("timestamp", ""),
            })
    return devices


def clear_all() -> None:
    """Erase all activity records.  Admin-only operation."""
    with _lock:
        try:
            ACTIVITY_LOG_PATH.write_text("[]", encoding="utf-8")
        except Exception:
            pass
