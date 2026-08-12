"""
main.py — Ghost desktop application entry point
================================================
Boot sequence:
  1. Apply DPI awareness (Windows)
  2. Create the root Tk window (hidden)
  3. Init fonts + theme
  4. Try to restore a saved session silently
  5. If no valid session → show LoginWindow
  6. On auth success → show MainWindow

All imports are kept lazy so the startup splash appears as fast as possible.
"""

from __future__ import annotations

import logging
import os
import sys
import tkinter as tk
from pathlib import Path

# ── Path setup so imports resolve whether running from source or frozen ────────
_HERE = Path(__file__).resolve().parent          # desktop/
_ROOT = _HERE.parent                             # workspace root (where config_utility.py is)
for _p in (str(_HERE), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("ghost.main")


# ── Windows DPI awareness ─────────────────────────────────────────────────────

def _set_dpi_awareness() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ── Application bootstrap ─────────────────────────────────────────────────────

def run() -> None:
    _set_dpi_awareness()

    # Hidden root for font initialisation (the real window is the MainWindow)
    root = tk.Tk()
    root.withdraw()

    import ui.theme as theme
    theme.init_fonts(root)
    theme.apply_ttk_theme(root)

    # Apply accent color from saved settings
    from settings_manager import settings
    theme.reload_accent(settings.accent_hex)

    # Try silent session restore
    import auth_manager
    import license_service

    def _start_login():
        from ui.login_window import LoginWindow
        LoginWindow(root, on_success=_on_auth, on_cancel=_on_cancel)

    def _on_cancel():
        root.destroy()
        sys.exit(0)

    def _on_auth(token: str, username: str,
                 lic: license_service.LicenseInfo) -> None:
        from ui.main_window import MainWindow
        win = MainWindow(token=token, username=username, lic_info=lic)
        root.destroy()
        win.mainloop()

    # Try silent restore in a background thread so the login window appears
    # instantly even if the network is slow
    def _try_restore():
        # Primary: key-only session restore (new flow)
        stored_key = auth_manager.load_key_session()
        if stored_key:
            result = auth_manager.load_activation(stored_key)
            if result.get("ok"):
                lic = license_service.LicenseInfo(result)
                root.after(0, lambda: _on_auth(stored_key, "", lic))
                return

        # Fallback: legacy JWT-based session (existing accounts)
        session = auth_manager.load_session()
        if session:
            token, username = session
            if token:
                result = auth_manager.validate_stored_session(token)
                if result.get("ok"):
                    lic = license_service.LicenseInfo(result)
                    root.after(0, lambda: _on_auth(token, username, lic))
                    return

        root.after(0, _start_login)

    import threading
    threading.Thread(target=_try_restore, daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    run()
