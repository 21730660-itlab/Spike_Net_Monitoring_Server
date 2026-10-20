from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import audit, flash, get_current_user, render
from ..models import User, utcnow
from ..security import password_problem, verify_csrf, verify_password, hash_password

router = APIRouter()

# Simple brute-force protection: per client IP, lock for 5 minutes after 5 failures.
_failures: dict[str, list[float]] = {}
MAX_FAILURES = 5
LOCK_SECONDS = 300


def _locked(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failures.get(ip, []) if now - t < LOCK_SECONDS]
    _failures[ip] = recent
    return len(recent) >= MAX_FAILURES


@router.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return render(request, "auth/login.html", {"error": None})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, username: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    if _locked(ip):
        return render(request, "auth/login.html",
                      {"error": "Too many failed attempts. Wait 5 minutes and try again."}, 429)
    user = db.scalars(select(User).where(func.lower(User.username) == username.strip().lower())).first()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        _failures.setdefault(ip, []).append(time.monotonic())
        audit(db, request, username.strip()[:64] or "?", "login.failed")
        db.commit()
        return render(request, "auth/login.html", {"error": "Wrong username or password."}, 401)

    _failures.pop(ip, None)
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session["user_id"] = user.id
    if csrf:
        request.session["csrf"] = csrf
    user.last_login = utcnow()
    audit(db, request, user.username, "login")
    db.commit()
    return RedirectResponse("/account/password" if user.must_change_password else "/", status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/account/password")
def password_page(request: Request, user: User = Depends(get_current_user)):
    return render(request, "auth/change_password.html", {"error": None, "forced": user.must_change_password})


@router.post("/account/password", dependencies=[Depends(verify_csrf)])
def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    error = None
    if not verify_password(current_password, user.password_hash):
        error = "Current password is wrong."
    elif new_password != confirm_password:
        error = "The new passwords do not match."
    elif verify_password(new_password, user.password_hash):
        error = "The new password must be different from the current one."
    else:
        error = password_problem(new_password, user.username)
    if error:
        return render(request, "auth/change_password.html",
                      {"error": error, "forced": user.must_change_password}, 422)

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    audit(db, request, user.username, "password.change")
    db.commit()
    flash(request, "Password changed.")
    return RedirectResponse("/", status_code=303)
