from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .config import Settings, settings as default_settings
from .db import SessionLocal, init_engine
from .deps import LoginRequired, PasswordChangeRequired, render
from .models import Role, User
from .alerts.engine import AlertManager
from .monitoring.engine import MonitorEngine
from .monitoring.poller import StatsPoller
from .routers import admin, alerts, auth, commands, dashboard, device_actions, devices, discovery, netmap
from .routers.inventory import ALL_CRUDS
from .security import SecretBox, hash_password

log = logging.getLogger("spike_monitor")


def ensure_bootstrap_admin(cfg: Settings) -> None:
    """Create the first admin from the preshared key if there are no users yet."""
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(User)):
            return
        db.add(User(
            username=cfg.bootstrap_admin_user,
            full_name="Administrator",
            password_hash=hash_password(cfg.bootstrap_admin_key),
            role=Role.ADMIN,
            must_change_password=True,
        ))
        db.commit()
        log.warning("Created first admin user '%s'. Log in with the preshared key and change the password.",
                    cfg.bootstrap_admin_user)


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or default_settings
    init_engine(cfg.database_url)
    box = SecretBox(cfg.secret_key_file)
    ensure_bootstrap_admin(cfg)

    engine = MonitorEngine(
        SessionLocal,
        interval=cfg.ping_interval,
        count=cfg.ping_count,
        timeout=cfg.ping_timeout,
        concurrency=cfg.ping_concurrency,
        retention_days=cfg.raw_retention_days,
    )

    poller = StatsPoller(
        SessionLocal,
        box,
        interval=cfg.stats_interval,
        concurrency=cfg.stats_concurrency,
        raw_retention_days=cfg.raw_retention_days,
        rollup_retention_days=cfg.rollup_retention_days,
    )

    alert_manager = AlertManager(SessionLocal, box, stats_interval=cfg.stats_interval)
    engine.listeners.append(alert_manager.schedule)  # check for alerts after every ping round

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if cfg.monitoring_enabled:
            engine.start()
            poller.start()
        yield
        await poller.stop()
        await engine.stop()

    app = FastAPI(title="Spike Net Monitoring Server", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.settings = cfg
    app.state.secret_box = box
    app.state.engine = engine
    app.state.poller = poller
    app.state.alert_manager = alert_manager
    app.state.session_factory = SessionLocal
    app.state.background_tasks = set()

    app.add_middleware(SessionMiddleware, secret_key=box.session_secret, session_cookie="spike_session",
                       max_age=12 * 3600, same_site="lax", https_only=False)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    for crud in ALL_CRUDS:
        app.include_router(crud.build_router())
    # After the generic device routes so /devices/new wins over /devices/{id}
    app.include_router(device_actions.router)
    app.include_router(devices.router)
    app.include_router(commands.router)
    app.include_router(discovery.router)
    app.include_router(netmap.router)
    app.include_router(alerts.router)
    app.include_router(admin.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not logged in"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(PasswordChangeRequired)
    async def _password_change(request: Request, exc: PasswordChangeRequired):
        return RedirectResponse("/account/password", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", {"code": exc.status_code, "detail": exc.detail}, exc.status_code)

    return app
