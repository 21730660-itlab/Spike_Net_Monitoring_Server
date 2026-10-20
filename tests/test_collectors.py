import asyncio

import asyncssh
import pytest

from spike_monitor.collectors import ubiquiti
from spike_monitor.collectors.common import CollectorError, format_bps, parse_duration, parse_signal
from spike_monitor.collectors.mikrotik import ApiTarget, collect, parse_health, read_inventory
from spike_monitor.collectors.ssh import HostKeyChanged, SshTarget, run_command

from .fake_routeros import FakeRouterOS

MCA = """deviceName=Tower1-AP,deviceId=24:A4:3C:00:11:22,firmwareVersion=XC.qca955x.v8.7.11,platform=Rocket 5AC,deviceIp=10.0.1.2
wlanOpmode=ap-ptmp
essid=SpikeNet-T1
freq=5745
signal=-58
noise=-95
ccq=954
uptime=86400
cpuTotal=100
cpuBusy=23
memTotal=125000
memFree=50000
lanRxBytes=1000
lanTxBytes=2000
wlanRxBytes=3000
wlanTxBytes=4000
"""
WSTA = '[{"mac":"aa:bb:cc:dd:ee:01","name":"cust-1","lastip":"10.20.0.5","signal":-61,"noisefloor":-96,"ccq":88,"tx":144,"rx":130,"uptime":3600}]'


def test_parsers():
    assert parse_duration("1w2d3h4m5s") == 788645
    assert parse_duration("2d03:04:05") == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    assert parse_duration("00:00:10") == 10
    assert parse_signal("-62dBm@6Mbps") == -62 and parse_signal("-70@HT20-7") == -70 and parse_signal(-55) == -55
    assert parse_health([{"temperature": "38", "voltage": "23.9"}]) == (38.0, 23.9)  # RouterOS v6
    assert parse_health([{"name": "cpu-temperature", "value": "51"}, {"name": "voltage", "value": "12"}]) == (51.0, 12.0)
    assert format_bps(12_345_678) == "12.3 Mbps"


def test_mikrotik_v7_and_legacy_v6():
    srv = FakeRouterOS()
    stats = collect(ApiTarget("127.0.0.1", "admin", "secret", port=srv.port, timeout=3))
    assert (stats.identity, stats.cpu_pct, stats.mem_pct, stats.temperature, stats.voltage) == (
        "Core-RB4011", 12.0, 75.0, 41.0, 24.1)
    assert stats.os_version == "7.15.3" and stats.serial_number == "HEX123"
    assert stats.peers[0].signal_dbm == -63 and stats.peers[0].noise_dbm == -103 and stats.ccq_pct == 91
    assert stats.neighbors[0].identity == "Tower2-AP"
    inv = read_inventory(ApiTarget("127.0.0.1", "admin", "secret", port=srv.port, timeout=3))
    assert inv.vlans == [{"name": "vlan100", "vlan_id": 100, "interface": "ether1"}]
    assert inv.mac_address == "AA:BB:CC:00:00:01" and inv.wireless[0]["ssid"] == "SpikeNet"

    legacy = FakeRouterOS(legacy=True)
    assert collect(ApiTarget("127.0.0.1", "admin", "secret", port=legacy.port, timeout=3)).identity == "Core-RB4011"


def test_mikrotik_board_without_health_menu():
    """Small boards (e.g. SXTsq on RouterOS v6) have no /system/health or /system/routerboard."""
    data = {k: v for k, v in FakeRouterOS().data.items()
            if not k.startswith(("/system/health", "/system/routerboard"))}
    srv = FakeRouterOS(data=data)
    stats = collect(ApiTarget("127.0.0.1", "admin", "secret", port=srv.port, timeout=3))
    assert stats.identity == "Core-RB4011" and stats.cpu_pct == 12
    assert stats.temperature is None and stats.voltage is None


def test_mikrotik_errors():
    srv = FakeRouterOS()
    with pytest.raises(CollectorError, match="wrong username or password"):
        collect(ApiTarget("127.0.0.1", "admin", "bad", port=srv.port, timeout=3))
    with pytest.raises(CollectorError, match="No username"):
        collect(ApiTarget("127.0.0.1", "", "", port=srv.port, timeout=3))


def test_ubiquiti_parse():
    stats = ubiquiti.parse_output(MCA + ubiquiti.SEPARATOR + "\n" + WSTA)
    assert stats.identity == "Tower1-AP" and stats.model == "Rocket 5AC"
    assert stats.cpu_pct == 23 and stats.mem_pct == 60 and stats.ccq_pct == 95.4
    assert stats.signal_dbm == -58 and stats.noise_dbm == -95
    assert [i.name for i in stats.interfaces] == ["lan", "wlan"]
    assert stats.peers[0].mac_address == "AA:BB:CC:DD:EE:01" and stats.peers[0].signal_dbm == -61


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "ubnt" and password == "pw"


async def _handle(process):
    if process.command.startswith("mca-status"):
        process.stdout.write(MCA + ubiquiti.SEPARATOR + "\n" + WSTA)
    else:
        process.stdout.write(f"ran: {process.command}\n")
    process.exit(0)


async def _start_server():
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(_Server, "127.0.0.1", 0, server_host_keys=[key], process_factory=_handle)
    return server, server.sockets[0].getsockname()[1]


def test_ssh_runner_and_host_key_pinning():
    async def scenario():
        server, port = await _start_server()
        try:
            res = await run_command(SshTarget("127.0.0.1", "ubnt", "pw", port=port), "/system identity print")
            assert res.output == "ran: /system identity print\n" and res.fingerprint.startswith("SHA256:")
            stats, res2 = await ubiquiti.collect(SshTarget("127.0.0.1", "ubnt", "pw", port=port,
                                                           known_fingerprint=res.fingerprint))
            assert stats.identity == "Tower1-AP"
            with pytest.raises(HostKeyChanged):
                await run_command(SshTarget("127.0.0.1", "ubnt", "pw", port=port, known_fingerprint="SHA256:other"), "x")
            with pytest.raises(CollectorError, match="wrong username or password"):
                await run_command(SshTarget("127.0.0.1", "ubnt", "nope", port=port), "x")
        finally:
            server.close()
    asyncio.run(scenario())
