"""
api.py — Ghost Shared Backend API
==================================
Single Flask application that serves the website, Discord bot,
desktop admin panel, and app through one set of HTTP endpoints.

Endpoints
---------
  Auth (customer-facing)
    POST /api/auth/register        Register a new account
    POST /api/auth/login           Login; returns signed JWT session token
    POST /api/auth/logout          Invalidate session (client-side drop)

  License (customer — requires valid JWT session)
    GET  /api/license/info         View own license info
    POST /api/license/reset        Reset own activation (self-service)
    GET  /api/purchases            View own purchase history
    GET  /api/downloads            List available downloads (tier-gated)
    POST /api/downloads/request    Request signed download token

  License admin (requires X-Admin-Key header or ADMIN-tier JWT)
    POST /api/admin/license/generate     Generate one license key
    GET  /api/admin/license/<key>        View any license info
    POST /api/admin/license/<key>/ban    Ban a key
    POST /api/admin/license/<key>/unban  Unban a key
    DELETE /api/admin/license/<key>      Delete key record
    POST /api/admin/license/<key>/extend Extend expiry
    POST /api/admin/license/<key>/reset  Reset activation record
    GET  /api/admin/keys                 List issued keys
    GET  /api/admin/users                List registered users
    DELETE /api/admin/users/<username>   Delete a user account

Security
--------
  • Customer routes require a valid JWT in Authorization: Bearer <token>
    or the ghost_token cookie (dashboard uses cookie path).
  • Admin routes require X-Admin-Key matching GHOST_ADMIN_API_KEY env var,
    OR a JWT whose tier is ADMIN.
  • Passwords are hashed with SHA-256 + per-user salt (matching keygen.py).
  • HMAC secret, admin key, and Stripe secrets stay in env vars — never
    returned in any response.
  • Duplicate-request protection via per-endpoint idempotency tokens.
  • Rate limiting: 10 req/min on auth endpoints, 60 req/min on others.
  • All state-changing operations append to api_audit.json.

Run
---
  python api.py                         (dev, port 5056)
  GHOST_API_PORT=8000 python api.py     (custom port)

Deps: flask, PyJWT, flask-limiter, python-dotenv (pip install -r requirements.txt)
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import sys
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any

# ── path: project root next to this file ─────────────────────────────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from dotenv import load_dotenv  # type: ignore
load_dotenv(_HERE / ".env")

import keygen    # reuse all key logic — never duplicated here
import inventory as _inv  # type: ignore

# ── Optional deps (graceful import errors turn into startup failures) ─────────
try:
    from flask import Flask, request, jsonify, g  # type: ignore
    import jwt as _pyjwt  # type: ignore   (PyJWT)
    from flask_limiter import Limiter  # type: ignore
    from flask_limiter.util import get_remote_address  # type: ignore
except ImportError as _e:
    raise SystemExit(
        f"Missing dependency: {_e}\n"
        "Run: pip install flask PyJWT flask-limiter python-dotenv"
    ) from _e

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("ghost.api")

# ── Env / secrets ─────────────────────────────────────────────────────────────
_ADMIN_API_KEY   = os.environ.get("GHOST_ADMIN_API_KEY", "").strip()
_JWT_SECRET      = os.environ.get("GHOST_JWT_SECRET", "").strip()
_JWT_ALGO        = "HS256"
_JWT_TTL_SECS    = int(os.environ.get("GHOST_JWT_TTL_SECS", 86400 * 7))   # 7 days
_ALLOWED_ORIGINS = os.environ.get("GHOST_ALLOWED_ORIGINS", "*")

if not _ADMIN_API_KEY:
    log.warning("GHOST_ADMIN_API_KEY is not set — admin endpoints will be inaccessible")
if not _JWT_SECRET:
    log.warning("GHOST_JWT_SECRET is not set — JWT signing will use an insecure fallback")
    _JWT_SECRET = "INSECURE-FALLBACK-SET-GHOST_JWT_SECRET-IN-ENV"
if not os.environ.get("GHOST_HMAC_SECRET"):
    log.warning("GHOST_HMAC_SECRET is not set — keygen is using the default dev seed")

# ── Data files ────────────────────────────────────────────────────────────────
API_AUDIT_LOG = _HERE / "api_audit.json"
ORDERS_DB     = _HERE / "orders.json"

# ── Admin panel session cookie ────────────────────────────────────────────────
# A short-lived JWT baked into the HttpOnly __Host-ghost_admin_session cookie.
# Issued by POST /api/admin/panel/auth and verified by require_admin.
# Cookie requirements (enforced here):
#   • Name:      __Host-ghost_admin_session  (__Host- prefix → browser requires Secure + Path=/)
#   • HttpOnly:  True
#   • Secure:    True  (enforced by __Host- prefix; Flask sets it explicitly)
#   • SameSite:  Lax
#   • Path:      /  (enforced by __Host- prefix)
#   • no Domain attribute  (required by __Host- prefix)
_PANEL_JWT_ALGO     = "HS256"
_PANEL_JWT_TTL      = 8 * 3600   # 8 hours
_ADMIN_COOKIE_NAME  = "__Host-ghost_admin_session"

# ── Thread lock for the audit log ─────────────────────────────────────────────
_audit_lock = threading.Lock()

# ── Idempotency key store: maps idempotency_key → (response_body, status_code)
# Stored in-memory; resets on server restart (sufficient for most cases).
_idempotency: dict[str, tuple[dict, int]] = {}
_idempotency_lock = threading.Lock()
_IDEMPOTENCY_TTL = 3600   # 1 hour

# ─────────────────────────────────────────────────────────────────────────────
# Flask app + rate limiter
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["PROPAGATE_EXCEPTIONS"] = True

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["60 per minute"],
    storage_uri="memory://",
)


# ── CORS ───────────────────────────────────────────────────────────────────────
@app.after_request
def _cors(response):
    # credentials: "include" requires an explicit origin (never wildcard).
    # If GHOST_ALLOWED_ORIGINS is "*" (dev default) we reflect the request origin
    # so credentialed requests work locally.  In production set an exact origin.
    origin = request.headers.get("Origin", "")
    if _ALLOWED_ORIGINS == "*":
        allowed = origin or "*"
    else:
        allowed = _ALLOWED_ORIGINS
    response.headers["Access-Control-Allow-Origin"]       = allowed
    response.headers["Access-Control-Allow-Credentials"]  = "true"
    response.headers["Access-Control-Allow-Headers"]      = "Content-Type, Authorization, X-Admin-Key, Idempotency-Key"
    response.headers["Access-Control-Allow-Methods"]      = "GET,POST,DELETE,PATCH,OPTIONS"
    return response


@app.route("/<path:_>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def _preflight(_=""):
    return "", 204


# ─────────────────────────────────────────────────────────────────────────────
# Audit helpers
# ─────────────────────────────────────────────────────────────────────────────

def _audit(action: str, actor: str, target: str, details: str = "", ok: bool = True) -> None:
    """Append one record to api_audit.json.  Never blocks — uses background thread.
    Captures the remote IP inside the request context before spawning the thread.
    """
    # Capture IP while still inside the request context (before the thread starts)
    try:
        ip = request.remote_addr or ""
    except RuntimeError:
        ip = ""

    def _write(_ip: str = ip):
        with _audit_lock:
            try:
                records: list[dict] = []
                if API_AUDIT_LOG.exists():
                    try:
                        records = json.loads(API_AUDIT_LOG.read_text("utf-8"))
                    except Exception:
                        pass
                records.append({
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "action":    action,
                    "actor":     actor,
                    "target":    target,
                    "details":   details,
                    "ok":        ok,
                    "ip":        _ip,
                })
                # Keep last 10 000 records
                tmp = API_AUDIT_LOG.with_suffix(API_AUDIT_LOG.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(records[-10_000:], indent=2, default=str), "utf-8"
                )
                tmp.replace(API_AUDIT_LOG)
            except Exception as exc:
                log.error("api_audit write error: %s", exc)

    threading.Thread(target=_write, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# JWT helpers
# ─────────────────────────────────────────────────────────────────────────────

def _issue_jwt(username: str, tier: str) -> str:
    payload = {
        "sub":  username,
        "tier": tier.upper(),
        "iat":  int(time.time()),
        "exp":  int(time.time()) + _JWT_TTL_SECS,
    }
    return _pyjwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGO)


def _decode_jwt(token: str) -> dict | None:
    try:
        return _pyjwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGO])
    except _pyjwt.ExpiredSignatureError:
        return None
    except _pyjwt.InvalidTokenError:
        return None


def _issue_panel_jwt(actor: str = "admin") -> str:
    """Issue a short-lived JWT for the web admin panel UI."""
    payload = {
        "sub":   actor,
        "scope": "admin_panel",
        "iat":   int(time.time()),
        "exp":   int(time.time()) + _PANEL_JWT_TTL,
    }
    return _pyjwt.encode(payload, _JWT_SECRET, algorithm=_PANEL_JWT_ALGO)


def _decode_panel_jwt(token: str) -> dict | None:
    """Validate a panel JWT; returns claims or None."""
    try:
        claims = _pyjwt.decode(token, _JWT_SECRET, algorithms=[_PANEL_JWT_ALGO])
        if claims.get("scope") != "admin_panel":
            return None
        return claims
    except _pyjwt.ExpiredSignatureError:
        return None
    except _pyjwt.InvalidTokenError:
        return None


def _get_token() -> str | None:
    """Extract Bearer token from Authorization header or ghost_token cookie."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.cookies.get("ghost_token")


