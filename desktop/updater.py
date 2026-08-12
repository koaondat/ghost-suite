"""
updater.py — Update check, download, and integrity verification
===============================================================
Background worker that fetches the latest release from the Ghost API,
compares versions, and optionally triggers the update dialog.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from app_config import API_BASE_URL, APP_VERSION, UPDATE_CHANNEL, PRODUCT_NAME

log = logging.getLogger("ghost.updater")


def _semver(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.lstrip("v").split(".", 2))
    except Exception:
        return (0, 0, 0)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_release(channel: str = "stable") -> dict | None:
    if not API_BASE_URL:
        return None
    url = f"{API_BASE_URL}/api/releases/latest?channel={channel}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": f"GhostDesktop/{APP_VERSION}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            return data
    except Exception as exc:
        log.debug("release fetch failed: %s", exc)
    return None


def check_for_update(channel: str | None = None) -> dict | None:
    """
    Check for a newer version.  Returns the release dict if an update is
    available, None otherwise.  Designed to be called from a worker thread.
    """
    ch      = channel or UPDATE_CHANNEL
    release = _fetch_release(ch)
    if not release:
        return None
    latest  = release.get("version", "0.0.0")
    if _semver(latest) <= _semver(APP_VERSION):
        return None
    return release


def download_update(
    release:     dict,
    progress_cb: Callable[[str, int | None], None],
) -> Path | None:
    """
    Download the update binary to a staging directory.
    `progress_cb(message, percent_or_None)` is called from the worker thread.
    Returns the staged Path on success, None on failure.
    """
    url      = release.get("downloadUrl", "")
    filename = release.get("filename", f"{PRODUCT_NAME}Setup.exe")
    expected = (release.get("sha256") or "").strip().lower()

    if not url.startswith("https://"):
        progress_cb("Error: download URL must be HTTPS", None)
        return None

    safe_name = Path(filename).name
    if not safe_name.lower().endswith(".exe") or "/" in filename or "\\" in filename:
        progress_cb("Error: invalid update filename", None)
        return None

    stage_dir = Path(tempfile.gettempdir()) / "ghost_update_staging"
    stage_dir.mkdir(parents=True, exist_ok=True)
    staged = stage_dir / safe_name

    progress_cb("Connecting…", 0)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": f"GhostDesktop/{APP_VERSION}"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            total      = int(resp.headers.get("Content-Length", 0) or 0)
            done       = 0
            t_start    = time.monotonic()
            with open(staged, "wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    elapsed = max(time.monotonic() - t_start, 0.001)
                    speed_k = (done / elapsed) / 1024
                    if total:
                        pct     = int(done * 100 / total)
                        mb_done = done / 1_048_576
                        mb_tot  = total / 1_048_576
                        eta     = int((total - done) / (done / elapsed)) if done else 0
                        msg     = f"Downloading… {pct}%  ({mb_done:.1f}/{mb_tot:.1f} MB)  {speed_k:.0f} KB/s  ETA {eta}s"
                    else:
                        msg = f"Downloading… {done // 1024} KB  {speed_k:.0f} KB/s"
                    progress_cb(msg, pct if total else None)
    except Exception as exc:
        progress_cb(f"Download failed: {exc}", None)
        return None

    # Integrity check
    progress_cb("Verifying…", 99)
    actual = _sha256_file(staged)
    if expected and actual != expected:
        try:
            staged.unlink()
        except Exception:
            pass
        progress_cb("Verification failed: file may be corrupt", None)
        return None

    return staged


def apply_update(staged: Path, progress_cb: Callable[[str, int | None], None]) -> None:
    """Launch ghost_updater.py / ghost_updater.exe to replace this exe."""
    progress_cb("Applying update…", 100)

    current_exe = (
        Path(sys.executable).resolve()
        if getattr(sys, "frozen", False)
        else Path(sys.argv[0]).resolve()
    )
    base = current_exe.parent

    if getattr(sys, "frozen", False):
        updater = base / "ghost_updater.exe"
        cmd = [str(updater)]
    else:
        updater = base / "ghost_updater.py"
        cmd = [sys.executable, str(updater)]

    if not Path(cmd[-1]).exists():
        progress_cb("Updater not found — please update manually", None)
        return

    import subprocess
    pid = os.getpid()
    backup_path = staged.parent / (staged.stem + "_backup.exe")
    try:
        subprocess.Popen(
            cmd + [str(pid), str(current_exe), str(staged), "--backup", str(backup_path)],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                          getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
    except Exception as exc:
        progress_cb(f"Could not launch updater: {exc}", None)
