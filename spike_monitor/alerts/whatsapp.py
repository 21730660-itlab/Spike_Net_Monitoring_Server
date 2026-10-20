"""WhatsApp senders: Meta's WhatsApp Cloud API, or CallMeBot for personal numbers.

CallMeBot (free, personal use): every person who wants alerts sends one WhatsApp message to
CallMeBot once, gets an API key back, and alerts then arrive on their own number from CallMeBot.

Meta WhatsApp Cloud API (official, for businesses):

Business-initiated messages must use an approved *template*. Create one in
WhatsApp Manager named ``spike_alert`` (category Utility) with the body:

    Spike Net alert: {{1}}

Template parameters may not contain new lines, so several alerts are joined with " | ".
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import https

GRAPH_URL = "https://graph.facebook.com"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
MAX_PARAM = 1000


class WhatsAppError(Exception):
    pass


def normalize_number(phone: str) -> str:
    """'+961 70 123 456' -> '96170123456' (digits only, international format)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[2:] if digits.startswith("00") else digits


def phone_number_id_problem(value: str) -> str | None:
    """Meta's Phone number ID is a long number shown in API Setup, not the phone number itself."""
    if not value:
        return None
    if value.startswith("+") or not value.isdigit():
        return ("The Phone number ID is not your phone number: it is a long number (like 123456789012345) "
                "shown under your number in Meta's WhatsApp → API Setup page.")
    if len(value) < 12:
        return "This looks like a phone number. Copy the Phone number ID from Meta's WhatsApp → API Setup page."
    return None


def send_callmebot(phone: str, apikey: str, text: str, timeout: float = 20) -> str:
    """Send to a personal number through CallMeBot. Raises WhatsAppError on failure."""
    number = normalize_number(phone)
    if not number:
        raise WhatsAppError("No WhatsApp number set for this user.")
    if not apikey:
        raise WhatsAppError(f"No CallMeBot API key for +{number}. Get one by sending "
                            "\"I allow callmebot to send me messages\" to CallMeBot on WhatsApp.")
    query = urllib.parse.urlencode({"phone": f"+{number}", "text": text[:1500], "apikey": apikey})
    try:
        with https.urlopen(f"{CALLMEBOT_URL}?{query}", timeout) as resp:
            body = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise WhatsAppError(f"CallMeBot error {exc.code}: {_plain(body)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WhatsAppError(f"Could not reach CallMeBot: {https.explain(exc)}") from exc
    lowered = body.lower()
    if "invalid" in lowered or "error" in lowered or "not allowed" in lowered:
        raise WhatsAppError(f"CallMeBot: {_plain(body)}")
    return "queued"


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:300]


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
        with https.urlopen(req, timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode()).get("error", {})
            detail = err.get("error_user_msg") or err.get("message") or str(exc)
        except ValueError:
            detail = str(exc)
        raise WhatsAppError(f"WhatsApp API error {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WhatsAppError(f"Could not reach WhatsApp: {https.explain(exc)}") from exc
    messages = body.get("messages") or [{}]
    return str(messages[0].get("id", ""))
