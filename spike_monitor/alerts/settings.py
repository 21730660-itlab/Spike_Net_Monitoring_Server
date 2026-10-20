"""Alert thresholds and notification settings, edited on the Alert settings page."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields

from sqlalchemy.orm import Session

from ..models import AppSetting, Severity

KEY = "alerts"


@dataclass
class AlertSettings:
    # Thresholds
    cpu_pct: float = 85.0
    temperature_c: float = 70.0
    signal_dbm: float = -75.0  # weaker than this on PTP links, stations and upstream links
    broadcast_pps: float = 1000.0  # broadcast+multicast packets/s on one port = possible loop
    poll_failed_minutes: int = 15  # device answers ping but its API/SSH fails this long

    # Timing
    warning_delay_minutes: int = 5  # warnings are sent only if they last this long
    repeat_minutes: int = 60  # remind about unacknowledged critical alerts (0 = never)
    notify_recovery: bool = True
    notify_reboot: bool = True

    # Desktop popup + sound on the server PC (Windows)
    desktop_enabled: bool = True
    desktop_min_severity: str = Severity.WARNING

    # Telegram (free, official): a bot token, plus a team group and/or personal chats per user
    telegram_enabled: bool = False
    tg_token_enc: str = ""  # encrypted bot token from @BotFather
    tg_group_chat_id: str = ""  # optional team group: gets every alert at or above tg_group_min_severity
    tg_group_min_severity: str = Severity.WARNING

    # WhatsApp: "callmebot" (free, alerts to personal numbers) or "meta" (official WhatsApp Cloud API)
    whatsapp_enabled: bool = False
    wa_provider: str = "callmebot"
    wa_phone_number_id: str = ""
    wa_token_enc: str = ""  # encrypted access token
    wa_mode: str = "template"  # "template" (works any time) or "text" (only within 24 h of the user writing)
    wa_template: str = "spike_alert"
    wa_language: str = "en"
    wa_api_version: str = "v21.0"


def load_settings(db: Session) -> AlertSettings:
    row = db.get(AppSetting, KEY)
    if row is None:
        return AlertSettings()
    try:
        stored = json.loads(row.value)
    except ValueError:
        stored = {}
    known = {f.name for f in fields(AlertSettings)}
    return AlertSettings(**{k: v for k, v in stored.items() if k in known})


def save_settings(db: Session, settings: AlertSettings) -> None:
    row = db.get(AppSetting, KEY)
    value = json.dumps(asdict(settings))
    if row is None:
        db.add(AppSetting(key=KEY, value=value))
    else:
        row.value = value
