"""
license_service.py — Live license data from the Ghost API
==========================================================
Fetches and caches license state from the backend.
Never trusts locally stored expiry dates for entitlement decisions.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
import urllib.error
import urllib.request
import json
from typing import Callable, Optional

from app_config import API_BASE_URL, APP_VERSION

log = logging.getLogger("ghost.license")


def _get(path: str, token: str, timeout: int = 10) -> dict:
    url     = f"{API_BASE_URL}{path}"
    headers = {"User-Agent":   f"GhostDesktop/{APP_VERSION}",
               "Authorization": f"Bearer {token}"}
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


class LicenseInfo:
    """
    Immutable snapshot of license state.

    Supports two source shapes:
      1. The new /api/license/activate response:
         { ok, status, product, plan, key_masked, expires_at, remaining_seconds }

      2. The legacy /api/license/info response (JWT-gated):
         { ok, license: { key, tier, valid, expired, banned, days_remaining,
                          created, expiry } }
    """

    __slots__ = (
        "ok", "status_label", "tier", "key", "activated_date",
        "expiry_date", "days_remaining", "is_active", "is_expired",
        "is_banned", "error",
    )

    def __init__(self, raw: dict):
        # ── New activation-response shape ─────────────────────────────────────
        if raw.get("status") and "license" not in raw:
            # /api/license/activate response
            status         = (raw.get("status") or "").lower()
            self.ok        = bool(raw.get("ok"))
            self.tier      = (raw.get("plan") or "").upper()
            self.key       = (raw.get("key_masked") or "").upper()
            self.is_active = status == "active" and self.ok
            self.is_expired = status == "expired"
            self.is_banned  = status in ("revoked", "banned")
            rem_secs        = raw.get("remaining_seconds")
            self.days_remaining = int(rem_secs // 86400) if rem_secs else 0
            self.error      = raw.get("error") or ""
            self.expiry_date    = raw.get("expires_at") or "—"
            self.activated_date = "—"
        else:
            # ── Legacy /api/license/info shape ────────────────────────────────
            lic = raw.get("license") or {}
            self.ok            = bool(raw.get("ok"))
            self.tier          = (lic.get("tier") or "").upper()
            self.key           = (lic.get("key") or "").upper()
            self.is_active     = bool(lic.get("valid")) and not bool(lic.get("expired")) \
                                 and not bool(lic.get("banned"))
            self.is_expired    = bool(lic.get("expired"))
            self.is_banned     = bool(lic.get("banned"))
            self.days_remaining = int(lic.get("days_remaining") or 0)
            self.error         = lic.get("error") or raw.get("error") or ""

            # Dates
            raw_created = lic.get("created") or ""
            raw_expiry  = lic.get("expiry") or ""
            self.activated_date = self._fmt_date(raw_created)
            self.expiry_date    = self._fmt_date(raw_expiry)

        # Status label (shared logic)
        if self.is_banned:
            self.status_label = "Revoked"
        elif self.is_expired:
            self.status_label = "Expired"
        elif self.is_active:
            self.status_label = "Active"
        else:
            self.status_label = "Invalid"

    @staticmethod
    def _fmt_date(raw: str) -> str:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(raw[:19], fmt[:len(fmt)])
                return dt.strftime("%b %d, %Y")
            except Exception:
                continue
        return raw[:10] if raw else "—"

    def time_remaining_str(self) -> str:
        """Human-readable countdown: '27d' or 'Lifetime' or 'Expired'."""
        if self.is_expired:
            return "Expired"
        if self.days_remaining <= 0:
            # 0 days_remaining means either Lifetime (no expiry) or just expired.
            # If the key is still active it must be a Lifetime key.
            return "Lifetime" if self.is_active else "Expired"
        return f"{self.days_remaining}d"

    def masked_key(self) -> str:
        """Return key with middle segments masked: XXXX-XXXX-XXXX-1234."""
        parts = self.key.split("-")
        if len(parts) < 4:
            return self.key
        masked = parts[:-1]
        return "-".join("XXXX" for _ in masked) + "-" + parts[-1]


def _post_activate(license_key: str, timeout: int = 10) -> dict:
    """POST /api/license/activate with only a license key — no JWT required."""
    import urllib.request as _urlreq
    url     = f"{API_BASE_URL}/api/license/activate"
    headers = {"Content-Type": "application/json",
               "User-Agent":   f"GhostDesktop/{APP_VERSION}"}
    body    = json.dumps({"license_key": license_key}).encode("utf-8")
    req     = _urlreq.Request(url, data=body, headers=headers, method="POST")
    try:
        with _urlreq.urlopen(req, timeout=timeout) as resp:
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


class LicensePoller:
    """
    Background poller that refreshes license state every `interval` seconds.
    Calls `on_update(LicenseInfo)` on the main thread via `schedule_cb`.
    `schedule_cb` should be Tk.after(0, fn) or equivalent.

    `token` is the license key (new flow) or a JWT (legacy flow).
    If the token looks like a license key (starts with GHOST-) the new
    activation endpoint is used; otherwise the legacy /api/license/info
    Bearer-auth endpoint is used.
    """

    def __init__(self, token: str, interval: int = 300,
                 on_update: Callable[[LicenseInfo], None] | None = None,
                 schedule_cb: Callable[[Callable], None] | None = None):
        self._token       = token
        self._interval    = interval
        self._on_update   = on_update
        self._schedule_cb = schedule_cb
        self._stop_evt    = threading.Event()
        self._thread      = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()

    def _run(self) -> None:
        while not self._stop_evt.wait(self._interval):
            try:
                raw  = self._fetch()
                info = LicenseInfo(raw)
                if self._on_update and self._schedule_cb:
                    fn = self._on_update
                    self._schedule_cb(lambda f=fn, i=info: f(i))
            except Exception as exc:
                log.debug("LicensePoller error: %s", exc)

    def _fetch(self) -> dict:
        if self._token.upper().startswith("GHOST-"):
            return _post_activate(self._token)
        return _get("/api/license/info", self._token)


def fetch_once(token: str) -> LicenseInfo:
    """
    Fetch license info once synchronously (call from a worker thread).
    Accepts either a license key (new flow) or a JWT (legacy flow).
    """
    if token.upper().startswith("GHOST-"):
        raw = _post_activate(token)
    else:
        raw = _get("/api/license/info", token)
    return LicenseInfo(raw)
