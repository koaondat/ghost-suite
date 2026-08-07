"""
keygen.py — Offline HMAC-SHA256 License Key Generator & Validator
==================================================================
This tool generates HMAC-signed offline keys in the format:

    GHOST-XXXXX-XXXXX-XXXXX-XXXXX   (5-char Base32 segments)

These are structurally different from web-admin-generated keys:

    GHOST-XXXX-XXXX-XXXX-XXXX       (4-char alphanumeric segments)

IMPORTANT: Web-admin keys (4-char segments) are validated exclusively
by looking them up in Upstash Redis (ghost:inventory).  They will NOT
pass the HMAC signature check in this file — that is by design.
Do NOT run web-admin-generated keys through this validator.

Usage:
    python keygen.py generate [--expires-days 90] [--tier PRO]
    python keygen.py validate <KEY>
    python keygen.py list-keys
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import struct
import sys
import threading
import time
from pathlib import Path

# ── Secret ─────────────────────────────────────────────────────────────────
# Read from environment variable.  Fall back to hardcoded seed only in dev.
# In production GHOST_HMAC_SECRET must be set to a 32+ byte hex string.
_HMAC_SECRET: bytes = os.environ.get(
    "GHOST_HMAC_SECRET", "QA-UTIL-SECRET-SEED-v1-d0n0t-sh4re"
).encode("utf-8")

# ── Hardcoded ADMIN master key (never expires, tier=ADMIN) ─────────────────
# This key is yours alone.  It bypasses the issued_keys DB entirely.
# Generated once below — do NOT regenerate it (the value is fixed).
ADMIN_MASTER_KEY        = "GHOST-AAFUS-6UAAA-AAAAE-5IHP2"
ADMIN_MASTER_KEY_LEGACY = "QA-AAFUS-6UAAA-AAAAE-5IHP2"   # kept for backward-compat

# ── Data files next to the exe / script ────────────────────────────────────
def _data_dir() -> Path:
    """Return the directory where runtime data files live.
    When frozen by PyInstaller this is next to the .exe; otherwise next
    to this source file."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

def _db_path(name: str) -> Path:
    return _data_dir() / name

KEYS_DB      = _db_path("issued_keys.json")
BANNED_DB    = _db_path("banned_keys.json")
BLACKLIST_DB = _db_path("blacklist.json")
WHITELIST_DB = _db_path("whitelist.json")
USERS_DB        = _db_path("users.json")

# Lazy-import activity_log to avoid a hard circular dependency.
# The import succeeds only when activity_log.py is next to keygen.py.
def _al():
    try:
        import activity_log as _mod
        return _mod
    except ImportError:
        return None

# ── Tier constants ──────────────────────────────────────────────────────────
TIERS = {"TRIAL": 0, "PRO": 1, "ADMIN": 2}

# ── Internal helpers ────────────────────────────────────────────────────────

def _encode_b32(data: bytes) -> str:
    return base64.b32encode(data).decode().rstrip("=")


def _decode_b32(s: str) -> bytes:
    pad = (8 - len(s) % 8) % 8
    return base64.b32decode(s + "=" * pad)


def _sign(payload: bytes) -> bytes:
    """Return first 4 bytes of HMAC-SHA256.  Callers needing 3 bytes slice [:3]."""
    return hmac.new(_HMAC_SECRET, payload, hashlib.sha256).digest()[:4]


def _today_ordinal() -> int:
    return datetime.date.today().toordinal()


