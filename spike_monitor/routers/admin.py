"""User management and audit log (admin only)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import audit, flash, render, require_admin
from ..forms import Field, choices, parse_form
from ..models import AuditLog, Role, Severity, Site, User
from .inventory import site_options
from ..security import hash_password, password_problem, verify_csrf

router = APIRouter(prefix="/admin")

USER_FIELDS = [
    Field("username", "Username", required=True),
    Field("full_name", "Full name"),
    Field("role", "Role", kind="select", required=True, options=choices(Role.ALL), default=Role.VIEWER,
          help="Admin: everything. Technician: manage devices, no passwords or users. Viewer: read only."),
    Field("phone", "WhatsApp number", placeholder="+961 70 123 456", help="International format, with country code."),
    Field("whatsapp_alerts", "Send me WhatsApp alerts", kind="bool", default=False),
    Field("callmebot_key", "CallMeBot API key", kind="password",
          help="Only for the CallMeBot WhatsApp option. Leave empty to keep the saved key."),
    Field("alert_min_severity", "Alerts to receive", kind="select", default=Severity.WARNING,
          options=[(Severity.CRITICAL, "Critical only (down, loops)"), (Severity.WARNING, "Critical + warnings"),
                   (Severity.INFO, "Everything (incl. reboots)")]),
    Field("alert_site_ids", "Only for these sites", kind="multiselect", options=site_options,
          help="Leave empty to receive alerts for all sites. Hold Ctrl to select several."),
    Field("password", "Password", kind="password",
          help="Required for new users. Leave empty to keep the current password."),
    Field("must_change_password", "Must change password at next login", kind="bool", default=True),
    Field("is_active", "Active", kind="bool", default=True),
]


def _admins_left(db: Session, excluding: int) -> int:
    return db.scalar(select(func.count()).select_from(User).where(
        User.role == Role.ADMIN, User.is_active, User.id != excluding)) or 0


@router.get("/users")
def users(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return render(request, "admin/users.html", {"users": db.scalars(select(User).order_by(User.username)).all()})


def _form(request, db, obj, values, errors, status=200):
    return render(request, "generic/form.html", {
        "crud": {"title": "Users", "singular": "User", "prefix": "/admin/users"},
        "obj": obj, "fields": USER_FIELDS, "values": values, "errors": errors,
        "options": {f.name: f.resolve_options(db) for f in USER_FIELDS},
    }, status_code=status)


@router.get("/users/new")
def new_user(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _form(request, db, None, {f.name: f.default for f in USER_FIELDS}, {})


@router.get("/users/{user_id}/edit")
def edit_user(user_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    target = db.get(User, user_id) or _404()
    values = {f.name: getattr(target, f.name, "") for f in USER_FIELDS}
    values["password"] = ""
    values["callmebot_key"] = ""
    values["alert_site_ids"] = [s.id for s in target.alert_sites]
    return _form(request, db, target, values, {})


def _404():
    raise HTTPException(404, "User not found")


async def _save(request: Request, db: Session, admin: User, target: User | None):
    result = parse_form(USER_FIELDS, await request.form())
    v = result.values
    v["alert_min_severity"] = v.get("alert_min_severity") or Severity.WARNING
    if result.ok:
        stmt = select(User).where(func.lower(User.username) == v["username"].lower())
        if target is not None:
            stmt = stmt.where(User.id != target.id)
        if db.scalars(stmt).first():
            result.errors["username"] = "This username is taken."
        if v["whatsapp_alerts"] and not v["phone"]:
            result.errors["phone"] = "Enter the WhatsApp number to send alerts to."
        if target is None and not v["password"]:
            result.errors["password"] = "Set a password for the new user."
        if v["password"]:
            problem = password_problem(v["password"], v["username"])
            if problem:
                result.errors["password"] = problem
        if target is not None and target.role == Role.ADMIN and (v["role"] != Role.ADMIN or not v["is_active"]) \
                and _admins_left(db, target.id) == 0:
            result.errors["role"] = "There must always be at least one active admin."
    if not result.ok:
        return _form(request, db, target, v, result.errors, 422)

    creating = target is None
    if creating:
        target = User()
        db.add(target)
    for key in ("username", "full_name", "role", "phone", "must_change_password", "is_active",
                "whatsapp_alerts", "alert_min_severity"):
        setattr(target, key, v[key])
    target.alert_sites = [s for s in (db.get(Site, i) for i in v["alert_site_ids"]) if s is not None]
    if v["password"]:
        target.password_hash = hash_password(v["password"])
    if v["callmebot_key"]:
        target.callmebot_key_enc = request.app.state.secret_box.encrypt(v["callmebot_key"].strip())
    db.flush()
    audit(db, request, admin.username, "user.create" if creating else "user.update",
          f"User #{target.id} {target.username}", f"role={target.role} active={target.is_active}")
    db.commit()
    flash(request, "User saved.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/new", dependencies=[Depends(verify_csrf)])
async def create_user(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return await _save(request, db, user, None)


@router.post("/users/{user_id}/edit", dependencies=[Depends(verify_csrf)])
async def update_user(user_id: int, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_admin)):
    return await _save(request, db, user, db.get(User, user_id) or _404())


@router.post("/users/{user_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    target = db.get(User, user_id) or _404()
    if target.id == user.id:
        flash(request, "You cannot delete your own account.", "error")
    elif target.role == Role.ADMIN and _admins_left(db, target.id) == 0:
        flash(request, "There must always be at least one active admin.", "error")
    else:
        db.delete(target)
        audit(db, request, user.username, "user.delete", f"User #{user_id} {target.username}")
        db.commit()
        flash(request, "User deleted.")
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/audit")
def audit_log(request: Request, page: int = 1, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    page = max(1, page)
    per_page = 100
    rows = db.scalars(select(AuditLog).order_by(AuditLog.ts.desc()).offset((page - 1) * per_page)
                      .limit(per_page + 1)).all()
    return render(request, "admin/audit.html",
                  {"rows": rows[:per_page], "page": page, "has_next": len(rows) > per_page})
