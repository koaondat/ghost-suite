from __future__ import annotations

# ── Load .env FIRST — before any import that reads os.environ at module level ─
import os
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"

if not _ENV_PATH.exists():
    raise SystemExit(
        f"[ghostkey] .env file not found at {_ENV_PATH}\n"
        "Create it from .env.example before starting the bot."
    )

os.environ.pop("DISCORD_TOKEN", None)
os.environ.pop("GHOST_API_URL", None)
os.environ.pop("GHOST_ADMIN_API_KEY", None)

load_dotenv(dotenv_path=_ENV_PATH, override=True)

# ── Startup guard ─────────────────────────────────────────────────────────────
if not os.getenv("DISCORD_TOKEN", "").strip():
    raise SystemExit(
        f"[ghostkey] DISCORD_TOKEN is not set in {_ENV_PATH}\n"
        "Add your bot token and restart."
    )

# ── Standard imports ──────────────────────────────────────────────────────────
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

BASE_DIR       = Path(__file__).resolve().parent
AUDIT_LOG      = BASE_DIR / "discord_audit_log.json"
BUYER_ROLE_LOG = BASE_DIR / "buyer_role_log.json"

TOKEN    = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)

# ── Channel IDs for event notifications ───────────────────────────────────────
def _channel_id(env_var: str) -> int | None:
    raw = os.getenv(env_var, "").strip()
    return int(raw) if raw.isdigit() else None

PURCHASE_LOG_CHANNEL_ID = _channel_id("PURCHASE_LOG_CHANNEL_ID")
LICENSE_LOG_CHANNEL_ID  = _channel_id("LICENSE_LOG_CHANNEL_ID")

# ── Startup env-var verification ──────────────────────────────────────────────
_REQUIRED_VARS = {
    "DISCORD_TOKEN":       bool(TOKEN),
    "GHOST_API_URL":       bool(os.getenv("GHOST_API_URL", "").strip()),
    "GHOST_ADMIN_API_KEY": bool(os.getenv("GHOST_ADMIN_API_KEY", "").strip()),
    "DISCORD_GUILD_ID":    bool(os.getenv("DISCORD_GUILD_ID", "").strip()),
    "CUSTOMER_ROLE_ID":    bool(os.getenv("CUSTOMER_ROLE_ID", "").strip()),
}
for _var, _present in _REQUIRED_VARS.items():
    print(f"[ghostkey] {_var}: {'SET' if _present else 'MISSING'}")
_missing = [v for v, ok in _REQUIRED_VARS.items() if not ok]
if _missing:
    print(f"[ghostkey] WARNING: {', '.join(_missing)} not found in {_ENV_PATH}")

_DISCORD_GUILD_ID_STR = os.getenv("DISCORD_GUILD_ID", "").strip()
_OAUTH_GUILD_ID = int(_DISCORD_GUILD_ID_STR) if _DISCORD_GUILD_ID_STR.isdigit() else GUILD_ID
_EFFECTIVE_GUILD_ID = _OAUTH_GUILD_ID or GUILD_ID

ADMIN_ROLE_IDS = {
    int(v.strip()) for v in os.getenv("ADMIN_ROLE_IDS", "").split(",") if v.strip().isdigit()
}
ADMIN_USER_IDS = {
    int(v.strip()) for v in os.getenv("ADMIN_USER_IDS", "").split(",") if v.strip().isdigit()
}

# ── Bulk generation limit ─────────────────────────────────────────────────────
BULKGEN_MAX = int(os.getenv("BULKGEN_MAX", "50") or 50)

# ── Logging ───────────────────────────────────────────────────────────────────
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ghostkey.bot")

intents = discord.Intents.default()
intents.members = True


# ── Legacy helpers ────────────────────────────────────────────────────────────
def is_admin(interaction: discord.Interaction) -> bool:
    return has_permission(interaction.user, "admin")

def admin_only():
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


# ── Command audit ─────────────────────────────────────────────────────────────
def audit(interaction: discord.Interaction, action: str, target: str, details: str = "") -> None:
    records = read_json(AUDIT_LOG)
    records.append({
        "timestamp":  dt.datetime.now(dt.timezone.utc).isoformat(),
        "admin_id":   interaction.user.id,
        "admin_name": str(interaction.user),
        "action":     action,
        "target":     target,
        "details":    details,
    })
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
    result: str,
    note: str = "",
) -> None:
    safe_key = ("*" * (len(license_key) - 8) + license_key[-8:]) if len(license_key) > 8 else "***"
    records = read_json(BUYER_ROLE_LOG)
    records.append({
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
    })
    save_json(BUYER_ROLE_LOG, records[-10_000:])


# ── Utility helpers ───────────────────────────────────────────────────────────
def status_text(key_data: dict) -> str:
    if key_data.get("banned") or (key_data.get("status") == "revoked"):
        return "Revoked"
    if key_data.get("expired") or (key_data.get("status") == "expired"):
        return "Expired"
    if key_data.get("valid") or key_data.get("status") in ("sold", "activated", "available"):
        return "Active"
    return "Invalid"

def _mask_key(key: str) -> str:
    """Mask all but last 4 chars of each segment for display."""
    parts = key.split("-")
    if len(parts) < 2:
        return key
    return "-".join("XXXX" if i < len(parts) - 1 else p for i, p in enumerate(parts))

def _format_date(iso: str) -> str:
    """Format ISO date string to 'Mon DD, YYYY'."""
    if not iso:
        return "N/A"
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(iso[:19], fmt[:len(fmt)])
            return d.strftime("%b %d, %Y")
        except Exception:
            continue
    return iso[:10] if len(iso) >= 10 else iso

def _expiry_from_plan(plan: str) -> str:
    """Compute expiry date string from plan slug."""
    days = api.PLAN_DAYS.get(plan)
    if not days:
        return "N/A"
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
    return exp.strftime("%b %d, %Y")

def _duration_label(plan: str) -> str:
    return api.PLAN_DISPLAY.get(plan, plan.title() if plan else "Unknown")


