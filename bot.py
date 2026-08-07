from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import api_client as api

_ENV_PATH = Path(__file__).with_name(".env")

# Unset any inherited OS-level value so it cannot shadow the .env file.
os.environ.pop("DISCORD_TOKEN", None)

load_dotenv(dotenv_path=_ENV_PATH, override=True)

# ── .env startup guard ────────────────────────────────────────────────────────
if not _ENV_PATH.exists():
    raise SystemExit(
        f"[ghostkey] .env file not found at {_ENV_PATH}\n"
        "Create it from .env.example before starting the bot."
    )
if not os.getenv("DISCORD_TOKEN", "").strip():
    raise SystemExit(
        f"[ghostkey] DISCORD_TOKEN is not set in {_ENV_PATH}\n"
        "Add your bot token and restart."
    )

BASE_DIR = Path(__file__).resolve().parent
AUDIT_LOG = BASE_DIR / "discord_audit_log.json"

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
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


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id in ADMIN_USER_IDS:
        return True
    if isinstance(interaction.user, discord.Member):
        return any(role.id in ADMIN_ROLE_IDS for role in interaction.user.roles)
    return False


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_admin(interaction):
            return True
        raise app_commands.CheckFailure("You do not have permission to use this command.")

    return app_commands.check(predicate)


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


def audit(interaction: discord.Interaction, action: str, target: str, details: str = "") -> None:
    records = read_json(AUDIT_LOG)
    records.append(
        {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "admin_id": interaction.user.id,
            "admin_name": str(interaction.user),
            "action": action,
            "target": target,
            "details": details,
        }
    )
    save_json(AUDIT_LOG, records[-5000:])


def status_text(key_data: dict) -> str:
    """Derive a status string from an API key info dict."""
    if key_data.get("banned"):
        return "Banned"
    if key_data.get("expired"):
        return "Expired"
    if key_data.get("valid"):
        return "Active"
    return "Invalid"


def key_embed_from_data(info: dict) -> discord.Embed:
    """Build a license embed from a /api/admin/license/<key> response."""
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
        # Start the background API health-check loop
        api_health_check.start()

    async def on_disconnect(self) -> None:
        logger.warning("Bot disconnected from Discord gateway — will attempt to reconnect automatically")

    async def on_resumed(self) -> None:
        logger.info("Bot session resumed successfully")


bot = GhostKeyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    await bot.change_presence(activity=discord.Game(name="GHOST license management"))


# ── Background health check against the shared API ────────────────────────────

@tasks.loop(minutes=5)
async def api_health_check() -> None:
    """Ping the shared API /health endpoint every 5 minutes and log the result."""
    try:
        data = await api._request("GET", "/health")
        logger.debug("API health: %s", data)
    except Exception as exc:
        logger.warning("API health check failed: %s", exc)


@api_health_check.before_loop
async def _before_health_check() -> None:
    await bot.wait_until_ready()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        message = str(error) or "You do not have permission to use this command."
    else:
        logger.exception("Slash command failed", exc_info=error)
        message = "The command failed. Check the bot console for details."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# ── License-card button row ───────────────────────────────────────────────────

class LicenseCardView(discord.ui.View):
    """Action buttons attached to every /genkey response."""

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
        try:
            info = await api.key_info(self.key)
            await interaction.response.send_message(embed=key_embed_from_data(info), ephemeral=True)
        except api.APIError as e:
            await interaction.response.send_message(f"⚠️ Could not load key info: {e}", ephemeral=True)

    @discord.ui.button(label="🗑️  Delete Key", style=discord.ButtonStyle.danger)
    async def delete_key(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            result = await api.delete_key(self.key)
            audit(interaction, "delete_key_record", self.key, "via license card button")
            await interaction.response.send_message(
                f"🗑️ Key `{self.key}` has been removed from the database.", ephemeral=True
            )
        except api.APIError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


# ── Bulk-delete confirmation view ────────────────────────────────────────────

class BulkDeleteView(discord.ui.View):
    """Confirm / cancel view for /bulkdelete."""

    def __init__(self, keys: list[str], invoker_id: int) -> None:
        super().__init__(timeout=120)
        self.keys = keys
        self.invoker_id = invoker_id
        self._done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the admin who invoked this command can use these buttons.",
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
@admin_only()
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


@bot.tree.command(name="keyinfo", description="View information about a license key")
@admin_only()
async def keyinfo(interaction: discord.Interaction, key: str) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        info = await api.key_info(key.strip().upper())
        await interaction.followup.send(embed=key_embed_from_data(info), ephemeral=True)
    except api.APIError as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)


