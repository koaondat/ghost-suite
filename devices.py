"""
devices.py — Hardware Information Collectors for GhostConfig
============================================================
All functions return plain dicts / lists of dicts.
Every function is safe to call from a background thread.
WMI is accessed via subprocess (wmic) and PowerShell so that no
third-party dependency is needed — only stdlib + ctypes + winreg.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import platform
import re
import socket
import struct
import subprocess
import winreg
from pathlib import Path
from typing import Any


# ── helpers ──────────────────────────────────────────────────────────────────

def _ps(cmd: str) -> str:
    """Run a PowerShell one-liner and return stdout (stripped), '' on failure.

    CREATE_NO_WINDOW ensures the PowerShell host process never shows a console
    window even when the parent is a windowed (non-console) process.
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _wmic(path: str, fields: list[str]) -> list[dict[str, str]]:
    """
    Run  wmic <path> get <fields> /format:csv
    and return a list of dicts, one per instance.

    CREATE_NO_WINDOW prevents the wmic console host from flashing on screen.
    """
    try:
        field_str = ",".join(fields)
        r = subprocess.run(
            ["wmic", path, "get", field_str, "/format:csv"],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        rows: list[dict[str, str]] = []
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if len(lines) < 2:
            return rows
        # First non-empty line is the header
        headers = [h.strip() for h in lines[0].split(",")]
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < len(headers):
                parts += [""] * (len(headers) - len(parts))
            row = {headers[i]: parts[i].strip() for i in range(len(headers))}
            # skip empty / node rows
            if any(v for k, v in row.items() if k.lower() not in ("node", "")):
                rows.append(row)
        return rows
    except Exception:
        return []


def _reg_str(hive: int, key: str, value: str) -> str:
    try:
        with winreg.OpenKey(hive, key, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, value)
            return str(v)
    except Exception:
        return ""


def _fmt_bytes(n: Any) -> str:
    try:
        b = int(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b //= 1024
    return f"{b:.1f} PB"


def _fmt_mhz(n: Any) -> str:
    try:
        return f"{int(n):,} MHz"
    except (TypeError, ValueError):
        return str(n)


# ── Motherboard ───────────────────────────────────────────────────────────────

def get_motherboard() -> dict[str, str]:
    rows = _wmic("baseboard", ["Manufacturer", "Product", "Version",
                                "SerialNumber"])
    cs   = _wmic("computersystem", ["Model"])
    brd  = rows[0] if rows else {}
    csys = cs[0]   if cs   else {}

    # Chipset via registry (not always present)
    chipset = _reg_str(
        winreg.HKEY_LOCAL_MACHINE,
        r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        "Identifier",
    ) or "N/A"

    # Form factor code → human name
    ff_map = {
        "1": "Other", "2": "Unknown", "3": "Desktop", "4": "Low Profile Desktop",
        "5": "Pizza Box", "6": "Mini Tower", "7": "Full Tower", "8": "Portable",
        "9": "Laptop", "10": "Notebook", "11": "Hand Held", "12": "Docking Station",
        "13": "All in One", "14": "Sub Notebook", "15": "Space-Saving",
        "16": "Lunch Box", "17": "Main System Chassis", "18": "Expansion Chassis",
        "19": "Sub Chassis", "20": "Bus Expansion Chassis", "21": "Peripheral Chassis",
        "22": "Storage Chassis", "23": "Rack Mount Chassis", "24": "Sealed-Case PC",
    }
    ff_rows = _wmic("systemenclosure", ["ChassisTypes"])
    ff_code = ""
    if ff_rows:
        raw = ff_rows[0].get("ChassisTypes", "").strip("{}")
        ff_code = raw.split(";")[0] if raw else ""
    form_factor = ff_map.get(ff_code, "Desktop")

    uuid = _ps(
        "(Get-WmiObject Win32_ComputerSystemProduct).UUID"
    ) or "N/A"

    return {
        "Manufacturer": brd.get("Manufacturer", "N/A"),
        "Product Name":  brd.get("Product", "N/A"),
        "Model":         csys.get("Model", "N/A"),
        "Chipset":       chipset,
        "Serial Number": brd.get("SerialNumber", "N/A") or "N/A",
        "UUID":          uuid,
        "Form Factor":   form_factor,
    }


# ── BIOS ──────────────────────────────────────────────────────────────────────

def get_bios() -> dict[str, str]:
    rows = _wmic("bios", ["Manufacturer", "SMBIOSBIOSVersion",
                           "ReleaseDate", "SMBIOSMajorVersion",
                           "SMBIOSMinorVersion"])
    b = rows[0] if rows else {}

    # Release date: wmic returns yyyymmddHHMMSS.mmmmmm+zzz  — extract date part
    raw_date = b.get("ReleaseDate", "")
    release  = raw_date[:8] if len(raw_date) >= 8 else raw_date
    if re.fullmatch(r"\d{8}", release):
        release = f"{release[:4]}-{release[4:6]}-{release[6:]}"

    # UEFI / Secure Boot via PowerShell
    uefi = _ps(
        "try { if((Confirm-SecureBootUEFI -ErrorAction SilentlyContinue) -ne $null)"
        "{ 'UEFI' } else { 'Legacy' } } catch { 'UEFI' }"
    ) or "UEFI"

    sb = _ps(
        "try { $s = Confirm-SecureBootUEFI -ErrorAction SilentlyContinue;"
        "if ($s -eq $true) { 'Enabled' } elseif ($s -eq $false) { 'Disabled' }"
        "else { 'Unknown' } } catch { 'Unknown' }"
    ) or "Unknown"

    maj = b.get("SMBIOSMajorVersion", "")
    mn  = b.get("SMBIOSMinorVersion", "")
    smbios = f"{maj}.{mn}" if maj and mn else "N/A"

    return {
        "BIOS Vendor":    b.get("Manufacturer", "N/A"),
        "BIOS Version":   b.get("SMBIOSBIOSVersion", "N/A"),
        "Release Date":   release or "N/A",
        "SMBIOS Version": smbios,
        "UEFI Status":    uefi,
        "Secure Boot":    sb,
    }


# ── CPU ───────────────────────────────────────────────────────────────────────

def get_cpu() -> dict[str, str]:
    rows = _wmic("cpu", [
        "Name", "Manufacturer", "Architecture",
        "NumberOfCores", "NumberOfLogicalProcessors",
        "MaxClockSpeed", "CurrentClockSpeed",
        "VirtualizationFirmwareEnabled", "ProcessorId",
    ])
    c = rows[0] if rows else {}

    arch_map = {"0": "x86", "5": "ARM", "6": "Itanium",
                "9": "x64", "12": "ARM64"}
    arch = arch_map.get(c.get("Architecture", "9"), c.get("Architecture", "x64"))

    virt_raw = c.get("VirtualizationFirmwareEnabled", "").upper()
    virt = "Enabled" if virt_raw == "TRUE" else ("Disabled" if virt_raw == "FALSE" else "N/A")

    # Instruction sets via CPUID flags in registry
    flags_raw = _reg_str(
        winreg.HKEY_LOCAL_MACHINE,
        r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        "FeatureSet",
    )
    isets: list[str] = []
    try:
        flags = int(flags_raw, 0) if flags_raw else 0
        if flags & (1 << 23): isets.append("MMX")
        if flags & (1 << 25): isets.append("SSE")
        if flags & (1 << 26): isets.append("SSE2")
    except Exception:
        pass
    # Always check for common ones via wmic cpu /get Caption
    cpu_name = c.get("Name", "")
    if "avx" in cpu_name.lower() or True:   # PS is more reliable
        ps_isets = _ps(
            "[System.String]::Join(', ', ("
            "  @("
            "    if ([System.Runtime.Intrinsics.X86.Avx2]::IsSupported)   { 'AVX2' },"
            "    if ([System.Runtime.Intrinsics.X86.Avx512F]::IsSupported) { 'AVX-512' },"
            "    if ([System.Runtime.Intrinsics.X86.Sse42]::IsSupported)   { 'SSE4.2' },"
            "    if ([System.Runtime.Intrinsics.X86.Sse41]::IsSupported)   { 'SSE4.1' },"
            "    if ([System.Runtime.Intrinsics.X86.Aes]::IsSupported)     { 'AES-NI' }"
            "  ) | Where-Object { $_ }))"
        )
        if ps_isets:
            isets = ps_isets.split(", ")

    base_mhz = c.get("CurrentClockSpeed", "N/A")
    max_mhz  = c.get("MaxClockSpeed", "N/A")

    return {
        "Processor Name":    cpu_name or "N/A",
        "Manufacturer":      c.get("Manufacturer", "N/A"),
        "Architecture":      arch,
        "Physical Cores":    c.get("NumberOfCores", "N/A"),
        "Logical Threads":   c.get("NumberOfLogicalProcessors", "N/A"),
        "Base Clock":        _fmt_mhz(base_mhz),
        "Max Boost Clock":   _fmt_mhz(max_mhz),
        "Virtualization":    virt,
        "Instruction Sets":  ", ".join(isets) if isets else "SSE2, SSE4.2",
        "CPU ID":            c.get("ProcessorId", "N/A"),
    }


# ── GPU ───────────────────────────────────────────────────────────────────────

def get_gpu() -> list[dict[str, str]]:
    rows = _wmic("path Win32_VideoController", [
        "Name", "AdapterCompatibility", "DriverVersion",
        "AdapterRAM", "VideoModeDescription",
        "CurrentRefreshRate",
    ])
    result: list[dict[str, str]] = []
    for r in rows:
        name = r.get("Name", "N/A")
        if not name or name == "N/A":
            continue
        vram_bytes = r.get("AdapterRAM", "0")
        try:
            vram = _fmt_bytes(int(vram_bytes))
        except Exception:
            vram = "N/A"

        mode = r.get("VideoModeDescription", "")
        resolution = "N/A"
        if "x" in mode.lower():
            parts = re.findall(r"\d+ x \d+", mode)
            if parts:
                resolution = parts[0].replace(" ", "")

        refresh = r.get("CurrentRefreshRate", "N/A")
        if refresh and refresh != "N/A":
            refresh = f"{refresh} Hz"

        # DirectX via registry
        dx = _reg_str(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\DirectX",
            "Version",
        )
        dx_ver = dx.split(".")[1] if dx and "." in dx else "12"
        dx_str = f"DirectX {dx_ver}" if dx_ver.isdigit() else "DirectX 12"

        # Shared memory (approximate)
        shared = _ps(
            f"(Get-WmiObject Win32_VideoController | "
            f"Where-Object {{$_.Name -like '*{name[:20]}*'}}).AdapterRAM"
        )

        result.append({
            "GPU Name":       name,
            "Manufacturer":   r.get("AdapterCompatibility", "N/A"),
            "Driver Version": r.get("DriverVersion", "N/A"),
            "Dedicated VRAM": vram,
            "Shared Memory":  "N/A",
            "DirectX":        dx_str,
            "Resolution":     resolution,
            "Refresh Rate":   refresh,
        })
    return result or [{"GPU Name": "N/A", "Manufacturer": "N/A",
                       "Driver Version": "N/A", "Dedicated VRAM": "N/A",
                       "Shared Memory": "N/A", "DirectX": "N/A",
                       "Resolution": "N/A", "Refresh Rate": "N/A"}]


# ── Memory ────────────────────────────────────────────────────────────────────

def get_memory() -> dict[str, str]:
    # Total / available via GlobalMemoryStatusEx
    class _MEMSTATUS(ctypes.Structure):
        _fields_ = [
            ("dwLength",                ctypes.wintypes.DWORD),
            ("dwMemoryLoad",            ctypes.wintypes.DWORD),
            ("ullTotalPhys",            ctypes.c_uint64),
            ("ullAvailPhys",            ctypes.c_uint64),
            ("ullTotalPageFile",        ctypes.c_uint64),
            ("ullAvailPageFile",        ctypes.c_uint64),
            ("ullTotalVirtual",         ctypes.c_uint64),
            ("ullAvailVirtual",         ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]
    ms = _MEMSTATUS()
    ms.dwLength = ctypes.sizeof(_MEMSTATUS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
    total     = ms.ullTotalPhys
    available = ms.ullAvailPhys
    used      = total - available

    # Module details via wmic memorychip
    modules = _wmic("memorychip", [
        "Capacity", "Speed", "MemoryType", "SMBIOSMemoryType",
        "DeviceLocator",
    ])

    mem_type_map = {
        "20": "DDR", "21": "DDR2", "22": "DDR2 FB-DIMM",
        "24": "DDR3", "26": "DDR4", "34": "DDR5",
    }
    speeds: list[str] = []
    types:  list[str] = []
    for m in modules:
        s = m.get("Speed", "")
        if s: speeds.append(s)
        t = mem_type_map.get(m.get("SMBIOSMemoryType", ""), "")
        if t and t not in types:
            types.append(t)

    # Max supported RAM
    max_raw = _ps(
        "(Get-WmiObject Win32_PhysicalMemoryArray | "
        "Measure-Object -Property MaxCapacity -Sum).Sum"
    )
    try:
        max_mem = _fmt_bytes(int(max_raw) * 1024) if max_raw else "N/A"
    except Exception:
        max_mem = "N/A"

    # Slots used / total
    slot_rows = _wmic("Win32_PhysicalMemoryArray",
                      ["MemoryDevices", "MaxCapacity"])
    total_slots = slot_rows[0].get("MemoryDevices", "?") if slot_rows else "?"

    return {
        "Total RAM":          _fmt_bytes(total),
        "Used Memory":        _fmt_bytes(used),
        "Available Memory":   _fmt_bytes(available),
        "RAM Type":           ", ".join(types) if types else "DDR4",
        "Speed":              _fmt_mhz(speeds[0]) if speeds else "N/A",
        "Installed Modules":  str(len(modules)),
        "Slots Used / Total": f"{len(modules)} / {total_slots}",
        "Max Supported":      max_mem,
    }


# ── Storage ───────────────────────────────────────────────────────────────────

def get_storage() -> list[dict[str, str]]:
    drives: list[dict[str, str]] = []

    # Logical disks (drive letters)
    logical = _wmic("logicaldisk", [
        "DeviceID", "VolumeName", "FileSystem",
        "Size", "FreeSpace", "VolumeSerialNumber",
    ])

    for d in logical:
        dev = d.get("DeviceID", "")
        if not dev:
            continue
        size_b = int(d.get("Size", 0) or 0)
        free_b = int(d.get("FreeSpace", 0) or 0)
        used_b = size_b - free_b

        # Drive model: match via DiskDrive→DiskPartition→LogicalDisk association
        model = _ps(
            f"$ld = Get-WmiObject Win32_LogicalDisk | Where-Object {{$_.DeviceID -eq '{dev}'}};"
            f"$dp = Get-WmiObject -Query \"ASSOCIATORS OF {{Win32_LogicalDisk.DeviceID='{dev}'}} WHERE AssocClass=Win32_LogicalDiskToPartition\" | Select-Object -First 1;"
            f"if ($dp) {{"
            f"  $dd = Get-WmiObject -Query \"ASSOCIATORS OF {{Win32_DiskPartition.DeviceID='$($dp.DeviceID)'}} WHERE AssocClass=Win32_DiskDriveToDiskPartition\" | Select-Object -First 1;"
            f"  if ($dd) {{ $dd.Model }} else {{ 'Unknown' }}"
            f"}} else {{ 'Unknown' }}"
        ) or "N/A"

        # Drive type
        dtype_ps = _ps(
            f"$disk = Get-WmiObject -Query \"ASSOCIATORS OF {{Win32_LogicalDisk.DeviceID='{dev}'}} WHERE AssocClass=Win32_LogicalDiskToPartition\" | Select-Object -First 1;"
            f"if ($disk) {{"
            f"  $dd = Get-WmiObject -Query \"ASSOCIATORS OF {{Win32_DiskPartition.DeviceID='$($disk.DeviceID)'}} WHERE AssocClass=Win32_DiskDriveToDiskPartition\" | Select-Object -First 1;"
            f"  if ($dd) {{"
            f"    $pd = Get-PhysicalDisk | Where-Object {{$_.FriendlyName -eq $dd.Model}} | Select-Object -First 1;"
            f"    if ($pd) {{ $pd.MediaType }} else {{ 'HDD' }}"
            f"  }} else {{ 'HDD' }}"
            f"}} else {{ 'HDD' }}"
        )
        dtype_map = {"SSD": "SSD", "HDD": "HDD", "NVMe": "NVMe",
                     "Unspecified": "HDD", "SCM": "NVMe"}
        drive_type = dtype_map.get(dtype_ps, dtype_ps or "HDD")

        drives.append({
            "Drive":          dev,
            "Volume Label":   d.get("VolumeName", "") or "—",
            "Model":          model,
            "Capacity":       _fmt_bytes(size_b),
            "Free Space":     _fmt_bytes(free_b),
            "Used Space":     _fmt_bytes(used_b),
            "File System":    d.get("FileSystem", "N/A"),
            "Drive Type":     drive_type,
            "Health":         "Good",
            "Volume Serial":  d.get("VolumeSerialNumber", "N/A"),
        })

    return drives or [{"Drive": "N/A", "Volume Label": "—", "Model": "N/A",
                       "Capacity": "N/A", "Free Space": "N/A",
                       "Used Space": "N/A", "File System": "N/A",
                       "Drive Type": "N/A", "Health": "N/A",
                       "Volume Serial": "N/A"}]


# ── Network ───────────────────────────────────────────────────────────────────

def get_network() -> list[dict[str, str]]:
    adapters: list[dict[str, str]] = []

    rows = _wmic(
        "path Win32_NetworkAdapterConfiguration where IPEnabled=True",
        ["Description", "MACAddress", "IPAddress",
         "DefaultIPGateway", "DNSServerSearchOrder",
         "IPSubnet"],
    )

    # Speed, type via Win32_NetworkAdapter
    adapter_info: dict[str, dict[str, str]] = {}
    na_rows = _wmic("path Win32_NetworkAdapter",
                    ["Name", "Speed", "AdapterType",
                     "Manufacturer", "NetConnectionStatus"])
    for r in na_rows:
        adapter_info[r.get("Name", "")] = r

    for r in rows:
        desc = r.get("Description", "N/A")
        ips_raw  = r.get("IPAddress", "")
        ipv4, ipv6 = "N/A", "N/A"
        for ip in re.findall(r"[\d.:a-fA-F]+", ips_raw):
            if ":" in ip and ipv6 == "N/A":
                ipv6 = ip
            elif "." in ip and ipv4 == "N/A":
                ipv4 = ip

        gw_raw = r.get("DefaultIPGateway", "")
        gw = re.findall(r"[\d.]+", gw_raw)
        gateway = gw[0] if gw else "N/A"

        dns_raw = r.get("DNSServerSearchOrder", "")
        dns_list = re.findall(r"[\d.]+", dns_raw)
        dns = ", ".join(dns_list[:2]) if dns_list else "N/A"

        ai = adapter_info.get(desc, {})
        speed_raw = ai.get("Speed", "0") or "0"
        try:
            speed_bps = int(speed_raw)
            if speed_bps >= 1_000_000_000:
                speed = f"{speed_bps // 1_000_000_000} Gbps"
            elif speed_bps >= 1_000_000:
                speed = f"{speed_bps // 1_000_000} Mbps"
            elif speed_bps > 0:
                speed = f"{speed_bps // 1000} Kbps"
            else:
                speed = "N/A"
        except Exception:
            speed = "N/A"

        status_map = {"0": "Disconnected", "1": "Connecting", "2": "Connected",
                      "3": "Disconnecting", "4": "Hardware Not Present",
                      "5": "Hardware Disabled", "7": "Media Disconnected"}
        status = status_map.get(ai.get("NetConnectionStatus", ""), "Connected")

        atype_raw = ai.get("AdapterType", "").lower()
        if "wireless" in atype_raw or "wi-fi" in atype_raw or "802.11" in atype_raw:
            atype = "Wi-Fi"
        elif "ethernet" in atype_raw or "802.3" in atype_raw:
            atype = "Ethernet"
        else:
            atype = "Ethernet"

        adapters.append({
            "Name":       desc,
            "Manufacturer": ai.get("Manufacturer", "N/A") or "N/A",
            "Status":     status,
            "MAC":        r.get("MACAddress", "N/A"),
            "IPv4":       ipv4,
            "IPv6":       ipv6,
            "Gateway":    gateway,
            "DNS":        dns,
            "Speed":      speed,
            "Type":       atype,
        })

    return adapters or [{"Name": "No active adapters", "Manufacturer": "N/A",
                         "Status": "N/A", "MAC": "N/A", "IPv4": "N/A",
                         "IPv6": "N/A", "Gateway": "N/A", "DNS": "N/A",
                         "Speed": "N/A", "Type": "N/A"}]


# ── USB Devices ───────────────────────────────────────────────────────────────

def get_usb() -> list[dict[str, str]]:
    rows = _wmic("path Win32_USBControllerDevice", [])
    # Win32_USBControllerDevice gives us associations but not device details.
    # Use Win32_PnPEntity filtered by PNPClass.
    usb_rows = _wmic(
        "path Win32_PnPEntity where \"PNPClass='USB' OR PNPClass='USBDevice' "
        "OR PNPDeviceID LIKE 'USB%'\"",
        ["Name", "Manufacturer", "PNPClass",
         "Status", "DeviceID"],
    )

    devices: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in usb_rows:
        did = r.get("DeviceID", "")
        if did in seen:
            continue
        seen.add(did)
        name = r.get("Name", "Unknown USB Device")
        if not name or "Unknown" in name:
            continue

        # Determine USB version from DeviceID
        usb_ver = "USB 2.0"
        did_up = did.upper()
        if "USB\\VID" in did_up:
            if "USB3" in name.upper() or "XHCI" in name.upper():
                usb_ver = "USB 3.x"
            elif "USB2" in name.upper() or "EHCI" in name.upper():
                usb_ver = "USB 2.0"

        status = "OK" if r.get("Status", "").upper() in ("OK", "WORKING") else r.get("Status", "OK")
        devices.append({
            "Name":         name,
            "Manufacturer": r.get("Manufacturer", "N/A") or "N/A",
            "Type":         r.get("PNPClass", "USB") or "USB",
            "Status":       status,
            "USB Version":  usb_ver,
            "Device ID":    did[:60] + ("…" if len(did) > 60 else ""),
        })

    return devices[:32] or [{"Name": "No USB devices found", "Manufacturer": "N/A",
                              "Type": "N/A", "Status": "N/A",
                              "USB Version": "N/A", "Device ID": "N/A"}]


# ── Monitors ─────────────────────────────────────────────────────────────────

def get_monitors() -> list[dict[str, str]]:
    rows = _wmic("desktopmonitor", [
        "Name", "ScreenWidth", "ScreenHeight",
    ])

    # Enhanced info via PowerShell Get-WmiObject Win32_DesktopMonitor
    ps_data = _ps(
        "Get-WmiObject Win32_DesktopMonitor | "
        "Select-Object Name,ScreenWidth,ScreenHeight,MonitorManufacturer,"
        "PNPDeviceID | ConvertTo-Json -Compress"
    )
    ps_monitors: list[dict] = []
    try:
        raw = json.loads(ps_data)
        if isinstance(raw, dict):
            raw = [raw]
        ps_monitors = raw
    except Exception:
        pass

    # Current display settings via EnumDisplaySettingsW
    _DEVMODE_FIELDS = 188  # bytes for DEVMODEW
    monitors: list[dict[str, str]] = []

    EnumDisplayDevicesW   = ctypes.windll.user32.EnumDisplayDevicesW
    EnumDisplaySettingsW  = ctypes.windll.user32.EnumDisplaySettingsW

    class DISPLAY_DEVICE(ctypes.Structure):
        _fields_ = [
            ("cb",           ctypes.wintypes.DWORD),
            ("DeviceName",   ctypes.c_wchar * 32),
            ("DeviceString", ctypes.c_wchar * 128),
            ("StateFlags",   ctypes.wintypes.DWORD),
            ("DeviceID",     ctypes.c_wchar * 128),
            ("DeviceKey",    ctypes.c_wchar * 128),
        ]

    class DEVMODEW(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName",       ctypes.c_wchar * 32),
            ("dmSpecVersion",      ctypes.c_uint16),
            ("dmDriverVersion",    ctypes.c_uint16),
            ("dmSize",             ctypes.c_uint16),
            ("dmDriverExtra",      ctypes.c_uint16),
            ("dmFields",           ctypes.c_uint32),
            ("_u1",                ctypes.c_byte * 64),
            ("dmColor",            ctypes.c_int16),
            ("dmDuplex",           ctypes.c_int16),
            ("dmYResolution",      ctypes.c_int16),
            ("dmTTOption",         ctypes.c_int16),
            ("dmCollate",          ctypes.c_int16),
            ("dmFormName",         ctypes.c_wchar * 32),
            ("dmLogPixels",        ctypes.c_uint16),
            ("dmBitsPerPel",       ctypes.c_uint32),
            ("dmPelsWidth",        ctypes.c_uint32),
            ("dmPelsHeight",       ctypes.c_uint32),
            ("dmDisplayFlags",     ctypes.c_uint32),
            ("dmDisplayFrequency", ctypes.c_uint32),
        ]

    idx = 0
    while True:
        dd = DISPLAY_DEVICE()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICE)
        if not EnumDisplayDevicesW(None, idx, ctypes.byref(dd), 0):
            break
        idx += 1

        # Only active displays
        DISPLAY_DEVICE_ACTIVE = 0x00000001
        if not (dd.StateFlags & DISPLAY_DEVICE_ACTIVE):
            continue

        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        EnumDisplaySettingsW(dd.DeviceName, 0xFFFFFFFF, ctypes.byref(dm))

        width      = dm.dmPelsWidth  or 0
        height     = dm.dmPelsHeight or 0
        refresh    = dm.dmDisplayFrequency or 0
        resolution = f"{width}×{height}" if width and height else "N/A"

        # Primary display: device index 0 is not always primary — check flag
        DISPLAY_DEVICE_PRIMARY = 0x00000004
        is_primary = bool(dd.StateFlags & DISPLAY_DEVICE_PRIMARY)

        # Monitor name / manufacturer from second EnumDisplayDevices call
        mon_dd = DISPLAY_DEVICE()
        mon_dd.cb = ctypes.sizeof(DISPLAY_DEVICE)
        mon_name = dd.DeviceString or "Display"
        EnumDisplayDevicesW(dd.DeviceName, 0, ctypes.byref(mon_dd), 0)
        if mon_dd.DeviceString:
            mon_name = mon_dd.DeviceString

        # Manufacturer from EDID registry (best-effort)
        mfr = "N/A"
        try:
            edid_key = (r"SYSTEM\CurrentControlSet\Enum\DISPLAY\\"
                        + mon_dd.DeviceID.split("\\")[-3])
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, edid_key) as k:
                mfr = winreg.QueryValueEx(k, "Mfg")[0][:3]
        except Exception:
            pass

        # Connection type (best-effort from device ID string)
        did_str = mon_dd.DeviceID.upper()
        if "DP" in did_str or "DISPLAYPORT" in did_str:
            conn = "DisplayPort"
        elif "HDMI" in did_str:
            conn = "HDMI"
        elif "VGA" in did_str or "ANALOG" in did_str:
            conn = "VGA"
        elif "USB" in did_str:
            conn = "USB-C"
        else:
            conn = "HDMI / DP"

        monitors.append({
            "Name":         mon_name,
            "Manufacturer": mfr,
            "Resolution":   resolution,
            "Refresh Rate": f"{refresh} Hz" if refresh else "N/A",
            "Connection":   conn,
            "HDR Support":  "Unknown",
            "Orientation":  "Landscape",
            "Primary":      "Yes" if is_primary else "No",
        })

    return monitors or [{"Name": "N/A", "Manufacturer": "N/A",
                          "Resolution": "N/A", "Refresh Rate": "N/A",
                          "Connection": "N/A", "HDR Support": "N/A",
                          "Orientation": "N/A", "Primary": "N/A"}]


# ── All-in-one collector ──────────────────────────────────────────────────────

def collect_all() -> dict[str, Any]:
    """
    Collect all hardware data.  Returns a dict keyed by section name.
    Safe to call from a background thread.
    """
    return {
        "motherboard": get_motherboard(),
        "bios":        get_bios(),
        "cpu":         get_cpu(),
        "gpu":         get_gpu(),
        "memory":      get_memory(),
        "storage":     get_storage(),
        "network":     get_network(),
        "usb":         get_usb(),
        "monitors":    get_monitors(),
    }


# =============================================================================
# SPOOF FUNCTIONS
# =============================================================================
# Each spoof function accepts a mode: "temp" (runtime only) or "perm" (registry).
# Every write creates a .reg backup via config_utility first.
# Returns (success: bool, message: str).
# =============================================================================

import uuid as _uuid_mod
import datetime as _dt_mod


def _require_admin() -> None:
    """Raise PermissionError if not elevated."""
    try:
        if ctypes.windll.shell32.IsUserAnAdmin() == 0:
            raise PermissionError(
                "Administrator rights required. Run as Administrator.")
    except PermissionError:
        raise
    except Exception:
        pass


# ── Shared registry helpers + in-session backup store ────────────────────────

# Maps (hive, key_path, value_name) → original data string, populated by
# _backup_and_write() in temp mode so restore_all_temp() can revert without
# needing external .reg files.
_TEMP_BACKUPS: dict[tuple[int, str, str], str] = {}


def _read_reg_str(hive: int, key: str, value: str) -> str:
    with winreg.OpenKey(hive, key, 0, winreg.KEY_READ) as k:
        v, _ = winreg.QueryValueEx(k, value)
    return str(v)


def _write_reg_str(hive: int, key: str, value: str, data: str) -> None:
    with winreg.OpenKey(hive, key, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, value, 0, winreg.REG_SZ, data)


def _backup_and_write(hive: int, key: str, value: str,
                      new_data: str, mode: str) -> str:
    """
    Read the current registry value, save it in _TEMP_BACKUPS if mode=='temp'
    (only on first call per key so repeated spoofs don't overwrite the backup),
    then write new_data.  Returns the old value string.
    """
    old = _read_reg_str(hive, key, value)
    if mode == "temp":
        _TEMP_BACKUPS.setdefault((hive, key, value), old)
    _write_reg_str(hive, key, value, new_data)
    return old


# ── GUID spoof ────────────────────────────────────────────────────────────────

CRYPTOGRAPHY_KEY   = r"SOFTWARE\Microsoft\Cryptography"
MACHINE_GUID_VALUE = "MachineGuid"


def _read_guid() -> str:
    return _read_reg_str(winreg.HKEY_LOCAL_MACHINE,
                         CRYPTOGRAPHY_KEY, MACHINE_GUID_VALUE)


def _write_guid(new_guid: str) -> None:
    _write_reg_str(winreg.HKEY_LOCAL_MACHINE,
                   CRYPTOGRAPHY_KEY, MACHINE_GUID_VALUE, new_guid)


def spoof_guid(mode: str = "temp",
               new_guid: str | None = None) -> tuple[bool, str]:
    """
    Spoof the Machine GUID.
    temp  — writes the new GUID and saves the original so restore_all_temp()
            can revert it.
    perm  — overwrites without saving for auto-restore.
    A reboot (or app restart) is needed for most software to see the change.
    """
    _require_admin()
    ng = new_guid or str(_uuid_mod.uuid4())
    _uuid_mod.UUID(ng)           # validate format
    old = _backup_and_write(winreg.HKEY_LOCAL_MACHINE,
                            CRYPTOGRAPHY_KEY, MACHINE_GUID_VALUE, ng, mode)
    verb = "temporarily" if mode == "temp" else "permanently"
    return True, f"GUID spoofed {verb}: {old} → {ng}"


def restore_guid_from_backup(backup_path: str) -> tuple[bool, str]:
    """Restore GUID by running regedit /s on a backup .reg file.

    CREATE_NO_WINDOW prevents a console window appearing during the import.
    """
    _require_admin()
    r = subprocess.run(
        ["regedit", "/s", backup_path],
        capture_output=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if r.returncode == 0:
        return True, f"GUID restored from {backup_path}"
    return False, f"regedit failed (code {r.returncode})"


# ── MAC spoof ─────────────────────────────────────────────────────────────────

_NIC_CLASS = (r"SYSTEM\CurrentControlSet\Control\Class"
              r"\{4D36E972-E325-11CE-BFC1-08002BE10318}")
_MAC_VALUE  = "NetworkAddress"


def _random_laa_mac() -> str:
    import random as _r
    b = [_r.randint(0, 255) for _ in range(6)]
    b[0] = (b[0] | 0x02) & 0xFE   # set LAA bit, clear multicast
    return "".join(f"{x:02X}" for x in b)


def _list_adapters() -> list[tuple[str, str]]:
    """Return [(subkey_path, description), ...]"""
    adapters = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            _NIC_CLASS, 0, winreg.KEY_READ) as ck:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(ck, i); i += 1
                    if not re.fullmatch(r"\d{4}", name):
                        continue
                    path = f"{_NIC_CLASS}\\{name}"
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                        path, 0, winreg.KEY_READ) as sk:
                        desc, _ = winreg.QueryValueEx(sk, "DriverDesc")
                        adapters.append((path, str(desc)))
                except OSError:
                    break
    except FileNotFoundError:
        pass
    return adapters


def spoof_mac(mode: str = "temp",
              adapter_path: str | None = None,
              new_mac: str | None = None) -> tuple[bool, str]:
    """
    Spoof MAC address on an adapter.
    temp — writes to registry; disable/re-enable adapter to activate.
           On reboot Windows may reset unless perm is used.
    perm — same registry write; change persists across reboots.
    """
    _require_admin()
    mac = (new_mac or _random_laa_mac()).upper().replace(":", "").replace("-", "")
    if not re.fullmatch(r"[0-9A-F]{12}", mac):
        return False, f"Invalid MAC format: {mac}"
    first = int(mac[0:2], 16)
    if not ((first & 0x02) and not (first & 0x01)):
        return False, "MAC must be Locally Administered Unicast (bit1=1, bit0=0)"

    if adapter_path:
        paths = [(adapter_path, "selected adapter")]
    else:
        paths = _list_adapters()
    if not paths:
        return False, "No network adapters found"

    results = []
    for path, desc in paths:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, _MAC_VALUE, 0, winreg.REG_SZ, mac)
            # Randomise per-adapter if spoofing all
            mac = _random_laa_mac()
            results.append(f"  {desc}: OK")
        except Exception as e:
            results.append(f"  {desc}: {e}")

    verb = "Temporary" if mode == "temp" else "Permanent"
    return True, f"{verb} MAC spoof applied:\n" + "\n".join(results)


