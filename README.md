# Spike Net Monitoring Server

Network monitoring for a small Layer 2 wireless ISP (MikroTik, Ubiquiti, Mimosa).
It runs on one Windows PC and your team opens it in a browser from any PC or phone.

See [docs/DESIGN.md](docs/DESIGN.md) for the full requirements and the phase plan.

## What works now (phase 1)

- Users with roles (Admin / Technician / Viewer), audit log
- Sites, VLANs, customers (MAC list), credential profiles, port profiles
- Devices with their own ports (Winbox, SSH, API, API-SSL, HTTP, HTTPS) and encrypted passwords
- CSV import for adding many devices at once
- Ping monitoring of every device: latency, packet loss, up / degraded / down
- Parent-aware status: if a tower is down, the devices behind it show **unreachable**
- Live dashboard, device list with filters, latency/loss graph per device

## Install on Windows

You do **not** need Windows Server; a normal Windows 10/11 PC that stays on works.

1. Install Python 3.11 or newer from <https://www.python.org/downloads/>.
   During setup, tick **"Add python.exe to PATH"**.
2. Copy this folder to the PC, for example `C:\SpikeNet`.
3. Double-click `scripts\install.bat` (only once).
4. Right-click `scripts\run.bat` → **Run as administrator**.
   (Without administrator rights it still works, using Windows `ping.exe`.)
5. Open <http://localhost:8000> and log in with:
   - user: `admin`
   - password: `admin@2026`

   You are asked to choose a new password straight away.
6. To let other PCs/phones in your network use it, run `scripts\open_firewall.bat` as administrator,
   then open `http://<this-PC-IP>:8000` from them.

Data (database and the encryption key) is stored in `C:\ProgramData\SpikeNetMonitor`.
**Back up `secret.key` together with `spike.db`**: without the key, stored device passwords cannot be read.

## Settings

Set environment variables before starting (for example in `run.bat`):

| Variable | Default | Meaning |
|---|---|---|
| `SPIKE_PORT` | `8000` | Web port |
| `SPIKE_DATA_DIR` | `C:\ProgramData\SpikeNetMonitor` | Database and key folder |
| `SPIKE_PING_INTERVAL` | `30` | Seconds between ping rounds |
| `SPIKE_PING_COUNT` | `3` | Pings per device per round |
| `SPIKE_PING_TIMEOUT` | `1.0` | Seconds to wait for each reply |
| `SPIKE_PING_CONCURRENCY` | `100` | Devices pinged in parallel |
| `SPIKE_RAW_RETENTION_DAYS` | `7` | How long raw ping results are kept |
| `SPIKE_BOOTSTRAP_ADMIN_KEY` | `admin@2026` | First admin password (used only when there are no users) |

## CSV import

On **Devices → Import CSV** download the template. Only `name` and `ip_address` are required.
Columns: `name, ip_address, brand, device_type, site, parent, mac_address, model, serial_number,
os_version, credential_profile, username, password, winbox_port, ssh_port, api_port, api_ssl_port,
http_port, https_port, notes`.

## Development

```
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
python -m spike_monitor
```