# ─────────────────────────────────────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────────────────────────────────────

def require_session(f):
    """Require a valid JWT session.  Sets g.user (username) and g.tier."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = _get_token()
        if not token:
            return jsonify({"ok": False, "error": "Authentication required"}), 401
        claims = _decode_jwt(token)
        if not claims:
            return jsonify({"ok": False, "error": "Session expired or invalid"}), 401
        g.user = claims["sub"]
        g.tier = claims.get("tier", "TRIAL")
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    """
    Require one of:
      • __Host-ghost_admin_session cookie (HttpOnly panel session — primary web path), OR
      • X-Admin-Key header matching GHOST_ADMIN_API_KEY (bot / CI scripts), OR
      • X-Admin-Panel-Token header (legacy — panel JWT in header, kept for compat), OR
      • A valid JWT whose tier is ADMIN.
    Sets g.actor for audit logging.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        import hmac as _hmac

        # ── Path 1: HttpOnly session cookie (browser admin panel) ─────────────
        cookie_token = request.cookies.get(_ADMIN_COOKIE_NAME, "").strip()
        if cookie_token:
            claims = _decode_panel_jwt(cookie_token)
            if not claims:
                return jsonify({"ok": False, "error": "Admin session expired. Please log in again."}), 401
            g.actor = claims.get("sub", "admin")
            g.tier  = "ADMIN"
            return f(*args, **kwargs)

        # ── Path 2: raw API key (bot, desktop panel, CI scripts) ──────────────
        api_key = request.headers.get("X-Admin-Key", "").strip()
        if api_key:
            if not _ADMIN_API_KEY:
                return jsonify({"ok": False, "error": "Admin access not configured"}), 403
            if not _hmac.compare_digest(api_key.encode(), _ADMIN_API_KEY.encode()):
                _audit("admin_auth_fail", "apikey", "", "Bad X-Admin-Key", ok=False)
                return jsonify({"ok": False, "error": "Invalid admin key"}), 403
            g.actor = "__api_key__"
            return f(*args, **kwargs)

        # ── Path 3: web admin panel JWT header (legacy / non-browser clients) ──
        panel_token = request.headers.get("X-Admin-Panel-Token", "").strip()
        if panel_token:
            claims = _decode_panel_jwt(panel_token)
            if not claims:
                return jsonify({"ok": False, "error": "Admin session expired. Please log in again."}), 401
            g.actor = claims.get("sub", "admin")
            g.tier  = "ADMIN"
            return f(*args, **kwargs)

        # ── Path 4: ADMIN-tier customer JWT ───────────────────────────────────
        token = _get_token()
        if not token:
            return jsonify({"ok": False, "error": "Admin authentication required"}), 401
        claims = _decode_jwt(token)
        if not claims:
            return jsonify({"ok": False, "error": "Session expired or invalid"}), 401
        if claims.get("tier", "").upper() != "ADMIN":
            _audit("admin_auth_fail", claims.get("sub", ""), "", "Insufficient tier", ok=False)
            return jsonify({"ok": False, "error": "Admin role required"}), 403
        g.actor = claims["sub"]
        g.tier  = "ADMIN"
        return f(*args, **kwargs)
    return wrapper


def idempotent(f):
    """
    Check the optional Idempotency-Key header.  If a cached response exists for
    that key it is returned immediately; otherwise the result is cached after the
    first successful (2xx) call.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        ikey = request.headers.get("Idempotency-Key", "").strip()
        if ikey:
            with _idempotency_lock:
                entry = _idempotency.get(ikey)
            if entry:
                body, status = entry
                return jsonify(body), status
        response = f(*args, **kwargs)
        # Flask may return (response, status) tuple
        if isinstance(response, tuple):
            resp_obj, status = response
        else:
            resp_obj, status = response, 200
        if ikey and 200 <= status < 300:
            with _idempotency_lock:
                _idempotency[ikey] = (resp_obj.get_json(), status)
        return resp_obj, status
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Safe helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_key_meta(meta: dict) -> dict:
    """Strip internal fields before returning key metadata to clients."""
    return {
        "key":            meta.get("key", ""),
        "valid":          meta.get("valid", False),
        "tier":           meta.get("tier", ""),
        "created":        str(meta.get("created") or ""),
        "expiry":         str(meta.get("expiry") or ""),
        "days_remaining": meta.get("days_remaining", -1),
        "expired":        meta.get("expired", False),
        "banned":         keygen.is_banned(meta.get("key", "")),
        "error":          meta.get("error", "") if not meta.get("valid") else "",
    }


def _load_orders() -> list[dict]:
    if ORDERS_DB.exists():
        try:
            return json.loads(ORDERS_DB.read_text("utf-8"))
        except Exception:
            pass
    return []


def _user_purchases(username: str) -> list[dict]:
    """Return purchase records tied to the calling user's license key."""
    # Resolve the user's key from users.json
    users = keygen._load_json(keygen.USERS_DB)
    user  = next((u for u in users if u.get("username", "").lower() == username.lower()), None)
    if not user:
        return []
    user_key = user.get("key", "").upper()
    orders = _load_orders()
    result = []
    for o in orders:
        # Match by license_key or discord/email if stored
        if o.get("license_key", "").upper() == user_key:
            result.append({
                "order_id":       o.get("order_id", ""),
                "plan":           o.get("plan", ""),
                "plan_label":     o.get("plan_label", ""),
                "tier":           o.get("tier", ""),
                "price_usd":      o.get("price_usd"),
                "payment_status": o.get("payment_status", ""),
                "created_at":     o.get("created_at", ""),
                "key_expires":    o.get("key_expires", ""),
            })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Admin Panel auth  (web UI login — separate from the customer auth)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/panel/auth", methods=["POST"])