@bot.tree.command(name="bankey", description="Ban a license key")
@admin_only()
async def bankey(interaction: discord.Interaction, key: str, reason: app_commands.Range[str, 0, 200] = "") -> None:
    clean = key.strip().upper()
    try:
        await api.ban_key(clean, reason)
        audit(interaction, "ban_key", clean, reason)
        await interaction.response.send_message(f"Banned `{clean}`. Reason: {reason or 'None'}", ephemeral=True)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


@bot.tree.command(name="unbankey", description="Remove a license key ban")
@admin_only()
async def unbankey(interaction: discord.Interaction, key: str) -> None:
    clean = key.strip().upper()
    try:
        result = await api.unban_key(clean)
        if result.get("unbanned"):
            audit(interaction, "unban_key", clean)
        await interaction.response.send_message(result.get("message", "Done."), ephemeral=True)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


@bot.tree.command(name="deletekey", description="Delete a key from the issued-key records")
@admin_only()
async def deletekey(interaction: discord.Interaction, key: str) -> None:
    clean = key.strip().upper()
    try:
        result = await api.delete_key(clean)
        if result.get("deleted"):
            audit(interaction, "delete_key_record", clean)
        await interaction.response.send_message(result.get("message", "Done."), ephemeral=True)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


@bot.tree.command(name="extendkey", description="Extend a key by generating a replacement with new expiry")
@app_commands.describe(key="The key to extend", days="New validity period in days")
@admin_only()
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


@bot.tree.command(name="resetactivation", description="Reset HWID/device binding for a key")
@admin_only()
async def resetactivation(interaction: discord.Interaction, key: str) -> None:
    clean = key.strip().upper()
    try:
        result = await api.reset_activation(clean)
        if result.get("reset"):
            audit(interaction, "reset_activation", clean)
        await interaction.response.send_message(result.get("message", "Done."), ephemeral=True)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


@bot.tree.command(name="listkeys", description="List recently issued keys")
@app_commands.describe(tier="Optional tier filter", limit="Number of keys to display")
@admin_only()
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


@bot.tree.command(name="userinfo", description="View a registered user's license information")
@admin_only()
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


@bot.tree.command(name="deleteuser", description="Delete a registered user account")
@admin_only()
async def deleteuser(interaction: discord.Interaction, username: str) -> None:
    try:
        result = await api.delete_user(username)
        if result.get("deleted"):
            audit(interaction, "delete_user", username)
        await interaction.response.send_message(result.get("message", "Done."), ephemeral=True)
    except api.APIError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)


@bot.tree.command(name="stats", description="Show license-system statistics")
@admin_only()
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
@admin_only()
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


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and add your token.")
    if not ADMIN_ROLE_IDS and not ADMIN_USER_IDS:
        raise SystemExit("Set ADMIN_ROLE_IDS or ADMIN_USER_IDS in .env before starting the bot.")
    if not api.ADMIN_KEY:
        raise SystemExit("GHOST_ADMIN_API_KEY is missing. Add it to .env before starting the bot.")
    logger.info("Starting GhostKey bot — API: %s", api.API_BASE)
    # reconnect=True (default) — discord.py automatically reconnects on
    # temporary gateway drops. Unrecoverable errors (bad token, etc.) raise
    # and are propagated to the process supervisor for restart.
    bot.run(TOKEN, log_handler=None, reconnect=True)
