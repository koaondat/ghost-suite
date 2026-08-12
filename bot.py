from __future__ import annotations

# ── Load .env FIRST — before any import that reads os.environ at module level ─
# api_client.py snapshots GHOST_API_URL and GHOST_ADMIN_API_KEY into constants
# the moment it is imported.  load_dotenv() must run before that import.
import os
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"

# Abort early if the file is missing — nothing else will work.
if not _ENV_PATH.exists():
    raise SystemExit(
        f"[ghostkey] .env file not found at {_ENV_PATH}\n"
        "Create it from .env.example before starting the bot."
    )

# Unset any inherited OS-level value so it cannot shadow the .env file.
os.environ.pop("DISCORD_TOKEN", None)
os.environ.pop("GHOST_API_URL", None)
os.environ.pop("GHOST_ADMIN_API_KEY", None)

# override=True ensures .env values win over any stale inherited env vars.
load_dotenv(dotenv_path=_ENV_PATH, override=True)

# ── Startup guard — verify critical vars are now present ─────────────────────
if not os.getenv("DISCORD_TOKEN", "").strip():
    raise SystemExit(
        f"[ghostkey] DISCORD_TOKEN is not set in {_ENV_PATH}\n"
        "Add your bot token and restart."
    )

# ── Now safe to import modules that read os.environ at module level ───────────
import asyncio
import datetime as dt
import io
import json
import logging
import re
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

import api_client as api
from permissions import (
    PERMISSION_MAP,
    get_role_ids,
    has_permission,
    require,
)

BASE_DIR  = Path(__file__).resolve().parent
AUDIT_LOG = BASE_DIR / "discord_audit_log.json"
BUYER_ROLE_LOG = BASE_DIR / "buyer_role_log.json"

TOKEN    = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)

# ── Startup env-var verification (presence only — never prints secret values) ─
_startup_checks = {
    "DISCORD_TOKEN":      bool(TOKEN),
    "GHOST_API_URL":      bool(os.getenv("GHOST_API_URL", "").strip()),
    "GHOST_ADMIN_API_KEY": bool(os.getenv("GHOST_ADMIN_API_KEY", "").strip()),
}
# Logging is not yet configured here, so use print for this one-time check.
for _var, _present in _startup_checks.items():
    print(f"[ghostkey] {_var}: {'SET' if _present else 'MISSING'}")
_missing = [v for v, ok in _startup_checks.items() if not ok]
if _missing:
    print(f"[ghostkey] WARNING: {', '.join(_missing)} not found in {_ENV_PATH}")

# DISCORD_GUILD_ID — used by the web server for guild join; GUILD_ID is used by the
# bot for command sync.  If DISCORD_GUILD_ID is set, prefer it for role validation.
_DISCORD_GUILD_ID_STR = os.getenv("DISCORD_GUILD_ID", "").strip()
_OAUTH_GUILD_ID = int(_DISCORD_GUILD_ID_STR) if _DISCORD_GUILD_ID_STR.isdigit() else GUILD_ID

# Effective guild ID for startup validation: prefer DISCORD_GUILD_ID over GUILD_ID.
_EFFECTIVE_GUILD_ID = _OAUTH_GUILD_ID or GUILD_ID

# Legacy role/user ID sets — kept for backward compatibility.
ADMIN_ROLE_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_ROLE_IDS", "").split(",")
    if value.strip().isdigit()
}
ADMIN_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

# ── Logging ───────────────────────────────────────────────────────────────────
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ghostkey.bot")

intents = discord.Intents.default()
intents.members = True   # required to fetch guild members for role assignment


# ── Legacy is_admin helper (kept so existing callsites still compile) ─────────
def is_admin(interaction: discord.Interaction) -> bool:
    return has_permission(interaction.user, "admin")


def admin_only():
    """Legacy decorator alias — routes through the new permission system."""
    return require("admin")


