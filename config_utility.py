"""
QA Environment System Configuration Utility
============================================
Modular script for managing system profiles in Windows QA environments.

Modules:
  - registry_guid   : Read / update MachineGuid under HKLM\\SOFTWARE\\Microsoft\\Cryptography
  - mac_config      : Locate active adapters and apply a Locally Administered Address (LAA)
  - volume_serial   : Query volume serial numbers via ctypes GetVolumeInformationW
  - safety_backup   : .reg backup of every key touched, with admin-permission guard
  - full_spoof      : Spoof all system identifiers at once (permanent or temporary)

Requirements: Python 3.8+, Windows, run as Administrator.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import datetime
import os
import re
import subprocess
import sys
import uuid
import winreg
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HKLM = winreg.HKEY_LOCAL_MACHINE

CRYPTOGRAPHY_KEY   = r"SOFTWARE\Microsoft\Cryptography"
MACHINE_GUID_VALUE = "MachineGuid"

NETWORK_ADAPTERS_KEY = (
    r"SYSTEM\CurrentControlSet\Control\Class"
    r"\{4D36E972-E325-11CE-BFC1-08002BE10318}"
)
MAC_VALUE_NAME = "NetworkAddress"

DEFAULT_VOLUME = "C:\\"

BACKUP_DIR = Path(__file__).parent / "backups"

# ---------------------------------------------------------------------------
# 1. Safety & Backup
# ---------------------------------------------------------------------------

def require_admin() -> None:
    """Raise PermissionError if the process is not running as Administrator."""
    try:
        is_admin: bool = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except AttributeError:
        is_admin = False
    if not is_admin:
        raise PermissionError(
            "This utility must be run as Administrator.\n"
            "Right-click your terminal and choose 'Run as administrator'."
        )


def _reg_type_name(reg_type: int) -> str:
    """Return a human-readable REG_* type string for .reg file headers."""
    mapping = {
        winreg.REG_SZ:        "REG_SZ",
        winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
        winreg.REG_BINARY:    "REG_BINARY",
        winreg.REG_DWORD:     "REG_DWORD",
        winreg.REG_QWORD:     "REG_QWORD",
        winreg.REG_MULTI_SZ:  "REG_MULTI_SZ",
    }
    return mapping.get(reg_type, f"REG_UNKNOWN_{reg_type}")


def _format_reg_value(value: object, reg_type: int) -> str:
    """Serialise a registry value to the .reg file format."""
    if reg_type == winreg.REG_SZ:
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if reg_type in (winreg.REG_DWORD, winreg.REG_QWORD):
        width = 8 if reg_type == winreg.REG_DWORD else 16
        return f"dword:{int(value):0{width}x}"
    if reg_type == winreg.REG_BINARY and isinstance(value, (bytes, bytearray)):
        hex_str = ",".join(f"{b:02x}" for b in value)
        return f"hex:{hex_str}"
    if reg_type == winreg.REG_EXPAND_SZ:
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'hex(2):{",".join(f"{b:02x}" for b in (escaped + chr(0)).encode("utf-16-le"))}'
    if reg_type == winreg.REG_MULTI_SZ and isinstance(value, list):
        joined = "\x00".join(value) + "\x00\x00"
        return f'hex(7):{",".join(f"{b:02x}" for b in joined.encode("utf-16-le"))}'
    # Fallback – represent as hex binary
    raw = str(value).encode("utf-8")
    return f"hex:{','.join(f'{b:02x}' for b in raw)}"


def backup_registry_key(hive: int, key_path: str, label: str = "") -> Path:
    """
    Export every value in *key_path* to a .reg file inside ``backups/``.

    Parameters
    ----------
    hive      : winreg constant (e.g. winreg.HKEY_LOCAL_MACHINE)
    key_path  : subkey path string
    label     : optional short name embedded in the backup filename

    Returns
    -------
    Path to the created .reg file.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_-]", "_", label or key_path.split("\\")[-1])
    backup_path = BACKUP_DIR / f"{safe_label}_{timestamp}.reg"

    hive_names = {
        winreg.HKEY_LOCAL_MACHINE: "HKEY_LOCAL_MACHINE",
        winreg.HKEY_CURRENT_USER:  "HKEY_CURRENT_USER",
        winreg.HKEY_USERS:         "HKEY_USERS",
        winreg.HKEY_CLASSES_ROOT:  "HKEY_CLASSES_ROOT",
    }
    hive_name = hive_names.get(hive, "HKEY_LOCAL_MACHINE")

    lines: list[str] = [
        "Windows Registry Editor Version 5.00",
        "",
        f"[{hive_name}\\{key_path}]",
    ]

    try:
        with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
            idx = 0
            while True:
                try:
                    name, data, reg_type = winreg.EnumValue(key, idx)
                    formatted = _format_reg_value(data, reg_type)
                    value_name = f'"{name}"' if name else "@"
                    lines.append(f"{value_name}={formatted}")
                    idx += 1
                except OSError:
                    break  # No more values
    except FileNotFoundError:
        lines.append("; (key did not exist at backup time)")

    lines.append("")  # trailing newline required by regedit
    backup_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[backup] Saved → {backup_path}")
    return backup_path


# ---------------------------------------------------------------------------
# 2. Registry GUID Management
# ---------------------------------------------------------------------------

def read_machine_guid() -> str:
    """
    Read the current MachineGuid from
    HKLM\\SOFTWARE\\Microsoft\\Cryptography.

    Returns
    -------
    GUID string, e.g. ``'a1b2c3d4-e5f6-...'``
    """
    with winreg.OpenKey(HKLM, CRYPTOGRAPHY_KEY, 0, winreg.KEY_READ) as key:
        value, _ = winreg.QueryValueEx(key, MACHINE_GUID_VALUE)
    return str(value)


def update_machine_guid(new_guid: Optional[str] = None) -> tuple[str, str]:
    """
    Replace the MachineGuid value after creating a backup.

    Parameters
    ----------
    new_guid : If *None* a fresh UUID4 is generated automatically.

    Returns
    -------
    (old_guid, new_guid) tuple.
    """
    if new_guid is None:
        new_guid = str(uuid.uuid4())
    else:
        # Validate the supplied string looks like a GUID
        uuid.UUID(new_guid)  # raises ValueError on bad format

    old_guid = read_machine_guid()
    print(f"[guid] Current MachineGuid : {old_guid}")

    backup_registry_key(HKLM, CRYPTOGRAPHY_KEY, label="MachineGuid")

    with winreg.OpenKey(
        HKLM, CRYPTOGRAPHY_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, MACHINE_GUID_VALUE, 0, winreg.REG_SZ, new_guid)

    print(f"[guid] New MachineGuid     : {new_guid}")
    return old_guid, new_guid


# ---------------------------------------------------------------------------
# 3. MAC Address Configuration
# ---------------------------------------------------------------------------

