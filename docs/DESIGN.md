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

### Phase 1: done
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

### Phase 2: done
- MikroTik RouterOS API (v6 incl. pre-6.43 login, and v7; plain API or API-SSL):
  CPU, RAM, temperature, voltage, uptime, interfaces with traffic rates, wireless
  registration table (legacy wireless, wifi, wifiwave2): signal, noise, CCQ, rates, clients.
- Ubiquiti airOS over SSH (`mca-status` + `wstalist`): CPU, RAM, signal, noise, CCQ, stations, traffic.
- Poller every 60 s (20 devices in parallel); devices that are down by ping are skipped.
- **Import from device**: identity, model, serial, version, MAC, VLANs (created and linked),
  IP addresses, bridges, wireless settings, interfaces, neighbors with one-click "Add".
- **Discovered devices** page: MNDP/CDP/LLDP neighbors not in the inventory yet.
- **Push settings** (admin only): commands to one or many devices over SSH, preview +
  confirmation, per-device output, RouterOS error detection, retry failed, audit log.
- SSH host keys remembered on first use; a changed key blocks the connection until an admin resets it.
- Graphs: traffic per interface, CPU / memory / temperature / clients; hourly averages kept
  for a year (raw samples 7 days), graph ranges up to 1 year.
- Wireless clients matched to the Customers list by MAC.
- Upstream ISP panel on the dashboard (devices of type "Upstream").
- **Network map** (added on request): Sites view (sites and the links between them, coloured by the
  worst status inside, wireless signal on the link), Devices view (every device and its uplink,
  filter by site), Geographic view (sites on a street map with link lines, drag pins / place sites).
  Layout can be arranged and saved; refreshes every 20 s.

### Phase 3: done
- Alert engine runs after every ping round and compares "what is wrong now" with open alerts.
- Alerts: down (critical; upstream link marked), degraded latency/loss, high CPU, high temperature,
  weak signal (PTP links, stations, upstream), broadcast storm / possible loop (MikroTik ethernet
  broadcast+multicast packets per second), device rebooted (uptime went backwards), cannot read device.
- Parent-aware: one alert for a down tower with "N devices behind it unreachable"; none for the devices behind it.
- Hysteresis on metric alerts; metric alerts are neither opened nor closed on stale data.
- Warnings are sent only after a delay (default 5 min); critical at once; reminders for unacknowledged
  critical alerts (default every 60 min); recovery messages with the duration.
- Acknowledge (one / all), maintenance mode per device and per site (alerts recorded, not sent).
- Telegram (recommended): bot token, optional team group with its own severity, personal chats per user
  with the same severity / site filters, "Find chats" helper and test button. Guide: docs/TELEGRAM_SETUP.md.
- WhatsApp through CallMeBot (free, alerts to personal numbers, per-user API key) or the Meta
  WhatsApp Cloud API: template or text mode, per-user number, severity and site subscriptions,
  several alerts grouped into one message, notification log, test button. Setup guide: docs/WHATSAPP_SETUP.md.
- Desktop popup + sound on the Windows server PC (built-in PowerShell toast + winsound, no extra packages).
- Every open browser page: alert bell with count, popups, alarm sound and system notifications.

### Owner change requests, round 1: done
- Device connection: connected by (wired / wireless / fiber), parent's port, this device's port.
  Typed by hand, imported from CSV, or detected automatically from the parent's wireless
  registration table and neighbor table (and the device's own neighbor table). The map shows
  wired solid, wireless dashed, fiber thick, labelled "parent port → device port · signal".
- Customers: choosing a site limits the device list to that site (checked on the server too).
- Clear history (admin) on every log page: status events (all / per device), device graph data,
  closed alerts, notification log, finished push jobs, discovered devices, audit log.
  A 5-second countdown before the confirm button works; clearing is itself written to the audit log.

### Owner change requests, round 2: done
- Port fields are drop-down lists of the device's real interfaces, filtered by connection type
  (wired = ethernet, wireless = wlan/wifi, fiber = sfp), with speed and IP in each entry, and
  "Other (type it)" for devices not read yet. The server rejects a port of the wrong type.
- Devices in different sites can only be connected wireless (form, server check, auto-detection).
- Interface IP addresses (/ip address) and negotiated ethernet speed (/interface ethernet monitor)
  are read from MikroTik; wireless link speed comes from the registration table (tx / rx rate).
  Shown on the device page, the interfaces table and the map; blank when unknown.
- Neighbors: nothing is imported unless ticked; each row can be imported as a Device (needs an IP)
  or a Customer (name + MAC, linked to the device that sees it), on the Discovered page and in
  Import from device.

### Phase 4: backups and tools
- Config backups: MikroTik `.rsc` export + `.backup` over SSH, Ubiquiti `system.cfg`;
  scheduled + manual, history, side-by-side diff, download.
- Encrypted export/import of the whole inventory **including passwords** (password-protected file),
  plus a plain CSV export without passwords.
- IP / subnet calculator (CIDR, masks, ranges, VLSM).
- Ping/traceroute tool.
- **Open in Winbox / SSH / web** buttons via a small helper installed on each technician PC
  (`winbox://` link handler that starts `winbox.exe` already logged in).
- Run as a Windows service; installer.

### Later (agreed, not scheduled)
- Mimosa support.
- PPPoE / customer sessions from MikroTik, customer CPE monitoring, queues.
- MAC-based customer access.
