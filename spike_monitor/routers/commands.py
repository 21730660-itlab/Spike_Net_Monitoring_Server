"""Push settings: run a RouterOS / shell script on one or many devices over SSH (admin only)."""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from ..collectors.common import CollectorError
from ..collectors.ssh import run_command
from ..db import get_db
from ..deps import audit, flash, render, require_admin
from ..models import CommandJob, CommandResult, Device, User, utcnow
from ..monitoring.poller import ssh_target
from ..security import SecretBox, verify_csrf

log = logging.getLogger(__name__)
router = APIRouter(prefix="/commands")

MAX_OUTPUT = 100_000
# RouterOS prints these when a command fails but still exits with status 0.
ROUTEROS_ERRORS = re.compile(
    r"failure:|syntax error|bad command name|expected end of command|input does not match|no such item|"
    r"invalid value|ambiguous value|not enough permissions",
    re.IGNORECASE,
)

SNIPPETS = [
    ("Show identity and version", "/system identity print\n/system resource print"),
    ("Export configuration", "/export compact"),
    ("Set NTP servers (v7)", "/system ntp client set enabled=yes servers=pool.ntp.org"),
    ("Set NTP servers (v6)", "/system ntp client set enabled=yes primary-ntp=162.159.200.1 secondary-ntp=162.159.200.123"),
    ("Set DNS servers", "/ip dns set servers=1.1.1.1,8.8.8.8"),
    ("Disable unused services", "/ip service disable telnet,ftp,www"),
    ("Add a log comment", ':log info "change by Spike Net Monitor"'),
]


def _devices(db: Session, ids: list[int]) -> list[Device]:
    if not ids:
        return []
    return db.scalars(select(Device).where(Device.id.in_(ids)).order_by(Device.name)).all()


def _parse_ids(values) -> list[int]:
    return sorted({int(v) for v in values if str(v).isdigit()})


@router.get("")
def jobs(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rows = db.scalars(select(CommandJob).options(selectinload(CommandJob.results))
                      .order_by(CommandJob.created_at.desc()).limit(100)).all()
    return render(request, "commands/list.html", {"jobs": rows})


@router.get("/new")
def new_job(request: Request, device: list[int] | None = Query(None), db: Session = Depends(get_db),
            user: User = Depends(require_admin)):
    devices = db.scalars(select(Device).options(selectinload(Device.site)).order_by(Device.name)).all()
    return render(request, "commands/new.html", {
        "devices": devices, "selected": set(device or []), "script": "", "title": "", "snippets": SNIPPETS,
        "error": None})


@router.post("/preview", dependencies=[Depends(verify_csrf)])
async def preview(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    form = await request.form()
    ids = _parse_ids(form.getlist("device_ids"))
    script = str(form.get("script", "")).replace("\r\n", "\n").strip()
    title = str(form.get("title", "")).strip()[:128]
    error = None
    if not ids:
        error = "Select at least one device."
    elif not script:
        error = "Write the commands to run."
    if error:
        devices = db.scalars(select(Device).options(selectinload(Device.site)).order_by(Device.name)).all()
        return render(request, "commands/new.html", {
            "devices": devices, "selected": set(ids), "script": script, "title": title, "snippets": SNIPPETS,
            "error": error}, 422)
    targets = _devices(db, ids)
    box = request.app.state.secret_box
    ready = [d for d in targets if d.use_ssh and d.login(box)[0]]
    skipped = [(d, "SSH turned off" if not d.use_ssh else "no login set") for d in targets if d not in ready]
    return render(request, "commands/preview.html", {
        "ready": ready, "skipped": skipped, "script": script, "title": title})


@router.post("/run", dependencies=[Depends(verify_csrf)])
async def run(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    form = await request.form()
    if form.get("confirm") != "yes":
        raise HTTPException(400, "Tick the confirmation box to run the commands.")
    script = str(form.get("script", "")).replace("\r\n", "\n").strip()
    targets = _devices(db, _parse_ids(form.getlist("device_ids")))
    if not script or not targets:
        raise HTTPException(400, "Nothing to run.")

    job = CommandJob(username=user.username, title=str(form.get("title", ""))[:128], script=script)
    job.results = [CommandResult(device_id=d.id, device_name=f"{d.name} ({d.ip_address})") for d in targets]
    db.add(job)
    db.flush()
    audit(db, request, user.username, "command.run", f"Job #{job.id} on {len(targets)} device(s)", script[:2000])
    db.commit()

    task = asyncio.create_task(run_job(
        request.app.state.session_factory, request.app.state.secret_box, job.id,
        request.app.state.settings.command_concurrency))
    tasks: set = request.app.state.background_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    flash(request, f"Running on {len(targets)} device(s)…")
    return RedirectResponse(f"/commands/{job.id}", status_code=303)


@router.get("/{job_id}")
def job_detail(job_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    job = db.get(CommandJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return render(request, "commands/detail.html", {"job": job})


async def run_job(session_factory: sessionmaker[Session], box: SecretBox, job_id: int, concurrency: int = 10) -> None:
    sem = asyncio.Semaphore(max(1, concurrency))
    with session_factory() as db:
        job = db.get(CommandJob, job_id)
        script = job.script
        result_ids = [r.id for r in job.results]

    async def one(result_id: int) -> None:
        async with sem:
            with session_factory() as db:
                result = db.get(CommandResult, result_id)
                device = db.get(Device, result.device_id) if result.device_id else None
                if device is None:
                    result.status, result.output = "error", "Device was deleted."
                elif not device.use_ssh:
                    result.status, result.output = "error", "SSH is turned off for this device."
                else:
                    try:
                        res = await run_command(ssh_target(device, box), script, command_timeout=120)
                        if res.fingerprint and not device.ssh_host_key:
                            device.ssh_host_key = res.fingerprint
                        result.output = res.output[:MAX_OUTPUT] or "(no output)"
                        failed = (res.exit_status not in (0, None)) or ROUTEROS_ERRORS.search(res.output)
                        result.status = "warning" if failed else "ok"
                    except CollectorError as exc:
                        result.status, result.output = "error", str(exc)
                    except Exception as exc:  # keep going with the other devices
                        log.exception("Command on %s failed", device.name)
                        result.status, result.output = "error", f"Unexpected error: {exc}"
                result.finished_at = utcnow()
                db.commit()

    await asyncio.gather(*(one(r) for r in result_ids))
    with session_factory() as db:
        job = db.get(CommandJob, job_id)
        job.status = "done"
        job.finished_at = utcnow()
        db.commit()