@limiter.limit("10 per minute")
def route_admin_panel_auth():
    """
    POST /api/admin/panel/auth { password }
    Authenticates the admin panel UI.  Accepts either:
      • A raw GHOST_ADMIN_API_KEY as the password (always works when configured)
      • A SHA-256 password hash stored in admin_settings.json

    On success sets the __Host-ghost_admin_session HttpOnly cookie so the
    browser automatically includes it in subsequent same-origin admin requests.
    Cookie properties:
      HttpOnly, Secure, SameSite=Lax, Path=/, no Domain attribute.
    The __Host- prefix additionally forces Path=/ and Secure in all browsers.
    """
    import hmac as _hmac
    data = request.get_json(silent=True) or {}
    password = (data.get("password") or "").strip()

    if not password:
        return jsonify({"ok": False, "error": "Password is required"}), 400

    authenticated = False

    # Check 1: direct match against raw admin API key
    if _ADMIN_API_KEY and _hmac.compare_digest(password.encode(), _ADMIN_API_KEY.encode()):
        authenticated = True

    # Check 2: SHA-256 hash stored in settings (allows a web-UI-only password)
    if not authenticated:
        settings = _load_settings()
        stored_hash = settings.get("admin_password_hash", "").strip().lower()
        if stored_hash:
            pw_hash = hashlib.sha256(password.encode()).hexdigest().lower()
            if _hmac.compare_digest(pw_hash, stored_hash):
                authenticated = True

    if not authenticated:
        log.warning("admin_panel_auth_fail ip=%s", request.remote_addr)
        _audit("admin_panel_auth_fail", "panel", "", "Bad password", ok=False)
        return jsonify({"ok": False, "error": "Invalid password"}), 401

    token = _issue_panel_jwt("admin")
    log.info("admin_panel_auth_success ip=%s", request.remote_addr)
    _audit("admin_panel_auth", "admin", "", "Panel login", ok=True)

    resp = jsonify({"ok": True, "ttl": _PANEL_JWT_TTL})
    # __Host- prefix requires: Secure=True, Path="/", no Domain attribute.
    # We never set Domain so that the __Host- contract is satisfied.
    resp.set_cookie(
        _ADMIN_COOKIE_NAME,
        token,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
        max_age=_PANEL_JWT_TTL,
    )
    return resp


@app.route("/api/admin/panel/logout", methods=["POST"])
def route_admin_panel_logout():
    """
    POST /api/admin/panel/logout
    Clears the __Host-ghost_admin_session cookie.
    JS cannot delete an HttpOnly cookie directly — this endpoint does it.
    """
    resp = jsonify({"ok": True})
    resp.delete_cookie(
        _ADMIN_COOKIE_NAME,
        path="/",
        samesite="Lax",
        secure=True,
    )
    return resp


@app.route("/api/admin/session", methods=["GET"])
def route_admin_session():
    """
    GET /api/admin/session
    Returns { authenticated: true } when the __Host-ghost_admin_session cookie
    is present and valid; { authenticated: false } with HTTP 401 otherwise.
    Used by the admin UI on page load to decide whether to show the login form.
    Never triggers the session-expiry toast — the client handles 401 silently.
    """
    cookie_token = request.cookies.get(_ADMIN_COOKIE_NAME, "").strip()
    if not cookie_token:
        return jsonify({"authenticated": False}), 401
    claims = _decode_panel_jwt(cookie_token)
    if not claims:
        return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True, "actor": claims.get("sub", "admin")})


@app.route("/api/admin/debug-session", methods=["GET"])
def route_admin_debug_session():
    """
    GET /api/admin/debug-session
    Safe diagnostic endpoint.  Never returns cookie values, passwords, hashes,
    or secrets — only boolean presence/validity flags.
    """
    cookie_token  = request.cookies.get(_ADMIN_COOKIE_NAME, "").strip()
    cookie_present = bool(cookie_token)
    session_valid  = False
    if cookie_present:
        session_valid = _decode_panel_jwt(cookie_token) is not None
    secret_configured = bool(_JWT_SECRET and _JWT_SECRET != "INSECURE-FALLBACK-SET-GHOST_JWT_SECRET-IN-ENV")
    return jsonify({
        "cookiePresent":    cookie_present,
        "sessionValid":     session_valid,
        "secretConfigured": secret_configured,
    })


@app.route("/api/admin/panel/verify", methods=["GET"])
def route_admin_panel_verify():
    """
    GET /api/admin/panel/verify
    Returns 200 if the X-Admin-Panel-Token is still valid, 401 otherwise.
    Used by the admin UI on page load to decide whether to skip the login form.
    """
    panel_token = request.headers.get("X-Admin-Panel-Token", "").strip()
    if not panel_token:
        return jsonify({"ok": False, "error": "No panel token provided"}), 401
    claims = _decode_panel_jwt(panel_token)
    if not claims:
        return jsonify({"ok": False, "error": "Session expired or invalid"}), 401
    remaining = int(claims.get("exp", 0) - time.time())
    return jsonify({"ok": True, "actor": claims.get("sub", "admin"), "expires_in": remaining})


# ─────────────────────────────────────────────────────────────────────────────
# Auth endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
@limiter.limit("10 per minute")
def route_register():
    """POST /api/auth/register — register a new account."""
    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email    = (data.get("email") or "").strip()
    password = data.get("password") or ""
    lic_key  = (data.get("license_key") or "").strip()

    if not username or len(username) < 3:
        log.info("register_fail ip=%s username=%r reason=username_too_short", request.remote_addr, username)
        return jsonify({"ok": False, "field": "username",
                        "error": "Username must be at least 3 characters"}), 400
    if not email or "@" not in email:
        log.info("register_fail ip=%s username=%r reason=invalid_email", request.remote_addr, username)
        return jsonify({"ok": False, "field": "email",
                        "error": "A valid email address is required"}), 400
    if not password or len(password) < 8:
        log.info("register_fail ip=%s username=%r reason=password_too_short", request.remote_addr, username)
        return jsonify({"ok": False, "field": "password",
                        "error": "Password must be at least 8 characters"}), 400
    if not lic_key:
        log.info("register_fail ip=%s username=%r reason=missing_license_key", request.remote_addr, username)
        return jsonify({"ok": False, "field": "license_key",
                        "error": "A valid license key is required to register"}), 400

    result = keygen.register_user(username, password, lic_key)

    if not result["ok"]:
        field = None
        err   = result["error"]
        if "Username" in err:
            field = "username"
        elif "key" in err.lower():
            field = "license_key"
        log.info("register_fail ip=%s username=%r key=%s reason=%r", request.remote_addr, username, lic_key[:12] + "…", err)
        _audit("register", username, lic_key, err, ok=False)
        return jsonify({"ok": False, "field": field, "error": err}), 409

    log.info("register_success ip=%s username=%r tier=%s", request.remote_addr, username, result.get("tier"))
    _audit("register", username, lic_key, f"tier={result.get('tier')}")
    token = _issue_jwt(username, result.get("tier", "PRO"))
    resp  = jsonify({"ok": True, "username": username, "tier": result.get("tier"), "token": token})
    resp.set_cookie("ghost_token", token, httponly=True, samesite="Lax",
                    max_age=_JWT_TTL_SECS, secure=not app.debug)
    return resp, 201


@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("10 per minute")
def route_login():
    """POST /api/auth/login — authenticate and return a JWT."""
    data      = request.get_json(silent=True) or {}
    identity  = (data.get("identity") or data.get("username") or "").strip()
    password  = data.get("password") or ""
    lic_key   = (data.get("license_key") or "").strip()

    if not identity or not password:
        log.info("login_fail ip=%s identity=%r reason=missing_credentials", request.remote_addr, identity)
        return jsonify({"ok": False, "error": "Username and password are required"}), 400

    # If the client omits the key, look it up from the users DB
    if not lic_key:
        users = keygen._load_json(keygen.USERS_DB)
        user  = next((u for u in users
                      if u.get("username", "").lower() == identity.lower()), None)
        if user:
            lic_key = user.get("key", "")

    if not lic_key:
        log.info("login_fail ip=%s identity=%r reason=account_not_found", request.remote_addr, identity)
        return jsonify({"ok": False, "error": "Account not found or license key missing"}), 401

    result = keygen.login_user(identity, password, lic_key)
    if not result["ok"]:
        log.info("login_fail ip=%s identity=%r reason=invalid_credentials", request.remote_addr, identity)
        _audit("login", identity, lic_key, result["error"], ok=False)
        # Generic message to avoid user enumeration
        return jsonify({"ok": False, "error": "Invalid credentials"}), 401

    log.info("login_success ip=%s username=%r tier=%s", request.remote_addr, result["username"], result.get("tier"))
    _audit("login", result["username"], lic_key, f"tier={result.get('tier')}")
    token = _issue_jwt(result["username"], result.get("tier", "PRO"))
    resp  = jsonify({"ok": True, "username": result["username"],
                     "tier": result.get("tier"), "token": token})
    resp.set_cookie("ghost_token", token, httponly=True, samesite="Lax",
                    max_age=_JWT_TTL_SECS, secure=not app.debug)
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def route_logout():
    """POST /api/auth/logout — clear the session cookie."""
    resp = jsonify({"ok": True})
    resp.delete_cookie("ghost_token")
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Customer license endpoints (JWT required)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/license/info", methods=["GET"])
@require_session
def route_license_info():
    """GET /api/license/info — return calling user's own license data."""
    users   = keygen._load_json(keygen.USERS_DB)
    user    = next((u for u in users if u.get("username", "").lower() == g.user.lower()), None)
    if not user:
        return jsonify({"ok": False, "error": "User record not found"}), 404

    key  = user.get("key", "")
    meta = keygen.validate_key(key)
    return jsonify({"ok": True, "license": _safe_key_meta(meta)})


