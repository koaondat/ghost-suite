"""
ghost_updater.py — GhostConfig Updater Helper
==============================================
Launched by GhostConfig.exe immediately before it exits during an update.
Waits for the main process to terminate, replaces the old executable with
the newly-downloaded one, relaunches the updated app, then exits.

Usage (called internally by gui.py — do not run manually):
    python ghost_updater.py <pid> <old_exe> <new_exe> [--backup <backup_path>]

Arguments
---------
  pid        PID of the running GhostConfig.exe to wait for
  old_exe    Absolute path of the currently-running executable
  new_exe    Absolute path of the staged update file (in temp dir)
  --backup   Where to save the pre-update backup (default: old_exe + ".bak")

Flow
----
  1. Wait (up to 30 s) for <pid> to exit.
  2. Copy old_exe → backup.
  3. Move new_exe → old_exe.
  4. Launch the updated old_exe (DETACHED_PROCESS).
  5. On any failure: restore from backup, exit 1.
  6. Delete the staged file if it still exists.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_MAX_WAIT   = 30       # seconds
_POLL_DELAY = 0.25     # seconds between liveness checks


# ── Process liveness ─────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    """Return True when the process is still running (Windows)."""
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        rc = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
        ctypes.windll.kernel32.CloseHandle(handle)
        return rc != 0      # WAIT_OBJECT_0 (0) means process has exited
    except Exception:
        pass
    # Fallback — tasklist
    try:
        out = subprocess.check_output(
            ["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
        return str(pid) in out
    except Exception:
        return False


def _wait_for_exit(pid: int) -> bool:
    """Block until *pid* exits or timeout. Returns True on clean exit."""
    deadline = time.monotonic() + _MAX_WAIT
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(_POLL_DELAY)
    return False


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="GhostConfig updater helper")
    parser.add_argument("pid",     type=int,  help="PID of the running GhostConfig process")
    parser.add_argument("old_exe", type=Path, help="Path of the current executable")
    parser.add_argument("new_exe", type=Path, help="Path of the staged update file")
    parser.add_argument("--backup", type=Path, default=None,
                        help="Backup path (default: old_exe + .bak)")
    args = parser.parse_args()

    old_exe = args.old_exe.resolve()
    new_exe = args.new_exe.resolve()
    backup  = (args.backup.resolve()
               if args.backup else old_exe.with_suffix(".exe.bak"))
    pid     = args.pid

    if not new_exe.exists():
        print(f"[updater] ERROR: staged file not found: {new_exe}", file=sys.stderr)
        return 1

    # 1. Wait for GhostConfig to exit ─────────────────────────────────────────
    print(f"[updater] Waiting for GhostConfig (PID {pid}) to exit…")
    if not _wait_for_exit(pid):
        print(f"[updater] ERROR: process {pid} did not exit within {_MAX_WAIT}s.",
              file=sys.stderr)
        return 1
    print("[updater] Process exited. Replacing executable…")

    # 2. Backup ───────────────────────────────────────────────────────────────
    try:
        if old_exe.exists():
            shutil.copy2(old_exe, backup)
            print(f"[updater] Backup saved → {backup}")
    except Exception as exc:
        print(f"[updater] WARNING: backup failed ({exc}); continuing.", file=sys.stderr)

    # 3. Replace ───────────────────────────────────────────────────────────────
    try:
        shutil.move(str(new_exe), str(old_exe))
        print(f"[updater] Replaced: {old_exe}")
    except Exception as exc:
        print(f"[updater] ERROR: replacement failed: {exc}", file=sys.stderr)
        # Try to restore backup
        if backup.exists():
            try:
                shutil.copy2(backup, old_exe)
                print(f"[updater] Restored from backup.", file=sys.stderr)
            except Exception as re:
                print(f"[updater] CRITICAL: restore also failed: {re}", file=sys.stderr)
        return 1

    # 4. Relaunch ──────────────────────────────────────────────────────────────
    try:
        subprocess.Popen(
            [str(old_exe)],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
        )
        print(f"[updater] Launched: {old_exe}")
    except Exception as exc:
        print(f"[updater] ERROR: could not launch updated exe: {exc}", file=sys.stderr)
        return 1

    # 5. Cleanup ───────────────────────────────────────────────────────────────
    try:
        if new_exe.exists():
            new_exe.unlink()
    except Exception:
        pass

    print("[updater] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
