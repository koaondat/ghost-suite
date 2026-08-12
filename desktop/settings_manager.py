"""
settings_manager.py — Persistent application settings
======================================================
Reads and writes a JSON settings file next to the executable.
All access is through typed properties so the rest of the code
never touches raw JSON keys directly.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from app_config import SETTINGS_FILE

log = logging.getLogger("ghost.settings")

_lock = threading.Lock()

_DEFAULTS: dict[str, Any] = {
    # General
    "launch_on_startup":    False,
    "start_minimized":      False,
    "minimize_to_tray":     True,
    "auto_update_check":    True,
    "confirm_before_close": True,
    # Appearance
    "theme":           "dark",
    "accent":          "red",
    "interface_scale": 100,
    "animations":      True,
    "reduced_motion":  False,
    # Application
    "update_channel":  "stable",
    # Spoofer options
    "spoof_system":    True,
    "spoof_storage":   True,
    "spoof_network":   True,
    "spoof_windows":   True,
    "cleanup_temp":    True,
    "randomize_every_run":   True,
    "create_backup":         True,
    "verify_after_spoof":    True,
}

_ACCENT_COLORS = {
    "red":    "#e53e3e",
    "blue":   "#3b82f6",
    "purple": "#7c3aed",
    "green":  "#16a34a",
    "orange": "#ea580c",
    "white":  "#e8eaed",
}


class Settings:
    """Thin wrapper around the settings JSON file."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = dict(_DEFAULTS)
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if SETTINGS_FILE.exists():
                with _lock:
                    raw = json.loads(SETTINGS_FILE.read_text("utf-8"))
                for k, v in raw.items():
                    if k in _DEFAULTS:
                        self._data[k] = v
        except Exception as exc:
            log.warning("settings load failed: %s", exc)

    def save(self) -> None:
        try:
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            with _lock:
                tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
            tmp.replace(SETTINGS_FILE)
        except Exception as exc:
            log.warning("settings save failed: %s", exc)

    # ── generic get/set ───────────────────────────────────────────────────────

    def get(self, key: str) -> Any:
        return self._data.get(key, _DEFAULTS.get(key))

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    # ── typed shortcuts ───────────────────────────────────────────────────────

    @property
    def theme(self) -> str:
        return str(self._data.get("theme", "dark"))

    @property
    def accent(self) -> str:
        return str(self._data.get("accent", "red"))

    @property
    def accent_hex(self) -> str:
        return _ACCENT_COLORS.get(self.accent, _ACCENT_COLORS["red"])

    @property
    def animations(self) -> bool:
        return bool(self._data.get("animations", True))

    @property
    def update_channel(self) -> str:
        return str(self._data.get("update_channel", "stable"))

    @property
    def auto_update_check(self) -> bool:
        return bool(self._data.get("auto_update_check", True))

    @property
    def spoof_options(self) -> dict[str, bool]:
        return {
            "system":       bool(self._data.get("spoof_system",   True)),
            "storage":      bool(self._data.get("spoof_storage",  True)),
            "network":      bool(self._data.get("spoof_network",  True)),
            "windows":      bool(self._data.get("spoof_windows",  True)),
            "cleanup":      bool(self._data.get("cleanup_temp",   True)),
            "randomize":    bool(self._data.get("randomize_every_run", True)),
            "backup":       bool(self._data.get("create_backup",  True)),
            "verify":       bool(self._data.get("verify_after_spoof", True)),
        }

    @staticmethod
    def accent_choices() -> list[str]:
        return list(_ACCENT_COLORS.keys())

    @staticmethod
    def accent_hex_for(name: str) -> str:
        return _ACCENT_COLORS.get(name, _ACCENT_COLORS["red"])


# Module-level singleton
settings = Settings()
