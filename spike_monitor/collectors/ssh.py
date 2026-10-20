"""SSH command runner (MikroTik, Ubiquiti airOS, anything with SSH).

Host keys are remembered on first connect (trust on first use). If a device's
key changes later the connection is refused until an admin clicks
"Forget SSH key" on the device page.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .common import CollectorError


class HostKeyChanged(CollectorError):
    pass


@dataclass
class SshTarget:
    host: str
    username: str
    password: str
    port: int = 22
    known_fingerprint: str = ""
    timeout: float = 15.0


@dataclass
class SshResult:
    output: str
    exit_status: int | None
    fingerprint: str


def _legacy_algorithms() -> dict:
    """Allow the older algorithms used by RouterOS v6 and airOS dropbear, after the modern ones."""
    try:
        from asyncssh import encryption, kex, mac, public_key

        def names(algs) -> list[str]:
            return [a.decode() if isinstance(a, bytes) else a for a in algs]

        return {
            "kex_algs": names(kex.get_kex_algs()),
            "encryption_algs": names(encryption.get_encryption_algs()),
            "mac_algs": names(mac.get_mac_algs()),
            "server_host_key_algs": names(public_key.get_public_key_algs()),
        }
    except Exception:  # pragma: no cover - depends on asyncssh internals
        return {}


async def run_command(target: SshTarget, command: str, command_timeout: float = 60.0) -> SshResult:
    import asyncssh

    if not target.username:
        raise CollectorError("No username set for this device (add a login or a credential profile).")
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                target.host,
                port=target.port,
                username=target.username,
                password=target.password,
                known_hosts=None,
                client_keys=None,
                agent_path=None,
                preferred_auth=("password", "keyboard-interactive"),
                **_legacy_algorithms(),
            ),
            timeout=target.timeout,
        )
    except asyncio.TimeoutError as exc:
        raise CollectorError(f"SSH port {target.port} timed out.") from exc
    except asyncssh.PermissionDenied as exc:
        raise CollectorError("SSH login failed: wrong username or password.") from exc
    except ConnectionRefusedError as exc:
        raise CollectorError(f"SSH port {target.port} refused the connection. Is SSH enabled and the port right?") from exc
    except (OSError, asyncssh.Error) as exc:
        raise CollectorError(f"SSH connection failed: {exc}") from exc

    try:
        key = conn.get_server_host_key()
        fingerprint = key.get_fingerprint("sha256") if key else ""
        if target.known_fingerprint and fingerprint and fingerprint != target.known_fingerprint:
            raise HostKeyChanged(
                "The device's SSH host key has changed. If the device was replaced or reset, an admin can "
                "click 'Forget SSH key' on the device page; otherwise someone may be intercepting the connection."
            )
        try:
            result = await asyncio.wait_for(conn.run(command, check=False), timeout=command_timeout)
        except asyncio.TimeoutError as exc:
            raise CollectorError(f"The command did not finish within {command_timeout:.0f} seconds.") from exc
        output = (result.stdout or "") + (result.stderr or "")
        if isinstance(output, bytes):  # pragma: no cover
            output = output.decode(errors="replace")
        return SshResult(output=output, exit_status=result.exit_status, fingerprint=fingerprint)
    finally:
        conn.close()