@app.route("/api/license/reset", methods=["POST"])
@require_session
@limiter.limit("3 per hour")
def route_license_reset():
    """
    POST /api/license/reset — self-service activation reset.
    Currently clears the device binding stored in the user record.
    """
    users   = keygen._load_json(keygen.USERS_DB)
    updated = False
    for u in users:
        if u.get("username", "").lower() == g.user.lower():
            u.pop("hwid", None)
            u.pop("device_id", None)
            updated = True
            break

    if not updated:
        return jsonify({"ok": False, "error": "User record not found"}), 404

    keygen._save_json(keygen.USERS_DB, users)
    _audit("license_reset_self", g.user, "", "Self-service activation reset")
    return jsonify({"ok": True, "message": "Activation reset. Re-activate on your device."})


@app.route("/api/purchases", methods=["GET"])
@require_session
def route_purchases():
    """GET /api/purchases — return calling user's purchase history."""
    purchases = _user_purchases(g.user)
    return jsonify({"ok": True, "purchases": purchases})


@app.route("/api/downloads", methods=["GET"])
@require_session
def route_downloads():
    """
    GET /api/downloads — return download manifest for the calling user.
    Actual binary URLs are NEVER returned; the client receives opaque tokens
    that must be redeemed via POST /api/downloads/request.
    """
    users = keygen._load_json(keygen.USERS_DB)
    user  = next((u for u in users if u.get("username", "").lower() == g.user.lower()), None)
    if not user:
        return jsonify({"ok": False, "error": "User record not found"}), 404

    tier   = (user.get("tier") or "TRIAL").upper()
    # Determine which platforms this tier can access
    access = {
        "TRIAL":    ["win"],
        "PRO":      ["win", "mac", "linux"],
        "LIFETIME": ["win", "mac", "linux"],
        "ADMIN":    ["win", "mac", "linux"],
    }.get(tier, ["win"])

    manifest = {
        "tier":       tier,
        "platforms":  access,
        "latest": {
            "version":      os.environ.get("GHOST_LATEST_VERSION", "v2.4.1"),
            "release_date": os.environ.get("GHOST_RELEASE_DATE", "2025-07-01"),
            "status":       "stable",
        },
    }
    return jsonify({"ok": True, "downloads": manifest})


