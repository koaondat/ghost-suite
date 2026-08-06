"""
GhostConfig — Centralized License & Permission System
======================================================
Provides role-based access control for all GhostConfig features.

Roles
-----
  LicenseRole.TRIAL  — Basic feature access only
  LicenseRole.PRO    — All normal user features
  LicenseRole.ADMIN  — All Pro features + administrative controls

Usage
-----
    from license_manager import PermissionManager, Permission

    pm = PermissionManager(settings_dict)
    pm.has_permission(Permission.BACKUP)          # → True / False
    pm.require_permission(Permission.ADMIN_PANEL) # → raises PermissionDeniedError
    pm.is_trial()   # → True / False
    pm.is_pro()     # → True / False
    pm.is_admin()   # → True / False
"""

from __future__ import annotations

import datetime
import json
import logging
from enum import Enum, auto
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger("ghostconfig.license")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
TRIAL_LIMITS_PATH = _HERE / "trial_limits.json"


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class LicenseRole(Enum):
    TRIAL = "trial"
    PRO   = "pro"
    ADMIN = "admin"


class LicenseStatus(Enum):
    ACTIVE  = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INVALID = "invalid"


class Permission(Enum):
    # ── Dashboard / basic ─────────────────────────────────────────────────
    VIEW_DASHBOARD      = auto()
    REFRESH_SYSTEM_INFO = auto()
    COPY_SYSTEM_INFO    = auto()
    VIEW_ACTIVITY_LOG   = auto()
    OPEN_SETTINGS       = auto()
    OPEN_SUPPORT        = auto()

    # ── Profiles ──────────────────────────────────────────────────────────
    CREATE_PROFILE         = auto()
    UNLIMITED_PROFILES     = auto()
    ADVANCED_PROFILE_TOOLS = auto()   # rename, duplicate, bulk actions

    # ── Backup / restore ─────────────────────────────────────────────────
    BACKUP  = auto()
    RESTORE = auto()

    # ── Export ────────────────────────────────────────────────────────────
    EXPORT_BASIC_REPORT = auto()
    EXPORT_FULL_REPORT  = auto()

    # ── Spoofer / advanced tools ─────────────────────────────────────────
    SPOOFER_ROTATE_GUID    = auto()
    SPOOFER_CUSTOM_GUID    = auto()
    SPOOFER_SET_MAC        = auto()
    SPOOFER_QUERY_VOLUMES  = auto()
    SPOOFER_FULL_PERM      = auto()   # Permanent full spoof (all identifiers)
    SPOOFER_FULL_TEMP      = auto()   # Temporary full spoof (session, restorable)
    SPOOFER_RESTORE_TEMP   = auto()   # Restore temp spoof originals
    PREMIUM_TOOLS          = auto()

    # ── Customization ─────────────────────────────────────────────────────
    CUSTOMIZATION   = auto()
    CUSTOM_PRESETS  = auto()

    # ── Pro-only settings ─────────────────────────────────────────────────
    PRO_SETTINGS = auto()

    # ── Admin ─────────────────────────────────────────────────────────────
    ADMIN_PANEL         = auto()
    GENERATE_LICENSES   = auto()
    REVOKE_LICENSES     = auto()
    RESET_ACTIVATIONS   = auto()
    VIEW_LICENSE_MGMT   = auto()
    VIEW_ADMIN_LOGS     = auto()
    VIEW_LOGIN_ACTIVITY = auto()   # Activity tab — login/register event log


# ---------------------------------------------------------------------------
# Permission mappings
# ---------------------------------------------------------------------------

_TRIAL_PERMISSIONS: frozenset[Permission] = frozenset({
    Permission.VIEW_DASHBOARD,
    Permission.REFRESH_SYSTEM_INFO,
    Permission.COPY_SYSTEM_INFO,
    Permission.VIEW_ACTIVITY_LOG,
    Permission.OPEN_SETTINGS,
    Permission.OPEN_SUPPORT,
    Permission.EXPORT_BASIC_REPORT,
    # Limited profile creation (max enforced separately via trial limits)
    Permission.CREATE_PROFILE,
    # Read-only spoofer operations
    Permission.SPOOFER_QUERY_VOLUMES,
})

