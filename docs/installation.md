# Installation

## Requirements

| Item | Requirement |
|------|-------------|
| Python | 3.13+ (only for pip install / from source -- not needed for pre-built binary) |
| OS | Windows 10/11, Ubuntu 20.04+, macOS, any modern Linux |
| pfSense | Tested on CE 2.8.1. Other CE or Plus versions may work but are untested |

Your pfSense device needs one of:

- **SSH access** (recommended) -- enable at System > Advanced > Admin Access > Secure Shell
- **HTTPS access** -- alternative method, less reliable for some backup types

## Option A: pip Install

```bash
pip install pfsentinel
```

Verify:

```bash
pfs --version
```

## Option B: Pre-Built Binary

Download the binary for your platform from the [Releases page](https://github.com/NX1X/pfSentinel/releases):

| File | Platform |
|------|----------|
| `pfs.exe` | Windows 10/11 (64-bit) |
| `pfs` | Linux (64-bit) |

**Windows:**

1. Download `pfs.exe` and place it in a folder on your PATH (e.g., `C:\Tools\`).
2. Open a terminal and run:

```
pfs --version
```

**Linux:**

```bash
chmod +x pfs
./pfs --version

# Optionally install system-wide
sudo mv pfs /usr/local/bin/pfs
```

No Python installation required. The binary is self-contained.

## Option C: From Source

```bash
git clone https://github.com/NX1X/pfSentinel.git
cd pfSentinel

python -m venv .venv

# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
.venv\Scripts\Activate.ps1

pip install -e .
pfs --version
```

For development (includes linting and testing tools):

```bash
pip install -e ".[dev]"
```

## Building a Binary

To build a standalone binary from source:

```bash
pip install pyinstaller
python scripts/build_exe.py
# Output: dist/pfs.exe (Windows) or dist/pfs (Linux)
```

## First-Time Setup

After installing, run the setup wizard:

```bash
pfs setup
```

This walks you through creating a config file and adding your first pfSense device. Alternatively, you can do it step by step:

```bash
pfs config init        # create config file with defaults
pfs device add         # add a pfSense device (interactive)
pfs device test        # verify the connection works
pfs backup run         # run your first backup
```

Configuration is stored at `~/.pfsentinel/config.json`. Passwords are stored in your OS keyring (Windows Credential Manager, GNOME Keyring, or file-based fallback on WSL/headless Linux).

## Next Steps

- [Usage Guide](usage.md) -- CLI reference, scheduling, notifications
- [Extended Backups](extended-backups.md) -- RRD, packages, DHCP, certs, logs, ZFS, archives
