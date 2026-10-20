"""Telegram Bot API sender (free, official, no limit on how many people use your bot).

Setup: talk to @BotFather in Telegram, send /newbot, copy the token. Everyone who wants alerts
opens the new bot and presses Start (or add the bot to a team group); then "Find chats" on the
Alert settings page lists their chat IDs.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import https

API_URL = "https://api.telegram.org"
MAX_TEXT = 4000


class TelegramError(Exception):
    pass


def _call(token: str, method: str, params: dict, timeout: float = 15) -> dict | list:
    if not token:
        raise TelegramError("Telegram is not configured (bot token missing).")
    req = urllib.request.Request(f"{API_URL}/bot{token}/{method}", data=urllib.parse.urlencode(params).encode(),
                                 method="POST")
    try:
        with https.urlopen(req, timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("description", str(exc))
        except ValueError:
            detail = str(exc)
        if exc.code == 401:
            detail += " (check the bot token)"
        elif exc.code == 403:
            detail += " (the person must open the bot and press Start first)"
        raise TelegramError(f"Telegram error {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramError(f"Could not reach Telegram: {https.explain(exc)}") from exc
    if not body.get("ok"):
        raise TelegramError(f"Telegram: {body.get('description', 'unknown error')}")
    return body.get("result", {})


def send(token: str, chat_id: str, text: str) -> str:
    if not str(chat_id).strip():
        raise TelegramError("No Telegram chat ID set.")
    result = _call(token, "sendMessage", {"chat_id": str(chat_id).strip(), "text": text[:MAX_TEXT],
                                          "disable_web_page_preview": "true"})
    return str(result.get("message_id", "")) if isinstance(result, dict) else ""


def find_chats(token: str) -> list[dict]:
    """People and groups that recently wrote to the bot: [{id, name, type}]."""
    updates = _call(token, "getUpdates", {"limit": 100, "timeout": 0})
    chats: dict[str, dict] = {}
    for u in updates if isinstance(updates, list) else []:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if "id" not in chat:
            continue
        name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
        if chat.get("username"):
            name = f"{name} (@{chat['username']})".strip()
        chats[str(chat["id"])] = {"id": str(chat["id"]), "name": name or "?", "type": chat.get("type", "")}
    return list(chats.values())
