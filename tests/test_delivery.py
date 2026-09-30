"""Delivery: every push is queued first, retried until it lands, and never sent twice."""
from __future__ import annotations

from datetime import timedelta

from intelnet import db, delivery, subscriptions, telegram
from intelnet.models import iso, parse_iso, utcnow


def _row(key: str, chat: str):
    with db.get_conn() as conn:
        return conn.execute("SELECT * FROM outbox WHERE key = ? AND chat_id = ?", (key, chat)).fetchone()


def _make_due(outbox_id: int) -> None:
    """As if the row's backoff had run out."""
    with db.get_conn() as conn:
        conn.execute("UPDATE outbox SET next_attempt_at = ? WHERE id = ?",
                     (iso(utcnow() - timedelta(seconds=1)), outbox_id))


def test_a_failed_push_stays_queued_and_lands_on_retry(fresh_db, sent, monkeypatch):
    db.add_subscription("5", "weather.digest", "il")
    answers = iter([telegram.Sent(False, error="HTTP 502"), telegram.Sent(True, message_id=77)])
    monkeypatch.setattr(telegram.bot, "deliver", lambda *a, **k: next(answers))
    assert subscriptions.push("weather.digest", ["il"], "digest:2026-09-29", "brief") == 0
    row = _row("digest:2026-09-29", "5")
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, "HTTP 502")
    assert parse_iso(row["next_attempt_at"]) > utcnow()                 # backing off
    assert delivery.retry_due() == {"due": 0, "sent": 0}                  # not due yet
    _make_due(row["id"])
    assert delivery.retry_due() == {"due": 1, "sent": 1}
    row = _row("digest:2026-09-29", "5")
    assert (row["status"], row["message_id"]) == ("sent", 77)
    assert db.already_notified("digest:2026-09-29", "5")
    assert subscriptions.push("weather.digest", ["il"], "digest:2026-09-29", "brief") == 0


def test_429_waits_as_asked_and_a_blocked_chat_is_dropped(fresh_db, sent, monkeypatch):
    db.add_subscription("6", "weather.digest", "il")
    db.add_subscription("7", "weather.digest", "il")
    answers = {"6": telegram.Sent(False, retry_after=17, error="Too Many Requests"),
               "7": telegram.Sent(False, permanent=True, error="Forbidden: bot was blocked by the user")}
    monkeypatch.setattr(telegram.bot, "deliver", lambda chat, *a, **k: answers[str(chat)])
    subscriptions.push("weather.digest", ["il"], "digest:2026-09-30", "brief")
    wait = parse_iso(_row("digest:2026-09-30", "6")["next_attempt_at"]) - utcnow()
    assert timedelta(seconds=14) < wait <= timedelta(seconds=17)
    assert _row("digest:2026-09-30", "7")["status"] == "dropped"
    assert delivery.retry_due()["due"] == 0


def test_a_row_is_claimed_once_so_two_senders_cannot_double_send(fresh_db):
    oid = db.enqueue("k", "1", "hi")
    assert db.claim_outbox(oid) and not db.claim_outbox(oid)
    db.settle_outbox(oid, "sent", message_id=5)
    assert not db.claim_outbox(oid)
    assert db.enqueue("k", "1", "hi again") is None                       # one row per (key, chat)


def test_rows_past_their_stale_time_are_retired(fresh_db):
    db.enqueue("old", "1", "hi", stale_at=utcnow() - timedelta(minutes=1))
    assert db.due_outbox() == [] and _row("old", "1")["status"] == "stale"


class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code, self._body = status, body

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> dict:
        return self._body


def test_telegram_answers_are_sorted_into_retry_or_give_up(monkeypatch):
    b = telegram.Bot(token="1:t", chat_id="1", enabled=True)
    replies = iter([
        _Resp(200, {"ok": True, "result": {"message_id": 9}}),
        _Resp(429, {"ok": False, "description": "Too Many Requests: retry after 12",
                    "parameters": {"retry_after": 12}}),
        _Resp(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}),
        _Resp(400, {"ok": False, "description": "Bad Request: message is not modified"}),
        _Resp(502, {}),
    ])

    class _Requests:
        @staticmethod
        def post(url, **kw):
            return next(replies)

    monkeypatch.setattr(telegram, "requests", _Requests())
    assert b.deliver("1", "hi").message_id == 9
    r = b.deliver("1", "hi")
    assert not r.ok and r.retry_after == 12 and not r.permanent
    r = b.deliver("1", "hi")
    assert not r.ok and r.permanent
    r = b.edit("1", 9, "same text")
    assert r.ok and r.message_id == 9                                       # unchanged is fine
    r = b.deliver("1", "hi")
    assert not r.ok and not r.permanent and r.error == "HTTP 502"


def test_a_failed_request_never_logs_the_token(monkeypatch, caplog):
    b = telegram.Bot(token="123:SECRET", chat_id="1", enabled=True)

    class _Requests:
        @staticmethod
        def post(url, **kw):
            raise ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(telegram, "requests", _Requests())
    assert not b.deliver("1", "hi").ok
    assert "SECRET" not in caplog.text


def test_markup_telegram_refuses_is_resent_with_plain_times(monkeypatch):
    b = telegram.Bot(token="1:t", chat_id="1", enabled=True)
    posted: list[str] = []

    class _Requests:
        @staticmethod
        def post(url, json=None, **kw):
            posted.append(json["text"])
            if "<tg-time" in json["text"]:
                return _Resp(400, {"ok": False, "description":
                                   'Bad Request: can\'t parse entities: Unsupported start tag "tg-time"'})
            return _Resp(200, {"ok": True, "result": {"message_id": 3}})

    monkeypatch.setattr(telegram, "requests", _Requests())
    when = utcnow()
    r = b.deliver("1", f"Until {telegram.tg_time(when)} · Severe")
    assert r.ok and r.message_id == 3 and len(posted) == 2
    assert "<tg-time" not in posted[1] and posted[1].startswith("Until ") and ("CDT" in posted[1] or "CST" in posted[1])


def test_a_failed_long_poll_never_logs_the_token(monkeypatch, caplog):
    b = telegram.Bot(token="123:SECRET", chat_id="1", enabled=True)

    class _Requests:
        @staticmethod
        def get(url, **kw):
            raise ConnectionError(f"Read timed out. url: {url}")

    monkeypatch.setattr(telegram, "requests", _Requests())
    assert b.get_updates(offset=5) is None
    assert "SECRET" not in caplog.text and "getUpdates failed" in caplog.text