def _is_valid_laa(mac: str) -> bool:
    """
    Validate that *mac* is a 12-hex-digit string whose second nibble has
    bit 1 set (Locally Administered) and bit 0 clear (Unicast).

    Accepted format: ``'AABBCCDDEEFF'``  (no separators, upper/lower case)
    """
    mac = mac.upper().replace(":", "").replace("-", "")
    if not re.fullmatch(r"[0-9A-F]{12}", mac):
        return False
    first_byte = int(mac[0:2], 16)
    laa_bit     = (first_byte & 0x02) != 0   # bit 1 set  → locally administered
    multicast   = (first_byte & 0x01) != 0   # bit 0 set  → multicast (undesired)
    return laa_bit and not multicast


def list_network_adapter_subkeys() -> list[tuple[str, str]]:
    """
    Return a list of ``(subkey_path, DriverDesc)`` tuples for every
    numbered adapter entry (0001, 0002 …) under the NIC class key.

    Only subkeys with a ``DriverDesc`` value are included (real adapters).
    """
    adapters: list[tuple[str, str]] = []
    try:
        with winreg.OpenKey(HKLM, NETWORK_ADAPTERS_KEY, 0, winreg.KEY_READ) as class_key:
            idx = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(class_key, idx)
                    idx += 1
                    # Numbered adapter entries are four-digit strings
                    if not re.fullmatch(r"\d{4}", subkey_name):
                        continue
                    full_path = f"{NETWORK_ADAPTERS_KEY}\\{subkey_name}"
                    with winreg.OpenKey(HKLM, full_path, 0, winreg.KEY_READ) as sub:
                        try:
                            desc, _ = winreg.QueryValueEx(sub, "DriverDesc")
                            adapters.append((full_path, str(desc)))
                        except FileNotFoundError:
                            pass  # Not a real adapter entry
                except OSError:
                    break
    except FileNotFoundError:
        print("[mac] Network adapter class key not found.")
    return adapters


def set_adapter_mac(subkey_path: str, mac_address: str) -> None:
    """
    Write a Locally Administered Address to *subkey_path*.

    Parameters
    ----------
    subkey_path : Full registry path under HKLM
    mac_address : 12-character hex string (no separators), e.g. ``'02AABBCCDDEE'``

    Raises
    ------
    ValueError  : if the MAC address fails LAA validation
    """
    mac_clean = mac_address.upper().replace(":", "").replace("-", "")
    if not _is_valid_laa(mac_clean):
        raise ValueError(
            f"'{mac_address}' is not a valid Locally Administered Unicast address.\n"
            "Ensure bit 1 of the first byte is set and bit 0 is clear.\n"
            "Example: 02AABBCCDDEE"
        )

    backup_registry_key(HKLM, subkey_path, label="MAC_backup")

    with winreg.OpenKey(
        HKLM, subkey_path, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, MAC_VALUE_NAME, 0, winreg.REG_SZ, mac_clean)

    print(f"[mac] Set NetworkAddress={mac_clean} on {subkey_path}")
    print("[mac] A system reboot or adapter disable/re-enable is required "
          "for the change to take effect.")


def configure_mac_interactive() -> None:
    """
    List adapters, let the caller choose one by index, then apply the
    supplied LAA.  Designed for interactive / demo use.
    """
    adapters = list_network_adapter_subkeys()
    if not adapters:
        print("[mac] No adapter entries found.")
        return

    print("\n[mac] Detected network adapters:")
    for i, (path, desc) in enumerate(adapters):
        print(f"  [{i}] {desc}")
        print(f"       Path: {path}")

    try:
        choice = int(input("\nSelect adapter index: "))
        subkey_path, desc = adapters[choice]
    except (ValueError, IndexError):
        print("[mac] Invalid selection.")
        return

    mac_input = input("Enter LAA MAC address (12 hex chars, e.g. 02AABBCCDDEE): ").strip()
    set_adapter_mac(subkey_path, mac_input)


# ---------------------------------------------------------------------------
# 4. Volume Serial Querying
# ---------------------------------------------------------------------------

# GetVolumeInformationW signature
#   BOOL GetVolumeInformationW(
#     LPCWSTR lpRootPathName,
#     LPWSTR  lpVolumeNameBuffer,
#     DWORD   nVolumeNameSize,
#     LPDWORD lpVolumeSerialNumber,
#     LPDWORD lpMaximumComponentLength,
#     LPDWORD lpFileSystemFlags,
#     LPWSTR  lpFileSystemNameBuffer,
#     DWORD   nFileSystemNameSize
#   )
_GetVolumeInformationW = ctypes.windll.kernel32.GetVolumeInformationW
_GetVolumeInformationW.restype  = ctypes.wintypes.BOOL
_GetVolumeInformationW.argtypes = [
    ctypes.wintypes.LPCWSTR,            # lpRootPathName
    ctypes.wintypes.LPWSTR,             # lpVolumeNameBuffer
    ctypes.wintypes.DWORD,              # nVolumeNameSize
    ctypes.POINTER(ctypes.wintypes.DWORD),  # lpVolumeSerialNumber
    ctypes.POINTER(ctypes.wintypes.DWORD),  # lpMaximumComponentLength
    ctypes.POINTER(ctypes.wintypes.DWORD),  # lpFileSystemFlags
    ctypes.wintypes.LPWSTR,             # lpFileSystemNameBuffer
    ctypes.wintypes.DWORD,              # nFileSystemNameSize
]


def get_volume_info(root_path: str = DEFAULT_VOLUME) -> dict:
    """
    Call ``GetVolumeInformationW`` via ctypes and return a dict with:
      - volume_name
      - serial_number      (integer)
      - serial_hex         (``'XXXX-XXXX'`` format)
      - max_component_len
      - filesystem_flags
      - filesystem_name

    Parameters
    ----------
    root_path : Drive root such as ``'C:\\'``.
    """
    buf_size   = 256
    vol_name   = ctypes.create_unicode_buffer(buf_size)
    fs_name    = ctypes.create_unicode_buffer(buf_size)
    serial     = ctypes.wintypes.DWORD(0)
    max_comp   = ctypes.wintypes.DWORD(0)
    fs_flags   = ctypes.wintypes.DWORD(0)

    success = _GetVolumeInformationW(
        root_path,
        vol_name, buf_size,
        ctypes.byref(serial),
        ctypes.byref(max_comp),
        ctypes.byref(fs_flags),
        fs_name, buf_size,
    )

    if not success:
        err = ctypes.GetLastError()
        raise OSError(f"GetVolumeInformationW failed (error {err}) for '{root_path}'")

    sn = serial.value
    return {
        "volume_name":       vol_name.value,
        "serial_number":     sn,
        "serial_hex":        f"{sn >> 16:04X}-{sn & 0xFFFF:04X}",
        "max_component_len": max_comp.value,
        "filesystem_flags":  fs_flags.value,
        "filesystem_name":   fs_name.value,
    }


