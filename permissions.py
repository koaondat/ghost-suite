"""
permissions.py — Role-based permission system for the GhostKey Discord bot
===========================================================================
Defines five permission levels and maps them to bot capabilities.
Role IDs are loaded exclusively from environment variables — never hard-coded.

Permission levels (highest → lowest):
  admin        BOT_ADMIN_ROLE_ID  |  Discord Administrator  |  ADMIN_USER_IDS
  manage_keys  KEY_MANAGER_ROLE_ID
  generate     KEY_GENERATOR_ROLE_ID
  support      SUPPORT_ROLE_ID
  customer     CUSTOMER_ROLE_ID  (no bot commands — role only)

Higher levels inherit all lower-level permissions:
  admin       → manage_keys → generate → support
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    pass

# ── Role IDs loaded from environment ─────────────────────────────────────────
def _role_id(env_var: str) -> int | None:
    raw = os.getenv(env_var, "").strip()
    return int(raw) if raw.isdigit() else None


def get_role_ids() -> dict[str, int | None]:
    """Return a mapping of role-name → Discord role ID (or None if not set)."""
    return {
        "customer":      _role_id("CUSTOMER_ROLE_ID"),
        "key_generator": _role_id("KEY_GENERATOR_ROLE_ID"),
        "key_manager":   _role_id("KEY_MANAGER_ROLE_ID"),
        "support":       _role_id("SUPPORT_ROLE_ID"),
        "bot_admin":     _role_id("BOT_ADMIN_ROLE_ID"),
    }


# ── ADMIN_USER_IDS legacy override ────────────────────────────────────────────
def _admin_user_ids() -> set[int]:
    return {
        int(v.strip())
        for v in os.getenv("ADMIN_USER_IDS", "").split(",")
        if v.strip().isdigit()
    }


# ── Legacy ADMIN_ROLE_IDS (kept for backward compat) ─────────────────────────
def _legacy_admin_role_ids() -> set[int]:
    return {
        int(v.strip())
        for v in os.getenv("ADMIN_ROLE_IDS", "").split(",")
        if v.strip().isdigit()
    }


# ── Core permission check ─────────────────────────────────────────────────────

def has_permission(member: discord.Member | discord.User, level: str) -> bool:
    """
    Return True if *member* has at least *level* permission.

    Levels (in ascending power):
        "support"      – read-only support commands
        "generate"     – generate keys (/genkey with quantity restrictions)
        "manage_keys"  – full key management + generate
        "admin"        – every command

    Hierarchy:  admin > manage_keys > generate > support
    """
    # Discord server Administrators always have full bot access.
    if isinstance(member, discord.Member):
        if member.guild_permissions.administrator:
            return True

    # ADMIN_USER_IDS emergency override.
    if member.id in _admin_user_ids():
        return True

    # Legacy ADMIN_ROLE_IDS → treated as bot_admin.
    if isinstance(member, discord.Member):
        member_role_ids = {r.id for r in member.roles}
        if member_role_ids & _legacy_admin_role_ids():
            return True

    role_ids = get_role_ids()

    def _has_role(env_key: str) -> bool:
        rid = role_ids.get(env_key)
        if rid is None:
            return False
        if isinstance(member, discord.Member):
            return any(r.id == rid for r in member.roles)
        return False

    # Admin — every command.
    if _has_role("bot_admin"):
        return True

    # manage_keys inherits generate and support.
    if level in ("manage_keys", "generate", "support"):
        if _has_role("key_manager"):
            return True

    # generate inherits support.
    if level in ("generate", "support"):
        if _has_role("key_generator"):
            return True

    # support only.
    if level == "support":
        if _has_role("support"):
            return True

    return False


# ── app_commands check factory ────────────────────────────────────────────────

def require(level: str):
    """
    Return an app_commands check that raises CheckFailure when the invoker
    does not have the required permission level.

    Usage::

        @bot.tree.command(...)
        @require("manage_keys")
        async def mycommand(interaction): ...
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if has_permission(member, level):
            return True
        raise app_commands.CheckFailure(
            "❌ You don't have permission to use this command."
        )
    return app_commands.check(predicate)


# ── Human-readable permission map (for /permissions command) ─────────────────

PERMISSION_MAP: dict[str, dict] = {
    "customer": {
        "env":   "CUSTOMER_ROLE_ID",
        "label": "Customer",
        "desc":  "Verified buyer role. No staff commands.",
        "commands": [],
    },
    "support": {
        "env":   "SUPPORT_ROLE_ID",
        "label": "Support",
        "desc":  "Read-only lookup and support commands. Cannot generate or revoke keys.",
        "commands": ["/keyinfo", "/lookup", "/licenseinfo", "/userinfo", "/listkeys", "/customer", "/order", "/status", "/appstatus", "/sync"],
    },
    "key_generator": {
        "env":   "KEY_GENERATOR_ROLE_ID",
        "label": "Key Generator",
        "desc":  "Can generate keys. Cannot manage or revoke.",
        "commands": ["/genkey", "/bulkgen"],
    },
    "key_manager": {
        "env":   "KEY_MANAGER_ROLE_ID",
        "label": "Key Manager",
        "desc":  "Full license management. Inherits Generator + Support.",
        "commands": [
            "/genkey", "/bulkgen", "/bulkdelete", "/lookup", "/licenseinfo",
            "/revoke", "/keyinfo", "/bankey", "/unbankey", "/deletekey",
            "/extendkey", "/resetactivation", "/listkeys", "/userinfo",
            "/deleteuser", "/customer", "/order", "/sync", "/status", "/appstatus",
        ],
    },
    "bot_admin": {
        "env":   "BOT_ADMIN_ROLE_ID",
        "label": "Bot Admin",
        "desc":  "Full access to every bot command.",
        "commands": ["All commands"],
    },
}
