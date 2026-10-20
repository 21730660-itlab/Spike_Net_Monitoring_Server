# Spike Net Monitoring Server: requirements and design

Network monitoring for a small Layer 2 wireless ISP. A Python server runs on one
Windows PC 24/7; the team uses it from a browser on any PC or phone.

## Network (as described by the owner)

- About 400 devices today, has to scale to thousands.
- Upstream ISP (wireless link) → MikroTik RouterBoards → wireless APs / towers → customers.
- Brands: MikroTik RouterBoard + MikroTik wireless (RouterOS **v6 and v7**), Ubiquiti, Mimosa.
- Flat Layer 2 today; VLANs must be creatable later. No switches for now.
- Customers connect by PPPoE to the upstream ISP. The owner reaches customers by MAC address.
- Management: routers/APs have IPs. **No SNMP.** MikroTik API (8728/8729) and SSH are allowed.
- Every device can have **different credentials** and **non-default ports** (SSH, Winbox, API…).

## Decisions

| Topic | Decision |
|---|---|
| Platform | Python server on Windows (10/11 or Server), web UI in the browser |
| Sites, VLANs | Unlimited, created by the user |
| Access | Ping for all devices; MikroTik via API + SSH; Ubiquiti via SSH + airOS web; Mimosa later |
| Roles | Admin (everything), Technician (devices, no passwords/users), Viewer (read only) |
| First admin | `admin` / preshared key `admin@2026`, password change forced on first login |
| Alerts | Desktop popup + sound, and WhatsApp through the official Meta Cloud API |
| Device secrets | Encrypted at rest (Fernet key in the data folder), never displayed |

## Phases

### Phase 1: done in this commit
- Login, roles, forced password change, brute-force lockout, CSRF protection, audit log.
- Sites, VLANs, credential profiles, port profiles, customers (MAC list), devices.
- Per-device ports (Winbox, SSH, API, API-SSL, HTTP, HTTPS) and port profiles.
- Per-device or shared encrypted credentials.
- Device parent (uplink) with loop protection.
- CSV import of devices (sites created automatically, parents resolved by name or IP).
- Async ping engine (100 parallel by default): latency, loss, up / degraded / down.
- **Parent-aware status**: devices behind a down parent become *unreachable* instead of *down*.
- Down debounce (2 failed cycles), raw ping history kept 7 days.
- Dashboard (live refresh), device list with filters, device page with latency/loss graph.

### Phase 2: device data
- MikroTik RouterOS API (v6 + v7) and SSH: CPU, RAM, temperature, voltage, uptime,
  interfaces and traffic, wireless signal / CCQ / noise / rates / registered clients.
- **Import from device**: identity, model, serial, version, interfaces, IPs, VLANs, bridges,
  wireless settings, neighbors (suggested as new devices).
- **Push settings to devices**: run a command/script on one or many MikroTik devices,
  with preview, confirmation, per-device result and audit log.
- Ubiquiti airOS: status (signal, CCQ, capacity, stations) via SSH / web.
- Traffic and wireless graphs; upstream link quality view.

### Phase 3: alerts
- Alert rules: down/up, latency, loss, weak signal, high CPU/temperature, loop detected.
- Parent-aware: one alert for a down tower, not one per device behind it.
- WhatsApp (Meta Cloud API, per-user numbers and site subscriptions), desktop tray app with sound.
- Acknowledge, maintenance mode.
- Loop / broadcast storm detection (broadcast rates, MAC flapping, RouterOS loop-protect).

### Phase 4: backups and tools
- Config backups: MikroTik `.rsc` export + `.backup` over SSH, Ubiquiti `system.cfg`;
  scheduled + manual, history, side-by-side diff, download.
- Encrypted export/import of the whole inventory **including passwords** (password-protected file),
  plus a plain CSV export without passwords.
- IP / subnet calculator (CIDR, masks, ranges, VLSM).
- Network map (tree), ping/traceroute tool.
- **Open in Winbox / SSH / web** buttons via a small helper installed on each technician PC
  (`winbox://` link handler that starts `winbox.exe` already logged in).
- Run as a Windows service; installer.

### Later (agreed, not scheduled)
- Mimosa support.
- PPPoE / customer sessions from MikroTik, customer CPE monitoring, queues.
- MAC-based customer access.