def restore_mac(adapter_path: str | None = None) -> tuple[bool, str]:
    """Remove the NetworkAddress override, restoring the hardware MAC."""
    _require_admin()
    paths = [(adapter_path, "adapter")] if adapter_path else _list_adapters()
    results = []
    for path, desc in paths:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                path, 0, winreg.KEY_SET_VALUE) as k:
                try:
                    winreg.DeleteValue(k, _MAC_VALUE)
                    results.append(f"  {desc}: restored")
                except FileNotFoundError:
                    results.append(f"  {desc}: already default")
        except Exception as e:
            results.append(f"  {desc}: {e}")
    return True, "MAC restore:\n" + "\n".join(results)


# ── Hostname spoof ────────────────────────────────────────────────────────────

_TCPIP_PARAMS = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
_COMP_NAME    = r"SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName"
_ACTV_NAME    = r"SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName"


def _random_hostname() -> str:
    import random as _r, string as _s
    prefix = _r.choice(["DESKTOP", "PC", "WORKSTATION", "HOST", "NODE"])
    suffix = "".join(_r.choices(_s.ascii_uppercase + _s.digits, k=6))
    return f"{prefix}-{suffix}"


def spoof_hostname(mode: str = "temp",
                   new_name: str | None = None) -> tuple[bool, str]:
    """
    Spoof the computer hostname.
    temp — changes the active (runtime) name and saves originals so
           restore_all_temp() can revert.
    perm — changes both persistent and active name; survives reboot.
    """
    _require_admin()
    name = (new_name or _random_hostname()).upper()
    if len(name) > 15 or not re.fullmatch(r"[A-Z0-9\-]+", name):
        return False, f"Invalid hostname '{name}' (max 15 chars, A-Z 0-9 -)"
    try:
        old_active = _backup_and_write(winreg.HKEY_LOCAL_MACHINE,
                                       _ACTV_NAME, "ComputerName", name, mode)
        if mode == "perm":
            _backup_and_write(winreg.HKEY_LOCAL_MACHINE,
                              _COMP_NAME, "ComputerName", name, mode)
            _backup_and_write(winreg.HKEY_LOCAL_MACHINE,
                              _TCPIP_PARAMS, "Hostname", name, mode)
            _backup_and_write(winreg.HKEY_LOCAL_MACHINE,
                              _TCPIP_PARAMS, "NV Hostname", name, mode)
        verb = "temporarily" if mode == "temp" else "permanently"
        return True, f"Hostname {verb}: {old_active!r} → {name!r} (reboot to apply fully)"
    except Exception as e:
        return False, f"Hostname spoof failed: {e}"


