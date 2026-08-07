"""
inventory.py — Ghost License Key Inventory
==========================================
Manages a stocked inventory of pre-loaded license keys.

Each key record (full schema):
  key           : string  — the license key
  plan          : string  — 'pro' | 'lifetime'
  status        : string  — 'available' | 'reserved' | 'sold' | 'activated' | 'revoked' | 'expired'
  customer      : string  — customer email or Discord username
  purchase_date : string  — ISO-8601 or '' if unused
  hwid          : string  — hardware ID bound to this key
  created_date  : string  — ISO-8601 when key was imported/created
  expiration    : string  — ISO-8601 expiry date, or '' for lifetime
  notes         : string  — admin notes
  order_id      : string  — PayPal capture ID or ''
  customer_email: string  — '' if unused
  assigned_user : string  — Discord username or ''

Thread-safe: all writes hold _inventory_lock.
Atomic writes via temp-file rename.
"""

from __future__ import annotations

import datetime
import json
import re
import threading
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent

INVENTORY_DB = _HERE / "key_inventory.json"

_inventory_lock = threading.Lock()

# Ghost key format: GHOST-XXXXX-XXXXX-XXXXX-XXXXX (alphanumeric segments)
_KEY_RE = re.compile(r'^[A-Z0-9]{4,}-[A-Z0-9]{4,}-[A-Z0-9]{4,}', re.IGNORECASE)

VALID_STATUSES = {'available', 'reserved', 'sold', 'activated', 'revoked', 'expired'}

# ── Plan normalisation ────────────────────────────────────────────────────────
# All of these must resolve to the same canonical slug used by the checkout.
# Canonical values: 'pro', 'lifetime', 'trial'
_PLAN_ALIASES: dict[str, str] = {
    # pro variants
    "pro":                  "pro",
    "monthly":              "pro",
    "ghost_pro_monthly":    "pro",
    "ghost pro monthly":    "pro",
    "ghost pro (monthly)":  "pro",
    "ghost_pro":            "pro",
    "ghost pro":            "pro",
    # lifetime variants
    "lifetime":             "lifetime",
    "ghost_lifetime":       "lifetime",
    "ghost lifetime":       "lifetime",
    # trial variants
    "trial":                "trial",
    "ghost_trial":          "trial",
    "ghost trial":          "trial",
}


