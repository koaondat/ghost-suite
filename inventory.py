"""
inventory.py — Ghost License Key Inventory
==========================================
Manages a stocked inventory of pre-loaded license keys.

Each key record:
  key           : string  — the license key
  plan          : string  — 'pro' | 'lifetime'
  status        : string  — 'unused' | 'assigned' | 'revoked'
  order_id      : string  — PayPal capture ID or '' if unused
  customer_email: string  — '' if unused
  purchase_date : string  — ISO-8601 or '' if unused
  assigned_user : string  — Discord username or '' if unused
  added_at      : string  — ISO-8601 when key was imported

Thread-safe: all writes hold _inventory_lock.
Atomic writes via temp-file rename.
"""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent

INVENTORY_DB = _HERE / "key_inventory.json"

_inventory_lock = threading.Lock()


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


# ── Public API ────────────────────────────────────────────────────────────────

def import_keys(keys: list[str], plan: str) -> dict:
    """
    Import a list of raw key strings into the inventory with status=unused.
    Skips duplicates (same key already present regardless of plan/status).

    Returns: { added: int, skipped: int, duplicates: [str] }
    """
    plan = plan.strip().lower()
    now  = _now_iso()

    with _inventory_lock:
        records   = _load()
        existing  = {r["key"].upper() for r in records}
        added     = []
        skipped   = []

        for raw in keys:
            clean = _normalize(raw)
            if not clean:
                continue
            if clean in existing:
                skipped.append(clean)
                continue
            records.append({
                "key":            clean,
                "plan":           plan,
                "status":         "unused",
                "order_id":       "",
                "customer_email": "",
                "purchase_date":  "",
                "assigned_user":  "",
                "added_at":       now,
            })
            existing.add(clean)
            added.append(clean)

        _save(records)

    return {"added": len(added), "skipped": len(skipped), "duplicates": skipped}


def get_next_unused(plan: str) -> Optional[dict]:
    """
    Return the first unused key matching the given plan, or None if out of stock.
    Does NOT mark it — call assign_key() after payment verification.
    """
    plan = plan.strip().lower()
    records = _load()
    return next(
        (r for r in records if r.get("plan") == plan and r.get("status") == "unused"),
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
    Mark a key as assigned. Returns True on success, False if key not found
    or already assigned/revoked. Idempotent: if the key is already assigned
    to the SAME order_id, returns True without modifying.
    """
    clean = _normalize(key)
    with _inventory_lock:
        records = _load()
        for r in records:
            if r["key"].upper() == clean:
                if r["status"] == "assigned" and r.get("order_id") == order_id:
                    return True   # idempotent
                if r["status"] != "unused":
                    return False
                r["status"]         = "assigned"
                r["order_id"]       = order_id
                r["customer_email"] = customer_email
                r["assigned_user"]  = assigned_user
                r["purchase_date"]  = purchase_date or _now_iso()
                _save(records)
                return True
    return False


def revoke_key(key: str) -> bool:
    """Mark a key as revoked. Returns True if found and updated."""
    clean = _normalize(key)
    with _inventory_lock:
        records = _load()
        for r in records:
            if r["key"].upper() == clean:
                r["status"] = "revoked"
                _save(records)
                return True
    return False


def delete_key(key: str) -> bool:
    """Permanently remove a key from the inventory. Returns True if found."""
    clean = _normalize(key)
    with _inventory_lock:
        records = _load()
        new     = [r for r in records if r["key"].upper() != clean]
        if len(new) == len(records):
            return False
        _save(new)
        return True


def delete_keys(keys: list[str]) -> dict:
    """Bulk delete. Returns { deleted: [str], not_found: [str] }."""
    cleans = {_normalize(k) for k in keys if k.strip()}
    with _inventory_lock:
        records  = _load()
        existing = {r["key"].upper() for r in records}
        deleted  = [k for k in cleans if k in existing]
        not_found = [k for k in cleans if k not in existing]
        new = [r for r in records if r["key"].upper() not in cleans]
        _save(new)
    return {"deleted": deleted, "not_found": not_found}


def get_key(key: str) -> Optional[dict]:
    """Return the record for a single key, or None."""
    clean = _normalize(key)
    return next((r for r in _load() if r["key"].upper() == clean), None)


def get_order_key(order_id: str) -> Optional[dict]:
    """Return the key record assigned to a specific order, or None."""
    return next((r for r in _load() if r.get("order_id") == order_id), None)


def list_keys(
    status: Optional[str] = None,
    plan: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    """Return all keys, optionally filtered by status, plan, or key search."""
    records = _load()
    if status:
        records = [r for r in records if r.get("status") == status.lower()]
    if plan:
        records = [r for r in records if r.get("plan") == plan.lower()]
    if search:
        q = search.strip().upper()
        records = [r for r in records if q in r["key"].upper()]
    return records


def stats() -> dict:
    """Return aggregate counts for the admin dashboard."""
    records  = _load()
    total    = len(records)
    unused   = sum(1 for r in records if r.get("status") == "unused")
    assigned = sum(1 for r in records if r.get("status") == "assigned")
    revoked  = sum(1 for r in records if r.get("status") == "revoked")
    by_plan: dict[str, int] = {}
    for r in records:
        p = r.get("plan", "unknown")
        by_plan[p] = by_plan.get(p, 0) + 1
    return {
        "total":    total,
        "unused":   unused,
        "assigned": assigned,
        "revoked":  revoked,
        "by_plan":  by_plan,
    }