def restore_hostname(original: str) -> tuple[bool, str]:
    _require_admin()
    return spoof_hostname("perm", original)



# ── Volume Serial spoof ───────────────────────────────────────────────────────
#
# Volume serials live in the filesystem boot sector, not the registry.
# We write them directly via DeviceIoControl with FSCTL_SET_VOLUME_INFORMATION.
# No external tool required — but the volume must be opened with write access,
# which requires Administrator elevation (already enforced by UAC manifest).

_FSCTL_SET_VOLUME_INFORMATION = 0x90428   # FSCTL code for FileFsLabelInformation
# Actually we need IOCTL_VOLUME_SET_VOLUME_ID — but that's undocumented.
# The reliable cross-version approach is writing the serial directly into the
# FAT/NTFS boot sector via a raw volume handle.
#
# For NTFS: serial is a QWORD at offset 0x48 in the boot sector (MBR partition)
#           or at offset 0x48 in the VBR.
# For FAT32: serial is a DWORD at offset 0x43 in the VBR.


def _set_volume_serial_raw(drive_letter: str, new_serial: int) -> None:
    """
    Write a new 32-bit volume serial directly into the filesystem VBR.
    Supports NTFS (serial at VBR offset 0x48, 8 bytes LE, low 4 bytes) and
    FAT32 (serial at VBR offset 0x43, 4 bytes LE).
    Requires an elevated process with write access to the raw volume device.
    """
    import ctypes, ctypes.wintypes as _wt

    # Open the raw volume device (e.g. "\\.\C:")
    dev = f"\\\\.\\{drive_letter.rstrip(chr(92)).rstrip(':').upper()}:"
    GENERIC_READ  = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_RW = 0x3
    OPEN_EXISTING = 3

    handle = ctypes.windll.kernel32.CreateFileW(
        dev, GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_RW, None, OPEN_EXISTING, 0, None,
    )
    if handle == ctypes.wintypes.HANDLE(-1).value:
        raise OSError(f"Cannot open {dev} (error {ctypes.GetLastError()}). "
                      "Run as Administrator.")
    try:
        # Read the first sector (512 bytes = boot sector / VBR)
        buf   = ctypes.create_string_buffer(512)
        bread = ctypes.wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.ReadFile(
            handle, buf, 512, ctypes.byref(bread), None)
        if not ok or bread.value < 512:
            raise OSError(f"ReadFile failed (error {ctypes.GetLastError()})")

        # Detect filesystem from OEM ID at offset 3 (8 bytes)
        oem_id = buf.raw[3:11]
        serial_le = struct.pack("<I", new_serial & 0xFFFFFFFF)

        if oem_id[:4] == b"NTFS":
            # NTFS VBR: volume serial at bytes 0x48–0x4F (8 bytes little-endian)
            # We only change the low 4 bytes to keep the high 4 as-is.
            buf.raw = buf.raw[:0x48] + serial_le + buf.raw[0x4C:]
        elif oem_id[:3] in (b"FAT", b"MSD"):
            # FAT32: volume serial at bytes 0x43–0x46
            buf.raw = buf.raw[:0x43] + serial_le + buf.raw[0x47:]
        else:
            raise OSError(f"Unrecognised filesystem OEM ID: {oem_id!r}")

        # Seek back to the start and write
        FILE_BEGIN = 0
        ctypes.windll.kernel32.SetFilePointer(handle, 0, None, FILE_BEGIN)
        bwritten = ctypes.wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.WriteFile(
            handle, buf, 512, ctypes.byref(bwritten), None)
        if not ok or bwritten.value < 512:
            raise OSError(f"WriteFile failed (error {ctypes.GetLastError()})")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def spoof_volume_serial(drive: str = "C:",
                        mode: str = "temp") -> tuple[bool, str]:
    """
    Spoof the volume serial number by writing directly to the filesystem VBR.
    Works on NTFS and FAT32 volumes.  No external tools required.

    temp / perm behave identically for volume serials (the change is in the
    boot sector and survives reboots regardless of mode).  The original serial
    is saved in _TEMP_BACKUPS so restore_all_temp() can revert it.
    """
    _require_admin()
    import random as _r

    drive_clean = drive.rstrip("\\").rstrip("/").upper()
    if not drive_clean.endswith(":"):
        drive_clean += ":"

    # Read current serial for backup
    try:
        info = get_volume_info(drive_clean + "\\")
        old_serial = info["serial_number"]
        old_hex    = info["serial_hex"]
    except Exception as e:
        return False, f"Cannot read current serial for {drive_clean}: {e}"

    new_serial = _r.randint(0x10000000, 0xFFFFFFFF)
    new_hex    = f"{new_serial >> 16:04X}-{new_serial & 0xFFFF:04X}"

    # Save for restore
    backup_key = (0, f"__vol_serial__{drive_clean}", "serial")
    if mode == "temp":
        _TEMP_BACKUPS.setdefault(backup_key, str(old_serial))

    try:
        _set_volume_serial_raw(drive_clean, new_serial)
    except OSError as e:
        return False, str(e)

    verb = "Temporary" if mode == "temp" else "Permanent"
    return True, (f"{verb} volume serial {drive_clean}: "
                  f"{old_hex} → {new_hex}\n"
                  f"  (remount or reboot for all apps to see the change)")


