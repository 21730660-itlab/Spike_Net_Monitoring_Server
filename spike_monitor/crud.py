"""Generic list / create / edit / delete pages for inventory tables."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_db
from .deps import audit, flash, render, require_role
from .forms import Field, parse_form
from .models import Role, User, utcnow
from .security import SecretBox, verify_csrf

Column = tuple[str, Callable[[Any], Any]]


@dataclass
class Crud:
    model: type
    prefix: str  # e.g. "/sites"
    title: str  # plural, e.g. "Sites"
    singular: str  # e.g. "Site"
    fields: list[Field]
    columns: list[Column]
    view_role: str = Role.VIEWER
    edit_role: str = Role.TECHNICIAN
    order_by: Any = None
    description: str = ""
    # Extra checks: return {field: error} for problems the form layer can't see.
    validate: Callable[[Session, dict, Any | None], dict[str, str]] | None = None
    # Copy parsed values onto the object. Default: setattr for every field.
    apply: Callable[[Session, Any, dict, SecretBox], None] | None = None
    # Values shown in the edit form. Default: getattr for every field.
    initial: Callable[[Any], dict] | None = None
    extra_fields: Callable[[Session, Any | None], list[Field]] | None = None
    detail_url: Callable[[Any], str] | None = None
    row_actions: str = ""  # extra template included in each list row's action cell
    templates: dict[str, str] = field(default_factory=dict)

    def form_fields(self, db: Session, obj: Any | None) -> list[Field]:
        return self.fields + (self.extra_fields(db, obj) if self.extra_fields else [])

    def build_router(self) -> APIRouter:
        router = APIRouter(prefix=self.prefix)
        crud = self
        viewer = require_role(self.view_role)
        editor = require_role(self.edit_role)

        def _get(db: Session, item_id: int):
            obj = db.get(crud.model, item_id)
            if obj is None:
                raise HTTPException(404, f"{crud.singular} not found")
            return obj

        def _form_page(request: Request, db: Session, obj, values: dict, errors: dict, status: int = 200):
            fields = crud.form_fields(db, obj)
            return render(
                request,
                crud.templates.get("form", "generic/form.html"),
                {
                    "crud": crud,
                    "obj": obj,
                    "fields": fields,
                    "options": {f.name: f.resolve_options(db) for f in fields},
                    "option_attrs": {f.name: f.option_attrs(db) for f in fields if f.option_attrs},
                    "values": values,
                    "errors": errors,
                },
                status_code=status,
            )

        def _initial(obj) -> dict:
            if crud.initial:
                return crud.initial(obj)
            return {f.name: getattr(obj, f.name, None) for f in crud.fields}

        async def _save(request: Request, db: Session, user: User, obj):
            form = await request.form()
            fields = crud.form_fields(db, obj)
            result = parse_form(fields, form)
            if result.ok and crud.validate:
                result.errors.update(crud.validate(db, result.values, obj))
            if not result.ok:
                return _form_page(request, db, obj, result.values, result.errors, 422)

            creating = obj is None
            if creating:
                obj = crud.model()
            if crud.apply:
                crud.apply(db, obj, result.values, request.app.state.secret_box)
            else:
                for f in fields:
                    setattr(obj, f.name, result.values[f.name])
            if creating:
                db.add(obj)
            try:
                db.flush()
                audit(db, request, user.username, f"{crud.singular.lower()}.{'create' if creating else 'update'}",
                      f"{crud.singular} #{obj.id} {getattr(obj, 'name', '')}")
                db.commit()
            except IntegrityError:
                db.rollback()
                return _form_page(request, db, None if creating else obj, result.values,
                                  {"__all__": f"A {crud.singular.lower()} with these details already exists."}, 422)
            flash(request, f"{crud.singular} saved.")
            target = crud.detail_url(obj) if crud.detail_url else crud.prefix
            return RedirectResponse(target, status_code=303)

        @router.get("")
        def list_page(request: Request, db: Session = Depends(get_db), user: User = Depends(viewer)):
            stmt = select(crud.model)
            if crud.order_by is not None:
                stmt = stmt.order_by(crud.order_by)
            items = db.scalars(stmt).all()
            return render(request, crud.templates.get("list", "generic/list.html"),
                          {"crud": crud, "items": items, "can_edit": user.has_role(crud.edit_role), "now": utcnow()})

        @router.get("/new")
        def new_page(request: Request, db: Session = Depends(get_db), user: User = Depends(editor)):
            fields = crud.form_fields(db, None)
            values = {f.name: f.default for f in fields}
            # Pre-fill from the link, e.g. "Add as device" on the discovery page.
            for f in fields:
                if f.name in request.query_params and f.kind not in ("password", "multiselect", "bool"):
                    values[f.name] = request.query_params[f.name][:255]
            return _form_page(request, db, None, values, {})

        @router.post("/new", dependencies=[Depends(verify_csrf)])
        async def create(request: Request, db: Session = Depends(get_db), user: User = Depends(editor)):
            return await _save(request, db, user, None)

        @router.get("/{item_id}/edit")
        def edit_page(item_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(editor)):
            obj = _get(db, item_id)
            return _form_page(request, db, obj, _initial(obj), {})

        @router.post("/{item_id}/edit", dependencies=[Depends(verify_csrf)])
        async def update(item_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(editor)):
            return await _save(request, db, user, _get(db, item_id))

        @router.post("/{item_id}/delete", dependencies=[Depends(verify_csrf)])
        def delete(item_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(editor)):
            obj = _get(db, item_id)
            name = getattr(obj, "name", "")
            db.delete(obj)
            audit(db, request, user.username, f"{crud.singular.lower()}.delete", f"{crud.singular} #{item_id} {name}")
            db.commit()
            flash(request, f"{crud.singular} deleted.")
            return RedirectResponse(crud.prefix, status_code=303)

        return router