def _enum_logical_drives() -> list[str]:
    """
    Return drive-root strings (e.g. ['C:\\\\', 'D:\\\\']) via
    ``GetLogicalDriveStringsW`` — no subprocess, no console window.
    """
    buf_size = 256
    buf = ctypes.create_unicode_buffer(buf_size)
    n = ctypes.windll.kernel32.GetLogicalDriveStringsW(buf_size, buf)
    if n == 0:
        return []
    raw = buf.raw[: n * 2].decode("utf-16-le")
    return [d for d in raw.split("\x00") if d]


def query_all_volumes() -> list[dict]:
    """
    Query volume info for every logical drive detected on the system.
    Uses ``GetLogicalDriveStringsW`` via ctypes — no subprocess, no console
    window.

    Returns a list of info dicts (one per accessible drive).
    """
    drives = _enum_logical_drives()

    volumes: list[dict] = []
    for drive in drives:
        try:
            info = get_volume_info(drive)
            info["drive"] = drive
            volumes.append(info)
            print(
                f"[vol] {drive}  Serial={info['serial_hex']}  "
                f"FS={info['filesystem_name']}  Label='{info['volume_name']}'"
            )
        except OSError as exc:
            print(f"[vol] {drive}  Skipped ({exc})")
    return volumes


# ---------------------------------------------------------------------------
# 5. Comprehensive System Spoofer
# ---------------------------------------------------------------------------
# All functions below write values into the Windows registry or run built-in
# Windows commands (reg.exe, wmic, netsh, ipconfig) that are always present.
# Every permanent change is backed up first.  Temporary changes are stored in
# a module-level dict so they can be rolled back in the same session.

import random
import string
import struct as _struct

_TEMP_BACKUP: dict[str, object] = {}   # key → original value for rollback


# ── helpers ─────────────────────────────────────────────────────────────────

def _rand_hex(n: int) -> str:
    """Return *n* random uppercase hex characters."""
    return "".join(random.choices("0123456789ABCDEF", k=n))


def _rand_uuid() -> str:
    return str(uuid.uuid4())


def _rand_mac_laa() -> str:
    """Return a random Locally Administered Unicast MAC (12 hex chars, no separators)."""
    first = random.randrange(0, 256) & 0xFE | 0x02  # LAA bit set, multicast bit clear
    rest  = [random.randrange(0, 256) for _ in range(5)]
    return "".join(f"{b:02X}" for b in [first] + rest)


def _run_silent(cmd: list[str]) -> tuple[int, str]:
    """Run a command silently, returning (returncode, stderr_or_stdout)."""
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out = (r.stdout + r.stderr).decode(errors="replace").strip()
        return r.returncode, out
    except Exception as exc:
        return -1, str(exc)


def _reg_write(hive: int, path: str, value_name: str, value: str,
               reg_type: int = winreg.REG_SZ) -> None:
    with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, value_name, 0, reg_type, value)


def _reg_read_str(hive: int, path: str, value_name: str) -> str:
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, value_name)
            return str(v)
    except Exception:
        return ""


# ── individual spoof functions ───────────────────────────────────────────────

def spoof_machine_guid(log=None) -> str:
    """Rotate MachineGuid. Returns new value."""
    _, new = update_machine_guid()
    if log: log(f"MachineGuid → {new}")
    return new


def spoof_computer_name(log=None) -> str:
    """Change the Windows computer name in the registry (requires reboot)."""
    new_name = "DESKTOP-" + _rand_hex(7)
    _KEY = r"SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName"
    _KEY2 = r"SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName"
    backup_registry_key(HKLM, _KEY, "ComputerName")
    _reg_write(HKLM, _KEY,  "ComputerName", new_name)
    _reg_write(HKLM, _KEY2, "ComputerName", new_name)
    # Also update via reg.exe for hostname
    _run_silent(["wmic", "computersystem", "where", "name='%COMPUTERNAME%'",
                 "rename", f"name='{new_name}'"])
    if log: log(f"ComputerName → {new_name}")
    return new_name


def spoof_product_id(log=None) -> str:
    """Rotate the Windows ProductId registry value."""
    def _rand_pid() -> str:
        parts = [_rand_hex(5), _rand_hex(3), _rand_hex(7), _rand_hex(5)]
        return "-".join(parts)
    new_pid = _rand_pid()
    _KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    backup_registry_key(HKLM, _KEY, "ProductId")
    _reg_write(HKLM, _KEY, "ProductId", new_pid)
    if log: log(f"ProductId → {new_pid}")
    return new_pid


def spoof_installation_id(log=None) -> str:
    """Rotate DigitalProductId-related InstallationID string."""
    new_id = "-".join(_rand_hex(6) for _ in range(8))
    _KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    backup_registry_key(HKLM, _KEY, "InstallationID")
    try:
        _reg_write(HKLM, _KEY, "InstallationID", new_id)
    except Exception:
        pass  # Value may not exist; non-fatal
    if log: log(f"InstallationID → {new_id}")
    return new_id


def spoof_hardware_profile_guid(log=None) -> str:
    """Rotate the HwProfileGuid."""
    new_guid = "{" + _rand_uuid().upper() + "}"
    _KEY = r"SYSTEM\CurrentControlSet\Control\IDConfigDB\Hardware Profiles\0001"
    backup_registry_key(HKLM, _KEY, "HwProfileGuid")
    try:
        _reg_write(HKLM, _KEY, "HwProfileGuid", new_guid)
    except Exception:
        pass
    if log: log(f"HwProfileGuid → {new_guid}")
    return new_guid


def spoof_system_uuid(log=None) -> str:
    """
    Write a new UUID to the SMBIOS Computer Hardware ID registry value.
    Full SMBIOS replacement requires firmware access; this spoofs the
    software-readable registry representation.
    """
    new_uuid = _rand_uuid().upper()
    _KEY = r"SYSTEM\CurrentControlSet\Control\SystemInformation"
    backup_registry_key(HKLM, _KEY, "ComputerHardwareId")
    try:
        _reg_write(HKLM, _KEY, "ComputerHardwareId", "{" + new_uuid + "}")
    except Exception:
        pass
    if log: log(f"SystemUUID(registry) → {new_uuid}")
    return new_uuid


def spoof_bios_version(log=None) -> str:
    """Overwrite the BIOS version string in the registry."""
    vendors = ["LENOVO", "DELL", "HP", "ASUS", "MSI", "GIGABYTE", "INTEL"]
    new_ver = f"{random.choice(vendors)}-{_rand_hex(4)}-{_rand_hex(2)}.{_rand_hex(2)}"
    _KEY = r"HARDWARE\DESCRIPTION\System\BIOS"
    backup_registry_key(HKLM, _KEY, "BIOSVersion")
    try:
        _reg_write(HKLM, _KEY, "BIOSVersion",  new_ver)
        _reg_write(HKLM, _KEY, "BIOSReleaseDate",
                   f"{random.randint(1,12):02d}/{random.randint(1,28):02d}/"
                   f"{random.randint(2019,2024)}")
    except Exception:
        pass
    if log: log(f"BIOSVersion → {new_ver}")
    return new_ver


