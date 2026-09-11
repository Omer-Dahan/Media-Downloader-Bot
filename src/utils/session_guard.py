"""Session guard and recovery utilities for handling fatal Telegram session errors."""

import html
import logging
import os
from pathlib import Path
from typing import Optional, Sequence

import requests
import pyrogram.errors

from utils.process_lock import release_process_lock

# Exceptions indicating that the Telegram auth key or session is permanently invalidated
FATAL_SESSION_EXCEPTIONS = (
    pyrogram.errors.AuthKeyDuplicated,
    pyrogram.errors.AuthKeyUnregistered,
    pyrogram.errors.AuthKeyInvalid,
    pyrogram.errors.SessionRevoked,
    pyrogram.errors.SessionPasswordNeeded,
    pyrogram.errors.UserDeactivated,
    pyrogram.errors.UserDeactivatedBan,
    pyrogram.errors.Unauthorized,
)

FATAL_PATTERNS = (
    "AUTH_KEY_DUPLICATED",
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "SESSION_REVOKED",
    "USER_DEACTIVATED",
    "USER_DEACTIVATED_BAN",
)


def is_fatal_session_error(exc: Optional[BaseException]) -> bool:
    """Determine whether an exception is an unrecoverable Telegram session error."""
    if exc is None:
        return False

    if isinstance(exc, FATAL_SESSION_EXCEPTIONS):
        return True

    exc_str = str(exc).upper()
    return any(pattern in exc_str for pattern in FATAL_PATTERNS)


def remove_invalidated_session(
    session_name: str = "main", workdir: str | Path = "."
) -> list[Path]:
    """Delete invalidated session files so Pyrogram can log in fresh on restart."""
    workdir_path = Path(workdir).resolve()
    removed = []

    # Search for session files in workdir, project root, and src
    search_dirs = [workdir_path]
    proj_root = Path(__file__).resolve().parent.parent.parent
    if proj_root not in search_dirs:
        search_dirs.append(proj_root)
    src_dir = proj_root / "src"
    if src_dir not in search_dirs:
        search_dirs.append(src_dir)

    for directory in search_dirs:
        for ext in [".session", ".session-journal"]:
            target = directory / f"{session_name}{ext}"
            if target.exists():
                try:
                    target.unlink()
                    removed.append(target)
                    logging.warning("🗑️ Deleted invalidated session file: %s", target)
                except Exception as e:
                    logging.error("Failed to delete session file %s: %s", target, e)

    return removed


def send_http_emergency_alert(
    bot_token: Optional[str],
    targets: Sequence[int | str | None],
    message: str,
) -> bool:
    """Send an emergency alert via HTTP Telegram Bot API when MTProto is down.

    Since MTProto auth key is invalidated, client.send_message cannot work.
    HTTP Bot API uses HTTPS with BOT_TOKEN directly, bypassing MTProto session.
    """
    if not bot_token:
        logging.warning("No BOT_TOKEN available to send HTTP emergency alert.")
        return False

    success = False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    for target in targets:
        if not target:
            continue
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": target,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=8,
            )
            if resp.status_code == 200:
                success = True
                logging.info("Sent emergency HTTP alert to %s", target)
            else:
                logging.warning(
                    "HTTP alert to %s returned status %d: %s",
                    target,
                    resp.status_code,
                    resp.text,
                )
        except Exception as e:
            logging.error("Failed to send HTTP emergency alert to %s: %s", target, e)

    return success


def handle_fatal_session_error(
    exc: BaseException,
    session_name: str = "main",
    bot_token: Optional[str] = None,
    alert_targets: Optional[Sequence[int | str | None]] = None,
    workdir: str | Path = ".",
    exit_process: bool = True,
) -> None:
    """Handle fatal session invalidation:

    1. Log critical error.
    2. Send out-of-band alert via HTTP Bot API to admin/archive.
    3. Delete invalidated session files so next start can re-auth.
    4. Release process lock.
    5. Exit process with code 1 so systemd restarts it cleanly.
    """
    exc_name = type(exc).__name__
    logging.critical(
        "🚨 FATAL SESSION ERROR: %s (%s). The bot cannot continue with this session.",
        exc_name,
        exc,
    )

    alert_msg = (
        f"🚨 <b>התראת מערכת קריטית: סשן טלגרם נפסל</b>\n\n"
        f"⚠️ שגיאה: <code>{html.escape(f'{exc_name}: {exc}')}</code>\n\n"
        f"קובץ הסשן הפגום (<code>{session_name}.session</code>) נמחק אוטומטית.\n"
        f"הבוט יוצא כעת כדי לאפשר הפעלה מחדש נקייה עם סשן חדש."
    )

    if bot_token and alert_targets:
        send_http_emergency_alert(bot_token, alert_targets, alert_msg)

    # Delete the dead session files
    remove_invalidated_session(session_name=session_name, workdir=workdir)

    # Release process lock so next instance can start immediately
    release_process_lock()

    if exit_process:
        logging.info("Exiting process with code 1 for systemd restart...")
        # Use os._exit to immediately terminate the process without getting blocked in asyncio loops
        os._exit(1)


def setup_asyncio_exception_handler(
    loop,
    session_name: str = "main",
    bot_token: Optional[str] = None,
    alert_targets: Optional[Sequence[int | str | None]] = None,
    workdir: str | Path = ".",
) -> None:
    """Set custom exception handler on asyncio event loop to catch unhandled task exceptions

    (such as Session.restart() raising AuthKeyDuplicated).
    """
    original_handler = loop.get_exception_handler()

    def _handler(loop_obj, context):
        exc = context.get("exception")
        if is_fatal_session_error(exc):
            handle_fatal_session_error(
                exc=exc,
                session_name=session_name,
                bot_token=bot_token,
                alert_targets=alert_targets,
                workdir=workdir,
                exit_process=True,
            )
            return

        if original_handler:
            original_handler(loop_obj, context)
        else:
            loop_obj.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    logging.info("🛡️ Installed fatal session exception handler on asyncio event loop")