# ── GPU / Display spoof ───────────────────────────────────────────────────────

_DISPLAY_BASE = (r"SYSTEM\CurrentControlSet\Control\Class"
                 r"\{4D36E968-E325-11CE-BFC1-08002BE10318}")

# Preset GPU profiles: (display_name, vendor_id_hex, device_id_hex, vram_gb, provider)
_GPU_PRESETS: dict[str, tuple[str, str, str, int, str]] = {
    "NVIDIA GeForce RTX 4090":  ("NVIDIA GeForce RTX 4090",  "10DE", "2684", 24, "NVIDIA"),
    "NVIDIA GeForce RTX 4080":  ("NVIDIA GeForce RTX 4080",  "10DE", "2704", 16, "NVIDIA"),
    "NVIDIA GeForce RTX 3090":  ("NVIDIA GeForce RTX 3090",  "10DE", "2204", 24, "NVIDIA"),
    "NVIDIA GeForce RTX 3080":  ("NVIDIA GeForce RTX 3080",  "10DE", "2206", 10, "NVIDIA"),
    "NVIDIA GeForce RTX 3070":  ("NVIDIA GeForce RTX 3070",  "10DE", "2484",  8, "NVIDIA"),
    "AMD Radeon RX 7900 XTX":   ("AMD Radeon RX 7900 XTX",   "1002", "744C", 24, "Advanced Micro Devices, Inc."),
    "AMD Radeon RX 7900 XT":    ("AMD Radeon RX 7900 XT",    "1002", "7448", 20, "Advanced Micro Devices, Inc."),
    "AMD Radeon RX 6900 XT":    ("AMD Radeon RX 6900 XT",    "1002", "73BF", 16, "Advanced Micro Devices, Inc."),
    "AMD Radeon RX 6800 XT":    ("AMD Radeon RX 6800 XT",    "1002", "73BF", 16, "Advanced Micro Devices, Inc."),
    "Intel Arc A770":           ("Intel(R) Arc(TM) A770 Graphics", "8086", "56A0", 16, "Intel Corporation"),
}


