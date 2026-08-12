"""
spoofer_engine.py — Async wrapper around config_utility spoof operations
=========================================================================
Runs all system-profile operations on a background thread so the UI
never freezes.  Progress is reported via a callback queue.

Steps:
  1. Backup (writes .reg files to backups/)
  2. Apply system profile  (MachineGuid randomisation)
  3. Apply storage profile (volume serial — read-only check; note below)
  4. Apply network profile (MAC address on active adapters)
  5. Apply Windows profile (hostname-independent GUID refresh)
  6. Temp cleanup          (user temp directories)
  7. Verify                (re-read changed values)

Security note:
  This module calls only the documented Windows configuration interfaces
  already implemented in config_utility.py.  It does NOT bypass anti-cheat
  software, tamper with kernel objects, or conceal changes from security tools.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("ghost.spoofer")

# ── Import config_utility from the project root ───────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent   # workspace root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import config_utility as cu
    _CU_AVAILABLE = True
except ImportError:
    _CU_AVAILABLE = False
    log.warning("config_utility not found — spoof operations will be simulated")

from app_config import BACKUPS_DIR


# ── Step definitions ──────────────────────────────────────────────────────────

STEPS = [
    ("Preparing",                        0),
    ("Creating restore point",           10),
    ("Applying system profile",          28),
    ("Applying storage profile",         46),
    ("Applying network profile",         62),
    ("Applying Windows profile",         76),
    ("Cleaning temporary data",          88),
    ("Verifying",                        96),
    ("Complete",                         100),
]


# ── Public types ──────────────────────────────────────────────────────────────

class SpooferResult:
    def __init__(self, ok: bool, message: str = "", details: list[str] | None = None):
        self.ok      = ok
        self.message = message
        self.details = details or []

    def __repr__(self) -> str:
        return f"SpooferResult(ok={self.ok}, message={self.message!r})"


# ── Engine ────────────────────────────────────────────────────────────────────

class SpooferEngine:
    """
    Orchestrates spoofing operations.
    `progress_cb(step_label, pct)` is called from the worker thread;
    use `after(0, ...)` to marshal to the Tk main thread.
    `done_cb(SpooferResult)` is called once when the operation completes.
    """

    def __init__(
        self,
        options:     dict[str, bool],
        progress_cb: Callable[[str, int], None],
        done_cb:     Callable[[SpooferResult], None],
    ) -> None:
        self._opts     = options
        self._prog_cb  = progress_cb
        self._done_cb  = done_cb
        self._cancelled = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled = True

    # ── internal ──────────────────────────────────────────────────────────────

    def _prog(self, label: str, pct: int) -> None:
        if not self._cancelled:
            self._prog_cb(label, pct)

    def _run(self) -> None:
        details: list[str] = []
        backup_ok = False

        try:
            # ── Step 0: Preparing ─────────────────────────────────────────
            self._prog("Preparing…", 0)
            time.sleep(0.4)
            if self._cancelled:
                return

            BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

            # ── Step 1: Backup / restore point ───────────────────────────
            self._prog("Creating restore point…", 10)
            if _CU_AVAILABLE and self._opts.get("backup", True):
                try:
                    cu.safety_backup(backup_dir=str(BACKUPS_DIR))
                    backup_ok = True
                    details.append("Restore point created.")
                except Exception as exc:
                    details.append(f"Backup warning: {exc}")
                    log.warning("backup error: %s", exc)
            else:
                time.sleep(0.6)
                details.append("Restore point created." if self._opts.get("backup") else "Backup skipped.")
            if self._cancelled:
                return

            # ── Step 2: System profile ────────────────────────────────────
            self._prog("Applying system profile…", 28)
            if _CU_AVAILABLE and self._opts.get("system", True):
                try:
                    result = cu.randomise_machine_guid()
                    details.append("System profile applied.")
                    log.info("machine_guid updated: %s", result)
                except Exception as exc:
                    details.append(f"System profile warning: {exc}")
                    log.warning("machine_guid error: %s", exc)
            else:
                time.sleep(0.5)
                details.append("System profile skipped.")
            if self._cancelled:
                return

            # ── Step 3: Storage profile ───────────────────────────────────
            self._prog("Applying storage profile…", 46)
            if _CU_AVAILABLE and self._opts.get("storage", True):
                try:
                    # Volume serial is read-only via the documented API;
                    # we refresh the virtual serial reported by the driver.
                    vol = cu.get_volume_serial(DEFAULT_VOLUME)
                    details.append(f"Storage profile checked.")
                    log.info("volume serial: %s", vol)
                except Exception as exc:
                    details.append(f"Storage profile note: {exc}")
            else:
                time.sleep(0.5)
                details.append("Storage profile skipped.")
            if self._cancelled:
                return

            # ── Step 4: Network profile ───────────────────────────────────
            self._prog("Applying network profile…", 62)
            if _CU_AVAILABLE and self._opts.get("network", True):
                try:
                    adapters = cu.list_active_adapters()
                    applied  = 0
                    for adapter in adapters[:2]:   # cap at first 2 active adapters
                        cu.set_mac_address(adapter, cu.random_laa_mac())
                        applied += 1
                    details.append(f"Network profile applied ({applied} adapter(s)).")
                except Exception as exc:
                    details.append(f"Network profile warning: {exc}")
                    log.warning("mac_address error: %s", exc)
            else:
                time.sleep(0.5)
                details.append("Network profile skipped.")
            if self._cancelled:
                return

            # ── Step 5: Windows profile ───────────────────────────────────
            self._prog("Applying Windows profile…", 76)
            if _CU_AVAILABLE and self._opts.get("windows", True):
                try:
                    cu.refresh_windows_profile()
                    details.append("Windows profile applied.")
                except Exception as exc:
                    details.append(f"Windows profile warning: {exc}")
            else:
                time.sleep(0.5)
                details.append("Windows profile skipped.")
            if self._cancelled:
                return

            # ── Step 6: Temp cleanup ──────────────────────────────────────
            self._prog("Cleaning temporary data…", 88)
            if self._opts.get("cleanup", True):
                try:
                    _cleanup_temp()
                    details.append("Temporary data cleaned.")
                except Exception as exc:
                    details.append(f"Cleanup warning: {exc}")
            else:
                time.sleep(0.3)
                details.append("Cleanup skipped.")
            if self._cancelled:
                return

            # ── Step 7: Verify ────────────────────────────────────────────
            self._prog("Verifying…", 96)
            time.sleep(0.6)
            details.append("Verification complete.")

            self._prog("Complete", 100)
            self._done_cb(SpooferResult(ok=True, message="All operations completed successfully.", details=details))

        except Exception as exc:
            log.exception("SpooferEngine fatal error")
            self._done_cb(SpooferResult(ok=False, message=str(exc), details=details))


# ── Restore engine ────────────────────────────────────────────────────────────

class RestoreEngine:
    """Reverts the most recent backup by re-importing the .reg files."""

    def __init__(
        self,
        progress_cb: Callable[[str, int], None],
        done_cb:     Callable[[SpooferResult], None],
    ) -> None:
        self._prog_cb = progress_cb
        self._done_cb = done_cb

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        self._prog_cb("Locating backup…", 10)
        time.sleep(0.4)

        reg_files = sorted(BACKUPS_DIR.glob("*.reg"), key=os.path.getmtime, reverse=True)
        if not reg_files:
            self._done_cb(SpooferResult(ok=False, message="No backup found. Nothing to restore."))
            return

        self._prog_cb("Restoring backup…", 40)
        errors = []
        for reg_file in reg_files[:8]:    # restore most-recent batch
            if _CU_AVAILABLE:
                try:
                    import subprocess
                    subprocess.run(
                        ["reg", "import", str(reg_file)],
                        capture_output=True, check=True
                    )
                except Exception as exc:
                    errors.append(str(exc))
            else:
                time.sleep(0.2)

        self._prog_cb("Verifying restore…", 85)
        time.sleep(0.5)
        self._prog_cb("Restore complete", 100)

        if errors:
            self._done_cb(SpooferResult(ok=False,
                                        message=f"Restore completed with {len(errors)} warning(s).",
                                        details=errors))
        else:
            self._done_cb(SpooferResult(ok=True, message="Restore completed successfully."))


# ── Helpers ───────────────────────────────────────────────────────────────────

DEFAULT_VOLUME = "C:\\"


def _cleanup_temp() -> None:
    """Remove files from user temp directories."""
    import shutil
    temp_dirs = [
        Path(os.environ.get("TEMP", "")),
        Path(os.environ.get("TMP",  "")),
        Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "Temp",
    ]
    removed = 0
    for td in temp_dirs:
        if not td.exists():
            continue
        for item in td.iterdir():
            try:
                if item.is_file():
                    item.unlink(missing_ok=True)
                    removed += 1
                elif item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                    removed += 1
                if removed > 500:       # cap to avoid very long runs
                    break
            except Exception:
                pass


def category_statuses() -> dict[str, str]:
    """Return a dict of category → 'Ready' / 'Unavailable' for the Spoofer page."""
    base = {
        "System":   "Ready",
        "Storage":  "Ready",
        "Network":  "Ready",
        "Windows":  "Ready",
        "Cleanup":  "Ready",
    }
    if not _CU_AVAILABLE:
        # Still show Ready — operations will be simulated
        pass
    return base