_PRO_PERMISSIONS: frozenset[Permission] = _TRIAL_PERMISSIONS | frozenset({
    Permission.UNLIMITED_PROFILES,
    Permission.ADVANCED_PROFILE_TOOLS,
    Permission.BACKUP,
    Permission.RESTORE,
    Permission.EXPORT_FULL_REPORT,
    Permission.SPOOFER_ROTATE_GUID,
    Permission.SPOOFER_CUSTOM_GUID,
    Permission.SPOOFER_SET_MAC,
    Permission.SPOOFER_FULL_PERM,
    Permission.SPOOFER_FULL_TEMP,
    Permission.SPOOFER_RESTORE_TEMP,
    Permission.PREMIUM_TOOLS,
    Permission.CUSTOMIZATION,
    Permission.CUSTOM_PRESETS,
    Permission.PRO_SETTINGS,
})

_ADMIN_PERMISSIONS: frozenset[Permission] = _PRO_PERMISSIONS | frozenset({
    Permission.ADMIN_PANEL,
    Permission.GENERATE_LICENSES,
    Permission.REVOKE_LICENSES,
    Permission.RESET_ACTIVATIONS,
    Permission.VIEW_LICENSE_MGMT,
    Permission.VIEW_ADMIN_LOGS,
    Permission.VIEW_LOGIN_ACTIVITY,
})

ROLE_PERMISSIONS: dict[LicenseRole, frozenset[Permission]] = {
    LicenseRole.TRIAL: _TRIAL_PERMISSIONS,
    LicenseRole.PRO:   _PRO_PERMISSIONS,
    LicenseRole.ADMIN: _ADMIN_PERMISSIONS,
}

# Human-readable minimum role required per permission (for upgrade popups)
PERMISSION_REQUIRED_ROLE: dict[Permission, LicenseRole] = {}
for _perm in Permission:
    if _perm in _TRIAL_PERMISSIONS:
        PERMISSION_REQUIRED_ROLE[_perm] = LicenseRole.TRIAL
    elif _perm in _PRO_PERMISSIONS:
        PERMISSION_REQUIRED_ROLE[_perm] = LicenseRole.PRO
    else:
        PERMISSION_REQUIRED_ROLE[_perm] = LicenseRole.ADMIN


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PermissionDeniedError(Exception):
    """Raised when a backend action is attempted without the required permission."""

    def __init__(self, permission: Permission, role: LicenseRole):
        self.permission = permission
        self.role = role
        required = PERMISSION_REQUIRED_ROLE.get(permission, LicenseRole.PRO)
        super().__init__(
            f"Permission denied: {required.value.capitalize()} license required "
            f"for '{permission.name}'. Current role: {role.value}."
        )


class LicenseExpiredError(Exception):
    """Raised when the license has expired."""


class LicenseRevokedError(Exception):
    """Raised when the license has been revoked."""


class LicenseInvalidError(Exception):
    """Raised when the license key cannot be validated."""


# ---------------------------------------------------------------------------
# Trial limits loader
# ---------------------------------------------------------------------------

_DEFAULT_TRIAL_LIMITS = {
    "max_saved_profiles": 2,
    "max_exports_per_day": 1,
    "allow_advanced_backup": False,
    "allow_restore": False,
    "allow_custom_presets": False,
    "allow_admin_panel": False,
    "allow_bulk_actions": False,
    "allow_pro_settings": False,
    "allow_premium_tools": False,
}


