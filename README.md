# Spike Net Monitoring Server

Network monitoring for a small Layer 2 wireless ISP (MikroTik, Ubiquiti, Mimosa).
It runs on one Windows PC and your team opens it in a browser from any PC or phone.

See [docs/DESIGN.md](docs/DESIGN.md) for the full requirements and the phase plan.

## What works now (phases 1–3)

- Users with roles (Admin / Technician / Viewer), audit log
- Sites, VLANs, customers (MAC list), credential profiles, port profiles
- Devices with their own ports (Winbox, SSH, API, API-SSL, HTTP, HTTPS) and encrypted passwords
- CSV import for adding many devices at once
- Ping monitoring of every device: latency, packet loss, up / degraded / down
- Parent-aware status: if a tower is down, the devices behind it show **unreachable**
- Live dashboard, device list with filters, latency/loss graph per device
- MikroTik (RouterOS v6 and v7, API) and Ubiquiti airOS (SSH): CPU, memory, temperature, voltage,
  uptime, interface traffic, wireless clients with signal / CCQ, matched to your customers by MAC
- Import from device (identity, model, serial, version, VLANs, IPs, wireless, neighbors)
- Discovered devices: neighbors your MikroTiks see that are not in the app yet
- Push settings (admin): run commands on one or many devices over SSH, with preview and per-device results
- Traffic / health graphs up to 1 year
- Network map: sites and their links, every device and its uplink, and a geographic map of your towers
  (the street map background needs internet on the PC viewing it)
- Alerts: down (one alert per tower, not per device behind it), degraded, high CPU / temperature, weak signal,
  broadcast storm / loop, reboots; acknowledge, maintenance mode, reminders and recovery messages
- Notifications: Telegram (recommended, see [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md)),
  WhatsApp (CallMeBot or Meta Cloud API, see [docs/WHATSAPP_SETUP.md](docs/WHATSAPP_SETUP.md)),
  Windows popup + sound on the server PC, and popups + sound in every open browser (click 🔇 to turn sound on)

## Preparing your devices

**MikroTik**: the API service must be on and reachable from the server PC:

```
/ip service set api disabled=no port=8728 address=<server-PC-IP>/32
```

For monitoring only, a read-only user is enough:

```
/user add name=monitor group=read password=<strong password>
```

Push settings logs in over SSH and needs a user in the `write` or `full` group.
If you changed the API, SSH or Winbox port, set it on the device in the app (or use a port profile).

**Ubiquiti airOS**: SSH must be on (System → Services → SSH Server). Use the device's admin login.

## Install on Windows

You do **not** need Windows Server; a normal Windows 10/11 PC that stays on works.

1. Install Python 3.11 or newer (3.13 recommended) from <https://www.python.org/downloads/>.
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
| `SPIKE_RAW_RETENTION_DAYS` | `7` | How long raw samples (ping, CPU, traffic) are kept |
| `SPIKE_STATS_INTERVAL` | `60` | Seconds between reads of device data (API / SSH) |
| `SPIKE_STATS_CONCURRENCY` | `20` | Devices read in parallel |
| `SPIKE_ROLLUP_RETENTION_DAYS` | `365` | How long hourly averages are kept |
| `SPIKE_COMMAND_CONCURRENCY` | `10` | Devices a pushed command runs on at the same time |
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
