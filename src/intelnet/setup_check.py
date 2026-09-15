"""`intelnet setup` — the turn-on checklist, checked for real.

Each check says what's missing and the exact next step, so going live is a
short list rather than a treasure hunt: env file → Telegram bot (getMe) →
admin chat → Google Drive client + token + folder → launchd jobs → LLMs.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from intelnet import db
from intelnet.config import PROJECT_ROOT, settings

LAUNCHD_LABELS = ("com.dr.intelnet.bot", "com.dr.intelnet.watch", "com.dr.intelnet.daily",
                  "com.dr.intelnet.notify")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    optional: bool = False


def telegram_get_me() -> dict[str, Any] | None:
    if not settings.telegram_bot_token:
        return None
    try:
        import requests

        r = requests.get(f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe", timeout=10)
        data = r.json()
        return data.get("result") if data.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


def launchd_state() -> dict[str, str]:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        return {}
    state = {}
    for label in LAUNCHD_LABELS:
        line = next((ln for ln in out.splitlines() if ln.endswith(label)), None)
        if line:
            pid, status, _ = line.split(maxsplit=2)
            state[label] = "running" if pid != "-" else f"loaded (last exit {status})"
    return state


def run_checks(*, online: bool = True) -> list[Check]:
    checks: list[Check] = []
    env = PROJECT_ROOT / ".env"
    checks.append(Check(".env", env.exists(), str(env) if env.exists() else "missing",
                        "cp .env.example .env and fill in the Telegram + Drive lines"))
    try:
        db.init_db()
        checks.append(Check("database", True, str(settings.db_path)))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("database", False, str(exc), "check DB_PATH is writable"))

    if settings.telegram_bot_token:
        me = telegram_get_me() if online else {"username": settings.telegram_bot_handle or "?"}
        if me:
            handle = me.get("username") or ""
            detail = f"@{handle} ({me.get('first_name', '')})"
            if not settings.telegram_bot_handle:
                detail += f" — add TELEGRAM_BOT_HANDLE={handle} to .env so the site can link it"
            checks.append(Check("telegram bot", True, detail))
        else:
            checks.append(Check("telegram bot", False, "token set but getMe failed",
                                "re-check TELEGRAM_BOT_TOKEN (from @BotFather); no 'bot' prefix"))
    else:
        checks.append(Check("telegram bot", False, "TELEGRAM_BOT_TOKEN empty",
                            "create a NEW bot with @BotFather → paste the token into .env"))
    checks.append(Check("admin chat", bool(settings.telegram_admin_chat_id),
                        settings.telegram_admin_chat_id or "TELEGRAM_ADMIN_CHAT_ID empty",
                        "message @userinfobot for your chat id → TELEGRAM_ADMIN_CHAT_ID"))

    if settings.gdrive_enabled:
        creds = Path(settings.gdrive_credentials_path)
        token = Path(settings.gdrive_token_path)
        checks.append(Check("drive: OAuth client", creds.exists(), str(creds),
                            "Google Cloud Console → OAuth client (Desktop) → secrets/gdrive_credentials.json"))
        checks.append(Check("drive: token", token.exists(), str(token) if token.exists() else "not authorized yet",
                            "run: uv run intelnet drive init  (browser consent once)"))
        if token.exists() and online:
            from intelnet.gdrive import DriveNotConfigured, DrivePublisher

            expected = settings.gdrive_account.strip()
            try:
                acct = DrivePublisher().account()
                checks.append(Check("drive: account", True, (acct or "unknown")
                                    + ("" if expected else " — set GDRIVE_ACCOUNT to pin it")))
            except DriveNotConfigured as exc:
                checks.append(Check("drive: account", False, str(exc),
                                    "uv run intelnet drive init --remote  (pick the service account)"))
        from intelnet.gdrive import KV_FOLDER, KV_LATEST

        folder = db.kv_get(KV_FOLDER)
        checks.append(Check("drive: folder + Latest doc", bool(folder and db.kv_get(KV_LATEST)),
                            f"folder {folder}" if folder else "not created",
                            "run: uv run intelnet drive init"))
    else:
        checks.append(Check("drive", True, "disabled (GDRIVE_ENABLED=false)", optional=True))

    state = launchd_state()
    for label in LAUNCHD_LABELS:
        checks.append(Check(f"launchd {label.rsplit('.', 1)[-1]}", label in state, state.get(label, "not loaded"),
                            "bash scripts/install_launchd.sh"))

    if settings.llm_enabled and online:
        from intelnet import llm

        for name, status in llm.probe().items():
            checks.append(Check(f"llm {name}", status.startswith("ok"), status,
                                "start the shared server (macro digest's launchd jobs)", optional=True))
    return checks


def summary(checks: list[Check]) -> tuple[int, int]:
    required = [c for c in checks if not c.optional]
    return sum(1 for c in required if c.ok), len(required)