def normalize_plan(plan: str) -> str:
    """
    Return the canonical plan slug for any human-readable or legacy plan name.

    Canonical values: 'pro', 'lifetime', 'trial'.
    Unknown values are returned as-is (lowercased + stripped) so that new
    plans don't silently break — the caller's validation layer will catch them.
    """
    if not plan:
        return ""
    key = plan.strip().lower()
    return _PLAN_ALIASES.get(key, key)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if INVENTORY_DB.exists():
        try:
            data = json.loads(INVENTORY_DB.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save(records: list[dict]) -> None:
    tmp = INVENTORY_DB.with_suffix(INVENTORY_DB.suffix + ".tmp")
    tmp.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    tmp.replace(INVENTORY_DB)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _normalize(key: str) -> str:
    return key.strip().upper()


def _is_valid_key(key: str) -> bool:
    """Validate Ghost key format: requires at least 3 dash-separated segments."""
    return bool(_KEY_RE.match(key.strip()))


def _migrate_record(r: dict) -> dict:
    """Migrate old records (unused/assigned/revoked) to new schema."""
    # Map old statuses to new
    status_map = {
        'unused':   'available',
        'assigned': 'sold',
        'revoked':  'revoked',
    }
    if r.get('status') in status_map:
        r['status'] = status_map[r['status']]
    # Ensure all fields exist
    r.setdefault('customer',       r.get('customer_email', '') or r.get('assigned_user', ''))
    r.setdefault('hwid',           '')
    r.setdefault('created_date',   r.get('added_at', _now_iso()))
    r.setdefault('expiration',     '')
    r.setdefault('notes',          r.get('note', ''))
    r.setdefault('purchase_date',  r.get('purchase_date', ''))
    r.setdefault('order_id',       r.get('order_id', ''))
    r.setdefault('customer_email', r.get('customer_email', ''))
    r.setdefault('assigned_user',  r.get('assigned_user', ''))
    r.setdefault('added_at',       r.get('added_at', r.get('created_date', _now_iso())))
    return r


def _load_migrated() -> list[dict]:
    """Load records, migrating legacy ones."""
    return [_migrate_record(r) for r in _load()]


# ── Public API ────────────────────────────────────────────────────────────────

def validate_key_format(key: str) -> bool:
    """Return True if the key matches the Ghost key format."""
    return _is_valid_key(key)


def import_keys(keys: list[str], plan: str, notes: str = "") -> dict:
    """
    Import a list of raw key strings into the inventory with status=available.
    Skips duplicates (same key already present regardless of plan/status).
    Validates Ghost key format; invalid keys are counted separately.

    Returns: { added: int, skipped: int, invalid: int, duplicates: [str], invalid_keys: [str] }
    """
    plan = normalize_plan(plan)
    now  = _now_iso()

    with _inventory_lock:
        records   = _load_migrated()
        existing  = {r["key"].upper() for r in records}
        added     = []
        skipped   = []
        invalid   = []

        for raw in keys:
            if not raw or not raw.strip():
                continue
            clean = _normalize(raw)
            if not clean:
                continue
            if not _is_valid_key(clean):
                invalid.append(clean)
                continue
            if clean in existing:
                skipped.append(clean)
                continue
            records.append({
                "key":            clean,
                "plan":           plan,
                "status":         "available",
                "customer":       "",
                "purchase_date":  "",
                "hwid":           "",
                "created_date":   now,
                "expiration":     "",
                "notes":          notes,
                "order_id":       "",
                "customer_email": "",
                "assigned_user":  "",
                "added_at":       now,
            })
            existing.add(clean)
            added.append(clean)

        _save(records)

    return {
        "added":        len(added),
        "skipped":      len(skipped),
        "invalid":      len(invalid),
        "duplicates":   skipped,
        "invalid_keys": invalid,
    }


def get_next_unused(plan: str) -> Optional[dict]:
    """
    Return the first available key matching the given plan, or None if out of stock.
    Does NOT mark it — call assign_key() after payment verification.
    """
    plan = normalize_plan(plan)
    records = _load_migrated()
    return next(
        (r for r in records if r.get("plan") == plan and r.get("status") in ('available', 'unused')),
        None,
    )


def assign_key(
    key: str,
    order_id: str,
    customer_email: str,
    assigned_user: str,
    purchase_date: Optional[str] = None,
) -> bool:
    """
    Mark a key as sold. Returns True on success, False if key not found
    or already sold/revoked. Idempotent: if the key is already assigned
    to the SAME order_id, returns True without modifying.
    """
    clean = _normalize(key)
    with _inventory_lock:
        records = _load_migrated()
        for r in records:
            if r["key"].upper() == clean:
                if r["status"] == "sold" and r.get("order_id") == order_id:
                    return True   # idempotent
                if r["status"] not in ('available', 'unused', 'reserved'):
                    return False
                r["status"]         = "sold"
                r["order_id"]       = order_id
                r["customer_email"] = customer_email
                r["customer"]       = customer_email or assigned_user
                r["assigned_user"]  = assigned_user
                r["purchase_date"]  = purchase_date or _now_iso()
                _save(records)
                return True
    return False


def revoke_key(key: str) -> bool:
    """Mark a key as revoked. Returns True if found and updated."""
    clean = _normalize(key)
    with _inventory_lock:
        records = _load_migrated()
        for r in records:
            if r["key"].upper() == clean:
                r["status"] = "revoked"
                _save(records)
                return True
    return False


def update_key(key: str, updates: dict) -> bool:
    """Update arbitrary fields on a key record. Returns True if found."""
    clean = _normalize(key)
    allowed_fields = {
        'status', 'customer', 'customer_email', 'assigned_user',
        'purchase_date', 'hwid', 'expiration', 'notes', 'plan',
        'order_id',
    }
    with _inventory_lock:
        records = _load_migrated()
        for r in records:
            if r["key"].upper() == clean:
                for field, value in updates.items():
                    if field in allowed_fields:
                        r[field] = value
                        # Keep legacy fields in sync
                        if field == 'customer':
                            r['customer_email'] = value
                            r['assigned_user']  = value
                _save(records)
                return True
    return False


def delete_key(key: str) -> bool:
    """Permanently remove a key from the inventory. Returns True if found."""
    clean = _normalize(key)
    with _inventory_lock:
        records = _load_migrated()
        new     = [r for r in records if r["key"].upper() != clean]
        if len(new) == len(records):
            return False
        _save(new)
        return True


def delete_keys(keys: list[str]) -> dict:
    """Bulk delete. Returns { deleted: [str], not_found: [str] }."""
    cleans = {_normalize(k) for k in keys if k.strip()}
    with _inventory_lock:
        records  = _load_migrated()
        existing = {r["key"].upper() for r in records}
        deleted  = [k for k in cleans if k in existing]
        not_found = [k for k in cleans if k not in existing]
        new = [r for r in records if r["key"].upper() not in cleans]
        _save(new)
    return {"deleted": deleted, "not_found": not_found}


def get_key(key: str) -> Optional[dict]:
    """Return the record for a single key, or None."""
    clean = _normalize(key)
    return next((r for r in _load_migrated() if r["key"].upper() == clean), None)


def get_order_key(order_id: str) -> Optional[dict]:
    """Return the key record assigned to a specific order, or None."""
    return next((r for r in _load_migrated() if r.get("order_id") == order_id), None)


def list_keys(
    status: Optional[str] = None,
    plan: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    """Return all keys, optionally filtered by status, plan, or key/customer search."""
    records = _load_migrated()
    if status:
        records = [r for r in records if r.get("status") == status.lower()]
    if plan:
        canonical = normalize_plan(plan)
        records = [r for r in records if normalize_plan(r.get("plan", "")) == canonical]
    if search:
        q = search.strip().lower()
        records = [r for r in records if (
            q in r["key"].lower() or
            q in (r.get("customer") or "").lower() or
            q in (r.get("customer_email") or "").lower() or
            q in (r.get("notes") or "").lower()
        )]
    return records


def stats() -> dict:
    """Return aggregate counts for the admin dashboard."""
    records   = _load_migrated()
    total     = len(records)
    available = sum(1 for r in records if r.get("status") in ('available', 'unused'))
    reserved  = sum(1 for r in records if r.get("status") == 'reserved')
    sold      = sum(1 for r in records if r.get("status") in ('sold', 'assigned'))
    activated = sum(1 for r in records if r.get("status") == 'activated')
    revoked   = sum(1 for r in records if r.get("status") == 'revoked')
    expired   = sum(1 for r in records if r.get("status") == 'expired')
    by_plan: dict[str, int] = {}
    for r in records:
        p = r.get("plan", "unknown")
        by_plan[p] = by_plan.get(p, 0) + 1
    return {
        "total":     total,
        "available": available,
        "unused":    available,   # legacy compat
        "reserved":  reserved,
        "sold":      sold,
        "assigned":  sold,        # legacy compat
        "activated": activated,
        "revoked":   revoked,
        "expired":   expired,
        "by_plan":   by_plan,
    }