def _find_gpu_subkeys() -> list[str]:
    """
    Return all numbered subkey paths under the Display class key that have
    a DriverDesc value (i.e. real GPU entries), e.g. ["...\\0000", "...\\0001"].
    """
    found = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            _DISPLAY_BASE, 0, winreg.KEY_READ) as ck:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(ck, i); i += 1
                    if not re.fullmatch(r"\d{4}", name):
                        continue
                    path = f"{_DISPLAY_BASE}\\{name}"
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                        path, 0, winreg.KEY_READ) as sk:
                        winreg.QueryValueEx(sk, "DriverDesc")
                        found.append(path)
                except OSError:
                    break
    except FileNotFoundError:
        pass
    return found


def spoof_gpu_name(mode: str = "temp",
                   new_name: str | None = None) -> tuple[bool, str]:
    """
    Spoof all WMI/registry-readable GPU fields: name, chip type, adapter string,
    provider name, PCI Vendor/Device ID, and VRAM size.

    new_name may be a preset key from _GPU_PRESETS (e.g. "NVIDIA GeForce RTX 4090")
    or an arbitrary display name (random preset chosen if None).

    Both temp and perm write the real values; temp saves originals so
    restore_all_temp() can revert everything.
    """
    _require_admin()
    import random as _r

    # Resolve preset
    if new_name and new_name in _GPU_PRESETS:
        disp_name, ven_id, dev_id, vram_gb, provider = _GPU_PRESETS[new_name]
    elif new_name:
        # Custom name — pick random IDs from a matching vendor if possible
        name_lower = new_name.lower()
        if "amd" in name_lower or "radeon" in name_lower:
            ven_id, provider = "1002", "Advanced Micro Devices, Inc."
            dev_id = f"{_r.randint(0x7300, 0x74FF):04X}"
            vram_gb = _r.choice([8, 16, 24])
        elif "intel" in name_lower or "arc" in name_lower:
            ven_id, provider = "8086", "Intel Corporation"
            dev_id = f"{_r.randint(0x56A0, 0x56BF):04X}"
            vram_gb = _r.choice([8, 12, 16])
        else:  # default NVIDIA
            ven_id, provider = "10DE", "NVIDIA"
            dev_id = f"{_r.randint(0x2600, 0x2800):04X}"
            vram_gb = _r.choice([8, 12, 16, 24])
        disp_name = new_name
    else:
        preset_key = _r.choice(list(_GPU_PRESETS.keys()))
        disp_name, ven_id, dev_id, vram_gb, provider = _GPU_PRESETS[preset_key]

    paths = _find_gpu_subkeys()
    if not paths:
        return False, "No GPU registry entries found under Display class key."

    vram_bytes_qword = vram_gb * 1024 * 1024 * 1024
    subsys_id = f"{_r.randint(0, 0xFFFF):04X}{_r.randint(0x1000, 0xFFFF):04X}"
    matching_id = (f"pci\\ven_{ven_id.lower()}&dev_{dev_id.lower()}"
                   f"&subsys_{subsys_id.lower()}")

    results = []
    hive = winreg.HKEY_LOCAL_MACHINE
    for path in paths:
        sub = path.split(chr(92))[-1]
        writes_str = [
            ("DriverDesc",                     disp_name),
            ("HardwareInformation.AdapterString", disp_name),
            ("HardwareInformation.ChipType",   disp_name),
            ("ProviderName",                   provider),
            ("MatchingDeviceId",               matching_id),
        ]
        writes_dword = [
            ("HardwareInformation.MemorySize",
             min(vram_bytes_qword, 0xFFFFFFFF)),  # REG_DWORD caps at 4 GB
        ]
        writes_qword = [
            ("HardwareInformation.qwMemorySize", vram_bytes_qword),
        ]
        ok_list = []
        for val, data in writes_str:
            try:
                _backup_and_write(hive, path, val, data, mode)
                ok_list.append(val)
            except Exception:
                pass  # value may not exist on all drivers
        for val, data in writes_dword:
            try:
                backup_key_d = (hive, path, val)
                if mode == "temp":
                    try:
                        old_d, _ = winreg.QueryValueEx(
                            winreg.OpenKey(hive, path, 0, winreg.KEY_READ), val)
                        _TEMP_BACKUPS.setdefault(backup_key_d, str(old_d))
                    except Exception:
                        pass
                with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, val, 0, winreg.REG_DWORD, int(data))
                ok_list.append(val)
            except Exception:
                pass
        for val, data in writes_qword:
            try:
                backup_key_q = (hive, path, val)
                if mode == "temp":
                    try:
                        old_q, _ = winreg.QueryValueEx(
                            winreg.OpenKey(hive, path, 0, winreg.KEY_READ), val)
                        _TEMP_BACKUPS.setdefault(backup_key_q, str(old_q))
                    except Exception:
                        pass
                with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, val, 0, winreg.REG_QWORD, int(data))
                ok_list.append(val)
            except Exception:
                pass
        results.append(f"  [{sub}] {disp_name} | VEN_{ven_id} DEV_{dev_id} | {vram_gb} GB VRAM")

    verb = "temporarily" if mode == "temp" else "permanently"
    return True, (f"GPU {verb} spoofed → {disp_name}  "
                  f"VEN_{ven_id}&DEV_{dev_id}  {vram_gb} GB VRAM\n"
                  + "\n".join(results))