def key_embed_from_data(info: dict) -> discord.Embed:
    """Build a license info embed from an inventory key record or HMAC key info dict."""
    # Support both inventory records and HMAC key_info responses
    lic    = info.get("license") or {}
    record = info.get("record") or {}

    # Inventory key record passed directly
    if info.get("key") and not lic:
        lic    = info
        record = info

    key_val  = lic.get("key") or record.get("key") or info.get("key", "Unknown")
    status   = (lic.get("status") or record.get("status") or "").lower()
    is_valid = lic.get("valid", True) and status not in ("revoked", "expired")

    embed = discord.Embed(
        title="🔑  Ghost License Information",
        color=discord.Color.from_rgb(124, 58, 237) if is_valid else discord.Color.red(),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="Key",        value=f"```{key_val}```",                                     inline=False)
    embed.add_field(name="Status",     value=status_text(lic or record),                              inline=True)
    embed.add_field(name="Plan",       value=_duration_label(record.get("plan") or lic.get("tier", "")), inline=True)
    embed.add_field(name="Created",    value=_format_date(record.get("created_date") or str(lic.get("created") or "")), inline=True)
    embed.add_field(name="Expiration", value=_format_date(record.get("expiration") or str(lic.get("expiry") or "")),    inline=True)
    hwid = record.get("hwid") or ""
    embed.add_field(name="HWID",       value="Bound" if hwid else "Not bound",                        inline=True)
    customer = record.get("customer") or record.get("customer_email") or ""
    if customer:
        embed.add_field(name="Customer",  value=customer[:64],  inline=True)
    order_id = record.get("order_id") or ""
    if order_id:
        embed.add_field(name="Order ID", value=order_id[:32], inline=True)
    discord_id = str(record.get("discord_id") or "")
    if discord_id:
        embed.add_field(name="Discord ID", value=discord_id, inline=True)
    notes = record.get("notes") or record.get("note") or ""
    if notes:
        embed.add_field(name="Notes", value=notes[:200], inline=False)
    if not is_valid and lic.get("error"):
        embed.add_field(name="Validation Message", value=lic["error"], inline=False)
    return embed


_BOT_START_TIME = time.time()


# ── Role hierarchy validation ─────────────────────────────────────────────────
async def validate_role_config(guild: discord.Guild) -> None:
    role_ids = get_role_ids()
    env_names = {
        "customer":      "CUSTOMER_ROLE_ID",
        "key_generator": "KEY_GENERATOR_ROLE_ID",
        "key_manager":   "KEY_MANAGER_ROLE_ID",
        "support":       "SUPPORT_ROLE_ID",
        "bot_admin":     "BOT_ADMIN_ROLE_ID",
    }
    guild_role_ids = {r.id for r in guild.roles}

    if _DISCORD_GUILD_ID_STR:
        if str(guild.id) != _DISCORD_GUILD_ID_STR:
            logger.error(
                "Startup: DISCORD_GUILD_ID=%s does not match connected guild '%s' (id=%d).",
                _DISCORD_GUILD_ID_STR, guild.name, guild.id,
            )
        else:
            logger.info("Startup: DISCORD_GUILD_ID=%s matches guild '%s' ✓", _DISCORD_GUILD_ID_STR, guild.name)
    else:
        logger.warning("Startup: DISCORD_GUILD_ID is not set in .env")

    me = guild.me
    if me:
        if not guild.me.guild_permissions.manage_roles:
            logger.error("Startup: Bot does NOT have 'Manage Roles' permission in guild '%s'.", guild.name)
        else:
            logger.info("Startup: Bot has Manage Roles permission in '%s' ✓", guild.name)

    for key, env_var in env_names.items():
        rid = role_ids.get(key)
        if rid is None:
            logger.warning("Role config: %s is not set in .env", env_var)
        elif rid not in guild_role_ids:
            logger.error("Role config: %s=%d NOT found in guild '%s'.", env_var, rid, guild.name)
        else:
            logger.info("Role config: %s=%d ✓", env_var, rid)

    customer_rid = role_ids.get("customer")
    if customer_rid and customer_rid in guild_role_ids and me:
        customer_role = guild.get_role(customer_rid)
        if customer_role:
            bot_top = me.top_role.position
            if bot_top <= customer_role.position:
                logger.error(
                    "Role hierarchy: Bot role (pos %d) is NOT above Customer role '%s' (pos %d).",
                    bot_top, customer_role.name, customer_role.position,
                )
            else:
                logger.info(
                    "Role hierarchy: Bot role (pos %d) above Customer role '%s' (pos %d) ✓",
                    bot_top, customer_role.name, customer_role.position,
                )


# ── API startup self-check ────────────────────────────────────────────────────
async def api_startup_check() -> None:
    """
    Verify API reachability and bot authentication.  Never prints the key.

    Reports:
      Ghost API: reachable ✓ / UNREACHABLE ✗
      Bot API authentication: ✓ / ✗  (401 = bad/missing key, 403 = wrong key)
      License API: ✓ / ✗
    """
    base = api._api_base()
    # Confirm key is configured without printing it
    key_len = len(api._admin_key())
    key_status = f"SET ({key_len} chars)" if key_len >= 8 else ("SET but very short — check .env" if key_len else "MISSING")
    logger.info("Startup: Ghost API base URL : %s", base)
    logger.info("Startup: GHOST_ADMIN_API_KEY: %s", key_status)

    # ── 1. Liveness probe (no auth required) ────────────────────────────────
    try:
        await api.health_check()
        logger.info("Startup: Ghost API           : reachable ✓  (%s/health)", base)
    except Exception as exc:
        logger.error("Startup: Ghost API           : UNREACHABLE ✗  %s — %s", base, exc)
        return

    # ── 2. Bot API authentication check (read-only, no side effects) ────────
    auth_ok = False
    try:
        await api._request("GET", "/api/admin/inventory/stats", headers=api._admin_headers())
        auth_ok = True
        logger.info("Startup: Bot API authentication: ✓  (Authorization: Bearer accepted)")
    except api.APIError as exc:
        if exc.status == 401:
            logger.error(
                "Startup: Bot API authentication: ✗  401 — key missing or rejected by backend. "
                "Ensure GHOST_ADMIN_API_KEY matches on bot and Vercel/server."
            )
        elif exc.status == 403:
            logger.error(
                "Startup: Bot API authentication: ✗  403 — key recognized but access denied. "
                "Check GHOST_ADMIN_API_KEY value on Vercel."
            )
        elif exc.status == 404:
            logger.warning(
                "Startup: Bot API authentication: ? 404 on /api/admin/inventory/stats "
                "— endpoint may not exist yet; trying fallback."
            )
        else:
            logger.error("Startup: Bot API authentication: ✗  HTTP %d — %s", exc.status, exc)
    except Exception as exc:
        logger.error("Startup: Bot API authentication: ✗  %s", exc)

    # ── 3. License API check ─────────────────────────────────────────────────
    if auth_ok:
        try:
            # Read-only search — returns empty list when nothing matches; never creates data
            await api._request("GET", "/api/admin/inventory", headers=api._admin_headers(),
                               params={"search": "__selftest__"})
            logger.info("Startup: License API         : ✓  (/api/admin/inventory)")
        except api.APIError as exc:
            logger.warning("Startup: License API         : ✗  HTTP %d — %s", exc.status, exc)
        except Exception as exc:
            logger.warning("Startup: License API         : ✗  %s", exc)
    else:
        logger.warning("Startup: License API         : skipped (authentication failed)")

    logger.info("Startup: API self-check complete.")


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
        logger.warning("Bot disconnected from Discord gateway — will attempt to reconnect")

    async def on_resumed(self) -> None:
        logger.info("Bot session resumed successfully")


