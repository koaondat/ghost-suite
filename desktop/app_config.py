"""
app_config.py — Central brand / version configuration
======================================================
All user-visible product strings live here.
Change these once; the entire application reflects the update.
"""

# ── Product identity ──────────────────────────────────────────────────────────
PRODUCT_NAME    = "Ghost"
PRODUCT_TAGLINE = "Premium Windows system utility."
DEVELOPER_NAME  = "Ghost Team"
BRAND_NAME      = "Ghost"
COPYRIGHT_YEAR  = "2026"

# ── Version & release ─────────────────────────────────────────────────────────
APP_VERSION     = "1.0.0"
UPDATE_CHANNEL  = "stable"          # "stable" | "beta"

# ── Window ────────────────────────────────────────────────────────────────────
WINDOW_TITLE    = PRODUCT_NAME
WINDOW_MIN_W    = 1060
WINDOW_MIN_H    = 680
WINDOW_DEFAULT  = "1200x760"

# ── Backend API ───────────────────────────────────────────────────────────────
import os

# Production URL is baked in.  Set GHOST_API_URL to override during development.
_PRODUCTION_API_URL = "https://ghost-suite-wp4n.vercel.app"
API_BASE_URL = (
    os.environ.get("GHOST_API_URL", "").strip().rstrip("/")
    or _PRODUCTION_API_URL
)

# ── Session storage ───────────────────────────────────────────────────────────
import sys
from pathlib import Path

_APP_DIR = (
    Path(sys.executable).parent if getattr(sys, "frozen", False)
    else Path(__file__).parent
)
SESSION_FILE  = _APP_DIR / ".ghost_session"
SETTINGS_FILE = _APP_DIR / "ghost_settings.json"
BACKUPS_DIR   = _APP_DIR / "backups"
LOG_FILE      = _APP_DIR / "ghost.log"

# ── Support links ─────────────────────────────────────────────────────────────
DISCORD_URL   = "https://discord.gg/ghost"
SUPPORT_URL   = "https://ghost.gg/support"
PURCHASE_URL  = "https://ghost.gg/purchase"