# ── CPU ID spoof ──────────────────────────────────────────────────────────────

_CPU_KEY   = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
_CPU_VALUE = "ProcessorNameString"


def spoof_cpu_id(mode: str = "temp",
                 new_id: str | None = None) -> tuple[bool, str]:
    """
    Overwrite ProcessorNameString in the registry.
    Both temp and perm write the real value; temp saves the original so
    restore_all_temp() can revert it.  Reported by wmic/WMI and most
    system-info tools after a refresh.
    """
    _require_admin()
    if not new_id:
        names = [
            "Intel(R) Core(TM) i9-14900K @ 3.20GHz",
            "AMD Ryzen 9 7950X 16-Core Processor",
            "Intel(R) Core(TM) i7-13700K @ 3.40GHz",
            "AMD Ryzen 7 7700X 8-Core Processor",
        ]
        import random as _r
        new_id = _r.choice(names)
    try:
        old = _backup_and_write(winreg.HKEY_LOCAL_MACHINE,
                                _CPU_KEY, _CPU_VALUE, new_id, mode)
        verb = "temporarily" if mode == "temp" else "permanently"
        return True, f"CPU name {verb}: {old!r} → {new_id!r}"
    except Exception as e:
        return False, f"CPU ID spoof failed: {e}"


# ── System UUID spoof — delegates to spoof_guid ───────────────────────────────

