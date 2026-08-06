#!/usr/bin/env bash
# deploy/start-bot.sh — Start the Ghost Discord bot
# --------------------------------------------------
# Runs bot.py directly.  The process supervisor (Docker restart policy,
# systemd, or PM2) is responsible for restarting on non-zero exit.
# discord.py handles transient gateway disconnects internally via
# reconnect=True; only fatal errors (bad token, etc.) will exit.
# --------------------------------------------------
set -euo pipefail

echo "[start-bot] Launching GhostKey Discord bot"
exec python bot.py
