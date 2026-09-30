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
    result: Any = None                 # the method's own result (a ForumTopic, getMe's User…)


class Bot(TelegramNotifier):
    """The network's Telegram client: admin chat = the notifier's default chat."""

    def deliver(self, chat_id: str | int, text: str, *, silent: bool = False,
                reply_to: int | None = None, markup: dict[str, Any] | None = None,
                rich: str | None = None, thread_id: int | None = None) -> Sent:
        """POST one message to any chat; `reply_to` quotes an earlier message, `markup`
        attaches a keyboard, `thread_id` puts it in a chat section (topic).

        With `rich` (Bot API 10.1 rich-message HTML) the message goes out as a rich
        message; if Telegram refuses it (a 400), the same message goes out as the plain
        HTML `text` instead, at once, so a formatting problem never costs a warning."""
        common: dict[str, Any] = {"chat_id": str(chat_id), "disable_notification": silent}
        if reply_to:
            common["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if markup:
            common["reply_markup"] = markup
        if thread_id:
            common["message_thread_id"] = thread_id
        if rich and settings.telegram_rich_messages:
            sent = self._call("sendRichMessage", {**common, "rich_message": {"html": rich, "skip_entity_detection": True}})
            if sent.ok or not sent.permanent or "chat not found" in sent.error or "blocked" in sent.error:
                return sent
            logger.warning("telegram: rich message refused (%s); sending the plain version", sent.error)
        return self._call("sendMessage", {**common, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})

    def edit(self, chat_id: str | int, message_id: int, text: str, *,
             markup: dict[str, Any] | None = None, rich: str | None = None) -> Sent:
        """Rewrite a message the bot sent earlier (editing never makes a sound). A rich
        card is edited as a rich message, falling back to the plain `text`. Buttons not
        passed in `markup` are removed, as Telegram does."""
        common: dict[str, Any] = {"chat_id": str(chat_id), "message_id": message_id}
        if markup:
            common["reply_markup"] = markup
        if rich and settings.telegram_rich_messages:
            sent = self._call("editMessageText", {**common, "rich_message": {"html": rich, "skip_entity_detection": True}})
            if sent.ok or not sent.permanent:
                return sent
            logger.warning("telegram: rich edit refused (%s); editing with the plain version", sent.error)
        return self._call("editMessageText", {**common, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})

    def edit_markup(self, chat_id: str | int, message_id: int, markup: dict[str, Any] | None) -> Sent:
        """Change only a message's buttons (the Mute button turning into Unmute)."""
        return self._call("editMessageReplyMarkup", {"chat_id": str(chat_id), "message_id": message_id,
                                                     "reply_markup": markup or {"inline_keyboard": []}})

    def create_topic(self, chat_id: str | int, name: str, icon_color: int | None = None) -> int | None:
        """A chat section (Bot API 9.4 topics in private chats). Returns its thread id."""
        payload: dict[str, Any] = {"chat_id": str(chat_id), "name": name[:128]}
        if icon_color:
            payload["icon_color"] = icon_color
        sent = self._call("createForumTopic", payload)
        return int(sent.result["message_thread_id"]) if sent.ok and isinstance(sent.result, dict) \
            and sent.result.get("message_thread_id") else None

    def get_me(self) -> dict[str, Any] | None:
        sent = self._call("getMe", {})
        return sent.result if sent.ok and isinstance(sent.result, dict) else None

    def answer_callback(self, query_id: str, text: str = "") -> Sent:
        """Stop a tapped button's spinner, with an optional one-line toast."""
        return self._call("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:190]})

    def send_to(self, chat_id: str | int, text: str, *, silent: bool = False,
                markup: dict[str, Any] | None = None, thread_id: int | None = None) -> bool:
        """POST one HTML message to any chat. False on no-op or failure."""
        return self.deliver(chat_id, text, silent=silent, markup=markup, thread_id=thread_id).ok

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
            return Sent(True, message_id=result.get("message_id") if isinstance(result, dict) else None,
                        result=result)
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

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict] | None:
        """Long-poll for updates: a list (maybe empty), or None when the request failed.

        digest-core's contract, but failures are logged without the URL (it carries
        the token), so a Telegram outage doesn't write the token into logs/.
        """
        if not self.enabled:
            return None
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = requests.get(self._url("getUpdates"), params=params, timeout=timeout + 10)
            body = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram: getUpdates failed: %s", type(exc).__name__)
            return None
        if resp.status_code != 200 or not body.get("ok"):
            logger.warning("telegram: getUpdates failed: HTTP %s", resp.status_code)
            return None
        return body.get("result", [])

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