bot = GhostKeyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    await bot.change_presence(activity=discord.Game(name="GHOST license management"))

    effective_id = _EFFECTIVE_GUILD_ID
    if effective_id:
        guild = bot.get_guild(effective_id)
        if guild:
            await validate_role_config(guild)
        else:
            logger.warning("Guild ID=%d not found — role validation skipped.", effective_id)
    elif bot.guilds:
        await validate_role_config(bot.guilds[0])

    await api_startup_check()


# ── Background: API health check ──────────────────────────────────────────────
@tasks.loop(minutes=5)
async def api_health_check() -> None:
    try:
        await api.health_check()
        logger.debug("API health: ok")
    except Exception as exc:
        logger.warning("API health check failed: %s", exc)

@api_health_check.before_loop
async def _before_health_check() -> None:
    await bot.wait_until_ready()


# ── Background: auto-assign Customer role for verified purchases ──────────────
@tasks.loop(minutes=2)
async def customer_role_task() -> None:
    customer_rid = get_role_ids().get("customer")
    if not customer_rid:
        return

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
        logger.error("customer_role_task: CUSTOMER_ROLE_ID=%d not found in guild.", customer_rid)
        return

    for record in pending:
        order_id       = record.get("order_id", "")
        discord_id_raw = str(record.get("discord_id", "")).strip()
        license_key    = record.get("license_key", "") or ""
        plan           = record.get("plan", "") or ""
        customer_id    = record.get("customer_id", "") or order_id

        if not discord_id_raw.isdigit():
            logger.info("customer_role_task: order=%s has no valid discord_id — skipping.", order_id)
            audit_role_assignment(
                discord_user="unknown", discord_id=discord_id_raw or "none",
                customer_id=customer_id, license_key=license_key or "none",
                order_id=order_id, plan=plan, role_id=customer_rid,
                result="failed:discord_account_not_linked",
                note="discord_id missing or non-numeric",
            )
            continue

        discord_id = int(discord_id_raw)
        member = guild.get_member(discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id)
            except discord.NotFound:
                member = None
            except Exception as exc:
                logger.warning("customer_role_task: fetch_member(%d) failed: %s", discord_id, exc)

        if member is None:
            logger.info("customer_role_task: discord_id=%d not in guild — will retry.", discord_id)
            audit_role_assignment(
                discord_user=f"<id:{discord_id}>", discord_id=discord_id,
                customer_id=customer_id, license_key=license_key or "none",
                order_id=order_id, plan=plan, role_id=customer_rid,
                result="failed:member_not_in_server",
                note="Member not found in guild",
            )
            continue

        discord_user_str = str(member)

        if customer_role in member.roles:
            logger.info("customer_role_task: %s already has Customer role — marking resolved.", discord_user_str)
            audit_role_assignment(
                discord_user=discord_user_str, discord_id=discord_id,
                customer_id=customer_id, license_key=license_key or "none",
                order_id=order_id, plan=plan, role_id=customer_rid,
                result="already_had_role",
            )
            try:
                await api.mark_customer_role_granted(order_id)
            except Exception as exc:
                logger.warning("customer_role_task: could not mark order %s resolved: %s", order_id, exc)
            continue

        try:
            await member.add_roles(customer_role, reason=f"Verified purchase — order {order_id} plan={plan}")
            logger.info("customer_role_task: ✓ Granted Customer role to %s for order %s.", discord_user_str, order_id)
            audit_role_assignment(
                discord_user=discord_user_str, discord_id=discord_id,
                customer_id=customer_id, license_key=license_key or "none",
                order_id=order_id, plan=plan, role_id=customer_rid,
                result="success",
            )
            try:
                await api.mark_customer_role_granted(order_id)
            except Exception as exc:
                logger.warning("customer_role_task: could not mark order %s resolved: %s", order_id, exc)

            # Post purchase notification if channel configured
            await _post_purchase_notification(guild, member, record, customer_role)

        except discord.Forbidden:
            msg = "Bot lacks Manage Roles or role is below Customer role in hierarchy."
            logger.error("customer_role_task: Forbidden assigning role to %s: %s", discord_user_str, msg)
            audit_role_assignment(
                discord_user=discord_user_str, discord_id=discord_id,
                customer_id=customer_id, license_key=license_key or "none",
                order_id=order_id, plan=plan, role_id=customer_rid,
                result="failed:bot_forbidden", note=msg,
            )
        except Exception as exc:
            logger.error("customer_role_task: error granting role to %s: %s", discord_user_str, exc)
            audit_role_assignment(
                discord_user=discord_user_str, discord_id=discord_id,
                customer_id=customer_id, license_key=license_key or "none",
                order_id=order_id, plan=plan, role_id=customer_rid,
                result=f"failed:{type(exc).__name__}", note=str(exc)[:200],
            )


@customer_role_task.before_loop
async def _before_customer_role_task() -> None:
    await bot.wait_until_ready()


