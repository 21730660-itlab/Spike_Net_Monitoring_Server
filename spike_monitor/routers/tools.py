"""Tools page: IP calculator, subnet split, VLSM planner, ping and traceroute."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import audit, render, require_technician
from ..models import User
from ..monitoring.pinger import ping
from ..security import verify_csrf
from ..tools import ipcalc
from ..tools.trace import traceroute

router = APIRouter(prefix="/tools")
HOST_RE = re.compile(r"^[A-Za-z0-9.:-]{1,253}$")


def _page(request, **ctx):
    base = {"calc": None, "calc_error": None, "split": None, "vlsm": None, "plan_error": None, "ping": None,
            "trace": None, "net_error": None, "form": {}}
    base.update(ctx)
    return render(request, "tools.html", base)


@router.get("")
def tools_page(request: Request, ip: str = "", user: User = Depends(require_technician)):
    if not ip:
        return _page(request)
    try:
        return _page(request, calc=ipcalc.calculate(ip), form={"ip": ip})
    except ipcalc.CalcError as exc:
        return _page(request, calc_error=str(exc), form={"ip": ip})


@router.get("/plan")
def plan(request: Request, base: str = "", prefix: str = "", needs: str = "", user: User = Depends(require_technician)):
    form = {"base": base, "prefix": prefix, "needs": needs}
    try:
        if needs.strip():
            return _page(request, vlsm=ipcalc.vlsm(base, ipcalc.parse_needs(needs)), form=form)
        if not prefix.strip().lstrip("/").isdigit():
            raise ipcalc.CalcError("Enter a new prefix (e.g. 27) or a list of subnets with host counts.")
        return _page(request, split=ipcalc.split(base, int(prefix.strip().lstrip("/"))), form=form)
    except ipcalc.CalcError as exc:
        return _page(request, plan_error=str(exc), form=form)


@router.post("/network", dependencies=[Depends(verify_csrf)])
async def network(request: Request, db: Session = Depends(get_db), user: User = Depends(require_technician)):
    form = await request.form()
    host = str(form.get("host", "")).strip()
    action = form.get("action", "ping")
    ctx = {"form": {"host": host}}
    if not HOST_RE.match(host) or host.startswith("-"):
        return _page(request, net_error="Enter an IP address or host name.", **ctx)
    audit(db, request, user.username, f"tools.{action}", host)
    db.commit()
    if action == "trace":
        hops, raw = await traceroute(host)
        return _page(request, trace={"host": host, "hops": hops, "raw": raw}, **ctx)
    try:
        count = max(1, min(int(form.get("count", 5)), 50))
    except ValueError:
        count = 5
    outcome = await ping(host, count, 1.0)
    return _page(request, ping={"host": host, "count": count, "o": outcome}, **ctx)
