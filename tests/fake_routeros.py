"""A tiny RouterOS API server for tests (speaks the real wire protocol via librouteros)."""
import socket
import threading

from librouteros.connections import SocketTransport
from librouteros.login import encode_password
from librouteros.protocol import ApiProtocol

V7_DATA = {
    "/system/identity/print": [{"name": "Core-RB4011"}],
    "/system/resource/print": [{"cpu-load": "12", "free-memory": "268435456", "total-memory": "1073741824",
                                "uptime": "1w2d3h4m5s", "version": "7.15.3 (stable)", "board-name": "RB4011iGS+"}],
    "/system/routerboard/print": [{"model": "RB4011iGS+", "serial-number": "HEX123", "current-firmware": "7.15.3"}],
    "/system/health/print": [{"name": "voltage", "value": "24.1", "type": "V"},
                             {"name": "temperature", "value": "41", "type": "C"}],
    "/interface/print": [
        {"name": "ether1", "type": "ether", "mac-address": "AA:BB:CC:00:00:01", "running": "true",
         "disabled": "false", "rx-byte": "1000000", "tx-byte": "500000", "comment": "uplink"},
        {"name": "wlan1", "type": "wlan", "mac-address": "AA:BB:CC:00:00:02", "running": "true",
         "disabled": "false", "rx-byte": "2000", "tx-byte": "3000"},
        {"name": "vlan100", "type": "vlan", "running": "true", "disabled": "false"},
    ],
    "/interface/wireless/registration-table/print": [
        {"interface": "wlan1", "mac-address": "11:22:33:44:55:66", "signal-strength": "-63dBm@6Mbps",
         "signal-to-noise": "40", "tx-ccq": "91", "tx-rate": "130Mbps", "rx-rate": "117Mbps",
         "uptime": "2h3m", "last-ip": "10.10.0.50", "radio-name": "Customer-CPE"},
    ],
    "/ip/neighbor/print": [
        {"interface": "ether1", "address": "10.0.0.9", "mac-address": "DE:AD:BE:EF:00:09", "identity": "Tower2-AP",
         "platform": "MikroTik", "board": "RB912UAG-5HPnD", "version": "6.49.17 (long-term)"},
    ],
    "/ip/address/print": [{"address": "10.0.0.1/24", "interface": "ether1"}],
    "/interface/vlan/print": [{"name": "vlan100", "vlan-id": "100", "interface": "ether1"}],
    "/interface/bridge/print": [{"name": "bridge1"}],
    "/interface/wireless/print": [{"name": "wlan1", "ssid": "SpikeNet", "frequency": "5180", "mode": "ap-bridge",
                                   "band": "5ghz-a/n/ac"}],
}


class FakeRouterOS:
    def __init__(self, username="admin", password="secret", legacy=False, data=None):
        self.username, self.password, self.legacy = username, password, legacy
        self.data = dict(V7_DATA if data is None else data)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.logins = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        proto = ApiProtocol(SocketTransport(conn), "latin-1")
        logged_in, challenge = False, "0123456789abcdef0123456789abcdef"
        try:
            while True:
                cmd, words = proto.readSentence()
                args = dict(w[1:].split("=", 1) for w in words if w.startswith("="))
                if cmd == "/login":
                    if self.legacy:
                        if "response" in args:
                            ok = args.get("name") == self.username and \
                                args["response"] == encode_password(challenge, self.password)
                            logged_in = ok
                            proto.writeSentence("!done") if ok else self._trap(proto, "cannot log in")
                        else:  # pre-6.43 routers ignore name/password and answer with a challenge
                            proto.writeSentence("!done", f"=ret={challenge}")
                    elif args.get("name") == self.username and args.get("password") == self.password:
                        logged_in = True
                        self.logins += 1
                        proto.writeSentence("!done")
                    else:
                        self._trap(proto, "invalid user name or password (6)")
                    continue
                if not logged_in:
                    self._trap(proto, "not logged in")
                    continue
                if cmd not in self.data:
                    self._trap(proto, "no such command prefix")
                    continue
                for row in self.data[cmd]:
                    proto.writeSentence("!re", *(f"={k}={v}" for k, v in row.items()))
                proto.writeSentence("!done")
        except Exception:
            conn.close()

    @staticmethod
    def _trap(proto, message):
        proto.writeSentence("!trap", f"=message={message}")
        proto.writeSentence("!done")