# ── Purchase notification helper ──────────────────────────────────────────────
async def _post_purchase_notification(
    guild: discord.Guild,
    member: discord.Member,
    order: dict,
    customer_role: discord.Role,
) -> None:
    if not PURCHASE_LOG_CHANNEL_ID:
        return
    channel = guild.get_channel(PURCHASE_LOG_CHANNEL_ID)
    if not channel or not isinstance(channel, discord.TextChannel):
        return
    try:
        lic_key = order.get("license_key", "")
        masked  = _mask_key(lic_key) if lic_key else "N/A"
        plan    = _duration_label(order.get("plan", ""))
        email   = order.get("customer_id", "") or order.get("email", "")
        # Mask email
        if "@" in email:
            local, domain = email.split("@", 1)
            email = local[:2] + "***@" + domain
        embed = discord.Embed(
            title="🛒  New Purchase",
            color=discord.Color.from_rgb(34, 197, 94),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(name="Customer",  value=email or "N/A",                          inline=True)
        embed.add_field(name="Discord",   value=f"<@{member.id}>",                       inline=True)
        embed.add_field(name="Product",   value="Ghost",                                  inline=True)
        embed.add_field(name="Duration",  value=plan,                                     inline=True)
        embed.add_field(name="Order",     value=order.get("order_id", "N/A")[:20],        inline=True)
        embed.add_field(name="License",   value=f"`{masked}`",                            inline=False)
        embed.add_field(name="Role",      value=f"{customer_role.mention} Assigned ✓",   inline=True)
        await channel.send(embed=embed)
    except Exception as exc:
        logger.warning("_post_purchase_notification failed: %s", exc)


# ── License event log helper ──────────────────────────────────────────────────
async def _post_license_event(event: str, details: str, actor: str = "") -> None:
    if not LICENSE_LOG_CHANNEL_ID:
        return
    guild = bot.get_guild(_EFFECTIVE_GUILD_ID) if _EFFECTIVE_GUILD_ID else (bot.guilds[0] if bot.guilds else None)
    if not guild:
        return
    channel = guild.get_channel(LICENSE_LOG_CHANNEL_ID)
    if not channel or not isinstance(channel, discord.TextChannel):
        return
    try:
        color_map = {
            "generated": discord.Color.from_rgb(124, 58, 237),
            "revoked":   discord.Color.red(),
            "activated": discord.Color.green(),
            "expired":   discord.Color.orange(),
            "reset":     discord.Color.blurple(),
        }
        color = next((v for k, v in color_map.items() if k in event.lower()), discord.Color.greyple())
        embed = discord.Embed(
            title=f"📋  License Event: {event}",
            description=details,
            color=color,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        if actor:
            embed.set_footer(text=f"Actor: {actor}")
        await channel.send(embed=embed)
    except Exception as exc:
        logger.warning("_post_license_event failed: %s", exc)


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
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return
        try:
            # Try inventory key first, then HMAC key
            try:
                info = await api.inventory_key_info(self.key)
                embed = key_embed_from_data(info.get("key") or info)
            except api.APIError:
                info = await api.key_info(self.key)
                embed = key_embed_from_data(info)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except api.APIError as e:
            await interaction.response.send_message(f"⚠️ Could not load key info: {e}", ephemeral=True)

    @discord.ui.button(label="🗑️  Revoke Key", style=discord.ButtonStyle.danger)
    async def revoke_key_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not has_permission(interaction.user, "manage_keys"):
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return
        try:
            await api.revoke_inventory_key(self.key)
            audit(interaction, "revoke_key", self.key, "via license card button")
            await _post_license_event("Key Revoked", f"Key: `{self.key}`\nRevoked by: {interaction.user}", str(interaction.user))
            await interaction.response.send_message(f"🚫 Key `{self.key}` has been revoked.", ephemeral=True)
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
            await interaction.response.send_message("Only the invoker can use these buttons.", ephemeral=True)
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
            audit(interaction, "bulk_delete_keys", f"{len(succeeded)} key(s)",
                  f"deleted={','.join(succeeded)}; not_found={','.join(not_found)}")
        embed = discord.Embed(
            title="🗑️  Bulk Delete — Complete",
            color=discord.Color.dark_red() if not_found else discord.Color.dark_teal(),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(name="✅ Deleted",   value=str(len(succeeded)), inline=True)
        embed.add_field(name="❌ Not Found", value=str(len(not_found)), inline=True)
        if not_found:
            embed.add_field(name="Keys Not Found", value="\n".join(f"`{k}`" for k in not_found), inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🚫  Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._done:
            await interaction.response.send_message("Already processed.", ephemeral=True)
            return
        self._done = True
        self._disable_all()
        embed = discord.Embed(title="🚫  Bulk Delete Cancelled", description="No keys were deleted.", color=discord.Color.greyple())
        await interaction.response.edit_message(embed=embed, view=self)

    def _disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


# =============================================================================
# SLASH COMMANDS
# =============================================================================

# ── /genkey ───────────────────────────────────────────────────────────────────
# Uses the CURRENT inventory key system: POST /api/admin/inventory/generate
# Plans match the website checkout: 1 Day | 3 Days | 1 Week | 30 Days | 3 Months

@bot.tree.command(name="genkey", description="Generate a Ghost license key using the current plan system")
@app_commands.describe(
    duration="Access duration",
    note="Optional internal note attached to the key",
)
@app_commands.choices(duration=[
    app_commands.Choice(name="1 Day",    value="day"),
    app_commands.Choice(name="3 Days",   value="3days"),
    app_commands.Choice(name="1 Week",   value="week"),
    app_commands.Choice(name="30 Days",  value="month"),
    app_commands.Choice(name="3 Months", value="3months"),
])
@require("generate")
async def genkey(
    interaction: discord.Interaction,
    duration: str = "month",
    note: app_commands.Range[str, 0, 200] = "",
) -> None:
    await interaction.response.defer(ephemeral=True)

    plan_label = _duration_label(duration)
    expiry_str = _expiry_from_plan(duration)
    today_str  = dt.datetime.now(dt.timezone.utc).strftime("%b %d, %Y")

    try:
        generated = await api.generate_inventory_keys(plan=duration, quantity=1, notes=note)
    except api.APIError as e:
        logger.error("/genkey failed: endpoint=%s status=%d error=%s", e.endpoint, e.status, e)
        await interaction.followup.send(
            f"⚠️ **Failed to generate key**\n"
            f"```\nEndpoint : {e.endpoint or '/api/admin/inventory/generate'}\n"
            f"Status   : {e.status}\n"
            f"Error    : {e}\n```",
            ephemeral=True,
        )
        return

    if not generated:
        await interaction.followup.send("⚠️ No keys were returned by the server.", ephemeral=True)
        return

    new_key = generated[0]
    audit(interaction, "generate_key", new_key, f"plan={duration}; note={note}")
    await _post_license_event(
        "Key Generated",
        f"Key: `{new_key}`\nPlan: {plan_label}\nExpiry: {expiry_str}\nNote: {note or 'none'}\nGenerated by: {interaction.user}",
        str(interaction.user),
    )

    embed = discord.Embed(
        title="👻  License Created",
        description="Your Ghost license key has been generated successfully.",
        color=discord.Color.from_rgb(124, 58, 237),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="🔑  Key",           value=f"```{new_key}```",            inline=False)
    embed.add_field(name="📋  Duration",       value=plan_label,                    inline=True)
    embed.add_field(name="📅  Expiration",     value=expiry_str,                    inline=True)
    embed.add_field(name="\u200b",             value="\u200b",                      inline=True)
    embed.add_field(name="✅  Status",          value="🟢 Active",                  inline=True)
    embed.add_field(name="👤  Created By",      value=f"<@{interaction.user.id}>",  inline=True)
    embed.add_field(name="\u200b",             value="\u200b",                      inline=True)
    embed.add_field(name="📝  Note",           value=note if note else "*None*",    inline=False)
    embed.set_footer(text=f"Ghost License System  •  {today_str}")
    await interaction.followup.send(embed=embed, view=LicenseCardView(new_key), ephemeral=True)


# ── /bulkgen ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="bulkgen", description="Bulk-generate Ghost license keys")
@app_commands.describe(
    duration="Access duration for all keys",
    amount="Number of keys to generate (max configured limit)",
    note="Optional internal note attached to all keys",
)
@app_commands.choices(duration=[
    app_commands.Choice(name="1 Day",    value="day"),
    app_commands.Choice(name="3 Days",   value="3days"),
    app_commands.Choice(name="1 Week",   value="week"),
    app_commands.Choice(name="30 Days",  value="month"),
    app_commands.Choice(name="3 Months", value="3months"),
])
@require("generate")
async def bulkgen(
    interaction: discord.Interaction,
    duration: str = "month",
    amount: app_commands.Range[int, 1, 100] = 5,
    note: app_commands.Range[str, 0, 200] = "",
) -> None:
    await interaction.response.defer(ephemeral=True)

    # Enforce configured max
    safe_amount = min(amount, BULKGEN_MAX)
    plan_label  = _duration_label(duration)
    expiry_str  = _expiry_from_plan(duration)
    today_str   = dt.datetime.now(dt.timezone.utc).strftime("%b %d, %Y")

    try:
        generated = await api.generate_inventory_keys(plan=duration, quantity=safe_amount, notes=note)
    except api.APIError as e:
        logger.error("/bulkgen failed: endpoint=%s status=%d error=%s", e.endpoint, e.status, e)
        await interaction.followup.send(
            f"⚠️ **Failed to generate keys**\n"
            f"```\nEndpoint : {e.endpoint or '/api/admin/inventory/generate'}\n"
            f"Status   : {e.status}\n"
            f"Error    : {e}\n```",
            ephemeral=True,
        )
        return

    if not generated:
        await interaction.followup.send("⚠️ No keys were returned by the server.", ephemeral=True)
        return

    audit(
        interaction, "bulk_generate_keys", f"{len(generated)} key(s)",
        f"plan={duration}; note={note}; keys={','.join(generated[:10])}{'…' if len(generated) > 10 else ''}",
    )
    await _post_license_event(
        "Bulk Keys Generated",
        f"Count: {len(generated)}\nPlan: {plan_label}\nExpiry: {expiry_str}\nNote: {note or 'none'}\nGenerated by: {interaction.user}",
        str(interaction.user),
    )

    # Always attach a downloadable .txt file for bulk
    txt_lines = [
        f"Ghost Bulk Key Generation — {len(generated)} keys",
        f"Duration: {plan_label}  |  Expiration: {expiry_str}  |  Note: {note or 'none'}",
        f"Generated by: {interaction.user} on {today_str}",
        "-" * 56,
    ] + generated
    txt_bytes = "\n".join(txt_lines).encode("utf-8")
    file = discord.File(io.BytesIO(txt_bytes), filename=f"ghost_keys_{duration}_{len(generated)}.txt")

    embed = discord.Embed(
        title=f"👻  Bulk Generation  ({len(generated)} keys)",
        description=(
            f"All **{len(generated)}** keys have been generated.\n"
            f"**Duration:** {plan_label}   •   **Expires:** {expiry_str}\n"
            f"**Note:** {note if note else '*none*'}\n\n"
            "📎 Full key list attached as a text file."
        ),
        color=discord.Color.from_rgb(124, 58, 237),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="🔑  First Key",     value=f"```{generated[0]}```",          inline=False)
    if len(generated) > 1:
        embed.add_field(name="🔑  Last Key",  value=f"```{generated[-1]}```",          inline=False)
    embed.add_field(name="📋  Duration",      value=plan_label,                         inline=True)
    embed.add_field(name="📅  Expiration",    value=expiry_str,                         inline=True)
    embed.add_field(name="👤  Generated By",  value=interaction.user.display_name,      inline=True)
    embed.set_footer(text=f"Ghost License System  •  {today_str}")
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)


# ── /lookup ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="lookup", description="Look up a Ghost license key")
@app_commands.describe(key="The license key to look up")
@require("support")
async def lookup(interaction: discord.Interaction, key: str) -> None:
    await interaction.response.defer(ephemeral=True)
    clean = key.strip().upper()
    try:
        # Try inventory key first (current system)
        try:
            data = await api.inventory_key_info(clean)
            inv_rec = data.get("key") or data  # api returns { ok, key } or the record directly
            embed = key_embed_from_data(inv_rec)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except api.APIError as inv_err:
            if inv_err.status == 404:
                # Not in inventory — try HMAC key
                info = await api.key_info(clean)
                embed = key_embed_from_data(info)
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                raise
    except api.APIError as e:
        logger.error("/lookup failed: endpoint=%s status=%d", e.endpoint, e.status)
        await interaction.followup.send(
            f"⚠️ Could not retrieve key info\n"
            f"```\nEndpoint : {e.endpoint}\nStatus   : {e.status}\nError    : {e}\n```",
            ephemeral=True,
        )


# ── /licenseinfo (alias for /lookup) ─────────────────────────────────────────
@bot.tree.command(name="licenseinfo", description="View full license details for a key")
@app_commands.describe(key="The license key to inspect")
@require("support")
async def licenseinfo(interaction: discord.Interaction, key: str) -> None:
    await interaction.response.defer(ephemeral=True)
    clean = key.strip().upper()
    try:
        try:
            data = await api.inventory_key_info(clean)
            inv_rec = data.get("key") or data
            embed = key_embed_from_data(inv_rec)
        except api.APIError as inv_err:
            if inv_err.status == 404:
                info = await api.key_info(clean)
                embed = key_embed_from_data(info)
            else:
                raise
        await interaction.followup.send(embed=embed, ephemeral=True)
    except api.APIError as e:
        await interaction.followup.send(
            f"⚠️ Key not found or error fetching info.\n"
            f"```Endpoint: {e.endpoint}\nStatus: {e.status}\nError: {e}```",
            ephemeral=True,
        )


# ── /revoke ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="revoke", description="Revoke a Ghost license key (blocks desktop activation)")
@app_commands.describe(key="The license key to revoke")
@require("manage_keys")
async def revoke(interaction: discord.Interaction, key: str) -> None:
    clean = key.strip().upper()
    await interaction.response.defer(ephemeral=True)
    try:
        result = await api.revoke_inventory_key(clean)
        audit(interaction, "revoke_key", clean, "via /revoke command")
        await _post_license_event(
            "Key Revoked",
            f"Key: `{clean}`\nRevoked by: {interaction.user}",
            str(interaction.user),
        )
        embed = discord.Embed(
            title="🚫  License Revoked",
            color=discord.Color.red(),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(name="Key",       value=f"```{clean}```",           inline=False)
        embed.add_field(name="Status",    value="🔴 Revoked",               inline=True)
        embed.add_field(name="Revoked By", value=f"<@{interaction.user.id}>", inline=True)
        embed.set_footer(text="Desktop activation is now blocked for this key.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except api.APIError as e:
        logger.error("/revoke failed: endpoint=%s status=%d error=%s", e.endpoint, e.status, e)
        await interaction.followup.send(
            f"⚠️ Failed to revoke key\n"
            f"```\nEndpoint: {e.endpoint}\nStatus: {e.status}\nError: {e}\n```",
            ephemeral=True,
        )


# ── /keyinfo ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="keyinfo", description="View information about a license key")
@require("support")
async def keyinfo(interaction: discord.Interaction, key: str) -> None:
    await interaction.response.defer(ephemeral=True)
    clean = key.strip().upper()
    try:
        try:
            data = await api.inventory_key_info(clean)
            embed = key_embed_from_data(data.get("key") or data)
        except api.APIError as inv_err:
            if inv_err.status == 404:
                info = await api.key_info(clean)
                embed = key_embed_from_data(info)
            else:
                raise
        await interaction.followup.send(embed=embed, ephemeral=True)
    except api.APIError as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)


# ── /bankey ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="bankey", description="Ban a license key (HMAC key system)")
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
@bot.tree.command(name="extendkey", description="Extend a license key expiry")
@app_commands.describe(key="The key to extend", days="Additional days")
@require("manage_keys")
async def extendkey(
    interaction: discord.Interaction,
    key: str,
    days: app_commands.Range[int, 1, 3650] = 30,
) -> None:
    clean = key.strip().upper()
    # Try inventory extension first, then HMAC extension
    try:
        try:
            result = await api.extend_inventory_key(clean, days)
            audit(interaction, "extend_key", clean, f"days={days}")
            await interaction.response.send_message(
                f"✅ Extended `{clean}` by {days} day(s).\nNew expiry: `{result.get('expiration', 'N/A')}`",
                ephemeral=True,
            )
        except api.APIError as inv_err:
            if inv_err.status == 404:
                result = await api.extend_key(clean, days)
                audit(interaction, "extend_key", clean, f"replacement={result.get('new_key')} days={days}")
                await interaction.response.send_message(
                    f"Extended `{clean}`.\n🔑 Replacement key: ```{result.get('new_key')}```",
                    ephemeral=True,
                )
            else:
                raise
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


# ── /resetactivation ──────────────────────────────────────────────────────────
@bot.tree.command(name="resetactivation", description="Reset HWID/device binding for a key")
@require("manage_keys")
async def resetactivation(interaction: discord.Interaction, key: str) -> None:
    clean = key.strip().upper()
    try:
        # Try inventory HWID reset first, then HMAC reset
        try:
            await api.reset_inventory_hwid(clean)
            audit(interaction, "reset_hwid", clean)
            await _post_license_event("HWID Reset", f"Key: `{clean}`\nReset by: {interaction.user}", str(interaction.user))
            await interaction.response.send_message(f"✅ HWID reset for `{clean}`.", ephemeral=True)
        except api.APIError as inv_err:
            if inv_err.status == 404:
                result = await api.reset_activation(clean)
                if result.get("reset"):
                    audit(interaction, "reset_activation", clean)
                await interaction.response.send_message(result.get("message", "Done."), ephemeral=True)
            else:
                raise
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


# ── /listkeys ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="listkeys", description="List recently issued license keys")
@app_commands.describe(
    filter_status="Filter by key status",
    filter_plan="Filter by plan/duration",
    limit="Number of keys to display (max 20)",
)
@app_commands.choices(filter_status=[
    app_commands.Choice(name="All",       value=""),
    app_commands.Choice(name="Available", value="available"),
    app_commands.Choice(name="Sold",      value="sold"),
    app_commands.Choice(name="Activated", value="activated"),
    app_commands.Choice(name="Revoked",   value="revoked"),
    app_commands.Choice(name="Expired",   value="expired"),
])
@require("support")
async def listkeys(
    interaction: discord.Interaction,
    filter_status: str = "",
    filter_plan: str = "",
    limit: app_commands.Range[int, 1, 20] = 10,
) -> None:
    try:
        records = await api.list_inventory_keys(
            status=filter_status or None,
            plan=filter_plan or None,
        )
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return

    if not records:
        await interaction.response.send_message("No matching keys found.", ephemeral=True)
        return

    display = records[-limit:]
    lines = []
    for r in reversed(display):
        k = r.get("key", "Unknown")
        s = r.get("status", "?")
        p = _duration_label(r.get("plan", ""))
        lines.append(f"`{k}` • {p} • {s.title()}")

    embed = discord.Embed(
        title=f"Ghost License Keys ({len(display)} shown / {len(records)} total)",
        description="\n".join(lines),
        color=discord.Color.blurple(),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
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
    embed.add_field(name="Tier",    value=user.get("tier", "Unknown"), inline=True)
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
@bot.tree.command(name="stats", description="Show real license system statistics")
@require("admin")
async def stats(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        s = await api.stats()
    except api.APIError as e:
        await interaction.followup.send(f"⚠️ Could not fetch stats: {e}", ephemeral=True)
        return

    # Also get inventory stats if available
    try:
        inv_data = await api._request("GET", "/api/admin/inventory/stats", headers=api._admin_headers())
    except Exception:
        inv_data = {}

    # Also get order count
    try:
        orders_data = await api._request("GET", "/api/admin/orders", headers=api._admin_headers())
        order_list = orders_data.get("orders", [])
        orders_today   = sum(1 for o in order_list if (o.get("created_at") or "")[:10] == dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"))
        orders_month   = sum(1 for o in order_list if (o.get("created_at") or "")[:7] == dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"))
        discord_linked = sum(1 for o in order_list if o.get("discord_id"))
        role_pending   = sum(1 for o in order_list if not o.get("discord_role_granted") and o.get("discord_id"))
    except Exception:
        orders_today = orders_month = discord_linked = role_pending = 0
        order_list = []

    # Inventory stats
    inv_active  = inv_data.get("activated", 0) + inv_data.get("sold", 0)
    inv_expired = inv_data.get("expired", 0)
    inv_revoked = inv_data.get("revoked", 0)
    inv_sold    = inv_data.get("sold", 0)

    embed = discord.Embed(
        title="📊  Ghost License Statistics",
        color=discord.Color.from_rgb(124, 58, 237),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="🟢 Active Licenses",  value=str(s.get("active", inv_active)),   inline=True)
    embed.add_field(name="⏰ Expired",          value=str(s.get("expired", inv_expired)),  inline=True)
    embed.add_field(name="🚫 Revoked",          value=str(s.get("banned", inv_revoked)),   inline=True)
    embed.add_field(name="🛒 Sold",             value=str(inv_sold or s.get("total_keys", 0)), inline=True)
    embed.add_field(name="🔗 Discord Linked",   value=str(discord_linked),                 inline=True)
    embed.add_field(name="⏳ Role Pending",     value=str(role_pending),                   inline=True)
    embed.add_field(name="📦 Orders Today",     value=str(orders_today),                   inline=True)
    embed.add_field(name="📦 Orders This Month",value=str(orders_month),                   inline=True)
    embed.add_field(name="👤 Registered Users", value=str(s.get("users", 0)),              inline=True)
    embed.set_footer(text="Data sourced live from the Ghost backend API")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /bulkdelete ───────────────────────────────────────────────────────────────
@bot.tree.command(name="bulkdelete", description="Delete multiple license keys at once")
@app_commands.describe(keys="Paste keys separated by commas or new lines")
@require("manage_keys")
async def bulkdelete(interaction: discord.Interaction, keys: str) -> None:
    raw_keys   = re.split(r"[,\n\r]+", keys)
    valid_keys = [k.strip().upper() for k in raw_keys if k.strip()]

    if not valid_keys:
        await interaction.response.send_message("No valid keys found in input.", ephemeral=True)
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
            f"**{len(deduped)} unique key(s)** found.\n"
            "Press **Confirm Delete** to permanently remove them."
        ),
        color=discord.Color.orange(),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="Keys to Delete", value=str(len(deduped)),                  inline=True)
    embed.add_field(name="Requested By",   value=interaction.user.display_name,       inline=True)
    preview_limit = 10
    preview_lines = [f"`{k}`" for k in deduped[:preview_limit]]
    if len(deduped) > preview_limit:
        preview_lines.append(f"*…and {len(deduped) - preview_limit} more*")
    embed.add_field(name="Preview", value="\n".join(preview_lines), inline=False)
    await interaction.response.send_message(embed=embed, view=BulkDeleteView(deduped, interaction.user.id), ephemeral=True)


# ── /customer ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="customer", description="View customer info for a Discord user")
@app_commands.describe(user="The Discord user to look up")
@require("support")
async def customer_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
) -> None:
    await interaction.response.defer(ephemeral=True)

    discord_id = str(user.id)
    role_ids   = get_role_ids()
    customer_rid = role_ids.get("customer")
    has_customer_role = customer_rid and any(r.id == customer_rid for r in user.roles)

    # Find the most recent order linked to this Discord user
    order = await api.find_customer_by_discord_id(user.id)

    embed = discord.Embed(
        title=f"👤  Customer Profile",
        color=discord.Color.from_rgb(124, 58, 237),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="Discord User",   value=f"{user.mention} (`{user.id}`)",              inline=False)
    embed.add_field(name="Server Member",  value="✅ Yes",                                       inline=True)
    embed.add_field(name="Customer Role",  value="✅ Assigned" if has_customer_role else "❌ Not assigned", inline=True)

    if order:
        email = order.get("email", "N/A")
        if "@" in email:
            local, domain = email.split("@", 1)
            email = local[:2] + "***@" + domain
        linked_at = _format_date(order.get("discord_linked_at") or order.get("created_at", ""))
        plan      = _duration_label(order.get("plan", ""))
        lic_key   = order.get("license_key", "")
        # Fetch expiry from inventory if we have a key
        expiry = "N/A"
        if lic_key:
            try:
                inv = await api.inventory_key_info(lic_key)
                rec = inv.get("key") or inv
                expiry = _format_date(rec.get("expiration", ""))
            except Exception:
                pass

        # Count all orders for this discord_id
        try:
            all_orders = await api.list_orders()
            order_count = sum(1 for o in all_orders if str(o.get("discord_id", "")) == discord_id)
            last_purchase = max(
                (o.get("created_at", "") for o in all_orders if str(o.get("discord_id", "")) == discord_id),
                default="",
            )
        except Exception:
            order_count   = 1
            last_purchase = order.get("created_at", "")

        embed.add_field(name="Website Account", value=email,                              inline=True)
        embed.add_field(name="Discord Linked",  value="✅ Yes",                            inline=True)
        embed.add_field(name="Linked At",       value=linked_at,                           inline=True)
        embed.add_field(name="Active License",  value=f"`{lic_key[:20]}…`" if len(lic_key) > 20 else f"`{lic_key}`" if lic_key else "None", inline=True)
        embed.add_field(name="Plan",            value=plan,                                inline=True)
        embed.add_field(name="Expires",         value=expiry,                              inline=True)
        embed.add_field(name="Order Count",     value=str(order_count),                    inline=True)
        embed.add_field(name="Last Purchase",   value=_format_date(last_purchase),         inline=True)
    else:
        embed.add_field(name="Website Account", value="Not linked / No orders found",     inline=False)
        embed.add_field(name="Discord Linked",  value="❌ No",                             inline=True)

    embed.set_footer(text="Sensitive data masked. Discord IDs used — not usernames.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /order ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="order", description="Look up an order by ID")
@app_commands.describe(order_id="The order ID to look up")
@require("support")
async def order_cmd(
    interaction: discord.Interaction,
    order_id: str,
) -> None:
    await interaction.response.defer(ephemeral=True)

    record = await api.get_order(order_id.strip())
    if not record:
        await interaction.followup.send(f"⚠️ Order `{order_id}` not found.", ephemeral=True)
        return

    email = record.get("email", "N/A")
    if "@" in email:
        local, domain = email.split("@", 1)
        email = local[:2] + "***@" + domain

    discord_id  = str(record.get("discord_id") or "Not linked")
    lic_key     = record.get("license_key", "")
    plan        = _duration_label(record.get("plan", ""))
    pstatus     = (record.get("payment_status") or "Unknown").title()
    dstatus     = (record.get("delivery_status") or "Unknown").title()
    created_at  = _format_date(record.get("created_at", ""))
    role_ok     = "✅ Assigned" if record.get("discord_role_granted") else "⏳ Pending"

    embed = discord.Embed(
        title=f"🧾  Order Details",
        color=discord.Color.from_rgb(124, 58, 237),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="Order ID",        value=f"`{record.get('order_id', 'N/A')}`", inline=False)
    embed.add_field(name="Customer",        value=email,                                 inline=True)
    embed.add_field(name="Discord ID",      value=discord_id,                            inline=True)
    embed.add_field(name="Product",         value="Ghost",                               inline=True)
    embed.add_field(name="Duration",        value=plan,                                  inline=True)
    embed.add_field(name="Payment Status",  value=pstatus,                               inline=True)
    embed.add_field(name="Delivery Status", value=dstatus,                               inline=True)
    embed.add_field(name="Purchase Date",   value=created_at,                            inline=True)
    embed.add_field(name="License",         value=f"`{lic_key}`" if lic_key else "None", inline=False)
    embed.add_field(name="Discord Role",    value=role_ok,                               inline=True)
    price = record.get("price_usd")
    if price:
        embed.add_field(name="Amount", value=f"${float(price):.2f}", inline=True)
    embed.set_footer(text="Customer email masked.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /sync ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="sync", description="Sync and repair a user's Discord role and license status")
@app_commands.describe(user="The Discord user to sync")
@require("support")
async def sync_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
) -> None:
    await interaction.response.defer(ephemeral=True)

    role_ids     = get_role_ids()
    customer_rid = role_ids.get("customer")
    guild        = interaction.guild
    results: dict[str, str] = {}

    # 1. Check Discord link
    order = await api.find_customer_by_discord_id(user.id)
    results["Discord Linked"] = "✅ Yes" if order else "❌ No order found for this Discord ID"

    # 2. Check server membership
    results["Server Member"] = "✅ Yes"  # They must be in the server to use the command

    # 3. Check active license
    license_active = False
    if order and order.get("license_key"):
        lic_key = order["license_key"]
        try:
            inv = await api.inventory_key_info(lic_key)
            rec = inv.get("key") or inv
            status = (rec.get("status") or "").lower()
            license_active = status in ("sold", "activated", "available")
            results["License Active"] = f"✅ Yes ({status.title()})" if license_active else f"❌ No ({status.title()})"
        except Exception:
            results["License Active"] = "⚠️ Could not verify"
    else:
        results["License Active"] = "❌ No license found"

    # 4. Check Customer role
    has_customer_role = customer_rid and any(r.id == customer_rid for r in user.roles)
    results["Customer Role"] = "✅ Assigned" if has_customer_role else "❌ Not assigned"

    # 5. Repair missing role if eligible
    repaired = False
    if not has_customer_role and license_active and customer_rid and guild:
        customer_role = guild.get_role(customer_rid)
        if customer_role:
            try:
                await user.add_roles(customer_role, reason=f"/sync repair by {interaction.user}")
                results["Customer Role"] = "✅ Repaired ← Role added by /sync"
                repaired = True
                audit(interaction, "sync_repair_role", str(user.id), f"added Customer role via /sync")
                await _post_license_event(
                    "Role Sync Repaired",
                    f"User: <@{user.id}>\nRepaired by: {interaction.user}",
                    str(interaction.user),
                )
            except discord.Forbidden:
                results["Customer Role"] = "❌ Bot lacks permission to assign role"
            except Exception as exc:
                results["Customer Role"] = f"❌ Error: {exc}"

    embed = discord.Embed(
        title=f"🔄  Role Sync — <@{user.id}>",
        description=f"Sync check for {user.mention}",
        color=discord.Color.green() if repaired else discord.Color.from_rgb(124, 58, 237),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    for label, value in results.items():
        embed.add_field(name=label, value=value, inline=False)
    if repaired:
        embed.set_footer(text="✅ Missing Customer role was repaired automatically.")
    else:
        embed.set_footer(text="No changes made.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /status ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="status", description="Check Ghost system status (real checks — no faked values)")
@require("support")
async def status_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    uptime_secs = int(time.time() - _BOT_START_TIME)
    uptime_str  = f"{uptime_secs // 3600}h {(uptime_secs % 3600) // 60}m"

    # Check Website/API
    api_ok    = False
    api_msg   = "❌ Offline"
    lic_ok    = False
    lic_msg   = "❌ Offline"
    db_ok     = False
    db_msg    = "❌ Unknown"
    version   = "Unknown"

    try:
        hc = await api.health_check()
        api_ok  = True
        api_msg = "✅ Online"
    except Exception as exc:
        api_msg = f"❌ {str(exc)[:60]}"

    if api_ok:
        try:
            sc = await api.status_check()
            db_ok  = sc.get("ok", False)
            db_msg = "✅ Online" if db_ok else "⚠️ Degraded"
            version = sc.get("version", "Unknown")
        except Exception as exc:
            db_msg = f"❌ {str(exc)[:60]}"

        try:
            await api._request("GET", "/api/admin/inventory/stats", headers=api._admin_headers())
            lic_ok  = True
            lic_msg = "✅ Online"
        except Exception as exc:
            lic_msg = f"❌ {str(exc)[:60]}"

    # Discord role sync
    role_ids = get_role_ids()
    customer_rid = role_ids.get("customer")
    sync_ok  = False
    sync_msg = "❌ CUSTOMER_ROLE_ID not configured"
    if customer_rid:
        guild = bot.get_guild(_EFFECTIVE_GUILD_ID) if _EFFECTIVE_GUILD_ID else None
        if guild:
            role_obj = guild.get_role(customer_rid)
            sync_ok  = role_obj is not None
            sync_msg = "✅ Ready" if sync_ok else f"❌ Role ID {customer_rid} not found in guild"
        else:
            sync_msg = "⚠️ Guild not found"

    embed = discord.Embed(
        title="🌐  Ghost System Status",
        color=discord.Color.green() if (api_ok and lic_ok) else discord.Color.orange(),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="🌍 Website API",         value=api_msg,  inline=True)
    embed.add_field(name="🔑 License API",          value=lic_msg,  inline=True)
    embed.add_field(name="🗄️ Database",             value=db_msg,   inline=True)
    embed.add_field(name="🤖 Bot",                  value="✅ Online", inline=True)
    embed.add_field(name="🔗 Discord Role Sync",    value=sync_msg, inline=True)
    embed.add_field(name="📦 Current Version",      value=version,  inline=True)
    embed.add_field(name="⏱️ Bot Uptime",           value=uptime_str, inline=True)
    embed.set_footer(text="All checks are real — no faked values.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /appstatus ────────────────────────────────────────────────────────────────
@bot.tree.command(name="appstatus", description="Show Ghost desktop app version and update status")
@require("support")
async def appstatus(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    release = await api.get_latest_release()
    dl_info = await api.get_download_info()

    version     = release.get("version") or dl_info.get("current_version", "Unknown")
    released_at = _format_date(release.get("releasedAt") or dl_info.get("release_date", ""))
    min_version = release.get("minVersion") or "N/A"
    dl_url      = release.get("downloadUrl") or dl_info.get("download_url", "")
    mandatory   = release.get("mandatory", False)
    notes       = release.get("releaseNotes") or release.get("release_notes") or []
    if isinstance(notes, list):
        notes_str = "\n".join(f"• {n}" for n in notes[:5]) if notes else "No release notes"
    else:
        notes_str = str(notes)[:300]

    available = "✅ Available" if dl_url else "⚠️ Not configured"

    embed = discord.Embed(
        title="🖥️  Ghost Desktop App Status",
        color=discord.Color.from_rgb(124, 58, 237),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="Current Version",    value=version,             inline=True)
    embed.add_field(name="Minimum Supported",  value=min_version,         inline=True)
    embed.add_field(name="Release Date",       value=released_at,         inline=True)
    embed.add_field(name="Download",           value=available,           inline=True)
    embed.add_field(name="Mandatory Update",   value="⚠️ Yes" if mandatory else "✅ No", inline=True)
    if notes_str:
        embed.add_field(name="Release Notes",  value=notes_str,           inline=False)
    embed.set_footer(text="Data sourced from the Ghost release API.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /permissions ──────────────────────────────────────────────────────────────
@bot.tree.command(name="permissions", description="Show the configured bot permission structure")
@require("admin")
async def permissions_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    guild    = interaction.guild
    role_ids = get_role_ids()

    embed = discord.Embed(
        title="🔐  Ghost Bot Permission Structure",
        description=(
            "Role → permission mapping.  Higher roles inherit lower permissions.\n"
            "✅ = role found in this server  ❌ = missing or unconfigured."
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
            value=f"**Status:** {status}\n**Access:** {info['desc']}\n**Commands:** {commands_str}",
            inline=False,
        )

    admin_uids  = [int(v.strip()) for v in os.getenv("ADMIN_USER_IDS", "").split(",") if v.strip().isdigit()]
    legacy_rids = [int(v.strip()) for v in os.getenv("ADMIN_ROLE_IDS", "").split(",") if v.strip().isdigit()]
    overrides = []
    if admin_uids:
        overrides.append(f"ADMIN_USER_IDS: {len(admin_uids)} user(s) — full access override")
    if legacy_rids:
        overrides.append(f"ADMIN_ROLE_IDS (legacy): {len(legacy_rids)} role(s) — treated as Bot Admin")
    overrides.append("Discord Administrator permission → full access")
    embed.add_field(name="Additional Overrides", value="\n".join(f"• {o}" for o in overrides), inline=False)
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