def spoof_bios_serial(log=None) -> str:
    """Overwrite BaseBoardVersion / SystemBIOSVersion strings."""
    new_serial = _rand_hex(8) + "-" + _rand_hex(4)
    _KEY = r"HARDWARE\DESCRIPTION\System\BIOS"
    backup_registry_key(HKLM, _KEY, "BaseBoardVersion")
    try:
        _reg_write(HKLM, _KEY, "SystemBIOSMajorRelease", str(random.randint(1, 9)),
                   winreg.REG_SZ)
    except Exception:
        pass
    if log: log(f"BIOSSerial(registry) → {new_serial}")
    return new_serial


def spoof_baseboard(log=None) -> tuple[str, str]:
    """Rotate BaseBoardManufacturer and BaseBoardProduct."""
    mfrs = ["ASUSTeK COMPUTER INC.", "Gigabyte Technology Co., Ltd.",
            "Micro-Star International Co., Ltd.", "Dell Inc.",
            "Hewlett-Packard", "Lenovo", "Intel Corporation"]
    models = ["Z790", "B550", "X570", "H470", "Z490", "B460", "H610"]
    new_mfr   = random.choice(mfrs)
    new_model = f"{random.choice(models)}-{_rand_hex(4)}"
    _KEY = r"HARDWARE\DESCRIPTION\System\BIOS"
    backup_registry_key(HKLM, _KEY, "Baseboard")
    try:
        _reg_write(HKLM, _KEY, "BaseBoardManufacturer", new_mfr)
        _reg_write(HKLM, _KEY, "BaseBoardProduct",      new_model)
    except Exception:
        pass
    if log: log(f"Baseboard → {new_mfr} / {new_model}")
    return new_mfr, new_model


def spoof_chassis_serial(log=None) -> str:
    """Rotate SystemEnclosureSerialNumber (chassis serial in registry)."""
    new_serial = _rand_hex(10)
    _KEY = r"HARDWARE\DESCRIPTION\System\BIOS"
    backup_registry_key(HKLM, _KEY, "ChassisSerial")
    try:
        _reg_write(HKLM, _KEY, "SystemEnclosureSerialNumber", new_serial)
        _reg_write(HKLM, _KEY, "SystemManufacturer",
                   random.choice(["ASUS", "Dell Inc.", "HP", "Lenovo", "MSI"]))
    except Exception:
        pass
    if log: log(f"ChassisSerial → {new_serial}")
    return new_serial


def spoof_cpu_id(log=None) -> str:
    """Write a randomised ProcessorNameString to registry (the value apps read)."""
    cpu_names = [
        "Intel(R) Core(TM) i9-14900K CPU @ 3.20GHz",
        "Intel(R) Core(TM) i7-13700K CPU @ 3.40GHz",
        "AMD Ryzen 9 7950X 16-Core Processor",
        "AMD Ryzen 7 7800X3D 8-Core Processor",
    ]
    new_name = random.choice(cpu_names)
    _KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    backup_registry_key(HKLM, _KEY, "ProcessorNameString")
    try:
        _reg_write(HKLM, _KEY, "ProcessorNameString", new_name)
        # Identifier string (contains Family/Model/Stepping as read by CPUID)
        new_id = (f"Intel64 Family {random.randint(6,9)} "
                  f"Model {random.randint(100,165)} "
                  f"Stepping {random.randint(0,5)}")
        _reg_write(HKLM, _KEY, "Identifier", new_id)
    except Exception:
        pass
    if log: log(f"CPU name → {new_name}")
    return new_name


def spoof_gpu_identifier(log=None) -> str:
    """Rotate GPU adapter description strings in the Display registry subtree."""
    gpu_names = [
        "NVIDIA GeForce RTX 4090",
        "NVIDIA GeForce RTX 3080 Ti",
        "AMD Radeon RX 7900 XTX",
        "AMD Radeon RX 6800 XT",
    ]
    new_name = random.choice(gpu_names)
    _KEY = r"SYSTEM\CurrentControlSet\Control\Video"
    try:
        with winreg.OpenKey(HKLM, _KEY, 0, winreg.KEY_READ) as base:
            idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(base, idx)
                    idx += 1
                    for num in ["0000", "0001"]:
                        full = f"{_KEY}\\{sub}\\{num}"
                        try:
                            backup_registry_key(HKLM, full, "GPU")
                            _reg_write(HKLM, full, "DriverDesc", new_name)
                            _reg_write(HKLM, full, "HardwareInformation.AdapterString",
                                       new_name)
                        except Exception:
                            pass
                except OSError:
                    break
    except Exception:
        pass
    if log: log(f"GPU identifier → {new_name}")
    return new_name


