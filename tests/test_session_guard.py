"""Tests for session guard and fatal Telegram session error handling."""

import asyncio
from unittest.mock import patch, MagicMock
from pathlib import Path

import pyrogram.errors
from utils.session_guard import (
    is_fatal_session_error,
    remove_invalidated_session,
    send_http_emergency_alert,
    handle_fatal_session_error,
    setup_asyncio_exception_handler,
)
from utils.process_lock import acquire_process_lock, release_process_lock


def test_is_fatal_session_error():
    """Verify fatal session errors are correctly classified."""
    # Direct fatal pyrogram exceptions
    assert is_fatal_session_error(pyrogram.errors.AuthKeyDuplicated(value="[406 AUTH_KEY_DUPLICATED]"))
    assert is_fatal_session_error(pyrogram.errors.AuthKeyUnregistered(value="[401 AUTH_KEY_UNREGISTERED]"))
    assert is_fatal_session_error(pyrogram.errors.AuthKeyInvalid(value="[401 AUTH_KEY_INVALID]"))
    assert is_fatal_session_error(pyrogram.errors.SessionRevoked(value="[401 SESSION_REVOKED]"))
    assert is_fatal_session_error(pyrogram.errors.UserDeactivated(value="[403 USER_DEACTIVATED]"))

    # Generic exceptions containing fatal markers in string
    assert is_fatal_session_error(RuntimeError("Telegram says: [406 AUTH_KEY_DUPLICATED]"))
    assert is_fatal_session_error(Exception("Server returned AUTH_KEY_UNREGISTERED"))

    # Non-fatal exceptions
    assert not is_fatal_session_error(None)
    assert not is_fatal_session_error(ConnectionResetError("Connection lost"))
    assert not is_fatal_session_error(TimeoutError("Operation timed out"))
    assert not is_fatal_session_error(ValueError("Invalid argument"))
    assert not is_fatal_session_error(KeyError("missing_key"))


def test_remove_invalidated_session(tmp_path):
    """Verify session and session-journal files are deleted upon invalidation."""
    session_file = tmp_path / "testbot.session"
    journal_file = tmp_path / "testbot.session-journal"
    unrelated_file = tmp_path / "other.txt"

    session_file.write_text("dummy session binary data")
    journal_file.write_text("dummy journal data")
    unrelated_file.write_text("keep this")

    assert session_file.exists()
    assert journal_file.exists()

    removed = remove_invalidated_session(session_name="testbot", workdir=tmp_path)

    assert not session_file.exists()
    assert not journal_file.exists()
    assert unrelated_file.exists()
    assert any(p.name == "testbot.session" for p in removed)


def test_send_http_emergency_alert():
    """Verify HTTP emergency alert sends POST to Telegram Bot API."""
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200

        result = send_http_emergency_alert(
            bot_token="123456:ABC-DEF",
            targets=[987654321, "-100123456789"],
            message="🚨 Emergency Alert Test",
        )

        assert result is True
        assert mock_post.call_count == 2
        call_url = mock_post.call_args_list[0][0][0]
        assert "api.telegram.org/bot123456:ABC-DEF/sendMessage" in call_url
        call_json = mock_post.call_args_list[0][1]["json"]
        assert call_json["chat_id"] == 987654321
        assert "Emergency Alert Test" in call_json["text"]


def test_handle_fatal_session_error(tmp_path):
    """Verify full recovery flow: logs, sends alert, deletes session, releases lock."""
    # Setup lock and dummy session
    lock_file = tmp_path / ".bot.lock"
    acquire_process_lock(lock_file)

    session_file = tmp_path / "main.session"
    session_file.write_text("session data")

    exc = pyrogram.errors.AuthKeyDuplicated(value="[406 AUTH_KEY_DUPLICATED]")

    with patch("utils.session_guard.send_http_emergency_alert") as mock_alert:
        handle_fatal_session_error(
            exc=exc,
            session_name="main",
            bot_token="fake_token",
            alert_targets=[12345],
            workdir=tmp_path,
            exit_process=False,  # Don't call os._exit in unit test
        )

        # 1. Alert was sent
        mock_alert.assert_called_once()

        # 2. Session file was removed
        assert not session_file.exists()

        # 3. Process lock was released (so another process can acquire it)
        assert acquire_process_lock(lock_file) is True
        release_process_lock()


def test_asyncio_exception_handler_intercepts_task_exception():
    """Verify that unhandled AuthKeyDuplicated in background task (like Session.restart())

    is caught by the custom exception handler instead of becoming an unretrieved task exception.
    """
    handled_errors = []

    async def scenario():
        loop = asyncio.get_running_loop()

        with patch("utils.session_guard.handle_fatal_session_error") as mock_handle:
            mock_handle.side_effect = lambda exc, **kw: handled_errors.append(exc)

            setup_asyncio_exception_handler(
                loop,
                session_name="main",
                bot_token="dummy_token",
                alert_targets=[111],
            )

            # Spawn a task that fails with AuthKeyDuplicated (reproducing Session.restart())
            async def broken_background_task():
                raise pyrogram.errors.AuthKeyDuplicated(value="[406 AUTH_KEY_DUPLICATED]")

            task = loop.create_task(broken_background_task())
            # Give the task time to execute and fail
            await asyncio.sleep(0.05)
            # In Python asyncio, Task.__del__ triggers loop.call_exception_handler
            del task
            import gc
            gc.collect()

            # Assert that the custom handler was triggered
            assert len(handled_errors) == 1
            assert isinstance(handled_errors[0], pyrogram.errors.AuthKeyDuplicated)

    asyncio.run(scenario())
