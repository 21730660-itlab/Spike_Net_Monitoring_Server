"""Takes configuration backups over SSH, keeps history, and runs them on a daily schedule.

MikroTik: ``/export`` as text (.rsc, used for comparing versions) and ``/system backup`` (.backup,
binary, for a full restore with Winbox). Ubiquiti airOS: ``/tmp/system.cfg``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..collectors.common import CollectorError
from ..collectors.ssh import run_and_download, run_command
from ..models import AppSetting, Brand, ConfigBackup, Device, Status, utcnow
from ..monitoring.poller import ssh_target
from ..security import SecretBox

log = logging.getLogger(__name__)

SETTINGS_KEY = "backups"
LAST_RUN_KEY = "backups_last_run"
EXTENSIONS = {"export": "rsc", "binary": "backup", "ubnt": "cfg"}
BACKUP_NAME = "spike-auto"
BACKUP_STATUSES = (Status.UP, Status.WARNING, Status.UNKNOWN)


@dataclass
class BackupSettings:
    enabled: bool = True
    time: str = "02:00"  # local time of the daily run
    keep: int = 30  # backups kept per device and type
    binary: bool = True  # also save MikroTik .backup files
    concurrency: int = 5


def load_backup_settings(db: Session) -> BackupSettings:
    row = db.get(AppSetting, SETTINGS_KEY)
    try:
        stored = json.loads(row.value) if row else {}
    except ValueError:
        stored = {}
    known = {f.name for f in fields(BackupSettings)}
    return BackupSettings(**{k: v for k, v in stored.items() if k in known})


def save_backup_settings(db: Session, s: BackupSettings) -> None:
    row = db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        db.add(AppSetting(key=SETTINGS_KEY, value=json.dumps(asdict(s))))
    else:
        row.value = json.dumps(asdict(s))


_HEADER = re.compile(r"^#.*(by RouterOS|software id)", re.IGNORECASE)


def normalized(text: str) -> str:
    """Config text without the parts that change on every export (the date line)."""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n") if not _HEADER.match(line))


def checksum(data: bytes, kind: str) -> str:
    if kind in ("export", "ubnt"):
        data = normalized(data.decode("utf-8", errors="replace")).encode()
    return hashlib.sha256(data).hexdigest()


def can_backup(device: Device) -> bool:
    return device.use_ssh and device.brand in (Brand.MIKROTIK, Brand.UBIQUITI)


class BackupManager:
    def __init__(self, session_factory: sessionmaker[Session], box: SecretBox, data_dir: Path):
        self.session_factory = session_factory
        self.box = box
        self.root = Path(data_dir) / "backups"
        self._task: asyncio.Task | None = None
        self.running = False

    # ------------------------------------------------------------------ files
    def file(self, backup: ConfigBackup) -> Path:
        return self.root / backup.path

    def _store(self, db: Session, device: Device, kind: str, data: bytes, trigger: str) -> ConfigBackup:
        digest = checksum(data, kind)
        previous = db.scalars(select(ConfigBackup).where(
            ConfigBackup.device_id == device.id, ConfigBackup.kind == kind, ConfigBackup.ok)
            .order_by(ConfigBackup.created_at.desc())).first()
        now = utcnow()
        if previous is not None and previous.checksum == digest and self.file(previous).exists():
            # Same configuration as last time: record it, but share the existing file.
            backup = ConfigBackup(device_id=device.id, kind=kind, created_at=now, path=previous.path,
                                  size=previous.size, checksum=digest, changed=False, trigger=trigger)
        else:
            # The checksum part keeps names unique even for two backups in the same second.
            rel = Path(str(device.id)) / f"{now.strftime('%Y%m%d-%H%M%S')}-{kind}-{digest[:8]}.{EXTENSIONS[kind]}"
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            backup = ConfigBackup(device_id=device.id, kind=kind, created_at=now, path=rel.as_posix(),
                                  size=len(data), checksum=digest, changed=previous is not None, trigger=trigger)
            if previous is None:
                backup.changed = False  # first backup: nothing to compare with
        db.add(backup)
        return backup

    def _prune(self, db: Session, device_id: int, kind: str, keep: int) -> None:
        """Keep the newest ``keep`` good backups (and the last 5 failures) of this device and type."""
        rows = db.scalars(select(ConfigBackup).where(ConfigBackup.device_id == device_id, ConfigBackup.kind == kind)
                          .order_by(ConfigBackup.created_at.desc(), ConfigBackup.id.desc())).all()
        good = [r for r in rows if r.ok]
        bad = [r for r in rows if not r.ok]
        kept, old = good[:max(1, keep)], good[max(1, keep):] + bad[5:]
        kept_paths = {r.path for r in kept}
        for r in old:
            db.delete(r)
            if r.path and r.path not in kept_paths:
                try:
                    self.file(r).unlink(missing_ok=True)
                except OSError:
                    pass

    def delete(self, db: Session, backup: ConfigBackup) -> None:
        others = db.scalars(select(ConfigBackup.id).where(ConfigBackup.path == backup.path,
                                                          ConfigBackup.id != backup.id)).first()
        if backup.path and not others:
            self.file(backup).unlink(missing_ok=True)
        db.delete(backup)

    # ------------------------------------------------------------------ taking backups
    async def _fetch(self, device: Device, binary: bool) -> list[tuple[str, bytes, str]]:
        """Returns [(kind, data, fingerprint)]."""
        target = ssh_target(device, self.box)
        if device.brand == Brand.UBIQUITI:
            res = await run_command(target, "cat /tmp/system.cfg", command_timeout=60)
            if "=" not in res.output:
                raise CollectorError(f"Unexpected answer to 'cat /tmp/system.cfg': {res.output.strip()[:200]}")
            return [("ubnt", res.output.encode(), res.fingerprint)]
        res = await run_command(target, "/export", command_timeout=180)
        text = res.output
        if "bad command" in text.lower() or "/" not in text:
            raise CollectorError(f"Export failed: {text.strip()[:200]}")
        out = [("export", text.encode(), res.fingerprint)]
        if binary:
            data, fp = await run_and_download(
                target, f"/system backup save name={BACKUP_NAME} dont-encrypt=yes",
                [f"{BACKUP_NAME}.backup", f"flash/{BACKUP_NAME}.backup"],
                cleanup=f'/file remove [find name~"{BACKUP_NAME}.backup"]')
            out.append(("binary", data, fp))
        return out

    async def backup_device(self, device_id: int, trigger: str) -> str:
        """Back up one device now. Returns an error message, or "" when it worked."""
        with self.session_factory() as db:
            s = load_backup_settings(db)
            device = db.get(Device, device_id)
            if device is None:
                return "Device not found."
            if not can_backup(device):
                return "Backups need a MikroTik or Ubiquiti device with SSH turned on."
            try:
                results = await self._fetch(device, s.binary)
            except CollectorError as exc:
                db.add(ConfigBackup(device_id=device.id, kind="ubnt" if device.brand == Brand.UBIQUITI else "export",
                                    ok=False, error=str(exc)[:1000], trigger=trigger))
                db.commit()
                return str(exc)
            except Exception as exc:  # keep the scheduler alive whatever one device does
                log.exception("Backup of %s failed", device.name)
                db.add(ConfigBackup(device_id=device.id, kind="export", ok=False, error=f"Unexpected error: {exc}",
                                    trigger=trigger))
                db.commit()
                return f"Unexpected error: {exc}"
            for kind, data, fingerprint in results:
                if fingerprint and not device.ssh_host_key:
                    device.ssh_host_key = fingerprint
                self._store(db, device, kind, data, trigger)
            db.flush()
            for kind, _, _ in results:
                self._prune(db, device.id, kind, s.keep)
            db.commit()
            return ""

    async def run_all(self, trigger: str) -> tuple[int, int]:
        """Back up every reachable MikroTik/Ubiquiti device. Returns (ok, failed)."""
        if self.running:
            return 0, 0
        self.running = True
        try:
            with self.session_factory() as db:
                s = load_backup_settings(db)
                ids = [d.id for d in db.scalars(select(Device).where(Device.monitoring_enabled,
                                                                      Device.status.in_(BACKUP_STATUSES)))
                       if can_backup(d) and d.login(self.box)[0]]
            sem = asyncio.Semaphore(max(1, s.concurrency))

            async def one(i):
                async with sem:
                    return await self.backup_device(i, trigger)

            errors = await asyncio.gather(*(one(i) for i in ids))
            failed = sum(1 for e in errors if e)
            return len(ids) - failed, failed
        finally:
            self.running = False

    # ------------------------------------------------------------------ daily schedule
    def due(self, now_local: datetime) -> bool:
        with self.session_factory() as db:
            s = load_backup_settings(db)
            if not s.enabled:
                return False
            last = db.get(AppSetting, LAST_RUN_KEY)
            try:
                hh, mm = (int(x) for x in s.time.split(":"))
            except ValueError:
                hh, mm = 2, 0
            today = now_local.date().isoformat()
            return (now_local.hour, now_local.minute) >= (hh, mm) and (last is None or last.value != today)

    def mark_ran(self, now_local: datetime) -> None:
        with self.session_factory() as db:
            row = db.get(AppSetting, LAST_RUN_KEY)
            if row is None:
                db.add(AppSetting(key=LAST_RUN_KEY, value=now_local.date().isoformat()))
            else:
                row.value = now_local.date().isoformat()
            db.commit()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="backups")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                now_local = datetime.now()
                if self.due(now_local):
                    self.mark_ran(now_local)
                    ok, failed = await self.run_all("schedule")
                    log.info("Daily backup finished: %s ok, %s failed", ok, failed)
            except Exception:
                log.exception("Backup scheduler error")
            await asyncio.sleep(60)
