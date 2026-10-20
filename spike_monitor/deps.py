"""Shared request helpers: current user, role checks, templates and audit log."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .collectors.common import format_bps, format_uptime
from .db import get_db
from .models import AuditLog, Role, Status, User
from .security import SecretBox, csrf_token

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def local_time(value: datetime | None) -> str:
    """Timestamps are stored in UTC; show them in the server's local time."""
    if not value:
        return "—"
    return value.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _label(value: str | None) -> str:
    return (value or "").replace("_", " ").title()


templates.env.filters["dt"] = local_time
templates.env.filters["label"] = _label
templates.env.filters["uptime"] = format_uptime
templates.env.filters["bps"] = format_bps
templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["Role"] = Role
templates.env.globals["Status"] = Status


class LoginRequired(Exception):
    """Raised when a page needs a logged-in user; handled with a redirect."""


class PasswordChangeRequired(Exception):
    """Raised when the user must set a new password before doing anything else."""


def get_secret_box(request: Request) -> SecretBox:
    return request.app.state.secret_box


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        request.session.pop("user_id", None)
        raise LoginRequired()
    if user.must_change_password and request.url.path not in ("/account/password", "/logout"):
        raise PasswordChangeRequired()
    request.state.user = user
    return user


def require_role(minimum: str):
    def _dep(user: User = Depends(get_current_user)) -> User:
        if not user.has_role(minimum):
            raise HTTPException(status_code=403, detail="You do not have permission to do this.")
        return user

    return _dep


require_viewer = require_role(Role.VIEWER)
require_technician = require_role(Role.TECHNICIAN)
require_admin = require_role(Role.ADMIN)


def audit(db: Session, request: Request, username: str, action: str, target: str = "", details: str = "") -> None:
    db.add(
        AuditLog(
            username=username,
            action=action,
            target=target[:255],
            details=details,
            ip=request.client.host if request.client else "",
        )
    )


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {"user": getattr(request.state, "user", None)}
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, message: str, category: str = "success") -> None:
    request.session.setdefault("flash", []).append({"message": message, "category": category})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("flash", [])


templates.env.globals["pop_flashes"] = pop_flashes
