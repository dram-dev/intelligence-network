"""Telegram transport for a MANY-chat network bot.

`digest_core.sinks.telegram.TelegramNotifier` is a one-chat notifier (the PC
and macro digests talk to their owner only). This network talks to every
sensor and subscriber, so `Bot` adds per-chat sends on top of the shared
client: same HTML escaping rules, same "never raise into the caller" policy.
"""
from __future__ import annotations

import logging

import requests
from digest_core.sinks import telegram as _tg
from digest_core.sinks.telegram import TelegramNotifier, esc, href, join_within

from intelnet.config import settings

logger = logging.getLogger(__name__)

MAX_MSG = 4000   # Telegram's limit is 4096; leave headroom

__all__ = ["Bot", "bot", "esc", "href", "join_within", "MAX_MSG"]


class Bot(TelegramNotifier):
    """The network's Telegram client: admin chat = the notifier's default chat."""

    def send_to(self, chat_id: str | int, text: str, *, silent: bool = False) -> bool:
        """POST one HTML message to any chat. False on no-op or failure."""
        if not self.enabled:
            logger.debug("telegram: disabled; not sending to %s", chat_id)
            return False
        try:
            resp = requests.post(
                self._url("sendMessage"),
                json={
                    "chat_id": str(chat_id),
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "disable_notification": silent,
                },
                timeout=_tg._TIMEOUT,
            )
            if resp.status_code == 403:
                # The user blocked the bot — a permanent condition for that chat.
                logger.info("telegram: chat %s has blocked the bot", chat_id)
                return False
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram: send to %s failed: %s", chat_id, exc)
            return False
        return True

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
            logger.debug("telegram: chat action failed: %s", exc)

    def broadcast(self, chat_ids: list[str], text: str) -> int:
        return sum(1 for c in chat_ids if self.send_to(c, text))


bot = Bot(
    token=settings.telegram_bot_token,
    chat_id=settings.telegram_admin_chat_id,
    enabled=settings.notify_enabled,
)