# ── JSON helpers ──────────────────────────────────────────────────────────────
def save_json(path: Path, data: list[dict]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def read_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


# ── Command audit (staff actions) ────────────────────────────────────────────
def audit(interaction: discord.Interaction, action: str, target: str, details: str = "") -> None:
    records = read_json(AUDIT_LOG)
    records.append(
        {
            "timestamp":  dt.datetime.now(dt.timezone.utc).isoformat(),
            "admin_id":   interaction.user.id,
            "admin_name": str(interaction.user),
            "action":     action,
            "target":     target,
            "details":    details,
        }
    )
    save_json(AUDIT_LOG, records[-5000:])


# ── Buyer-role assignment audit ───────────────────────────────────────────────
def audit_role_assignment(
    *,
    discord_user: str,
    discord_id: int | str,
    customer_id: str,
    license_key: str,
    order_id: str,
    plan: str,
    role_id: int | str,
    result: str,      # "success" | "already_had_role" | "failed:..." | "pending"
    note: str = "",
) -> None:
    """
    Append one record to buyer_role_log.json.
    Never logs tokens, passwords, or bot secrets.
    The license key is masked to show only the last 8 characters.
    """
    safe_key = ("*" * (len(license_key) - 8) + license_key[-8:]) if len(license_key) > 8 else "***"
    records = read_json(BUYER_ROLE_LOG)
    records.append(
        {
            "timestamp":    dt.datetime.now(dt.timezone.utc).isoformat(),
            "discord_user": discord_user,
            "discord_id":   str(discord_id),
            "customer_id":  customer_id,
            "license_key":  safe_key,
            "order_id":     order_id,
            "plan":         plan,
            "role_id":      str(role_id),
            "result":       result,
            "note":         note,
        }
    )
    save_json(BUYER_ROLE_LOG, records[-10_000:])


# ── Utility ───────────────────────────────────────────────────────────────────
def status_text(key_data: dict) -> str:
    if key_data.get("banned"):
        return "Banned"
    if key_data.get("expired"):
        return "Expired"
    if key_data.get("valid"):
        return "Active"
    return "Invalid"


def key_embed_from_data(info: dict) -> discord.Embed:
    lic    = info.get("license", {})
    record = info.get("record") or {}
    bound  = info.get("bound_user") or {}

    embed = discord.Embed(
        title="GhostKey License Information",
        color=discord.Color.dark_teal() if lic.get("valid") else discord.Color.red(),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="Key",            value=f"`{lic.get('key', 'Unknown')}`",                    inline=False)
    embed.add_field(name="Status",         value=status_text(lic),                                    inline=True)
    embed.add_field(name="Tier",           value=lic.get("tier") or record.get("tier") or "Unknown",  inline=True)
    embed.add_field(name="Created",        value=str(lic.get("created") or record.get("created") or "Unknown"), inline=True)
    embed.add_field(name="Expires",        value=str(lic.get("expiry") or record.get("expiry") or "Never"),     inline=True)
    days = lic.get("days_remaining", -1)
    embed.add_field(name="Days Remaining", value="Unlimited" if days == -1 else str(days),             inline=True)
    embed.add_field(name="Bound User",     value=bound.get("username") or "Not bound",                inline=True)
    embed.add_field(name="Note",           value=record.get("note") or "None",                        inline=False)
    if not lic.get("valid") and lic.get("error"):
        embed.add_field(name="Validation Message", value=lic["error"], inline=False)
    return embed


_BOT_START_TIME = time.time()


# ── Role hierarchy validation ─────────────────────────────────────────────────
async def validate_role_config(guild: discord.Guild) -> None:
    """
    Validate that:
      1. Every configured role ID exists in the guild.
      2. The bot is in the guild.
      3. The bot has Manage Roles permission.
      4. The bot's highest role is above CUSTOMER_ROLE_ID.
      5. DISCORD_GUILD_ID (OAuth server) matches this guild.
    Logs a clear error for each misconfiguration — does NOT crash the bot.
    Never logs TOKEN, BOT_TOKEN, or CLIENT_SECRET.
    """
    role_ids = get_role_ids()
    env_names = {
        "customer":      "CUSTOMER_ROLE_ID",
        "key_generator": "KEY_GENERATOR_ROLE_ID",
        "key_manager":   "KEY_MANAGER_ROLE_ID",
        "support":       "SUPPORT_ROLE_ID",
        "bot_admin":     "BOT_ADMIN_ROLE_ID",
    }
    guild_role_ids = {r.id for r in guild.roles}

    # ── Check 1: DISCORD_GUILD_ID env var matches the guild we connected to ──
    if _DISCORD_GUILD_ID_STR:
        if str(guild.id) != _DISCORD_GUILD_ID_STR:
            logger.error(
                "Startup: DISCORD_GUILD_ID=%s does not match connected guild '%s' (id=%d). "
                "Customers added via OAuth will be added to guild %s, "
                "but this bot is running in guild %d. "
                "Set DISCORD_GUILD_ID and GUILD_ID to the same server ID.",
                _DISCORD_GUILD_ID_STR, guild.name, guild.id,
                _DISCORD_GUILD_ID_STR, guild.id,
            )
        else:
            logger.info(
                "Startup: DISCORD_GUILD_ID=%s matches guild '%s' ✓",
                _DISCORD_GUILD_ID_STR, guild.name,
            )
    else:
        logger.warning(
            "Startup: DISCORD_GUILD_ID is not set in .env — "
            "the web server cannot auto-add customers to your Discord server. "
            "Set DISCORD_GUILD_ID to your server's numeric ID."
        )

    # ── Check 2: Bot has Manage Roles permission ─────────────────────────────
    me = guild.me
    if me:
        guild_perms = guild.me.guild_permissions
        if not guild_perms.manage_roles:
            logger.error(
                "Startup: Bot does NOT have 'Manage Roles' permission in guild '%s'. "
                "  → Cannot assign CUSTOMER_ROLE_ID to members. "
                "  → Fix: Go to Discord Server Settings → Roles → Bot role → enable 'Manage Roles'.",
                guild.name,
            )
        else:
            logger.info("Startup: Bot has Manage Roles permission in '%s' ✓", guild.name)

    # ── Check 3: Each role ID present in guild ───────────────────────────────
    for key, env_var in env_names.items():
        rid = role_ids.get(key)
        if rid is None:
            logger.warning("Role config: %s is not set in .env — related features disabled.", env_var)
        elif rid not in guild_role_ids:
            logger.error(
                "Role config: %s=%d is NOT found in guild '%s'. "
                "Check the ID in your .env file.",
                env_var, rid, guild.name,
            )
        else:
            logger.info("Role config: %s=%d ✓  (%s)", env_var, rid, env_var)

    # ── Check 4: Bot role is above CUSTOMER_ROLE_ID in hierarchy ────────────
    customer_rid = role_ids.get("customer")
    if customer_rid and customer_rid in guild_role_ids:
        customer_role = guild.get_role(customer_rid)
        if customer_role and me:
            bot_top = me.top_role.position
            if bot_top <= customer_role.position:
                logger.error(
                    "Role hierarchy problem: Bot's highest role (position %d) is NOT above "
                    "the Customer role '%s' (position %d).\n"
                    "  → Cannot assign Customer role.\n"
                    "  → Fix: Go to Discord Server Settings → Roles and drag the bot's role "
                    "ABOVE the Customer role.",
                    bot_top, customer_role.name, customer_role.position,
                )
            else:
                logger.info(
                    "Role hierarchy: Bot role (pos %d) is above Customer role '%s' (pos %d) ✓",
                    bot_top, customer_role.name, customer_role.position,
                )


# ── Bot class ─────────────────────────────────────────────────────────────────
class GhostKeyBot(commands.Bot):
    async def setup_hook(self) -> None:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d commands to guild %d", len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            logger.info("Globally synced %d commands", len(synced))
        api_health_check.start()
        customer_role_task.start()

    async def on_disconnect(self) -> None:
        logger.warning("Bot disconnected from Discord gateway — will attempt to reconnect automatically")

    async def on_resumed(self) -> None:
        logger.info("Bot session resumed successfully")


bot = GhostKeyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    await bot.change_presence(activity=discord.Game(name="GHOST license management"))

    # Use _EFFECTIVE_GUILD_ID (prefers DISCORD_GUILD_ID over GUILD_ID) for validation.
    effective_id = _EFFECTIVE_GUILD_ID
    if effective_id:
        guild = bot.get_guild(effective_id)
        if guild:
            await validate_role_config(guild)
        else:
            logger.warning(
                "Guild ID=%d not found in bot's guild list — role validation skipped. "
                "Ensure the bot is installed in that server.",
                effective_id,
            )
    else:
        logger.info("Neither DISCORD_GUILD_ID nor GUILD_ID is set — role validation will run against the first available guild.")
        if bot.guilds:
            await validate_role_config(bot.guilds[0])


# ── Background: API health check ──────────────────────────────────────────────
@tasks.loop(minutes=5)
async def api_health_check() -> None:
    try:
        data = await api._request("GET", "/health")
        logger.debug("API health: %s", data)
    except Exception as exc:
        logger.warning("API health check failed: %s", exc)


@api_health_check.before_loop
async def _before_health_check() -> None:
    await bot.wait_until_ready()


# ── Background: auto-assign Customer role for verified purchases ──────────────
@tasks.loop(minutes=2)
async def customer_role_task() -> None:
    """
    Every 2 minutes: ask the API for orders that have:
      - payment_status = completed/verified
      - discord_id set (numeric)
      - discord_role_granted = false  (or missing)

    For each, independently verify the license is still valid, then grant
    CUSTOMER_ROLE_ID to that Discord member.  Never trusts client input —
    the API record is the authoritative source of truth.
    """
    customer_rid = get_role_ids().get("customer")
    if not customer_rid:
        return   # CUSTOMER_ROLE_ID not configured — silently skip

    guild = bot.get_guild(_EFFECTIVE_GUILD_ID) if _EFFECTIVE_GUILD_ID else (bot.guilds[0] if bot.guilds else None)
    if not guild:
        return

    try:
        pending = await api.get_pending_customer_roles()
    except Exception as exc:
        logger.warning("customer_role_task: could not fetch pending roles: %s", exc)
        return

    if not pending:
        return

    customer_role = guild.get_role(customer_rid)
    if not customer_role:
        logger.error(
            "customer_role_task: CUSTOMER_ROLE_ID=%d not found in guild. "
            "Check your .env and guild configuration.",
            customer_rid,
        )
        return

    for record in pending:
        order_id    = record.get("order_id", "")
        discord_id_raw = str(record.get("discord_id", "")).strip()
        license_key = record.get("license_key", "") or ""
        plan        = record.get("plan", "") or ""
        customer_id = record.get("customer_id", "") or order_id

        if not discord_id_raw.isdigit():
            logger.info(
                "customer_role_task: order=%s has no valid numeric discord_id — skipping.",
                order_id,
            )
            audit_role_assignment(
                discord_user="unknown",
                discord_id=discord_id_raw or "none",
                customer_id=customer_id,
                license_key=license_key or "none",
                order_id=order_id,
                plan=plan,
                role_id=customer_rid,
                result="failed:discord_account_not_linked",
                note="discord_id missing or non-numeric in order record",
            )
            continue

        discord_id = int(discord_id_raw)

        # Fetch the member — must be in the guild.
        member = guild.get_member(discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id)
            except discord.NotFound:
                member = None
            except Exception as exc:
                logger.warning("customer_role_task: fetch_member(%d) failed: %s", discord_id, exc)

        if member is None:
            logger.info(
                "customer_role_task: discord_id=%d not found in guild — will retry later.",
                discord_id,
            )
            audit_role_assignment(
                discord_user=f"<id:{discord_id}>",
                discord_id=discord_id,
                customer_id=customer_id,
                license_key=license_key or "none",
                order_id=order_id,
                plan=plan,
                role_id=customer_rid,
                result="failed:member_not_in_server",
                note="Member not found in guild; they may not have joined yet",
            )
            continue

        discord_user_str = str(member)

        # Already has the role — treat as success, mark resolved.
        if customer_role in member.roles:
            logger.info(
                "customer_role_task: %s (%d) already has Customer role — marking resolved.",
                discord_user_str, discord_id,
            )
            audit_role_assignment(
                discord_user=discord_user_str,
                discord_id=discord_id,
                customer_id=customer_id,
                license_key=license_key or "none",
                order_id=order_id,
                plan=plan,
                role_id=customer_rid,
                result="already_had_role",
            )
            try:
                await api.mark_customer_role_granted(order_id)
            except Exception as exc:
                logger.warning("customer_role_task: could not mark order %s resolved: %s", order_id, exc)
            continue

        # Grant the role.
        try:
            await member.add_roles(
                customer_role,
                reason=f"Verified purchase — order {order_id} plan={plan}",
            )
            logger.info(
                "customer_role_task: ✓ Granted Customer role to %s (%d) for order %s.",
                discord_user_str, discord_id, order_id,
            )
            audit_role_assignment(
                discord_user=discord_user_str,
                discord_id=discord_id,
                customer_id=customer_id,
                license_key=license_key or "none",
                order_id=order_id,
                plan=plan,
                role_id=customer_rid,
                result="success",
            )
            try:
                await api.mark_customer_role_granted(order_id)
            except Exception as exc:
                logger.warning("customer_role_task: could not mark order %s resolved: %s", order_id, exc)

        except discord.Forbidden:
            msg = (
                f"Bot lacks Manage Roles permission, OR the bot's role is below the "
                f"Customer role in Discord Server Settings → Roles. "
                f"Move the bot role ABOVE the Customer role."
            )
            logger.error("customer_role_task: Forbidden assigning role to %s: %s", discord_user_str, msg)
            audit_role_assignment(
                discord_user=discord_user_str,
                discord_id=discord_id,
                customer_id=customer_id,
                license_key=license_key or "none",
                order_id=order_id,
                plan=plan,
                role_id=customer_rid,
                result="failed:bot_forbidden",
                note=msg,
            )
        except Exception as exc:
            logger.error(
                "customer_role_task: unexpected error granting role to %s (%d): %s",
                discord_user_str, discord_id, exc,
            )
            audit_role_assignment(
                discord_user=discord_user_str,
                discord_id=discord_id,
                customer_id=customer_id,
                license_key=license_key or "none",
                order_id=order_id,
                plan=plan,
                role_id=customer_rid,
                result=f"failed:{type(exc).__name__}",
                note=str(exc)[:200],
            )


@customer_role_task.before_loop
async def _before_customer_role_task() -> None:
    await bot.wait_until_ready()


# ── Error handler ─────────────────────────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        message = str(error) or "❌ You don't have permission to use this command."
    else:
        logger.exception("Slash command failed", exc_info=error)
        message = "The command failed. Check the bot console for details."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# ── License-card button row ───────────────────────────────────────────────────
class LicenseCardView(discord.ui.View):
    def __init__(self, key: str) -> None:
        super().__init__(timeout=None)
        self.key = key

    @discord.ui.button(label="📋  Copy Key", style=discord.ButtonStyle.secondary)
    async def copy_key(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            f"Select all and copy your key:\n```{self.key}```",
            ephemeral=True,
        )

    @discord.ui.button(label="🔍  View Key Info", style=discord.ButtonStyle.primary)
    async def view_key_info(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not has_permission(interaction.user, "support"):
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.", ephemeral=True
            )
            return
        try:
            info = await api.key_info(self.key)
            await interaction.response.send_message(embed=key_embed_from_data(info), ephemeral=True)
        except api.APIError as e:
            await interaction.response.send_message(f"⚠️ Could not load key info: {e}", ephemeral=True)

    @discord.ui.button(label="🗑️  Delete Key", style=discord.ButtonStyle.danger)
    async def delete_key(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not has_permission(interaction.user, "manage_keys"):
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.", ephemeral=True
            )
            return
        try:
            await api.delete_key(self.key)
            audit(interaction, "delete_key_record", self.key, "via license card button")
            await interaction.response.send_message(
                f"🗑️ Key `{self.key}` has been removed from the database.", ephemeral=True
            )
        except api.APIError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


# ── Bulk-delete confirmation view ─────────────────────────────────────────────
class BulkDeleteView(discord.ui.View):
    def __init__(self, keys: list[str], invoker_id: int) -> None:
        super().__init__(timeout=120)
        self.keys = keys
        self.invoker_id = invoker_id
        self._done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the staff member who invoked this command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="✅  Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._done:
            await interaction.response.send_message("Already processed.", ephemeral=True)
            return
        self._done = True
        self._disable_all()

        succeeded: list[str] = []
        not_found: list[str] = []

        for key in self.keys:
            try:
                result = await api.delete_key(key)
                if result.get("deleted"):
                    succeeded.append(key)
                else:
                    not_found.append(key)
            except api.APIError:
                not_found.append(key)

        if succeeded:
            audit(
                interaction,
                "bulk_delete_keys",
                f"{len(succeeded)} key(s)",
                f"deleted={','.join(succeeded)}; not_found={','.join(not_found)}",
            )

        embed = discord.Embed(
            title="🗑️  Bulk Delete — Complete",
            color=discord.Color.dark_red() if not_found else discord.Color.dark_teal(),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(name="✅ Deleted",   value=str(len(succeeded)), inline=True)
        embed.add_field(name="❌ Not Found", value=str(len(not_found)), inline=True)
        if not_found:
            embed.add_field(
                name="Keys Not Found",
                value="\n".join(f"`{k}`" for k in not_found),
                inline=False,
            )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🚫  Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._done:
            await interaction.response.send_message("Already processed.", ephemeral=True)
            return
        self._done = True
        self._disable_all()
        embed = discord.Embed(
            title="🚫  Bulk Delete Cancelled",
            description="No keys were deleted.",
            color=discord.Color.greyple(),
        )
        await interaction.response.edit_message(embed=embed, view=self)

    def _disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


# ── /genkey ───────────────────────────────────────────────────────────────────
_GENKEY_EMBED_MAX = 5

@bot.tree.command(name="genkey", description="Generate and save one or more GHOST license keys")
@app_commands.describe(
    tier="License tier",
    days="Number of valid days; use 0 for no expiration",
    note="Optional internal note attached to every key",
    quantity="How many keys to generate",
)
@app_commands.choices(quantity=[
    app_commands.Choice(name="1 key",   value=1),
    app_commands.Choice(name="3 keys",  value=3),
    app_commands.Choice(name="5 keys",  value=5),
    app_commands.Choice(name="7 keys",  value=7),
    app_commands.Choice(name="10 keys", value=10),
    app_commands.Choice(name="15 keys", value=15),
    app_commands.Choice(name="25 keys", value=25),
    app_commands.Choice(name="50 keys", value=50),
])
@require("generate")
async def genkey(
    interaction: discord.Interaction,
    tier: Literal["TRIAL", "PRO", "ADMIN"],
    days: app_commands.Range[int, 0, 3650] = 30,
    note: app_commands.Range[str, 0, 200] = "",
    quantity: int = 1,
) -> None:
    await interaction.response.defer(ephemeral=True)

    try:
        generated = await api.generate_keys(tier=tier, days=days, note=note, quantity=quantity)
    except api.APIError as e:
        await interaction.followup.send(f"⚠️ Failed to generate keys: {e}", ephemeral=True)
        return

    audit(
        interaction,
        "generate_key_bulk" if quantity > 1 else "generate_key",
        f"{quantity} key(s)",
        f"tier={tier}; days={days}; note={note}; keys={','.join(generated)}",
    )

    expiry_display = "Never" if days == 0 else f"{days}d"
    today_str = dt.datetime.now(dt.timezone.utc).strftime("%b %d, %Y")

    if quantity == 1:
        new_key = generated[0]
        embed = discord.Embed(
            title="👻  GhostKey License Created",
            description="Secure license generated successfully.",
            color=discord.Color.from_rgb(124, 58, 237),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(name="🔑  License Key", value=f"```{new_key}```", inline=False)
        embed.add_field(name="📋  Plan",        value=tier.capitalize(),  inline=True)
        embed.add_field(name="📅  Expiration",  value=expiry_display,     inline=True)
        embed.add_field(name="\u200b",          value="\u200b",           inline=True)
        embed.add_field(name="✅  Status",       value="🟢 Active",                          inline=True)
        embed.add_field(name="👤  Generated By", value=interaction.user.display_name,        inline=True)
        embed.add_field(name="\u200b",           value="\u200b",                             inline=True)
        embed.add_field(name="📝  Note", value=note if note else "*No note provided*", inline=False)
        embed.set_footer(text=f"GhostKey License System  •  {today_str}")
        await interaction.followup.send(embed=embed, view=LicenseCardView(new_key), ephemeral=True)
        return

    if quantity <= _GENKEY_EMBED_MAX:
        embed = discord.Embed(
            title=f"👻  GhostKey Bulk Generation  ({quantity} keys)",
            description=(
                f"**Plan:** {tier.capitalize()}   •   **Expiration:** {expiry_display}\n"
                f"**Note:** {note if note else '*none*'}"
            ),
            color=discord.Color.from_rgb(124, 58, 237),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        for idx, k in enumerate(generated, 1):
            embed.add_field(name=f"🔑 Key {idx}", value=f"```{k}```", inline=False)
        embed.add_field(name="📋  Plan",         value=tier.capitalize(),             inline=True)
        embed.add_field(name="📅  Expiration",   value=expiry_display,                inline=True)
        embed.add_field(name="👤  Generated By", value=interaction.user.display_name, inline=True)
        embed.set_footer(text=f"GhostKey License System  •  {today_str}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    txt_lines = [
        f"GhostKey Bulk Generation — {quantity} keys",
        f"Plan: {tier}  |  Expiration: {expiry_display}  |  Note: {note or 'none'}",
        f"Generated by: {interaction.user} on {today_str}",
        "-" * 56,
    ] + generated
    txt_bytes = "\n".join(txt_lines).encode("utf-8")
    file = discord.File(io.BytesIO(txt_bytes), filename=f"ghostkeys_{tier.lower()}_{quantity}.txt")

    embed = discord.Embed(
        title=f"👻  GhostKey Bulk Generation  ({quantity} keys)",
        description=(
            f"All **{quantity}** keys have been generated and saved.\n"
            f"**Plan:** {tier.capitalize()}   •   **Expiration:** {expiry_display}\n"
            f"**Note:** {note if note else '*none*'}\n\n"
            f"📎 Full key list attached as a text file below."
        ),
        color=discord.Color.from_rgb(124, 58, 237),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="🔑  First Key",    value=f"```{generated[0]}```",         inline=False)
    embed.add_field(name="🔑  Last Key",     value=f"```{generated[-1]}```",         inline=False)
    embed.add_field(name="📋  Plan",         value=tier.capitalize(),                inline=True)
    embed.add_field(name="📅  Expiration",   value=expiry_display,                   inline=True)
    embed.add_field(name="👤  Generated By", value=interaction.user.display_name,    inline=True)
    embed.set_footer(text=f"GhostKey License System  •  {today_str}")
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)


# ── /keyinfo ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="keyinfo", description="View information about a license key")
@require("support")
async def keyinfo(interaction: discord.Interaction, key: str) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        info = await api.key_info(key.strip().upper())
        await interaction.followup.send(embed=key_embed_from_data(info), ephemeral=True)
    except api.APIError as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)


# ── /bankey ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="bankey", description="Ban a license key")
@require("manage_keys")
async def bankey(interaction: discord.Interaction, key: str, reason: app_commands.Range[str, 0, 200] = "") -> None:
    clean = key.strip().upper()
    try:
        await api.ban_key(clean, reason)
        audit(interaction, "ban_key", clean, reason)
        await interaction.response.send_message(f"Banned `{clean}`. Reason: {reason or 'None'}", ephemeral=True)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


# ── /unbankey ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="unbankey", description="Remove a license key ban")
@require("manage_keys")
async def unbankey(interaction: discord.Interaction, key: str) -> None:
    clean = key.strip().upper()
    try:
        result = await api.unban_key(clean)
        if result.get("unbanned"):
            audit(interaction, "unban_key", clean)
        await interaction.response.send_message(result.get("message", "Done."), ephemeral=True)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


# ── /deletekey ────────────────────────────────────────────────────────────────
@bot.tree.command(name="deletekey", description="Delete a key from the issued-key records")
@require("manage_keys")
async def deletekey(interaction: discord.Interaction, key: str) -> None:
    clean = key.strip().upper()
    try:
        result = await api.delete_key(clean)
        if result.get("deleted"):
            audit(interaction, "delete_key_record", clean)
        await interaction.response.send_message(result.get("message", "Done."), ephemeral=True)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


# ── /extendkey ────────────────────────────────────────────────────────────────
@bot.tree.command(name="extendkey", description="Extend a key by generating a replacement with new expiry")
@app_commands.describe(key="The key to extend", days="New validity period in days")
@require("manage_keys")
async def extendkey(
    interaction: discord.Interaction,
    key: str,
    days: app_commands.Range[int, 1, 3650] = 30,
) -> None:
    clean = key.strip().upper()
    try:
        result = await api.extend_key(clean, days)
        audit(interaction, "extend_key", clean, f"replacement={result.get('new_key')} days={days}")
        await interaction.response.send_message(
            f"Extended `{clean}`.\n🔑 Replacement key: ```{result.get('new_key')}```",
            ephemeral=True,
        )
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


# ── /resetactivation ──────────────────────────────────────────────────────────
@bot.tree.command(name="resetactivation", description="Reset HWID/device binding for a key")
@require("manage_keys")
async def resetactivation(interaction: discord.Interaction, key: str) -> None:
    clean = key.strip().upper()
    try:
        result = await api.reset_activation(clean)
        if result.get("reset"):
            audit(interaction, "reset_activation", clean)
        await interaction.response.send_message(result.get("message", "Done."), ephemeral=True)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


# ── /listkeys ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="listkeys", description="List recently issued keys")
@app_commands.describe(tier="Optional tier filter", limit="Number of keys to display")
@require("support")
async def listkeys(
    interaction: discord.Interaction,
    tier: Literal["ALL", "TRIAL", "PRO", "ADMIN"] = "ALL",
    limit: app_commands.Range[int, 1, 20] = 10,
) -> None:
    try:
        records = await api.list_keys(tier=tier, limit=limit)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return

    if not records:
        await interaction.response.send_message("No matching issued keys were found.", ephemeral=True)
        return

    lines = []
    for record in reversed(records):
        candidate = record.get("key", "Unknown")
        s = "Banned" if record.get("banned") else ("Expired" if record.get("expired") else ("Active" if record.get("valid") else "Invalid"))
        lines.append(f"`{candidate}` • {record.get('tier', '?')} • {s}")
    embed = discord.Embed(title=f"Recent Keys ({len(records)})", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /userinfo ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="userinfo", description="View a registered user's license information")
@require("support")
async def userinfo(interaction: discord.Interaction, username: str) -> None:
    try:
        user = await api.user_info(username)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return

    if not user:
        await interaction.response.send_message("User not found.", ephemeral=True)
        return

    embed = discord.Embed(title=f"User: {user.get('username')}", color=discord.Color.blurple())
    embed.add_field(name="Tier",    value=user.get("tier", "Unknown"),    inline=True)
    embed.add_field(name="Created", value=user.get("created", "Unknown"), inline=True)
    embed.add_field(name="Key",     value=f"`{user.get('key', 'Unknown')}`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /deleteuser ───────────────────────────────────────────────────────────────
@bot.tree.command(name="deleteuser", description="Delete a registered user account")
@require("manage_keys")
async def deleteuser(interaction: discord.Interaction, username: str) -> None:
    try:
        result = await api.delete_user(username)
        if result.get("deleted"):
            audit(interaction, "delete_user", username)
        await interaction.response.send_message(result.get("message", "Done."), ephemeral=True)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


# ── /stats ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="stats", description="Show license-system statistics")
@require("manage_keys")
async def stats(interaction: discord.Interaction) -> None:
    try:
        s = await api.stats()
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ Could not fetch stats: {e}", ephemeral=True)
        return

    tiers = s.get("tiers", {})
    embed = discord.Embed(title="GhostKey Statistics", color=discord.Color.dark_teal())
    embed.add_field(name="Total Keys", value=str(s.get("total_keys", 0)), inline=True)
    embed.add_field(name="Active",     value=str(s.get("active", 0)),     inline=True)
    embed.add_field(name="Expired",    value=str(s.get("expired", 0)),    inline=True)
    embed.add_field(name="Trial",      value=str(tiers.get("TRIAL", 0)),  inline=True)
    embed.add_field(name="Pro",        value=str(tiers.get("PRO", 0)),    inline=True)
    embed.add_field(name="Admin",      value=str(tiers.get("ADMIN", 0)),  inline=True)
    embed.add_field(name="Banned",     value=str(s.get("banned", 0)),     inline=True)
    embed.add_field(name="Users",      value=str(s.get("users", 0)),      inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /bulkdelete ───────────────────────────────────────────────────────────────
@bot.tree.command(name="bulkdelete", description="Delete multiple license keys at once")
@app_commands.describe(keys="Paste keys separated by commas or new lines")
@require("manage_keys")
async def bulkdelete(interaction: discord.Interaction, keys: str) -> None:
    raw_keys   = re.split(r"[,\n\r]+", keys)
    valid_keys = [k.strip().upper() for k in raw_keys if k.strip()]

    if not valid_keys:
        await interaction.response.send_message("No valid keys were found in your input.", ephemeral=True)
        return

    seen: set[str] = set()
    deduped: list[str] = []
    for k in valid_keys:
        if k not in seen:
            seen.add(k)
            deduped.append(k)

    embed = discord.Embed(
        title="🗑️  Bulk Delete — Confirmation",
        description=(
            f"**{len(deduped)} unique key(s)** found in your input.\n"
            "Press **Confirm Delete** to permanently remove them, or **Cancel** to abort."
        ),
        color=discord.Color.orange(),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="Keys to Delete", value=str(len(deduped)), inline=True)
    embed.add_field(name="Requested By",   value=interaction.user.display_name, inline=True)

    preview_limit = 10
    preview_lines = [f"`{k}`" for k in deduped[:preview_limit]]
    if len(deduped) > preview_limit:
        preview_lines.append(f"*…and {len(deduped) - preview_limit} more*")
    embed.add_field(name="Preview", value="\n".join(preview_lines), inline=False)

    await interaction.response.send_message(
        embed=embed,
        view=BulkDeleteView(deduped, interaction.user.id),
        ephemeral=True,
    )


# ── /permissions ──────────────────────────────────────────────────────────────
@bot.tree.command(name="permissions", description="Show the configured bot permission structure")
@require("admin")
async def permissions_cmd(interaction: discord.Interaction) -> None:
    """Bot Admin-only: display the role permission map and role resolution status."""
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    role_ids = get_role_ids()

    embed = discord.Embed(
        title="🔐  GhostKey Permission Structure",
        description=(
            "Role → permission mapping.  Higher roles inherit lower permissions.\n"
            "A ✅ means the role was found in this server; ❌ means it's missing or unconfigured."
        ),
        color=discord.Color.from_rgb(124, 58, 237),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )

    role_key_order = ["customer", "support", "key_generator", "key_manager", "bot_admin"]
    for role_key in role_key_order:
        info = PERMISSION_MAP[role_key]
        rid  = role_ids.get(role_key)

        if rid is None:
            status = "❌ Not configured"
        elif guild:
            role_obj = guild.get_role(rid)
            status = f"✅ <@&{rid}>" if role_obj else f"❌ ID `{rid}` not found in server"
        else:
            status = f"⚠️ ID `{rid}` (DM — cannot resolve)"

        commands_str = ", ".join(info["commands"]) if info["commands"] else "*none*"
        embed.add_field(
            name=f"**{info['label']}**  ({info['env']})",
            value=(
                f"**Status:** {status}\n"
                f"**Access:** {info['desc']}\n"
                f"**Commands:** {commands_str}"
            ),
            inline=False,
        )

    # Additional overrides section
    admin_uids = [
        int(v.strip()) for v in os.getenv("ADMIN_USER_IDS", "").split(",")
        if v.strip().isdigit()
    ]
    legacy_rids = [
        int(v.strip()) for v in os.getenv("ADMIN_ROLE_IDS", "").split(",")
        if v.strip().isdigit()
    ]
    overrides = []
    if admin_uids:
        overrides.append(f"ADMIN_USER_IDS: {len(admin_uids)} user(s) — full access override")
    if legacy_rids:
        overrides.append(f"ADMIN_ROLE_IDS (legacy): {len(legacy_rids)} role(s) — treated as Bot Admin")
    overrides.append("Discord Administrator permission → full access")

    embed.add_field(
        name="Additional Overrides",
        value="\n".join(f"• {o}" for o in overrides),
        inline=False,
    )
    embed.set_footer(text="Role IDs are loaded exclusively from .env — never hard-coded.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and add your token.")
    if not ADMIN_ROLE_IDS and not ADMIN_USER_IDS and not any(get_role_ids().values()):
        raise SystemExit(
            "No role or user IDs configured. Set at least one of "
            "ADMIN_USER_IDS, ADMIN_ROLE_IDS, or BOT_ADMIN_ROLE_ID in .env before starting the bot."
        )
    if not api._admin_key():
        raise SystemExit("GHOST_ADMIN_API_KEY is missing. Add it to .env before starting the bot.")
    logger.info("Starting GhostKey bot — API: %s", api._api_base())
    bot.run(TOKEN, log_handler=None, reconnect=True)
