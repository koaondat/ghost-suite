"""
build.py — GhostConfig build helper
=====================================
Run:  python build.py

1. Runs PyInstaller against "QA System Config.spec"
2. Copies scaffold JSON files to dist/ — but NEVER overwrites existing ones.
   This preserves users, keys, and bans across rebuilds.

Output exe: dist/GhostConfig.exe
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
DIST = HERE / "dist"

# These files are seeded with an empty list [] on first build only.
# If they already exist in dist/ they are left untouched (preserves live data).
SCAFFOLD_FILES = [
    "issued_keys.json",
    "banned_keys.json",
    "blacklist.json",
    "whitelist.json",
    "users.json",
]

# These files are always copied fresh from source (config — not live data).
CONFIG_FILES = [
    "trial_limits.json",
]

def main():
    # ── 1. Run PyInstaller ────────────────────────────────────────────────
    print("Building exe...")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "QA System Config.spec", "--noconfirm",
         "--distpath", str(DIST)],
        cwd=str(HERE),
    )
    if result.returncode != 0:
        print("PyInstaller failed.")
        sys.exit(result.returncode)

    # ── 2. Seed scaffold JSON files (skip if already exist) ───────────────
    import shutil
    DIST.mkdir(exist_ok=True)
    for fname in SCAFFOLD_FILES:
        dst = DIST / fname
        src = HERE / fname
        if dst.exists():
            print(f"  Skipped {fname} (already exists in dist/ — live data preserved)")
        elif src.exists():
            shutil.copy2(src, dst)
            print(f"  Copied  {fname} from source")
        else:
            dst.write_text("[]", encoding="utf-8")
            print(f"  Created {fname} (empty scaffold)")

    # ── 3. Copy config files (always fresh — not live data) ───────────────
    for fname in CONFIG_FILES:
        dst = DIST / fname
        src = HERE / fname
        if src.exists():
            shutil.copy2(src, dst)
            print(f"  Config  {fname} copied to dist/")
        else:
            print(f"  Warning: {fname} not found in source — skipped")

    print(f"\nDone. Exe: {DIST / 'GhostConfig.exe'}")

if __name__ == "__main__":
    main()
