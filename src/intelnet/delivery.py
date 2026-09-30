"""Delivery — every push goes through a durable outbox.

    fan-out ──enqueue──▶ outbox ──send now──▶ Telegram
                           ▲  └─ failed? pending again, with backoff
                           └── retry_due()  (the alert loop every ~30 s, the watch every 5 min)

A push is written to `outbox` before it is sent, so a Telegram outage, a 5xx or
a 429 during a warning leaves the message queued rather than lost. Rows retry
with exponential backoff (429s wait exactly as long as Telegram asks) until
they land or pass their stale time. A blocked chat or a message Telegram
rejects outright is dropped, not retried.

Row actions: `send` (a plain message), `card` (a message whose id is kept so
later versions can edit it), `edit` (rewrite a chat's card) and `reply` (a
follow-up quoting the card). Any process may deliver; a row is claimed before
each attempt, so the bot, the watch and the alert loop never send it twice.

Pacing keeps inside Telegram's limits: about a second between messages to one
chat and at most ~25 a second overall.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any

from intelnet import db
from intelnet.models import utcnow
from intelnet.telegram import Sent, bot

logger = logging.getLogger(__name__)

PER_CHAT_SECONDS = 1.05       # Telegram: about one message a second to a chat
GLOBAL_SECONDS = 0.04         # …and no more than ~30 a second in all
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 900
MAX_ATTEMPTS = 20

_last_to_chat: dict[str, float] = {}
_last_any = 0.0


def send_now(ids: list[int | None]) -> int:
    """Attempt these outbox rows right away, most urgent first. Returns messages sent.

    Edits don't count: they change a message the chat already has.
    """
    rows = db.pending_outbox(i for i in ids if i)
    return sum(1 for r in rows if _attempt(r) and r["action"] != "edit")


def retry_due(limit: int = 200) -> dict[str, int]:
    """Try again every queued row whose next attempt is due."""
    if not bot.enabled:
        return {"due": 0, "sent": 0}
    rows = db.due_outbox(limit)
    sent = sum(1 for r in rows if _attempt(r))
    return {"due": len(rows), "sent": sent}


def _attempt(row: Any) -> bool:
    if not bot.enabled or not db.claim_outbox(row["id"]):
        return False
    _pace(str(row["chat_id"]))
    result = _dispatch(row)
    attempts = int(row["attempts"]) + 1
    if result.ok:
        db.settle_outbox(row["id"], "sent", message_id=result.message_id)
        db.record_notification(row["key"], row["chat_id"])
        if row["action"] == "card" and row["thread"] and result.message_id:
            db.remember_card(row["thread"], row["chat_id"], result.message_id, row["text"])
        elif row["action"] == "edit" and row["thread"]:
            db.update_card(row["thread"], row["chat_id"], text=row["text"])
        return True
    if result.permanent or attempts >= MAX_ATTEMPTS:
        db.settle_outbox(row["id"], "dropped", error=result.error)
        return False
    wait = result.retry_after or min(RETRY_BASE_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS)
    db.settle_outbox(row["id"], "pending", error=result.error,
                     retry_at=utcnow() + timedelta(seconds=wait))
    logger.info("delivery: %s to %s will retry in %ss (%s)", row["key"], row["chat_id"], wait,
                result.error)
    return False


def _dispatch(row: Any) -> Sent:
    chat, action, text = row["chat_id"], row["action"], row["text"]
    silent = bool(row["silent"])
    markup = json.loads(row["markup_json"]) if row["markup_json"] else None
    if action in ("edit", "reply"):
        card = db.card(row["thread"], chat) if row["thread"] else None
        if action == "edit":
            if card is None:
                return Sent(False, permanent=True, error="no card to edit")
            return bot.edit(chat, card["message_id"], text)
        return bot.deliver(chat, text, silent=silent, reply_to=card["message_id"] if card else None,
                           markup=markup)
    return bot.deliver(chat, text, silent=silent, markup=markup)


def _pace(chat_id: str) -> None:
    """Sleep just enough to stay inside Telegram's per-chat and overall rates."""
    global _last_any
    now = time.monotonic()
    wait = max(_last_to_chat.get(chat_id, 0.0) + PER_CHAT_SECONDS, _last_any + GLOBAL_SECONDS) - now
    if wait > 0:
        time.sleep(wait)
        now = time.monotonic()
    _last_to_chat[chat_id] = _last_any = now