@app.route("/api/downloads/request", methods=["POST"])
@require_session
@limiter.limit("20 per hour")
def route_download_request():
    """
    POST /api/downloads/request { token, license_key }
    Returns a short-lived signed download token.
    The actual CDN URL is resolved by the delivery layer from the token.
    Real signed URLs should be generated here using the private CDN secret
    stored in GHOST_CDN_SECRET.  This stub returns an opaque reference token.
    """
    data    = request.get_json(silent=True) or {}
    dl_token = (data.get("token") or "").strip()
    if not dl_token:
        return jsonify({"ok": False, "error": "Download token is required"}), 400

    # Validate the caller's license is active before issuing a download
    users = keygen._load_json(keygen.USERS_DB)
    user  = next((u for u in users if u.get("username", "").lower() == g.user.lower()), None)
    if not user:
        return jsonify({"ok": False, "error": "User record not found"}), 404

    meta = keygen.validate_key(user.get("key", ""))
    if not meta.get("valid"):
        return jsonify({"ok": False, "error": "Active license required to download"}), 403

    # Build a signed reference token (replace with real signed CDN URL in production)
    # Pattern: sha256(dl_token + username + timestamp + CDN_SECRET)[:24]
    cdn_secret = os.environ.get("GHOST_CDN_SECRET", "REPLACE-ME").encode()
    ts         = str(int(time.time() // 3600))    # 1-hour granularity
    sig        = hashlib.sha256(
        (dl_token + g.user + ts).encode() + cdn_secret
    ).hexdigest()[:24]

    _audit("download_request", g.user, dl_token, f"sig={sig[:8]}…")
    return jsonify({"ok": True, "ref": f"dl:{sig}:{dl_token}", "ttl": 3600})


# ─────────────────────────────────────────────────────────────────────────────
# Admin — license management
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/license/generate", methods=["POST"])
@require_admin
@idempotent
@limiter.limit("30 per minute")
def route_admin_generate():
    """POST /api/admin/license/generate { tier, days, note, quantity }"""
    data     = request.get_json(silent=True) or {}
    tier     = (data.get("tier") or "PRO").upper()
    days     = int(data.get("days", 365))
    note     = (data.get("note") or "").strip()[:200]
    quantity = max(1, min(int(data.get("quantity", 1)), 100))

    if tier not in keygen.TIERS:
        return jsonify({"ok": False, "error": f"Unknown tier. Choose from: {list(keygen.TIERS)}"}), 400
    if not (0 <= days <= 3650):
        return jsonify({"ok": False, "error": "days must be between 0 and 3650"}), 400

    generated = []
    for _ in range(quantity):
        key  = keygen.generate_key(expires_days=days, tier=tier)
        meta = keygen.validate_key(key)
        meta["note"] = note
        keygen.save_key_record(key, meta)
        generated.append(key)

    _audit("generate_key", g.actor, f"{quantity} key(s)",
           f"tier={tier} days={days} note={note} keys={','.join(generated)}")
    return jsonify({"ok": True, "keys": generated, "tier": tier, "days": days}), 201


@app.route("/api/admin/license/<key>", methods=["GET"])
@require_admin
def route_admin_key_info(key: str):
    """GET /api/admin/license/<key> — view full license info."""
    meta   = keygen.validate_key(key)
    record = next((r for r in keygen.load_all_keys()
                   if r.get("key", "").upper() == key.strip().upper()), None)
    bound  = next((u for u in keygen.load_all_users()
                   if u.get("key", "").upper() == key.strip().upper()), None)
    return jsonify({
        "ok":       True,
        "license":  _safe_key_meta(meta),
        "record":   record,
        "bound_user": {
            "username": bound.get("username") if bound else None,
            "tier":     bound.get("tier") if bound else None,
            "created":  bound.get("created") if bound else None,
        } if bound else None,
    })


@app.route("/api/admin/license/<key>/ban", methods=["POST"])
@require_admin
def route_admin_ban(key: str):
    """POST /api/admin/license/<key>/ban { reason? }"""
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()[:200]
    clean  = key.strip().upper()
    keygen.ban_key(clean, reason)
    _audit("ban_key", g.actor, clean, reason)
    return jsonify({"ok": True, "key": clean, "banned": True})


@app.route("/api/admin/license/<key>/unban", methods=["POST"])
@require_admin
def route_admin_unban(key: str):
    """POST /api/admin/license/<key>/unban"""
    clean   = key.strip().upper()
    removed = keygen.unban_key(clean)
    if removed:
        _audit("unban_key", g.actor, clean)
    return jsonify({"ok": True, "key": clean, "unbanned": removed,
                    "message": "Key unbanned." if removed else "Key was not banned."})


@app.route("/api/admin/license/<key>", methods=["DELETE"])
@require_admin
def route_admin_delete_key(key: str):
    """DELETE /api/admin/license/<key> — remove from issued_keys DB."""
    clean   = key.strip().upper()
    deleted = keygen.delete_key_record(clean)
    if deleted:
        _audit("delete_key", g.actor, clean)
    return jsonify({"ok": True, "key": clean, "deleted": deleted,
                    "message": "Key deleted." if deleted else "Key record not found."})


@app.route("/api/admin/license/<key>/extend", methods=["POST"])
@require_admin
def route_admin_extend(key: str):
    """
    POST /api/admin/license/<key>/extend { days }
    Re-generates a key with an extended expiry is not possible with HMAC-embedded
    dates; instead this endpoint generates a new key for the same tier and
    marks the old one as extended in its note, then returns the replacement key.
    The caller is responsible for re-issuing the new key to the customer.
    """
    data  = request.get_json(silent=True) or {}
    days  = int(data.get("days", 30))
    clean = key.strip().upper()

    if not (1 <= days <= 3650):
        return jsonify({"ok": False, "error": "days must be between 1 and 3650"}), 400

    records = keygen.load_all_keys()
    record  = next((r for r in records if r.get("key", "").upper() == clean), None)
    if not record:
        return jsonify({"ok": False, "error": "Key record not found"}), 404

    tier    = record.get("tier", "PRO")
    new_key = keygen.generate_key(expires_days=days, tier=tier)
    new_meta = keygen.validate_key(new_key)
    new_meta["note"] = f"extended from:{clean}"
    keygen.save_key_record(new_key, new_meta)

    # Mark old key's note
    for r in records:
        if r.get("key", "").upper() == clean:
            r["note"] = (r.get("note", "") + f" [replaced_by:{new_key}]").strip()
    keygen._save_json(keygen.KEYS_DB, records)

    _audit("extend_key", g.actor, clean, f"replacement={new_key} days={days}")
    return jsonify({"ok": True, "old_key": clean, "new_key": new_key,
                    "tier": tier, "days": days})


@app.route("/api/admin/license/<key>/reset", methods=["POST"])
@require_admin
def route_admin_reset_activation(key: str):
    """POST /api/admin/license/<key>/reset — clear the HWID binding for a key."""
    clean = key.strip().upper()
    users = keygen._load_json(keygen.USERS_DB)
    found = False
    for u in users:
        if u.get("key", "").upper() == clean:
            u.pop("hwid", None)
            u.pop("device_id", None)
            found = True
    if found:
        keygen._save_json(keygen.USERS_DB, users)
        _audit("reset_activation", g.actor, clean)
    return jsonify({"ok": True, "key": clean, "reset": found,
                    "message": "Activation reset." if found else "No bound user found for this key."})


@app.route("/api/admin/keys", methods=["GET"])
@require_admin
def route_admin_list_keys():
    """GET /api/admin/keys?tier=PRO&limit=50 — list issued keys."""
    tier  = (request.args.get("tier") or "ALL").upper()
    limit = min(int(request.args.get("limit", 50)), 500)

    records = keygen.load_all_keys()
    if tier != "ALL":
        records = [r for r in records if r.get("tier", "").upper() == tier]
    records = records[-limit:]

    # Attach live validation status without exposing internal HMAC
    enriched = []
    for r in records:
        meta = keygen.validate_key(r.get("key", ""))
        enriched.append({
            "key":     r.get("key"),
            "tier":    r.get("tier"),
            "created": r.get("created"),
            "expiry":  r.get("expiry"),
            "note":    r.get("note", ""),
            "banned":  keygen.is_banned(r.get("key", "")),
            "valid":   meta.get("valid", False),
            "expired": meta.get("expired", False),
        })

    return jsonify({"ok": True, "keys": enriched, "total": len(enriched)})


# ─────────────────────────────────────────────────────────────────────────────
# Admin — user management
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/users", methods=["GET"])
@require_admin
def route_admin_list_users():
    """GET /api/admin/users — list all registered users (no pw_hash / salt)."""
    users = keygen.load_all_users()
    return jsonify({"ok": True, "users": users, "total": len(users)})


@app.route("/api/admin/users/<username>", methods=["DELETE"])
@require_admin
def route_admin_delete_user(username: str):
    """DELETE /api/admin/users/<username>"""
    deleted = keygen.delete_user(username)
    if deleted:
        _audit("delete_user", g.actor, username)
    return jsonify({"ok": True, "username": username, "deleted": deleted,
                    "message": "User deleted." if deleted else "User not found."})


# ─────────────────────────────────────────────────────────────────────────────
# Admin — bulk delete keys
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/license/bulk-delete", methods=["POST"])
@require_admin
@limiter.limit("10 per minute")
def route_admin_bulk_delete():
    """
    POST /api/admin/license/bulk-delete { keys: [str, ...] }
    Delete up to 100 keys in a single request.  Each key is deleted
    independently; partial success is reported.
    """
    data = request.get_json(silent=True) or {}
    keys_raw = data.get("keys") or []
    if not isinstance(keys_raw, list):
        return jsonify({"ok": False, "error": "'keys' must be a list"}), 400

    keys_clean = list({k.strip().upper() for k in keys_raw if isinstance(k, str) and k.strip()})
    if not keys_clean:
        return jsonify({"ok": False, "error": "No valid keys provided"}), 400
    if len(keys_clean) > 100:
        return jsonify({"ok": False, "error": "Maximum 100 keys per bulk-delete request"}), 400

    deleted: list[str] = []
    not_found: list[str] = []
    for k in keys_clean:
        if keygen.delete_key_record(k):
            deleted.append(k)
        else:
            not_found.append(k)

    if deleted:
        _audit("bulk_delete_keys", g.actor, f"{len(deleted)} keys",
               f"deleted={','.join(deleted)} not_found={','.join(not_found)}")

    return jsonify({
        "ok":        True,
        "deleted":   deleted,
        "not_found": not_found,
        "summary":   f"{len(deleted)} deleted, {len(not_found)} not found",
    }), 200


# ─────────────────────────────────────────────────────────────────────────────
# Admin — aggregate statistics
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/stats", methods=["GET"])
@require_admin
def route_admin_stats():
    """GET /api/admin/stats — aggregate counts for the admin dashboard."""
    keys  = keygen.load_all_keys()
    users = keygen.load_all_users()

    tiers   = {"TRIAL": 0, "PRO": 0, "LIFETIME": 0, "ADMIN": 0}
    active  = 0
    expired = 0
    banned  = 0

    for r in keys:
        t = str(r.get("tier", "")).upper()
        if t in tiers:
            tiers[t] += 1
        if keygen.is_banned(r.get("key", "")):
            banned += 1
        else:
            meta = keygen.validate_key(r.get("key", ""))
            if meta.get("expired"):
                expired += 1
            elif meta.get("valid"):
                active += 1

    return jsonify({
        "ok":         True,
        "total_keys": len(keys),
        "active":     active,
        "expired":    expired,
        "banned":     banned,
        "tiers":      tiers,
        "users":      len(users),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Admin — Inventory management
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/inventory", methods=["GET"])
@require_admin
def route_admin_list_inventory():
    """GET /api/admin/inventory?status=&plan=&search= — list all stocked keys."""
    status = request.args.get("status", "").strip() or None
    plan   = request.args.get("plan", "").strip() or None
    search = request.args.get("search", "").strip() or None
    keys   = _inv.list_keys(status=status, plan=plan, search=search)
    return jsonify({"ok": True, "keys": keys, "total": len(keys)})


@app.route("/api/admin/inventory/stats", methods=["GET"])
@require_admin
def route_admin_inventory_stats():
    """GET /api/admin/inventory/stats — aggregate counts."""
    s = _inv.stats()
    # Also load orders to add total_orders and revenue
    orders = _load_orders()
    revenue = sum(float(o.get("price_usd", 0) or 0) for o in orders)
    s["total_orders"] = len(orders)
    s["revenue"]      = round(revenue, 2)
    return jsonify({"ok": True, **s})


@app.route("/api/admin/inventory/import", methods=["POST"])
@require_admin
def route_admin_inventory_import():
    """POST /api/admin/inventory/import { keys: [str], plan: str }"""
    data = request.get_json(silent=True) or {}
    keys = data.get("keys") or []
    plan = (data.get("plan") or "").strip().lower()
    if not isinstance(keys, list):
        return jsonify({"ok": False, "error": "'keys' must be a list"}), 400
    if not plan:
        return jsonify({"ok": False, "error": "'plan' is required (pro|lifetime)"}), 400
    result = _inv.import_keys(keys, plan)
    _audit("inventory_import", g.actor, f"{result['added']} key(s)",
           f"plan={plan} added={result['added']} skipped={result['skipped']}")
    return jsonify({"ok": True, **result}), 201


@app.route("/api/admin/inventory/bulk-delete", methods=["POST"])
@require_admin
def route_admin_inventory_bulk_delete():
    """POST /api/admin/inventory/bulk-delete { keys: [str] }"""
    data = request.get_json(silent=True) or {}
    keys = data.get("keys") or []
    if not isinstance(keys, list) or not keys:
        return jsonify({"ok": False, "error": "'keys' must be a non-empty list"}), 400
    result = _inv.delete_keys(keys)
    if result["deleted"]:
        _audit("inventory_bulk_delete", g.actor, f"{len(result['deleted'])} key(s)",
               f"deleted={','.join(result['deleted'])}")
    return jsonify({"ok": True, **result})


@app.route("/api/admin/inventory/<key>", methods=["DELETE"])
@require_admin
def route_admin_inventory_delete(key: str):
    """DELETE /api/admin/inventory/<key>"""
    clean   = key.strip().upper()
    deleted = _inv.delete_key(clean)
    if deleted:
        _audit("inventory_delete_key", g.actor, clean)
    return jsonify({"ok": True, "key": clean, "deleted": deleted,
                    "message": "Key deleted." if deleted else "Key not found."})


@app.route("/api/admin/inventory/<key>/revoke", methods=["POST"])
@require_admin
def route_admin_inventory_revoke(key: str):
    """POST /api/admin/inventory/<key>/revoke"""
    clean   = key.strip().upper()
    revoked = _inv.revoke_key(clean)
    if revoked:
        _audit("inventory_revoke_key", g.actor, clean)
    return jsonify({"ok": True, "key": clean, "revoked": revoked,
                    "message": "Key revoked." if revoked else "Key not found."})


@app.route("/api/admin/inventory/<key>", methods=["PATCH"])
@require_admin
def route_admin_inventory_update(key: str):
    """PATCH /api/admin/inventory/<key> { status?, customer?, notes?, hwid?, expiration?, plan? }"""
    clean   = key.strip().upper()
    data    = request.get_json(silent=True) or {}
    allowed = {'status', 'customer', 'notes', 'hwid', 'expiration', 'plan',
               'purchase_date', 'order_id', 'customer_email'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields to update"}), 400
    # Validate status if provided
    if 'status' in updates and updates['status'] not in _inv.VALID_STATUSES:
        return jsonify({"ok": False, "error": f"Invalid status. Choose from: {list(_inv.VALID_STATUSES)}"}), 400
    updated = _inv.update_key(clean, updates)
    if updated:
        _audit("inventory_update_key", g.actor, clean, str(updates))
    return jsonify({"ok": updated, "key": clean, "updated": updated,
                    "message": "Key updated." if updated else "Key not found."})


@app.route("/api/admin/inventory/<key>/extend", methods=["POST"])
@require_admin
def route_admin_inventory_extend(key: str):
    """POST /api/admin/inventory/<key>/extend { days }"""
    clean = key.strip().upper()
    data  = request.get_json(silent=True) or {}
    days  = int(data.get("days", 30))
    if not (1 <= days <= 3650):
        return jsonify({"ok": False, "error": "days must be between 1 and 3650"}), 400
    record = _inv.get_key(clean)
    if not record:
        return jsonify({"ok": False, "error": "Key not found"}), 404
    # Extend expiration
    base = record.get("expiration") or record.get("purchase_date") or ""
    try:
        base_dt = datetime.datetime.fromisoformat(base) if base else datetime.datetime.now(datetime.timezone.utc)
    except ValueError:
        base_dt = datetime.datetime.now(datetime.timezone.utc)
    new_exp = (base_dt + datetime.timedelta(days=days)).isoformat()
    _inv.update_key(clean, {"expiration": new_exp})
    _audit("inventory_extend_key", g.actor, clean, f"days={days} new_expiration={new_exp}")
    return jsonify({"ok": True, "key": clean, "expiration": new_exp, "days_added": days})


# ─────────────────────────────────────────────────────────────────────────────
# Admin — Orders (read-only view)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/orders", methods=["GET"])
@require_admin
def route_admin_list_orders():
    """GET /api/admin/orders — list all orders."""
    orders = _load_orders()
    return jsonify({"ok": True, "orders": orders, "total": len(orders)})


@app.route("/api/admin/orders/<order_id>", methods=["GET"])
@require_admin
def route_admin_get_order(order_id: str):
    """GET /api/admin/orders/<order_id>"""
    orders = _load_orders()
    record = next((o for o in orders if o.get("order_id", "") == order_id.strip()), None)
    if not record:
        return jsonify({"ok": False, "error": "Order not found"}), 404
    return jsonify({"ok": True, "order": record})


# ─────────────────────────────────────────────────────────────────────────────
# Admin — Customers (derived from orders + users data)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/customers", methods=["GET"])
@require_admin
def route_admin_list_customers():
    """GET /api/admin/customers?search= — list unique customers from orders."""
    search = (request.args.get("search") or "").strip().lower()
    orders = _load_orders()

    # Build customer map indexed by email
    customers: dict[str, dict] = {}
    for o in orders:
        email = (o.get("email") or "").strip().lower()
        if not email:
            email = (o.get("discord") or "").strip().lower()
        if not email:
            continue
        if email not in customers:
            customers[email] = {
                "email":          o.get("email", ""),
                "discord":        o.get("discord", ""),
                "total_orders":   0,
                "total_spent":    0.0,
                "active_licenses": 0,
                "first_purchase": o.get("created_at", ""),
                "last_purchase":  o.get("created_at", ""),
                "orders":         [],
            }
        c = customers[email]
        c["total_orders"]   += 1
        c["total_spent"]    += float(o.get("price_usd") or 0)
        if o.get("delivery_status") == "delivered" and o.get("license_key"):
            c["active_licenses"] += 1
        if o.get("created_at", "") > c["last_purchase"]:
            c["last_purchase"] = o.get("created_at", "")
        if o.get("created_at", "") < c["first_purchase"] or not c["first_purchase"]:
            c["first_purchase"] = o.get("created_at", "")
        c["orders"].append({
            "order_id":       o.get("order_id", ""),
            "plan":           o.get("plan", ""),
            "price_usd":      o.get("price_usd"),
            "license_key":    o.get("license_key"),
            "delivery_status":o.get("delivery_status", ""),
            "payment_status": o.get("payment_status", ""),
            "created_at":     o.get("created_at", ""),
        })

    result = list(customers.values())
    if search:
        result = [c for c in result if (
            search in (c.get("email") or "").lower() or
            search in (c.get("discord") or "").lower()
        )]
    result.sort(key=lambda c: c.get("last_purchase", ""), reverse=True)
    return jsonify({"ok": True, "customers": result, "total": len(result)})


@app.route("/api/admin/customers/<email>/revoke", methods=["POST"])
@require_admin
def route_admin_customer_revoke(email: str):
    """POST /api/admin/customers/<email>/revoke — revoke all customer keys."""
    email   = email.strip().lower()
    orders  = _load_orders()
    revoked = []
    for o in orders:
        if (o.get("email") or "").lower() == email and o.get("license_key"):
            _inv.revoke_key(o["license_key"])
            revoked.append(o["license_key"])
    _audit("customer_revoke_all", g.actor, email, f"revoked={','.join(revoked)}")
    return jsonify({"ok": True, "email": email, "revoked": revoked})


@app.route("/api/admin/customers/<email>/reset-hwid", methods=["POST"])
@require_admin
def route_admin_customer_reset_hwid(email: str):
    """POST /api/admin/customers/<email>/reset-hwid — clear HWID for all customer keys."""
    email = email.strip().lower()
    orders = _load_orders()
    reset  = []
    for o in orders:
        if (o.get("email") or "").lower() == email and o.get("license_key"):
            _inv.update_key(o["license_key"].upper(), {"hwid": ""})
            reset.append(o["license_key"])
    _audit("customer_reset_hwid", g.actor, email, f"keys={','.join(reset)}")
    return jsonify({"ok": True, "email": email, "reset": reset})


# ─────────────────────────────────────────────────────────────────────────────
# Admin — Downloads management
# ─────────────────────────────────────────────────────────────────────────────

DOWNLOADS_DB = _HERE / "downloads.json"
_downloads_lock = threading.Lock()


def _load_downloads() -> dict:
    if DOWNLOADS_DB.exists():
        try:
            return json.loads(DOWNLOADS_DB.read_text("utf-8"))
        except Exception:
            pass
    return {
        "current_version": "",
        "release_date":    "",
        "changelog":       "",
        "filename":        "",
        "download_url":    "",
        "download_count":  0,
        "history":         [],
    }


def _save_downloads(data: dict) -> None:
    with _downloads_lock:
        tmp = DOWNLOADS_DB.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), "utf-8")
        tmp.replace(DOWNLOADS_DB)


@app.route("/api/admin/downloads", methods=["GET"])
@require_admin
def route_admin_get_downloads():
    """GET /api/admin/downloads — get current download info."""
    data = _load_downloads()
    return jsonify({"ok": True, **data})


@app.route("/api/admin/downloads", methods=["POST"])
@require_admin
def route_admin_update_downloads():
    """POST /api/admin/downloads — update download metadata."""
    req_data = request.get_json(silent=True) or {}
    current  = _load_downloads()

    # Archive current version to history if version changes
    new_version = req_data.get("current_version", "").strip()
    if new_version and new_version != current.get("current_version"):
        history = current.get("history", [])
        if current.get("current_version"):
            history.append({
                "version":       current["current_version"],
                "release_date":  current.get("release_date", ""),
                "changelog":     current.get("changelog", ""),
                "filename":      current.get("filename", ""),
                "archived_at":   datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
        current["history"] = history[-20:]  # keep last 20 versions

    allowed = {"current_version", "release_date", "changelog", "filename", "download_url"}
    for k, v in req_data.items():
        if k in allowed:
            current[k] = v

    _save_downloads(current)
    _audit("downloads_update", g.actor, new_version or current.get("current_version", ""),
           f"version={current.get('current_version')} url={current.get('download_url','')[:60]}")
    return jsonify({"ok": True, **current})


@app.route("/api/admin/downloads/increment", methods=["POST"])
@require_admin
def route_admin_downloads_increment():
    """POST /api/admin/downloads/increment — increment download counter."""
    current = _load_downloads()
    current["download_count"] = int(current.get("download_count", 0)) + 1
    _save_downloads(current)
    return jsonify({"ok": True, "download_count": current["download_count"]})


@app.route("/api/admin/downloads/rollback", methods=["POST"])
@require_admin
def route_admin_downloads_rollback():
    """POST /api/admin/downloads/rollback { version } — roll back to a previous version."""
    data    = request.get_json(silent=True) or {}
    version = (data.get("version") or "").strip()
    current = _load_downloads()
    history = current.get("history", [])
    target  = next((h for h in history if h.get("version") == version), None)
    if not target:
        return jsonify({"ok": False, "error": f"Version '{version}' not found in history"}), 404

    # Swap current → history, target → current
    if current.get("current_version"):
        history = [h for h in history if h.get("version") != version]
        history.append({
            "version":      current["current_version"],
            "release_date": current.get("release_date", ""),
            "changelog":    current.get("changelog", ""),
            "filename":     current.get("filename", ""),
            "archived_at":  datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })

    current["current_version"] = target["version"]
    current["release_date"]    = target.get("release_date", "")
    current["changelog"]       = target.get("changelog", "")
    current["filename"]        = target.get("filename", "")
    current["history"]         = history[-20:]
    _save_downloads(current)
    _audit("downloads_rollback", g.actor, version, f"rolled back to {version}")
    return jsonify({"ok": True, **current})


# ─────────────────────────────────────────────────────────────────────────────
# Admin — Settings
# ─────────────────────────────────────────────────────────────────────────────

SETTINGS_DB = _HERE / "admin_settings.json"
_settings_lock = threading.Lock()


def _load_settings() -> dict:
    defaults = {
        "site_name":           "Ghost",
        "logo_url":            "",
        "discord_invite":      "",
        "download_url":        "",
        "maintenance_mode":    False,
        "announcement_banner": "",
        "paypal_client_id":    "",
        "paypal_environment":  "sandbox",
        "admin_password_hash": "",
    }
    if SETTINGS_DB.exists():
        try:
            data = json.loads(SETTINGS_DB.read_text("utf-8"))
            defaults.update(data)
            return defaults
        except Exception:
            pass
    return defaults


def _save_settings(data: dict) -> None:
    with _settings_lock:
        tmp = SETTINGS_DB.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), "utf-8")
        tmp.replace(SETTINGS_DB)


@app.route("/api/admin/settings", methods=["GET"])
@require_admin
def route_admin_get_settings():
    """GET /api/admin/settings — return all settings (no passwords)."""
    s = _load_settings()
    s.pop("admin_password_hash", None)
    return jsonify({"ok": True, "settings": s})


@app.route("/api/admin/settings", methods=["POST"])
@require_admin
def route_admin_update_settings():
    """POST /api/admin/settings — update site settings."""
    data    = request.get_json(silent=True) or {}
    current = _load_settings()
    allowed = {
        "site_name", "logo_url", "discord_invite", "download_url",
        "maintenance_mode", "announcement_banner",
        "paypal_client_id", "paypal_environment",
    }
    for k, v in data.items():
        if k in allowed:
            current[k] = v
    _save_settings(current)
    _audit("settings_update", g.actor, "settings", str({k: v for k, v in data.items() if k in allowed}))
    current.pop("admin_password_hash", None)
    return jsonify({"ok": True, "settings": current})


@app.route("/api/admin/settings/password", methods=["POST"])
@require_admin
def route_admin_change_password():
    """POST /api/admin/settings/password { new_password_hash } — update admin panel password."""
    import hashlib as _hl
    data    = request.get_json(silent=True) or {}
    new_pw  = (data.get("new_password") or "").strip()
    new_hash= (data.get("new_password_hash") or "").strip().lower()

    if new_pw and not new_hash:
        # Accept plain text (hashed server-side for convenience)
        new_hash = _hl.sha256(new_pw.encode()).hexdigest()

    if not new_hash or len(new_hash) != 64:
        return jsonify({"ok": False, "error": "Provide new_password or new_password_hash (64-char SHA-256 hex)"}), 400

    current = _load_settings()
    current["admin_password_hash"] = new_hash
    _save_settings(current)
    _audit("password_changed", g.actor, "admin_panel_password", "password hash updated")
    return jsonify({"ok": True, "message": "Password updated. Re-deploy with new ADMIN_PANEL_PASSWORD_HASH env var or use the stored hash."})


# ─────────────────────────────────────────────────────────────────────────────
# Admin — Activity Log
# ─────────────────────────────────────────────────────────────────────────────

ACTIVITY_LOG = _HERE / "activity_log.json"
_activity_lock = threading.Lock()


def _log_activity(action: str, actor: str, target: str, details: str = "", level: str = "info") -> None:
    """Append a human-readable activity record."""
    try:
        ip = request.remote_addr or ""
    except RuntimeError:
        ip = ""

    def _write(_ip: str = ip):
        with _activity_lock:
            try:
                records: list[dict] = []
                if ACTIVITY_LOG.exists():
                    try:
                        records = json.loads(ACTIVITY_LOG.read_text("utf-8"))
                    except Exception:
                        pass
                records.append({
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "action":    action,
                    "actor":     actor,
                    "target":    target,
                    "details":   details,
                    "level":     level,
                    "ip":        _ip,
                })
                tmp = ACTIVITY_LOG.with_suffix(".tmp")
                tmp.write_text(json.dumps(records[-5_000:], indent=2, default=str), "utf-8")
                tmp.replace(ACTIVITY_LOG)
            except Exception as exc:
                log.error("activity_log write error: %s", exc)

    threading.Thread(target=_write, daemon=True).start()


@app.route("/api/admin/activity", methods=["GET"])
@require_admin
def route_admin_activity_log():
    """GET /api/admin/activity?limit=100&level= — return activity log."""
    limit = min(int(request.args.get("limit", 200)), 1000)
    level = (request.args.get("level") or "").strip().lower()
    try:
        records: list[dict] = []
        if ACTIVITY_LOG.exists():
            records = json.loads(ACTIVITY_LOG.read_text("utf-8"))
    except Exception:
        records = []
    if level:
        records = [r for r in records if r.get("level", "info") == level]
    records = list(reversed(records[-limit:]))
    return jsonify({"ok": True, "log": records, "total": len(records)})


@app.route("/api/admin/activity", methods=["DELETE"])
@require_admin
def route_admin_clear_activity():
    """DELETE /api/admin/activity — clear the activity log."""
    with _activity_lock:
        if ACTIVITY_LOG.exists():
            ACTIVITY_LOG.write_text("[]", "utf-8")
    _audit("activity_log_cleared", g.actor, "activity_log")
    return jsonify({"ok": True, "message": "Activity log cleared."})


# ─────────────────────────────────────────────────────────────────────────────
# Admin — Enhanced Dashboard Stats
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/dashboard", methods=["GET"])
@require_admin
def route_admin_dashboard():
    """GET /api/admin/dashboard — full dashboard stats including revenue graphs."""
    orders  = _load_orders()
    inv_s   = _inv.stats()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today   = now_utc.date()
    month   = today.replace(day=1)

    revenue_today  = 0.0
    revenue_month  = 0.0
    revenue_total  = 0.0
    orders_today   = 0
    new_customers_today = 0
    failed_payments = 0
    pending_orders  = 0

    # For graphs: last 30 days
    days_30: dict[str, dict] = {}
    for i in range(29, -1, -1):
        d = (now_utc - datetime.timedelta(days=i)).date().isoformat()
        days_30[d] = {"revenue": 0.0, "orders": 0, "customers": 0}

    seen_emails: set[str] = set()

    for o in orders:
        price = float(o.get("price_usd") or 0)
        created = o.get("created_at", "")
        pstatus = (o.get("payment_status") or "").lower()

        if pstatus == "payment_failed":
            failed_payments += 1
        if pstatus in ("pending", "delivery_pending"):
            pending_orders += 1

        try:
            o_date = datetime.datetime.fromisoformat(created).date()
        except (ValueError, TypeError):
            continue

        if pstatus not in ("verified", "completed"):
            continue

        revenue_total += price
        if o_date == today:
            revenue_today += price
            orders_today  += 1
        if o_date >= month:
            revenue_month += price

        date_str = o_date.isoformat()
        if date_str in days_30:
            days_30[date_str]["revenue"] += price
            days_30[date_str]["orders"]  += 1
            email = (o.get("email") or "").lower()
            if email and email not in seen_emails:
                seen_emails.add(email)
                days_30[date_str]["customers"] += 1

    graph_labels   = list(days_30.keys())
    graph_revenue  = [round(days_30[d]["revenue"], 2) for d in graph_labels]
    graph_orders   = [days_30[d]["orders"]   for d in graph_labels]
    graph_customers= [days_30[d]["customers"] for d in graph_labels]

    return jsonify({
        "ok": True,
        "cards": {
            "revenue_today":    round(revenue_today, 2),
            "revenue_month":    round(revenue_month, 2),
            "revenue_total":    round(revenue_total, 2),
            "total_orders":     len(orders),
            "active_licenses":  inv_s.get("sold", 0) + inv_s.get("activated", 0),
            "keys_remaining":   inv_s.get("available", 0),
            "pending_orders":   pending_orders,
            "failed_payments":  failed_payments,
        },
        "inventory": inv_s,
        "graphs": {
            "labels":    graph_labels,
            "revenue":   graph_revenue,
            "orders":    graph_orders,
            "customers": graph_customers,
        },
    })


# ─────────────────────────────────────────────────────────────────────────────
# Admin — validate license (used by desktop app / bot proxy)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/license/validate", methods=["POST"])
@limiter.limit("30 per minute")
def route_validate_license():
    """
    POST /api/license/validate { key }
    Public-ish endpoint — no session required (the desktop app calls this
    offline-first; this is the online check path).  Returns limited info
    (no record notes, no user binding) to avoid leaking data to unauthenticated callers.
    """
    data = request.get_json(silent=True) or {}
    key  = (data.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "key is required"}), 400

    meta = keygen.validate_key(key)
    return jsonify({
        "ok":      meta.get("valid", False),
        "valid":   meta.get("valid", False),
        "tier":    meta.get("tier", ""),
        "expired": meta.get("expired", False),
        "banned":  keygen.is_banned(key),
        "days_remaining": meta.get("days_remaining", -1),
        "error":   meta.get("error", "") if not meta.get("valid") else "",
    })


# ─────────────────────────────────────────────────────────────────────────────
# Health & status endpoints
# ─────────────────────────────────────────────────────────────────────────────

_START_TIME = time.time()


@app.route("/health", methods=["GET"])
def route_health():
    """Shallow liveness probe — returns 200 as long as the process is up."""
    return jsonify({"ok": True, "service": "ghost-api", "status": "healthy"})


@app.route("/status", methods=["GET"])
def route_status():
    """Deep readiness probe — includes uptime and data-file accessibility."""
    uptime_secs = int(time.time() - _START_TIME)
    try:
        key_count  = len(keygen.load_all_keys())
        user_count = len(keygen.load_all_users())
        data_ok    = True
    except Exception:
        key_count  = -1
        user_count = -1
        data_ok    = False

    return jsonify({
        "ok":         data_ok,
        "service":    "ghost-api",
        "status":     "ready" if data_ok else "degraded",
        "uptime_secs": uptime_secs,
        "keys":       key_count,
        "users":      user_count,
        "version":    os.environ.get("GHOST_LATEST_VERSION", "unknown"),
    }), 200 if data_ok else 503


# ─────────────────────────────────────────────────────────────────────────────
# Error handlers
# ─────────────────────────────────────────────────────────────────────────────

@app.errorhandler(404)
def _404(_e):
    return jsonify({"ok": False, "error": "Not found"}), 404


@app.errorhandler(405)
def _405(_e):
    return jsonify({"ok": False, "error": "Method not allowed"}), 405


@app.errorhandler(429)
def _429(_e):
    return jsonify({"ok": False, "error": "Too many requests — please slow down"}), 429


@app.errorhandler(500)
def _500(_e):
    log.exception("Unhandled exception")
    return jsonify({"ok": False, "error": "Internal server error"}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("GHOST_API_PORT", 5056))
    log.info("Ghost shared API starting on port %d", port)
    if not _ADMIN_API_KEY:
        log.warning("GHOST_ADMIN_API_KEY not set — admin routes disabled")
    app.run(host="0.0.0.0", port=port, debug=False)
