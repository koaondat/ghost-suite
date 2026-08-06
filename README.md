# QA Environment System Configuration Utility

A modular Python script for managing system profiles on Windows QA machines.  
Uses **winreg**, **ctypes**, and **subprocess** — no third-party dependencies.

---

## Requirements

| Requirement | Detail |
|---|---|
| OS | Windows 10 / 11 / Server 2016+ |
| Python | 3.8 or later (64-bit recommended) |
| Privileges | **Must be run as Administrator** |

---

## Module overview

```
config_utility.py
│
├── require_admin()                  # Guard — raises PermissionError if not elevated
│
├── [Safety & Backup]
│   ├── backup_registry_key()        # Exports a key to a .reg file before every write
│   ├── _format_reg_value()          # Serialises any REG_* type to .reg syntax
│   └── _reg_type_name()             # Helper — human-readable type label
│
├── [Registry GUID Management]
│   ├── read_machine_guid()          # Reads HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid
│   └── update_machine_guid()        # Backs up then writes a new GUID (auto or supplied)
│
├── [MAC Address Configuration]
│   ├── list_network_adapter_subkeys() # Enumerates NIC entries in the Class registry key
│   ├── _is_valid_laa()              # Validates Locally Administered Unicast address
│   ├── set_adapter_mac()            # Backs up then writes NetworkAddress to an adapter key
│   └── configure_mac_interactive()  # Interactive helper for choosing adapter + MAC
│
├── [Volume Serial Querying]
│   ├── get_volume_info()            # Calls GetVolumeInformationW via ctypes
│   └── query_all_volumes()          # Enumerates all drives with fsutil + subprocess
│
└── main() / _menu()                 # CLI entry point + interactive menu
```

---

## Usage

### Interactive menu

```powershell
python config_utility.py
```

### Command-line interface

```powershell
# Read the current MachineGuid
python config_utility.py read-guid

# Auto-rotate MachineGuid (generates a new UUID4)
python config_utility.py rotate-guid

# Set a specific GUID
python config_utility.py set-guid "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

# List all detected network adapters
python config_utility.py list-adapters

# Apply a Locally Administered Address to a specific adapter subkey
python config_utility.py set-mac "SYSTEM\CurrentControlSet\Control\Class\{4D36E972-E325-11CE-BFC1-08002BE10318}\0001" "02AABBCCDDEE"

# Query serial numbers for every volume
python config_utility.py query-volumes
```

---

## Backup files

Every write operation automatically creates a `.reg` backup in `backups/` before  
making any changes.  File names follow the pattern:

```
backups/<label>_<YYYYMMDD_HHMMSS>.reg
```

To restore, double-click the `.reg` file or run:

```powershell
regedit /s backups\MachineGuid_20240101_120000.reg
```

---

## MAC address rules

The `set_adapter_mac` function enforces the **Locally Administered Address (LAA)** convention:

| Bit | Position | Required value |
|-----|----------|---------------|
| Locally Administered | Bit 1 of first byte | `1` |
| Unicast | Bit 0 of first byte | `0` |

Valid first-byte examples: `02`, `06`, `0A`, `0E`  
Example full address: `02AABBCCDDEE`

> **Note:** After writing the `NetworkAddress` registry value, the adapter must be  
> disabled and re-enabled (or the system rebooted) for the change to take effect.

---

## Security notes

- The script calls `IsUserAnAdmin()` at startup and refuses to run without elevation.
- No values are written without a backup being created first.
- GUID input is validated via `uuid.UUID()` to prevent malformed writes.
- MAC input is validated with a regex + bitwise LAA check before any registry write.
