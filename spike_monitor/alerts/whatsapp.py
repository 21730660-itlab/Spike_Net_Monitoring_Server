"""WhatsApp Cloud API (Meta) sender.

Business-initiated messages must use an approved *template*. Create one in
WhatsApp Manager named ``spike_alert`` (category Utility) with the body:

    Spike Net alert: {{1}}

Template parameters may not contain new lines, so several alerts are joined with " | ".
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

GRAPH_URL = "https://graph.facebook.com"
MAX_PARAM = 1000


class WhatsAppError(Exception):
    pass


def normalize_number(phone: str) -> str:
    """'+961 70 123 456' -> '96170123456' (digits only, international format)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[2:] if digits.startswith("00") else digits


def build_payload(to: str, text: str, mode: str, template: str, language: str) -> dict:
    if mode == "text":
        return {"messaging_product": "whatsapp", "to": to, "type": "text",
                "text": {"preview_url": False, "body": text[:4000]}}
    param = re.sub(r"\s*\n\s*", " | ", text)
    param = re.sub(r"\s{4,}", "   ", param)[:MAX_PARAM]
    return {
        "messaging_product": "whatsapp", "to": to, "type": "template",
        "template": {"name": template, "language": {"code": language},
                     "components": [{"type": "body", "parameters": [{"type": "text", "text": param}]}]},
    }


def send(phone_number_id: str, token: str, payload: dict, api_version: str = "v21.0", timeout: float = 15) -> str:
    """Send one message. Returns the WhatsApp message id; raises WhatsAppError on failure."""
    if not phone_number_id or not token:
        raise WhatsAppError("WhatsApp is not configured (phone number ID and access token are required).")
    req = urllib.request.Request(
        f"{GRAPH_URL}/{api_version}/{phone_number_id}/messages",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode()).get("error", {})
            detail = err.get("error_user_msg") or err.get("message") or str(exc)
        except ValueError:
            detail = str(exc)
        raise WhatsAppError(f"WhatsApp API error {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WhatsAppError(f"Could not reach WhatsApp: {exc}") from exc
    messages = body.get("messages") or [{}]
    return str(messages[0].get("id", ""))