def _load_json(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_json(path: Path, data: list) -> None:
    """Atomic write: write to a .tmp file then rename to avoid partial reads."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


# ── Public key API ──────────────────────────────────────────────────────────

def generate_key(expires_days: int = 365, tier: str = "PRO") -> str:
    tier_upper = tier.upper()
    if tier_upper not in TIERS:
        raise ValueError(f"Unknown tier '{tier}'. Choose from: {list(TIERS)}")

    created   = _today_ordinal()
    expiry    = (created + expires_days) if expires_days > 0 else 0
    tier_byte = TIERS[tier_upper]

    payload = struct.pack(">IIB", created, expiry, tier_byte)
    sig     = _sign(payload)[:3]
    blob    = payload + sig   # 12 bytes exactly
    parts   = [_encode_b32(blob[i:i+3])[:5] for i in range(0, 12, 3)]
    return f"GHOST-{parts[0]}-{parts[1]}-{parts[2]}-{parts[3]}"


def validate_key(key: str) -> dict:
    """
    Validate a key string and return its metadata.

    Returns
    -------
    dict: valid, tier, created, expiry, days_remaining, expired, error
    """
    result: dict = {
        "valid": False, "tier": "", "created": None,
        "expiry": None, "days_remaining": -1,
        "expired": False, "error": "", "key": key.strip().upper(),
    }

    clean = key.strip().upper().replace(" ", "")

    # ── Master admin key shortcut ───────────────────────────────────────
    if clean in (ADMIN_MASTER_KEY.strip().upper(),
                 ADMIN_MASTER_KEY_LEGACY.strip().upper()):
        result.update({
            "valid": True, "tier": "ADMIN",
            "created": datetime.date(2024, 1, 1),
            "expiry": None, "days_remaining": -1,
            "expired": False, "error": "",
        })
        return result

    try:
        # Accept both new GHOST- prefix and legacy QA- prefix
        if clean.startswith("GHOST-"):
            payload_str = clean[6:]
        elif clean.startswith("QA-"):
            payload_str = clean[3:]
        else:
            result["error"] = "Key must start with 'GHOST-'"
            return result

        parts = payload_str.split("-")
        # This validator is for HMAC-signed offline keys (5-char Base32 segments).
        # Web-admin-generated keys use 4-char segments and are validated via Redis only.
        if len(parts) != 4 or any(len(p) != 5 for p in parts):
            result["error"] = (
                "Key format must be GHOST-XXXXX-XXXXX-XXXXX-XXXXX "
                "(offline HMAC key). Web-admin keys (GHOST-XXXX-XXXX-XXXX-XXXX) "
                "are validated via Redis, not this function."
            )
            return result

        try:
            raw_chunks = [_decode_b32(p) for p in parts]
        except Exception:
            result["error"] = "Key contains invalid Base32 characters"
            return result

        raw = b"".join(raw_chunks)
        if len(raw) < 12:
            result["error"] = "Key payload too short"
            return result

        payload      = raw[:9]
        stored_sig   = raw[9:12]
        expected_sig = _sign(payload)[:3]

        if not hmac.compare_digest(stored_sig, expected_sig):
            result["error"] = "Invalid key — signature mismatch"
            return result

        created_ord, expiry_ord, tier_byte = struct.unpack(">IIB", payload)
        created_date = datetime.date.fromordinal(created_ord)
        expiry_date  = datetime.date.fromordinal(expiry_ord) if expiry_ord else None
        tier_name    = next((k for k, v in TIERS.items() if v == tier_byte), "UNKNOWN")

        today    = datetime.date.today()
        expired  = bool(expiry_date and today > expiry_date)
        days_left = (expiry_date - today).days if expiry_date else -1

        # ── Check banned list ───────────────────────────────────────────
        if is_banned(clean):
            result["error"] = "Key is banned"
            return result

        result.update({
            "valid":          not expired,
            "tier":           tier_name,
            "created":        created_date,
            "expiry":         expiry_date,
            "days_remaining": days_left,
            "expired":        expired,
            "error":          "Key has expired" if expired else "",
        })

    except Exception as exc:
        result["error"] = f"Malformed key: {exc}"

    return result


# ── Issued-keys DB ──────────────────────────────────────────────────────────

def save_key_record(key: str, meta: dict) -> None:
    records = _load_json(KEYS_DB)
    records.append({
        "key":     key,
        "tier":    meta.get("tier"),
        "created": str(meta.get("created")),
        "expiry":  str(meta.get("expiry")),
        "note":    meta.get("note", ""),
    })
    _save_json(KEYS_DB, records)


def load_all_keys() -> list[dict]:
    return _load_json(KEYS_DB)


def delete_key_record(key: str) -> bool:
    """Remove a key from the issued_keys DB. Returns True if found & removed."""
    records = _load_json(KEYS_DB)
    clean   = key.strip().upper()
    new     = [r for r in records if r.get("key", "").upper() != clean]
    if len(new) == len(records):
        return False
    _save_json(KEYS_DB, new)
    return True


# ── Ban list ────────────────────────────────────────────────────────────────

def ban_key(key: str, reason: str = "") -> None:
    records = _load_json(BANNED_DB)
    clean   = key.strip().upper()
    if not any(r.get("key") == clean for r in records):
        records.append({"key": clean, "reason": reason,
                         "date": str(datetime.date.today())})
        _save_json(BANNED_DB, records)


def unban_key(key: str) -> bool:
    records = _load_json(BANNED_DB)
    clean   = key.strip().upper()
    new     = [r for r in records if r.get("key") != clean]
    if len(new) == len(records):
        return False
    _save_json(BANNED_DB, new)
    return True


def is_banned(key: str) -> bool:
    clean = key.strip().upper()
    return any(r.get("key") == clean for r in _load_json(BANNED_DB))


def load_banned() -> list[dict]:
    return _load_json(BANNED_DB)


# ── Blacklist (machine identifiers / IPs / notes) ──────────────────────────

def blacklist_add(entry: str, note: str = "") -> None:
    records = _load_json(BLACKLIST_DB)
    clean   = entry.strip()
    if not any(r.get("entry") == clean for r in records):
        records.append({"entry": clean, "note": note,
                         "date": str(datetime.date.today())})
        _save_json(BLACKLIST_DB, records)


def blacklist_remove(entry: str) -> bool:
    records = _load_json(BLACKLIST_DB)
    clean   = entry.strip()
    new     = [r for r in records if r.get("entry") != clean]
    if len(new) == len(records):
        return False
    _save_json(BLACKLIST_DB, new)
    return True


def load_blacklist() -> list[dict]:
    return _load_json(BLACKLIST_DB)


# ── Whitelist (trusted machine identifiers / IPs) ──────────────────────────

def whitelist_add(entry: str, note: str = "") -> None:
    records = _load_json(WHITELIST_DB)
    clean   = entry.strip()
    if not any(r.get("entry") == clean for r in records):
        records.append({"entry": clean, "note": note,
                         "date": str(datetime.date.today())})
        _save_json(WHITELIST_DB, records)


def whitelist_remove(entry: str) -> bool:
    records = _load_json(WHITELIST_DB)
    clean   = entry.strip()
    new     = [r for r in records if r.get("entry") != clean]
    if len(new) == len(records):
        return False
    _save_json(WHITELIST_DB, new)
    return True


def load_whitelist() -> list[dict]:
    return _load_json(WHITELIST_DB)


# ── User account system ─────────────────────────────────────────────────────

def _hash_password(password: str, salt: str) -> str:
    """SHA-256 of salt+password, hex-encoded."""
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def register_user(username: str, password: str, license_key: str) -> dict:
    """
    Register a new user account.

    Returns
    -------
    dict: ok(bool), error(str), tier(str)
    """
    username = username.strip()
    if not username or len(username) < 2:
        _log_activity("register_fail", username, license_key,
                      "Registration failed", "Username must be at least 2 characters.")
        return {"ok": False, "error": "Username must be at least 2 characters."}
    if not password or len(password) < 8:
        _log_activity("register_fail", username, license_key,
                      "Registration failed", "Password must be at least 8 characters.")
        return {"ok": False, "error": "Password must be at least 8 characters."}

    # Validate the license key first
    meta = validate_key(license_key)
    if not meta["valid"]:
        err = f"License key invalid: {meta['error']}"
        _log_activity("register_fail", username, license_key, "Registration failed", err)
        return {"ok": False, "error": err}

    users = _load_json(USERS_DB)
    if any(u["username"].lower() == username.lower() for u in users):
        _log_activity("register_fail", username, license_key,
                      "Registration failed", "Username already taken.")
        return {"ok": False, "error": "Username already taken."}

    # Each key can only be registered to one account
    clean_key = license_key.strip().upper()
    if clean_key not in (ADMIN_MASTER_KEY.strip().upper(),
                         ADMIN_MASTER_KEY_LEGACY.strip().upper()):
        if any(u.get("key", "").upper() == clean_key for u in users):
            _log_activity("register_fail", username, license_key,
                          "Registration failed", "License key already bound to another account.")
            return {"ok": False, "error": "License key already bound to another account."}

    salt   = os.urandom(16).hex()
    pw_hash = _hash_password(password, salt)
    users.append({
        "username": username,
        "salt":     salt,
        "pw_hash":  pw_hash,
        "key":      clean_key,
        "tier":     meta["tier"],
        "created":  str(datetime.date.today()),
    })
    _save_json(USERS_DB, users)
    _log_activity("register_success", username, license_key,
                  f"Registered successfully (tier={meta['tier']})")
    return {"ok": True, "error": "", "tier": meta["tier"]}


def login_user(username: str, password: str, license_key: str) -> dict:
    """
    Authenticate a user.

    Returns
    -------
    dict: ok(bool), error(str), tier(str), username(str)
    """
    username  = username.strip()
    clean_key = license_key.strip().upper()

    # Re-validate the key (catches bans / expiry at login time)
    meta = validate_key(license_key)
    if not meta["valid"]:
        err = f"License key invalid: {meta['error']}"
        _log_activity("login_fail", username, license_key, "Login failed", err)
        return {"ok": False, "error": err, "tier": "", "username": ""}

    users = _load_json(USERS_DB)
    user  = next((u for u in users if u["username"].lower() == username.lower()), None)

    if user is None:
        _log_activity("login_fail", username, license_key,
                      "Login failed", "Account not found.")
        return {"ok": False, "error": "Account not found.", "tier": "", "username": ""}

    # Verify password
    expected = _hash_password(password, user["salt"])
    if not hmac.compare_digest(expected, user["pw_hash"]):
        _log_activity("login_fail", username, license_key,
                      "Login failed", "Incorrect password.")
        return {"ok": False, "error": "Incorrect password.", "tier": "", "username": ""}

    # Verify the key matches what was registered
    if user.get("key", "").upper() != clean_key:
        _log_activity("login_fail", username, license_key,
                      "Login failed", "License key does not match this account.")
        return {"ok": False, "error": "License key does not match this account.",
                "tier": "", "username": ""}

    _log_activity("login_success", user["username"], license_key,
                  f"Login successful (tier={meta['tier']})")
    return {"ok": True, "error": "", "tier": meta["tier"], "username": user["username"]}


def _log_activity(event_type: str, username: str, license_key: str,
                  result_msg: str, error_msg: str = "") -> None:
    """Fire-and-forget activity log write on a background thread."""
    al = _al()
    if al is None:
        return
    t = threading.Thread(
        target=al.record,
        args=(event_type, username, license_key, result_msg, error_msg),
        daemon=True,
    )
    t.start()


# ── Admin key login (passwordless, rate-limited) ────────────────────────────

# In-memory rate-limit table: maps opaque attempt-token → (fail_count, reset_time)
# The token is SHA-256(candidate_key_normalised)[:16] so the plain-text key is
# never stored in the table.
_admin_key_rate: dict[str, tuple[int, float]] = {}
_admin_key_rate_lock = threading.Lock()

_ADMIN_KEY_MAX_ATTEMPTS = 5   # consecutive failures before lockout
_ADMIN_KEY_WINDOW_SECS  = 300 # 5-minute sliding window


def _admin_key_token(candidate: str) -> str:
    """Return an opaque 16-char hex token for rate-limiting (not the key itself)."""
    return hashlib.sha256(candidate.strip().upper().encode()).hexdigest()[:16]


def _check_admin_key_rate(token: str) -> bool:
    """Return True if the attempt is allowed, False if rate-limited."""
    now = time.monotonic()
    with _admin_key_rate_lock:
        count, reset_at = _admin_key_rate.get(token, (0, now + _ADMIN_KEY_WINDOW_SECS))
        if now > reset_at:
            # Window expired — reset
            _admin_key_rate[token] = (0, now + _ADMIN_KEY_WINDOW_SECS)
            return True
        return count < _ADMIN_KEY_MAX_ATTEMPTS


def _record_admin_key_fail(token: str) -> None:
    now = time.monotonic()
    with _admin_key_rate_lock:
        count, reset_at = _admin_key_rate.get(token, (0, now + _ADMIN_KEY_WINDOW_SECS))
        if now > reset_at:
            count, reset_at = 0, now + _ADMIN_KEY_WINDOW_SECS
        _admin_key_rate[token] = (count + 1, reset_at)


def _reset_admin_key_rate(token: str) -> None:
    with _admin_key_rate_lock:
        _admin_key_rate.pop(token, None)


def login_admin_key(candidate_key: str) -> dict:
    """
    Authenticate using an ADMIN license key pasted into the username field.

    Rules
    -----
    - The key must be cryptographically valid (HMAC passes).
    - The key's tier must be ADMIN — Trial and Pro keys are always rejected.
    - The key must not be expired.
    - The key must not be banned.
    - Failed attempts are rate-limited (5 per 5-minute window per key).

    Returns
    -------
    dict: ok(bool), error(str), tier(str), username(str)
          username is set to "__admin_key__" to flag the session type.
    """
    clean = candidate_key.strip().upper()
    token = _admin_key_token(clean)

    # Rate-limit check (keyed on opaque token, not the plain key)
    if not _check_admin_key_rate(token):
        err = "Too many failed attempts. Please wait before trying again."
        _log_activity("admin_key_login_fail", "", candidate_key,
                      "Admin key login failed", err)
        return {"ok": False, "error": err, "tier": "", "username": ""}

    meta = validate_key(candidate_key)

    # Must be structurally valid
    if not meta.get("valid"):
        _record_admin_key_fail(token)
        err = f"Key invalid: {meta.get('error', 'unknown error')}"
        _log_activity("admin_key_login_fail", "", candidate_key,
                      "Admin key login failed", err)
        return {"ok": False, "error": err, "tier": "", "username": ""}

    # Tier must be exactly ADMIN — reject Trial and Pro silently with the same
    # generic message to avoid leaking which tier the key belongs to.
    if meta.get("tier", "").upper() != "ADMIN":
        _record_admin_key_fail(token)
        err = "Key is not valid for this login method."
        _log_activity("admin_key_login_fail", "", candidate_key,
                      "Admin key login failed", err)
        return {"ok": False, "error": err, "tier": "", "username": ""}

    # All checks passed — reset rate-limit counter and log success
    _reset_admin_key_rate(token)
    _log_activity("admin_key_login_success", "__admin_key__", candidate_key,
                  "Admin key login successful (tier=ADMIN)")
    return {"ok": True, "error": "", "tier": "ADMIN", "username": "__admin_key__"}


def load_all_users() -> list[dict]:
    """Return all user records (passwords excluded)."""
    return [
        {k: v for k, v in u.items() if k not in ("pw_hash", "salt")}
        for u in _load_json(USERS_DB)
    ]


def delete_user(username: str) -> bool:
    users = _load_json(USERS_DB)
    new   = [u for u in users if u["username"].lower() != username.lower()]
    if len(new) == len(users):
        return False
    _save_json(USERS_DB, new)
    return True


# ── CLI ─────────────────────────────────────────────────────────────────────

def _cmd_generate(args: argparse.Namespace) -> None:
    key  = generate_key(expires_days=args.expires_days, tier=args.tier)
    meta = validate_key(key)
    meta["note"] = getattr(args, "note", "")
    print(f"\n  Key   : {key}")
    print(f"  Tier  : {meta['tier']}")
    print(f"  Valid : {meta['created']} → {meta['expiry'] or 'never'}")
    save_key_record(key, meta)
    print(f"  Saved → {KEYS_DB}\n")


def _cmd_validate(args: argparse.Namespace) -> None:
    meta   = validate_key(args.key)
    status = "VALID" if meta["valid"] else f"INVALID ({meta['error']})"
    print(f"\n  Key    : {args.key}")
    print(f"  Status : {status}")
    if meta["valid"]:
        print(f"  Tier   : {meta['tier']}")
        exp = meta['expiry']
        print(f"  Expiry : {exp or 'never'}" +
              (f" ({meta['days_remaining']} days left)" if exp else ""))
    print()


def _cmd_list(_args: argparse.Namespace) -> None:
    records = load_all_keys()
    if not records:
        print("No keys issued yet.")
        return
    print(f"\n  {'KEY':<35} {'TIER':<8} {'CREATED':<12} {'EXPIRY'}")
    print("  " + "-" * 72)
    for r in records:
        print(f"  {r['key']:<35} {r.get('tier',''):<8} "
              f"{r.get('created',''):<12} {r.get('expiry','')}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(prog="keygen", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate", help="Generate a new license key")
    gen.add_argument("--expires-days", type=int, default=365)
    gen.add_argument("--tier", default="PRO", choices=list(TIERS))
    gen.add_argument("--note", default="")

    val = sub.add_parser("validate", help="Validate an existing key")
    val.add_argument("key")

    sub.add_parser("list-keys", help="List all issued keys")

    args = parser.parse_args()
    {"generate": _cmd_generate, "validate": _cmd_validate,
     "list-keys": _cmd_list}[args.cmd](args)


if __name__ == "__main__":
    main()