def load_trial_limits() -> dict:
    """Load trial limits from trial_limits.json, falling back to defaults."""
    if TRIAL_LIMITS_PATH.exists():
        try:
            data = json.loads(TRIAL_LIMITS_PATH.read_text(encoding="utf-8"))
            limits = dict(_DEFAULT_TRIAL_LIMITS)
            limits.update({k: v for k, v in data.items() if not k.startswith("_")})
            return limits
        except Exception as exc:
            logger.warning("Failed to load trial_limits.json: %s — using defaults.", exc)
    return dict(_DEFAULT_TRIAL_LIMITS)


# ---------------------------------------------------------------------------
# License validation
# ---------------------------------------------------------------------------

def _resolve_role_from_key(key: str, settings: dict) -> tuple[LicenseRole, LicenseStatus]:
    """
    Derive a (LicenseRole, LicenseStatus) from the stored license key using
    the HMAC-validated keygen.validate_key() function.

    Validation order
    ----------------
    1. Delegate to keygen.validate_key() for HMAC + tier resolution.
    2. Banned keys are treated as TRIAL-revoked.
    3. Expired keys are flagged as EXPIRED while keeping the original role.
    4. Unknown / malformed keys → TRIAL with a warning.

    Expiry / revocation fields (optional) in settings dict are also checked
    as an override (kept for backward compatibility):
      license_revoked : "1" means revoked
    """
    if not key or key in ("GHOST-XXXX-XXXX-XXXX", ""):
        logger.info("No valid license key found — defaulting to Trial.")
        return LicenseRole.TRIAL, LicenseStatus.ACTIVE

    # Use the HMAC-based validator so the role comes from the key itself
    try:
        import keygen as _kg  # local import to avoid circular deps at module level
        meta = _kg.validate_key(key)
    except Exception as exc:
        logger.error("keygen.validate_key failed: %s — defaulting to Trial.", exc)
        return LicenseRole.TRIAL, LicenseStatus.INVALID

    tier_str = (meta.get("tier") or "TRIAL").upper()
    role_map = {
        "ADMIN": LicenseRole.ADMIN,
        "PRO":   LicenseRole.PRO,
        "TRIAL": LicenseRole.TRIAL,
    }
    role = role_map.get(tier_str, LicenseRole.TRIAL)

    # Banned → treat as revoked
    if _kg.is_banned(key):
        logger.error("License key is banned — treating as revoked.")
        return role, LicenseStatus.REVOKED

    # Explicit revocation flag in settings (manual override)
    if settings.get("license_revoked", "0") == "1":
        logger.error("License key has been revoked (settings flag).")
        return role, LicenseStatus.REVOKED

    # HMAC expired
    if meta.get("expired"):
        logger.warning("License key has expired.")
        return role, LicenseStatus.EXPIRED

    # HMAC invalid (bad signature)
    if not meta.get("valid"):
        logger.warning("License key failed HMAC validation: %s", meta.get("error", ""))
        return LicenseRole.TRIAL, LicenseStatus.INVALID

    return role, LicenseStatus.ACTIVE


# ---------------------------------------------------------------------------
# PermissionManager
# ---------------------------------------------------------------------------

