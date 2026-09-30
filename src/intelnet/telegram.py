"""Telegram transport for a MANY-chat network bot.

`digest_core.sinks.telegram.TelegramNotifier` is a one-chat notifier (the PC
and macro digests talk to their owner only). This network talks to every
sensor and subscriber, so `Bot` adds per-chat sends and edits on top of the
shared client: same HTML escaping rules, same "never raise into the caller"
policy. `deliver` and `edit` report what Telegram said (message id, retry
delay, whether retrying can help) so the outbox can act on it; `send_to` is the
plain yes/no form for replies.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
from digest_core.sinks import telegram as _tg
from digest_core.sinks.telegram import TelegramNotifier, esc, href, join_within

from intelnet.config import settings
from intelnet.models import local_time

logger = logging.getLogger(__name__)

MAX_MSG = 4000   # Telegram's limit is 4096; leave headroom

__all__ = ["Bot", "Sent", "bot", "esc", "href", "join_within", "tg_time", "MAX_MSG"]

# Fallback text inside <tg-time> for clients that predate it: Illinois wall clock.
_TG_TIME_FALLBACK = {"t": "%-I:%M %p %Z", "wt": "%a %-I:%M %p %Z"}
_TG_TIME = re.compile(r"<tg-time [^>]*>(.*?)</tg-time>", re.DOTALL)


def tg_time(dt: datetime, fmt: str = "wt") -> str:
    """A moment that Telegram shows in each reader's own time zone.

    Bot API 9.5 date-time entity: `wt` is weekday + time, `t` time only, `r` relative
    ("in 20 minutes"). The text inside is the Illinois wall-clock fallback.
    """
    shown = local_time(dt, _TG_TIME_FALLBACK.get(fmt, "%a %-d %b %-I:%M %p %Z"))
    return f'<tg-time unix="{int(dt.timestamp())}" format="{fmt}">{esc(shown)}</tg-time>'


@dataclass
class Sent:
    """What Telegram said to one send or edit."""

    ok: bool
    message_id: int | None = None
    retry_after: float | None = None   # 429: wait this long before trying again
    permanent: bool = False            # blocked, chat gone, bad markup: retrying won't help
    error: str = ""


class Bot(TelegramNotifier):
    """The network's Telegram client: admin chat = the notifier's default chat."""

    def deliver(self, chat_id: str | int, text: str, *, silent: bool = False,
                reply_to: int | None = None) -> Sent:
        """POST one HTML message to any chat; `reply_to` quotes an earlier message."""
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self._call("sendMessage", payload)

    def edit(self, chat_id: str | int, message_id: int, text: str) -> Sent:
        """Rewrite a message the bot sent earlier (editing never makes a sound)."""
        return self._call("editMessageText", {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    def send_to(self, chat_id: str | int, text: str, *, silent: bool = False) -> bool:
        """POST one HTML message to any chat. False on no-op or failure."""
        return self.deliver(chat_id, text, silent=silent).ok

    def _call(self, method: str, payload: dict[str, Any]) -> Sent:
        chat = payload.get("chat_id")
        if not self.enabled:
            logger.debug("telegram: disabled; not sending to %s", chat)
            return Sent(False, error="telegram disabled")
        try:
            resp = requests.post(self._url(method), json=payload, timeout=_tg._TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            # The exception's text includes the URL, and the URL includes the token.
            logger.warning("telegram: %s to %s failed: %s", method, chat, type(exc).__name__)
            return Sent(False, error=type(exc).__name__)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.ok and body.get("ok"):
            result = body.get("result")
            return Sent(True, message_id=result.get("message_id") if isinstance(result, dict) else None)
        desc = str(body.get("description") or f"HTTP {resp.status_code}")
        if method == "editMessageText" and "message is not modified" in desc:
            return Sent(True, message_id=payload.get("message_id"))
        if "parse entities" in desc and "<tg-time" in str(payload.get("text")):
            # Never lose a warning to markup: send the plain Illinois-time version instead.
            logger.warning("telegram: <tg-time> refused (%s); sending plain times", desc)
            return self._call(method, {**payload, "text": _TG_TIME.sub(r"\1", payload["text"])})
        retry_after = (body.get("parameters") or {}).get("retry_after")
        if resp.status_code == 403:
            # The user blocked the bot or left: a permanent condition for that chat.
            logger.info("telegram: chat %s is closed to the bot (%s)", chat, desc)
        else:
            logger.warning("telegram: %s to %s failed: %s", method, chat, desc)
        return Sent(False, retry_after=float(retry_after) if retry_after else None,
                    permanent=resp.status_code in (400, 403), error=desc)

    def typing(self, chat_id: str | int) -> None:
        if not self.enabled:
            return
        try:
            requests.post(
                self._url("sendChatAction"),
                json={"chat_id": str(chat_id), "action": "typing"},
                timeout=_tg._TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("telegram: chat action failed: %s", type(exc).__name__)

    def broadcast(self, chat_ids: list[str], text: str) -> int:
        return sum(1 for c in chat_ids if self.send_to(c, text))


bot = Bot(
    token=settings.telegram_bot_token,
    chat_id=settings.telegram_admin_chat_id,
    enabled=settings.notify_enabled,
)