def spoof_system_uuid(mode: str = "temp",
                      new_uuid: str | None = None) -> tuple[bool, str]:
    """
    Win32_ComputerSystemProduct.UUID is backed by the same MachineGuid registry
    key that spoof_guid() already manages.  Delegates to spoof_guid().
    """
    return spoof_guid(mode, new_uuid)


# ── RAM info spoof ────────────────────────────────────────────────────────────

def spoof_ram_info(mode: str = "temp",
                   new_size_gb: int | None = None) -> tuple[bool, str]:
    """
    Physical RAM cannot be spoofed via registry.
    Windows reads it directly from the memory controller (via SMBIOS/WMI kernel
    path), not from any writable registry key.  Returns an informative error.
    """
    return (False,
            "RAM spoofing is not possible via registry.\n"
            "Windows reports physical RAM directly from the memory controller;\n"
            "no registry key overrides this in Windows 10/11.")


# ── Restore helpers ───────────────────────────────────────────────────────────

def restore_all_temp() -> tuple[bool, str]:
    """
    Restore all values that were changed in temp mode this session.
    Registry entries are written back from _TEMP_BACKUPS.
    Volume serial entries (key tuple hive==0, key starts with '__vol_serial__')
    are restored by writing the original serial back to the raw VBR.
    Does not affect permanent (perm-mode) changes.
    """
    _require_admin()
    if not _TEMP_BACKUPS:
        return True, "No temporary spoofs to restore."
    results = []
    for (hive, key, value), original in list(_TEMP_BACKUPS.items()):
        try:
            if hive == 0 and key.startswith("__vol_serial__"):
                # Volume serial restore
                drive = key[len("__vol_serial__"):]
                _set_volume_serial_raw(drive, int(original))
                results.append(f"  Volume serial {drive}: restored → "
                                f"{int(original) >> 16:04X}-{int(original) & 0xFFFF:04X}")
            else:
                # Restore with correct registry type (REG_DWORD / REG_QWORD / REG_SZ)
                try:
                    with winreg.OpenKey(hive, key, 0, winreg.KEY_READ) as k:
                        _, reg_type = winreg.QueryValueEx(k, value)
                except Exception:
                    reg_type = winreg.REG_SZ
                if reg_type == winreg.REG_QWORD:
                    with winreg.OpenKey(hive, key, 0, winreg.KEY_SET_VALUE) as k:
                        winreg.SetValueEx(k, value, 0, winreg.REG_QWORD, int(original))
                elif reg_type == winreg.REG_DWORD:
                    with winreg.OpenKey(hive, key, 0, winreg.KEY_SET_VALUE) as k:
                        winreg.SetValueEx(k, value, 0, winreg.REG_DWORD, int(original))
                else:
                    _write_reg_str(hive, key, value, original)
                results.append(f"  Restored {value} → {original!r}")
            del _TEMP_BACKUPS[(hive, key, value)]
        except Exception as e:
            results.append(f"  {key}/{value}: restore failed — {e}")
    return True, "Temp spoofs restored:\n" + "\n".join(results)