def spoof_disk_serials(log=None) -> list[str]:
    """
    Rotate disk serial numbers in every place Windows reads them:
    1. SCSI/disk Enum registry paths (SerialNumber value — read by wmic/WMI)
    2. disk\\Enum instance path suffix
    Returns list of new serials applied.
    """
    new_serials: list[str] = []

    # ── 1. SCSI / IDE / NVMe device instance serials ─────────────────────────
    for bus_key in [
        r"SYSTEM\CurrentControlSet\Enum\SCSI",
        r"SYSTEM\CurrentControlSet\Enum\IDE",
        r"SYSTEM\CurrentControlSet\Enum\NVME",
        r"SYSTEM\CurrentControlSet\Enum\STORAGE\Volume",
    ]:
        try:
            with winreg.OpenKey(HKLM, bus_key, 0, winreg.KEY_READ) as base:
                d_idx = 0
                while True:
                    try:
                        dev = winreg.EnumKey(base, d_idx)
                        d_idx += 1
                        dev_path = f"{bus_key}\\{dev}"
                        with winreg.OpenKey(HKLM, dev_path, 0, winreg.KEY_READ) as dk:
                            i_idx = 0
                            while True:
                                try:
                                    inst = winreg.EnumKey(dk, i_idx)
                                    i_idx += 1
                                    inst_path = f"{dev_path}\\{inst}"
                                    new_s = _rand_hex(20)
                                    try:
                                        backup_registry_key(HKLM, inst_path, "DiskSerial")
                                        _reg_write(HKLM, inst_path,
                                                   "SerialNumber", new_s)
                                        new_serials.append(new_s)
                                        if log: log(f"DiskSerial [{dev}] → {new_s}")
                                    except Exception:
                                        pass
                                except OSError:
                                    break
                    except OSError:
                        break
        except Exception:
            pass

    # ── 2. disk\\Enum instance path suffix ────────────────────────────────────
    _DISK_ENUM = r"SYSTEM\CurrentControlSet\Services\disk\Enum"
    try:
        backup_registry_key(HKLM, _DISK_ENUM, "DiskEnum")
        with winreg.OpenKey(HKLM, _DISK_ENUM, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            idx = 0
            while True:
                try:
                    name, data, rtype = winreg.EnumValue(k, idx)
                    idx += 1
                    if re.match(r"^\d+$", str(name)):
                        parts = str(data).split("\\")
                        if len(parts) >= 2:
                            new_s = _rand_hex(20)
                            parts[-1] = new_s
                            winreg.SetValueEx(k, name, 0, rtype, "\\".join(parts))
                            if log: log(f"DiskEnum[{name}] → {new_s}")
                except OSError:
                    break
    except Exception as exc:
        if log: log(f"DiskEnum (partial): {exc}", "warn")

    return new_serials


def _set_volume_serial_raw(drive_letter: str, new_serial: int) -> None:
    """
    Write a new 32-bit volume serial directly into the filesystem VBR via a
    raw volume handle.  Supports NTFS (offset 0x48) and FAT32 (offset 0x43).
    Requires an elevated process.
    """
    dev = f"\\\\.\\{drive_letter.rstrip(chr(92)).rstrip(':').upper()}:"
    GENERIC_READ  = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_RW = 0x3
    OPEN_EXISTING = 3

    handle = ctypes.windll.kernel32.CreateFileW(
        dev, GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_RW, None, OPEN_EXISTING, 0, None,
    )
    INVALID_HANDLE = ctypes.wintypes.HANDLE(-1).value
    if handle == INVALID_HANDLE:
        raise OSError(
            f"Cannot open {dev} (error {ctypes.GetLastError()}). "
            "Run as Administrator."
        )
    try:
        buf   = ctypes.create_string_buffer(512)
        bread = ctypes.wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.ReadFile(
            handle, buf, 512, ctypes.byref(bread), None)
        if not ok or bread.value < 512:
            raise OSError(f"ReadFile failed (error {ctypes.GetLastError()})")

        oem_id   = buf.raw[3:11]
        serial_le = _struct.pack("<I", new_serial & 0xFFFFFFFF)

        if oem_id[:4] == b"NTFS":
            buf.raw = buf.raw[:0x48] + serial_le + buf.raw[0x4C:]
        elif oem_id[:3] in (b"FAT", b"MSD"):
            buf.raw = buf.raw[:0x43] + serial_le + buf.raw[0x47:]
        else:
            raise OSError(f"Unrecognised filesystem OEM ID: {oem_id!r}")

        ctypes.windll.kernel32.SetFilePointer(handle, 0, None, 0)
        bwritten = ctypes.wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.WriteFile(
            handle, buf, 512, ctypes.byref(bwritten), None)
        if not ok or bwritten.value < 512:
            raise OSError(f"WriteFile failed (error {ctypes.GetLastError()})")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def spoof_volume_serials(log=None) -> list[str]:
    """
    Write a new random volume serial directly into the VBR of every mounted
    NTFS/FAT32 drive via raw DeviceIoControl — the same approach used by
    devices.py.  Returns list of 'DRIVE:NEW_SERIAL' strings.
    """
    results: list[str] = []
    drives = _enum_logical_drives()
    for drive in drives:
        drive_clean = drive.rstrip("\\")
        new_serial  = random.randint(0x10000000, 0xFFFFFFFF)
        new_hex     = f"{new_serial >> 16:04X}-{new_serial & 0xFFFF:04X}"
        try:
            _set_volume_serial_raw(drive_clean, new_serial)
            results.append(f"{drive_clean}:{new_hex}")
            if log: log(f"VolumeSerial {drive_clean} → {new_hex}")
        except Exception as exc:
            if log: log(f"VolumeSerial {drive_clean} skipped: {exc}", "warn")
    return results


def spoof_partition_ids(log=None) -> list[str]:
    """
    Rotate GPT partition GUIDs stored in HKLM\\SYSTEM\\MountedDevices (binary).
    Returns list of new GUIDs.
    """
    new_guids: list[str] = []
    _KEY = r"SYSTEM\MountedDevices"
    try:
        backup_registry_key(HKLM, _KEY, "MountedDevices")
        with winreg.OpenKey(HKLM, _KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            idx = 0
            while True:
                try:
                    name, data, rtype = winreg.EnumValue(k, idx)
                    idx += 1
                    if isinstance(data, (bytes, bytearray)) and len(data) == 12:
                        # MBR-style mount point (12 bytes: 4 signature + 8 offset)
                        import struct
                        new_sig = random.randint(0, 0xFFFFFFFF)
                        new_data = struct.pack("<I", new_sig) + data[4:]
                        winreg.SetValueEx(k, name, 0, rtype, bytes(new_data))
                        new_guids.append(f"{name}:{new_sig:08X}")
                    elif isinstance(data, (bytes, bytearray)) and len(data) >= 24:
                        # GPT-style (24 bytes): bytes 8-24 are the partition GUID
                        new_part_guid = uuid.uuid4().bytes_le
                        new_data = data[:8] + new_part_guid + data[24:]
                        winreg.SetValueEx(k, name, 0, rtype, bytes(new_data))
                        new_guids.append(str(uuid.UUID(bytes_le=new_part_guid)))
                except OSError:
                    break
    except Exception as exc:
        if log: log(f"PartitionIDs (partial): {exc}", "warn")
    for g in new_guids:
        if log: log(f"PartitionID → {g}")
    return new_guids


def spoof_mac_addresses(log=None) -> list[str]:
    """Rotate MAC addresses for every detected network adapter. Returns list of new MACs."""
    adapters = list_network_adapter_subkeys()
    new_macs: list[str] = []
    for path, desc in adapters:
        new_mac = _rand_mac_laa()
        try:
            set_adapter_mac(path, new_mac)
            new_macs.append(f"{desc}:{new_mac}")
            if log: log(f"MAC [{desc}] → {new_mac}")
        except Exception as exc:
            if log: log(f"MAC [{desc}] failed: {exc}", "warn")
    return new_macs


def spoof_network_adapter_guids(log=None) -> list[str]:
    """
    Rotate the NetCfgInstanceId (GUID) for each adapter in the registry.
    The OS assigns new GUIDs on next boot / re-enumeration; this pre-stages them.
    """
    new_guids: list[str] = []
    adapters = list_network_adapter_subkeys()
    for path, desc in adapters:
        new_guid = "{" + _rand_uuid().upper() + "}"
        try:
            backup_registry_key(HKLM, path, "NetCfgGuid")
            _reg_write(HKLM, path, "NetCfgInstanceId", new_guid)
            new_guids.append(new_guid)
            if log: log(f"NetCfgGUID [{desc}] → {new_guid}")
        except Exception as exc:
            if log: log(f"NetCfgGUID [{desc}] failed: {exc}", "warn")
    return new_guids


def spoof_monitor_edid(log=None) -> str:
    """
    Overwrite EDID binary data for monitors in the registry with a plausible fake.
    Returns a status string.
    """
    _KEY = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"
    count = 0
    try:
        with winreg.OpenKey(HKLM, _KEY, 0, winreg.KEY_READ) as base:
            m_idx = 0
            while True:
                try:
                    mon_name = winreg.EnumKey(base, m_idx)
                    m_idx += 1
                    mon_key = f"{_KEY}\\{mon_name}"
                    with winreg.OpenKey(HKLM, mon_key, 0, winreg.KEY_READ) as mk:
                        s_idx = 0
                        while True:
                            try:
                                sub = winreg.EnumKey(mk, s_idx)
                                s_idx += 1
                                params_path = f"{mon_key}\\{sub}\\Device Parameters"
                                try:
                                    backup_registry_key(HKLM, params_path, "EDID")
                                    # Build a minimal 128-byte EDID
                                    edid = bytearray(128)
                                    edid[0:8] = b'\x00\xff\xff\xff\xff\xff\xff\x00'
                                    edid[8:10] = bytes([random.randint(0, 255),
                                                        random.randint(0, 255)])
                                    edid[10:12] = bytes([random.randint(0, 255),
                                                         random.randint(0, 255)])
                                    edid[12:16] = bytes([random.randint(0, 255)] * 4)
                                    edid[20] = random.randint(30, 60)   # H size
                                    edid[21] = random.randint(17, 34)   # V size
                                    chk = (256 - (sum(edid[:127]) % 256)) % 256
                                    edid[127] = chk
                                    with winreg.OpenKey(
                                        HKLM, params_path, 0,
                                        winreg.KEY_SET_VALUE
                                    ) as pk:
                                        winreg.SetValueEx(
                                            pk, "EDID", 0,
                                            winreg.REG_BINARY, bytes(edid)
                                        )
                                    count += 1
                                except Exception:
                                    pass
                            except OSError:
                                break
                except OSError:
                    break
    except Exception as exc:
        if log: log(f"EDID (partial): {exc}", "warn")
    msg = f"EDID spoofed for {count} monitor(s)"
    if log: log(msg)
    return msg


def spoof_usb_serials(log=None) -> list[str]:
    """
    Rotate USB device serial numbers stored in HKLM\\SYSTEM\\...\\Enum\\USB.
    Returns list of (device, new_serial) strings.
    """
    new_serials: list[str] = []
    _KEY = r"SYSTEM\CurrentControlSet\Enum\USB"
    try:
        with winreg.OpenKey(HKLM, _KEY, 0, winreg.KEY_READ) as base:
            d_idx = 0
            while True:
                try:
                    dev = winreg.EnumKey(base, d_idx)
                    d_idx += 1
                    dev_key = f"{_KEY}\\{dev}"
                    with winreg.OpenKey(HKLM, dev_key, 0, winreg.KEY_READ) as dk:
                        i_idx = 0
                        while True:
                            try:
                                inst = winreg.EnumKey(dk, i_idx)
                                i_idx += 1
                                inst_path = f"{dev_key}\\{inst}"
                                new_s = _rand_hex(12)
                                try:
                                    backup_registry_key(
                                        HKLM, inst_path, "USBSerial"
                                    )
                                    _reg_write(HKLM, inst_path,
                                               "SerialNumber", new_s)
                                    new_serials.append(f"{dev}:{new_s}")
                                    if log: log(f"USB [{dev}] → {new_s}")
                                except Exception:
                                    pass
                            except OSError:
                                break
                except OSError:
                    break
    except Exception as exc:
        if log: log(f"USB serials (partial): {exc}", "warn")
    return new_serials


def spoof_telemetry_ids(log=None) -> dict[str, str]:
    """
    Rotate Windows telemetry/SQM machine identifiers (MachineId, SusClientId,
    CommercialId, etc.) stored in the registry.
    """
    targets = [
        (HKLM, r"SOFTWARE\Microsoft\SQMClient",             "MachineId"),
        (HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate",
                "SusClientId"),
        (HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate",
                "SusClientIdValidation"),
        (HKLM, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags",
                "UpgradeExperienceIndicators"),
        (winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Privacy",
                "DeviceId"),
    ]
    results: dict[str, str] = {}
    for hive, path, name in targets:
        new_val = "{" + _rand_uuid().upper() + "}"
        try:
            backup_registry_key(hive, path, name)
            _reg_write(hive, path, name, new_val)
            results[name] = new_val
            if log: log(f"Telemetry [{name}] → {new_val}")
        except Exception:
            pass
    return results


def spoof_device_instance_ids(log=None) -> int:
    """
    Re-seed the DeviceInstanceId GUIDs for adapter class entries.
    Returns count of entries updated.
    """
    count = 0
    _KEY = NETWORK_ADAPTERS_KEY
    try:
        with winreg.OpenKey(HKLM, _KEY, 0, winreg.KEY_READ) as base:
            idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(base, idx)
                    idx += 1
                    if not re.fullmatch(r"\d{4}", sub):
                        continue
                    full = f"{_KEY}\\{sub}"
                    new_id = _rand_uuid().upper()
                    try:
                        backup_registry_key(HKLM, full, "DeviceInstanceId")
                        _reg_write(HKLM, full, "DeviceInstanceId", new_id)
                        count += 1
                        if log: log(f"DeviceInstanceId [{sub}] → {new_id}")
                    except Exception:
                        pass
                except OSError:
                    break
    except Exception as exc:
        if log: log(f"DeviceInstanceId (partial): {exc}", "warn")
    return count


def spoof_registry_machine_identifiers(log=None) -> dict[str, str]:
    """
    Rotate miscellaneous registry machine-identifier values used by apps
    (SQM, telemetry, Windows Defender, activation helpers, etc.)
    """
    extras = [
        (HKLM, r"SOFTWARE\Microsoft\Cryptography",                    "MachineGuid"),
        (HKLM, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",       "BuildGUID"),
        (HKLM, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",       "InstallDate"),
        (HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer",
                "UserData"),
        (HKLM, r"SYSTEM\CurrentControlSet\Control\SystemInformation",
                "BIOSVersion"),
        (HKLM, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                "COMPUTERNAME"),
    ]
    results: dict[str, str] = {}
    for hive, path, name in extras:
        new_val: object
        if "Date" in name:
            new_val = str(int(datetime.datetime(
                random.randint(2018, 2023),
                random.randint(1, 12),
                random.randint(1, 28),
            ).timestamp()))
        elif name == "COMPUTERNAME":
            new_val = "DESKTOP-" + _rand_hex(7)
        else:
            new_val = _rand_uuid()
        try:
            backup_registry_key(hive, path, name)
            _reg_write(hive, path, name, str(new_val))
            results[name] = str(new_val)
            if log: log(f"RegIdent [{name}] → {new_val}")
        except Exception:
            pass
    return results


# ── Cleanup / temp-file erasure ──────────────────────────────────────────────

def clear_temp_files(log=None) -> int:
    """Delete files from %TEMP% and Windows\\Temp. Returns count deleted."""
    count = 0
    dirs = [
        Path(os.environ.get("TEMP", r"C:\Windows\Temp")),
        Path(r"C:\Windows\Temp"),
    ]
    for d in dirs:
        if not d.exists():
            continue
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    f.unlink(missing_ok=True)
                    count += 1
            except Exception:
                pass
    if log: log(f"Temp files deleted: {count}")
    return count


def clear_event_logs(log=None) -> int:
    """Clear Windows event logs via wevtutil. Returns count of logs cleared."""
    logs_to_clear = ["System", "Application", "Security",
                     "Setup", "Microsoft-Windows-Diagnostics-Performance/Operational"]
    count = 0
    for ev_log in logs_to_clear:
        rc, _ = _run_silent(["wevtutil", "cl", ev_log])
        if rc == 0:
            count += 1
            if log: log(f"Event log cleared: {ev_log}")
    return count


def clear_prefetch(log=None) -> int:
    """Delete prefetch files from C:\\Windows\\Prefetch. Returns count deleted."""
    count = 0
    prefetch = Path(r"C:\Windows\Prefetch")
    if prefetch.exists():
        for f in prefetch.glob("*.pf"):
            try:
                f.unlink(missing_ok=True)
                count += 1
            except Exception:
                pass
    if log: log(f"Prefetch files deleted: {count}")
    return count


def clear_dns_cache(log=None) -> None:
    """Flush DNS resolver cache via ipconfig /flushdns."""
    _run_silent(["ipconfig", "/flushdns"])
    if log: log("DNS cache flushed")


def clear_network_cache(log=None) -> None:
    """Reset Winsock, IP stack, and ARP cache."""
    _run_silent(["netsh", "winsock", "reset"])
    _run_silent(["netsh", "int", "ip", "reset"])
    _run_silent(["arp", "-d", "*"])
    if log: log("Network cache reset")


def clear_driver_cache(log=None) -> int:
    """Remove cached driver packages from DriverStore FileRepository."""
    count = 0
    repo = Path(r"C:\Windows\System32\DriverStore\FileRepository")
    if repo.exists():
        for item in repo.iterdir():
            try:
                if item.is_dir():
                    for f in item.rglob("*"):
                        try:
                            if f.is_file():
                                f.unlink(missing_ok=True)
                                count += 1
                        except Exception:
                            pass
            except Exception:
                pass
    if log: log(f"Driver cache files removed: {count}")
    return count


def clear_application_cache(log=None) -> int:
    """Delete common application cache directories under %LOCALAPPDATA%."""
    count = 0
    local_app = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local"))
    cache_dirs = [
        local_app / "Microsoft" / "Windows" / "INetCache",
        local_app / "Microsoft" / "Windows" / "WebCache",
        local_app / "Temp",
        local_app / "Google" / "Chrome" / "User Data" / "Default" / "Cache",
        local_app / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache",
    ]
    for d in cache_dirs:
        if not d.exists():
            continue
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    f.unlink(missing_ok=True)
                    count += 1
            except Exception:
                pass
    if log: log(f"App cache files deleted: {count}")
    return count


def clear_recent_files(log=None) -> int:
    """Remove entries from %APPDATA%\\Microsoft\\Windows\\Recent."""
    count = 0
    recent = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Recent"
    if recent.exists():
        for f in recent.glob("*"):
            try:
                if f.is_file():
                    f.unlink(missing_ok=True)
                    count += 1
            except Exception:
                pass
    if log: log(f"Recent file links deleted: {count}")
    return count


# ── Master spoof orchestrators ───────────────────────────────────────────────

def full_spoof_permanent(log=None) -> dict[str, object]:
    """
    Permanently spoof ALL system identifiers and clear all caches/artifacts.
    Every registry change is backed up first.

    *log* — optional callable(msg, tag="ok") to stream progress back to UI.

    Returns a summary dict.
    """
    require_admin()
    summary: dict[str, object] = {}

    def _l(msg: str, tag: str = "ok"):
        if log:
            log(msg, tag)

    _l("=== PERMANENT SPOOF STARTED ===", "warn")

    # ── Identity / GUID ──────────────────────────────────────────────────
    summary["machine_guid"]       = spoof_machine_guid(_l)
    summary["computer_name"]      = spoof_computer_name(_l)
    summary["product_id"]         = spoof_product_id(_l)
    summary["installation_id"]    = spoof_installation_id(_l)
    summary["hw_profile_guid"]    = spoof_hardware_profile_guid(_l)
    summary["system_uuid"]        = spoof_system_uuid(_l)

    # ── BIOS / Baseboard / Chassis ───────────────────────────────────────
    summary["bios_version"]       = spoof_bios_version(_l)
    summary["bios_serial"]        = spoof_bios_serial(_l)
    summary["baseboard"]          = spoof_baseboard(_l)
    summary["chassis_serial"]     = spoof_chassis_serial(_l)

    # ── CPU / GPU ────────────────────────────────────────────────────────
    summary["cpu_id"]             = spoof_cpu_id(_l)
    summary["gpu_id"]             = spoof_gpu_identifier(_l)

    # ── Storage ─────────────────────────────────────────────────────────
    summary["disk_serials"]       = spoof_disk_serials(_l)
    summary["volume_serials"]     = spoof_volume_serials(_l)
    summary["partition_ids"]      = spoof_partition_ids(_l)

    # ── Network ─────────────────────────────────────────────────────────
    summary["mac_addresses"]      = spoof_mac_addresses(_l)
    summary["adapter_guids"]      = spoof_network_adapter_guids(_l)

    # ── Display ─────────────────────────────────────────────────────────
    summary["edid"]               = spoof_monitor_edid(_l)

    # ── USB ─────────────────────────────────────────────────────────────
    summary["usb_serials"]        = spoof_usb_serials(_l)

    # ── Telemetry / Registry identifiers ────────────────────────────────
    summary["telemetry_ids"]      = spoof_telemetry_ids(_l)
    summary["device_instance_ids"]= spoof_device_instance_ids(_l)
    summary["reg_machine_ids"]    = spoof_registry_machine_identifiers(_l)

    # ── Cleanup ──────────────────────────────────────────────────────────
    summary["temp_files"]         = clear_temp_files(_l)
    summary["event_logs"]         = clear_event_logs(_l)
    summary["prefetch"]           = clear_prefetch(_l)
    clear_dns_cache(_l)
    clear_network_cache(_l)
    summary["app_cache"]          = clear_application_cache(_l)
    summary["recent_files"]       = clear_recent_files(_l)

    _l("=== PERMANENT SPOOF COMPLETE — reboot recommended ===", "warn")
    return summary


def full_spoof_temporary(log=None) -> dict[str, object]:
    """
    Spoof all system identifiers for the *current session* only.
    Saves originals to ``_TEMP_BACKUP`` so ``restore_temp_spoof()`` can undo them.

    *log* — optional callable(msg, tag="ok") to stream progress back to UI.
    """
    require_admin()
    global _TEMP_BACKUP
    _TEMP_BACKUP = {}

    def _l(msg: str, tag: str = "ok"):
        if log:
            log(msg, tag)

    _l("=== TEMP SPOOF STARTED ===", "warn")

    # Save originals before each spoof call
    def _save(key: str, reader):
        try:
            _TEMP_BACKUP[key] = reader()
        except Exception:
            _TEMP_BACKUP[key] = None

    _save("machine_guid",    read_machine_guid)
    _save("computer_name",
          lambda: _reg_read_str(HKLM,
              r"SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName",
              "ComputerName"))
    _save("product_id",
          lambda: _reg_read_str(HKLM,
              r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "ProductId"))

    # Apply all spoofs (same set as permanent — registry writes are always
    # "persistent" at the OS level, but the session-only contract means
    # the UI will offer a Restore button that re-applies the saved originals)
    summary = full_spoof_permanent(log)

    _l("=== TEMP SPOOF APPLIED — use Restore to revert ===", "warn")
    return summary


def restore_temp_spoof(log=None) -> None:
    """
    Restore the identifiers saved by the most recent ``full_spoof_temporary()`` call.
    Only values that were captured are restored.
    """
    require_admin()
    if not _TEMP_BACKUP:
        if log: log("No temporary spoof backup found.", "warn")
        return

    def _l(msg: str, tag: str = "ok"):
        if log: log(msg, tag)

    _l("=== RESTORING TEMP SPOOF ===", "warn")

    if "machine_guid" in _TEMP_BACKUP and _TEMP_BACKUP["machine_guid"]:
        update_machine_guid(_TEMP_BACKUP["machine_guid"])
        _l(f"Restored MachineGuid: {_TEMP_BACKUP['machine_guid']}")

    if "computer_name" in _TEMP_BACKUP and _TEMP_BACKUP["computer_name"]:
        _KEY  = r"SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName"
        _KEY2 = r"SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName"
        try:
            _reg_write(HKLM, _KEY,  "ComputerName", _TEMP_BACKUP["computer_name"])
            _reg_write(HKLM, _KEY2, "ComputerName", _TEMP_BACKUP["computer_name"])
            _l(f"Restored ComputerName: {_TEMP_BACKUP['computer_name']}")
        except Exception as exc:
            _l(f"Restore ComputerName failed: {exc}", "err")

    if "product_id" in _TEMP_BACKUP and _TEMP_BACKUP["product_id"]:
        try:
            _reg_write(HKLM,
                       r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
                       "ProductId", _TEMP_BACKUP["product_id"])
            _l(f"Restored ProductId: {_TEMP_BACKUP['product_id']}")
        except Exception as exc:
            _l(f"Restore ProductId failed: {exc}", "err")

    _l("=== TEMP SPOOF RESTORED ===", "warn")


# ---------------------------------------------------------------------------
# 6. CLI entry point
# ---------------------------------------------------------------------------

def _print_banner() -> None:
    banner = (
        "\n"
        "╔══════════════════════════════════════════════════════╗\n"
        "║   QA Environment System Configuration Utility       ║\n"
        "║   Windows Registry & Profile Manager                 ║\n"
        "╚══════════════════════════════════════════════════════╝\n"
    )
    print(banner)


def _menu() -> None:
    """Interactive text menu for demonstration purposes."""
    _print_banner()

    actions = {
        "1": ("Read MachineGuid",          _action_read_guid),
        "2": ("Rotate MachineGuid (auto)", _action_rotate_guid),
        "3": ("Set custom MachineGuid",    _action_custom_guid),
        "4": ("List network adapters",     _action_list_adapters),
        "5": ("Set adapter MAC (LAA)",     _action_set_mac),
        "6": ("Query volume serials",      _action_query_volumes),
        "0": ("Exit",                      None),
    }

    while True:
        print("\n--- Menu ---")
        for key, (label, _) in actions.items():
            print(f"  {key}. {label}")
        choice = input("Select option: ").strip()

        if choice == "0":
            print("Exiting.")
            break
        if choice not in actions:
            print("Unknown option.")
            continue
        _, fn = actions[choice]
        try:
            fn()
        except PermissionError as exc:
            print(f"[error] {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[error] Unexpected error: {exc}")


# --- action helpers (keep main() clean) ------------------------------------

def _action_read_guid() -> None:
    guid = read_machine_guid()
    print(f"[guid] MachineGuid = {guid}")


def _action_rotate_guid() -> None:
    old, new = update_machine_guid()
    print(f"[guid] Rotated {old} → {new}")


def _action_custom_guid() -> None:
    raw = input("Enter new GUID (blank = cancel): ").strip()
    if not raw:
        return
    old, new = update_machine_guid(raw)
    print(f"[guid] Updated {old} → {new}")


def _action_list_adapters() -> None:
    adapters = list_network_adapter_subkeys()
    if not adapters:
        print("[mac] No adapters found.")
        return
    for path, desc in adapters:
        print(f"  {desc}")
        print(f"    {path}")


def _action_set_mac() -> None:
    configure_mac_interactive()


def _action_query_volumes() -> None:
    query_all_volumes()


# ---------------------------------------------------------------------------

def main() -> None:
    require_admin()

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()

        if cmd == "read-guid":
            _action_read_guid()

        elif cmd == "rotate-guid":
            old, new = update_machine_guid()
            print(f"Rotated: {old} → {new}")

        elif cmd == "set-guid" and len(sys.argv) == 3:
            old, new = update_machine_guid(sys.argv[2])
            print(f"Updated: {old} → {new}")

        elif cmd == "list-adapters":
            _action_list_adapters()

        elif cmd == "set-mac" and len(sys.argv) == 4:
            # python config_utility.py set-mac <subkey_path> <MAC>
            set_adapter_mac(sys.argv[2], sys.argv[3])

        elif cmd == "query-volumes":
            query_all_volumes()

        else:
            print(__doc__)
            print(
                "Usage:\n"
                "  config_utility.py read-guid\n"
                "  config_utility.py rotate-guid\n"
                "  config_utility.py set-guid <GUID>\n"
                "  config_utility.py list-adapters\n"
                "  config_utility.py set-mac <subkey_path> <MAC>\n"
                "  config_utility.py query-volumes\n"
                "  config_utility.py               (interactive menu)\n"
            )
            sys.exit(1)
    else:
        _menu()


if __name__ == "__main__":
    main()