class PermissionManager:
    """
    Central permission authority for GhostConfig.

    Instantiate once and pass to every UI component that needs access control.
    Always re-validate by calling ``reload(settings)`` after a license key change.
    """

    def __init__(self, settings: dict):
        self._role   = LicenseRole.TRIAL
        self._status = LicenseStatus.ACTIVE
        self._limits = load_trial_limits()
        self.reload(settings)

    # ── Initialisation ──────────────────────────────────────────────────

    def reload(self, settings: dict) -> None:
        """Re-derive role and status from the current settings dict."""
        key = settings.get("license_key", "")
        try:
            self._role, self._status = _resolve_role_from_key(key, settings)
        except Exception as exc:
            logger.error("License validation error: %s", exc)
            self._role   = LicenseRole.TRIAL
            self._status = LicenseStatus.INVALID

        logger.info(
            "License loaded — role=%s  status=%s",
            self._role.value, self._status.value,
        )
        self._limits = load_trial_limits()

    # ── Role predicates ─────────────────────────────────────────────────

    def is_trial(self) -> bool:
        return self._role == LicenseRole.TRIAL

    def is_pro(self) -> bool:
        return self._role == LicenseRole.PRO

    def is_admin(self) -> bool:
        return self._role == LicenseRole.ADMIN

    # ── Status helpers ───────────────────────────────────────────────────

    @property
    def role(self) -> LicenseRole:
        return self._role

    @property
    def status(self) -> LicenseStatus:
        return self._status

    @property
    def limits(self) -> dict:
        return self._limits

    def is_active(self) -> bool:
        return self._status == LicenseStatus.ACTIVE

    # ── Permission checks ────────────────────────────────────────────────

    def has_permission(self, permission: Permission) -> bool:
        """
        Return True if the current role grants *permission* AND the license
        is in an active state.

        For expired/revoked licenses only basic VIEW permissions are granted
        so the user can see their status and navigate to Settings.
        """
        if self._status == LicenseStatus.REVOKED:
            logger.warning(
                "Permission check '%s' denied — license revoked.", permission.name
            )
            return False

        if self._status == LicenseStatus.EXPIRED:
            # Allow minimal navigation even on expired license
            _expired_allowed = {
                Permission.VIEW_DASHBOARD,
                Permission.VIEW_ACTIVITY_LOG,
                Permission.OPEN_SETTINGS,
                Permission.OPEN_SUPPORT,
            }
            return permission in _expired_allowed

        return permission in ROLE_PERMISSIONS.get(self._role, frozenset())

    def require_permission(self, permission: Permission) -> None:
        """
        Assert that the current role has *permission*.

        Raises
        ------
        PermissionDeniedError  — if permission is absent
        LicenseExpiredError    — if the license has expired
        LicenseRevokedError    — if the license has been revoked
        """
        if self._status == LicenseStatus.REVOKED:
            msg = f"Permission denied: license is revoked. Action: {permission.name}"
            logger.error(msg)
            raise LicenseRevokedError(msg)

        if self._status == LicenseStatus.EXPIRED:
            msg = f"Permission denied: license has expired. Action: {permission.name}"
            logger.warning(msg)
            raise LicenseExpiredError(msg)

        if not self.has_permission(permission):
            logger.warning(
                "Permission denied: %s license required for '%s'. Current: %s",
                PERMISSION_REQUIRED_ROLE.get(permission, LicenseRole.PRO).value,
                permission.name,
                self._role.value,
            )
            raise PermissionDeniedError(permission, self._role)

    # ── Trial limit helpers ───────────────────────────────────────────────

    def get_limit(self, key: str):
        """Return a trial limit value by key, or None if not found."""
        return self._limits.get(key)

    def check_trial_limit(self, key: str, current_value: int) -> bool:
        """
        Return True if *current_value* is within the trial limit for *key*.
        Always returns True for non-Trial roles.
        """
        if not self.is_trial():
            return True
        limit = self._limits.get(key)
        if limit is None:
            return True
        return current_value < int(limit)

    # ── Display helpers ──────────────────────────────────────────────────

    def badge_text(self) -> str:
        if self.is_admin():
            return "  ADMIN  "
        if self.is_pro():
            return "  PRO  "
        return "  TRIAL  "

    def badge_color(self) -> str:
        """Return a background hex color for the license badge."""
        if self.is_admin():
            return "#b45309"   # amber/gold
        if self.is_pro():
            return "#7c3aed"   # purple
        return "#6b7a9a"       # gray (muted)

    def required_role_label(self, permission: Permission) -> str:
        """Return a human-readable upgrade label for a locked permission."""
        req = PERMISSION_REQUIRED_ROLE.get(permission, LicenseRole.PRO)
        return req.value.capitalize()
