"""Small declarative form layer used by the inventory pages.

Each page lists its fields once; the same list renders the HTML form,
parses the submitted values and reports validation errors.
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

Options = list[tuple[str, str]]

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


@dataclass
class Field:
    name: str
    label: str
    kind: str = "text"  # text, combo, int, float, bool, select, multiselect, textarea, password, ip, mac, port
    required: bool = False
    help: str = ""
    options: Callable[[Session], Options] | Options | None = None
    placeholder: str = ""
    default: Any = None
    group: str = ""
    min_value: float | None = None
    max_value: float | None = None
    # Extra data-* attributes per option value, e.g. {"12": {"site": "3"}} (used for filtering in the browser)
    option_attrs: Callable[[Session], dict[str, dict[str, str]]] | None = None
    # For kind "combo": free text with suggestions (an HTML datalist)
    suggestions: list[str] = field(default_factory=list)

    def resolve_options(self, db: Session) -> Options:
        if self.options is None:
            return []
        return self.options(db) if callable(self.options) else list(self.options)


@dataclass
class FormResult:
    values: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def choices(values: tuple[str, ...] | list[str]) -> Options:
    return [(v, v.replace("_", " ").title()) for v in values]


def normalize_mac(value: str) -> str:
    return value.replace("-", ":").upper()


def parse_form(fields: list[Field], form: Any) -> FormResult:
    """Convert raw form strings into typed values according to ``fields``."""
    result = FormResult()
    for f in fields:
        if f.kind == "multiselect":
            raw_list = [v for v in form.getlist(f.name) if v]
            try:
                result.values[f.name] = [int(v) for v in raw_list]
            except ValueError:
                result.errors[f.name] = "Invalid selection."
            continue
        if f.kind == "bool":
            result.values[f.name] = form.get(f.name) in ("on", "true", "1", "yes")
            continue

        raw = str(form.get(f.name, "")).strip()
        if raw == "":
            if f.required:
                result.errors[f.name] = f"{f.label} is required."
            # Empty optional numbers/selects become None, text stays "".
            result.values[f.name] = "" if f.kind in ("text", "combo", "textarea", "password", "mac") else None
            continue

        try:
            if f.kind in ("int", "port"):
                value: Any = int(raw)
                if f.kind == "port" and not 1 <= value <= 65535:
                    raise ValueError("Port must be between 1 and 65535.")
            elif f.kind == "float":
                value = float(raw)
            elif f.kind == "select":
                allowed = {k for k, _ in (f.options if isinstance(f.options, list) else [])}
                if allowed and raw not in allowed:
                    raise ValueError("Invalid choice.")
                value = int(raw) if raw.isdigit() and not allowed else raw
            elif f.kind == "ip":
                value = str(ipaddress.ip_address(raw)) if not _is_hostname(raw) else raw
            elif f.kind == "mac":
                if not MAC_RE.match(raw):
                    raise ValueError("MAC must look like AA:BB:CC:DD:EE:FF.")
                value = normalize_mac(raw)
            else:
                value = raw
            if f.min_value is not None and value < f.min_value:
                raise ValueError(f"Must be at least {f.min_value:g}.")
            if f.max_value is not None and value > f.max_value:
                raise ValueError(f"Must be at most {f.max_value:g}.")
        except ValueError as exc:
            msg = str(exc)
            if "invalid literal" in msg or "could not convert" in msg or "does not appear" in msg:
                msg = f"{f.label} is not valid."
            result.errors[f.name] = msg
            value = raw
        result.values[f.name] = value
    return result


_HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z][A-Za-z0-9-]{0,62}$")


def _is_hostname(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return False
    except ValueError:
        if _HOST_RE.match(value):
            return True
        raise ValueError("Enter a valid IP address or hostname.")
