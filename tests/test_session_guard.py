"""Tests for session guard and fatal Telegram session error handling."""

import asyncio
from unittest.mock import patch, MagicMock
from pathlib import Path

import pytest
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


def test_send_http_emergency_alert_nested_and_invalid_targets(caplog):
    """Verify nested targets are flattened and invalid targets are filtered with warnings."""
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200

        nested_targets = [
            [111111, 222222],
            "-1001234567890",
            None,
            "",
            [],
            0,
            False,
            "   ",
            {"unsupported": "dict"},
        ]

        result = send_http_emergency_alert(
            bot_token="123456:TEST",
            targets=nested_targets,
            message="🚨 Test Alert",
        )

        assert result is True
        # Only valid scalar targets: 111111, 222222, and "-1001234567890"
        assert mock_post.call_count == 3

        sent_chat_ids = [call[1]["json"]["chat_id"] for call in mock_post.call_args_list]
        assert sent_chat_ids == [111111, 222222, "-1001234567890"]

        # Ensure no list or invalid object was passed as chat_id
        for chat_id in sent_chat_ids:
            assert isinstance(chat_id, (int, str))
            assert not isinstance(chat_id, (list, tuple, dict, set, bool))

        # Check that warnings were logged for invalid targets
        assert any("Skipping invalid alert target" in record.message for record in caplog.records)


def test_send_http_emergency_alert_all_invalid():
    """Verify function returns False and does not make HTTP calls when all targets are invalid."""
    with patch("requests.post") as mock_post:
        result = send_http_emergency_alert(
            bot_token="123456:TEST",
            targets=[None, "", [], 0, False],
            message="🚨 Test Alert",
        )
        assert result is False
        assert mock_post.call_count == 0


def test_main_call_site_integration_emergency_alert():
    """Simulate the actual call site in main.py with OWNER as list and ARCHIVE_CHANNEL.

    Verifies each target receives a separate HTTP request with a scalar chat_id.
    """
    mock_owner = [111111, 222222]
    mock_archive = -1003333333333

    # Pattern used in main.py:
    resolved_targets = [t for t in [*mock_owner, mock_archive] if t]

    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200

        result = send_http_emergency_alert(
            bot_token="token_xyz",
            targets=resolved_targets,
            message="🚨 Alert from main",
        )

        assert result is True
        assert mock_post.call_count == 3
        sent_ids = [call[1]["json"]["chat_id"] for call in mock_post.call_args_list]
        assert sent_ids == [111111, 222222, -1003333333333]
        for cid in sent_ids:
            assert isinstance(cid, int)

    # When ARCHIVE_CHANNEL is None:
    resolved_without_archive = [t for t in [*mock_owner, None] if t]
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200

        result = send_http_emergency_alert(
            bot_token="token_xyz",
            targets=resolved_without_archive,
            message="🚨 Alert from main without archive",
        )

        assert result is True
        assert mock_post.call_count == 2
        sent_ids = [call[1]["json"]["chat_id"] for call in mock_post.call_args_list]
        assert sent_ids == [111111, 222222]

    # Even if unflattened list is passed directly to handle_fatal_session_error:
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        exc = pyrogram.errors.AuthKeyDuplicated(value="[406 AUTH_KEY_DUPLICATED]")

        handle_fatal_session_error(
            exc=exc,
            session_name="dummy",
            bot_token="token_xyz",
            alert_targets=[mock_owner, mock_archive],  # Nested list test
            workdir="/tmp",
            exit_process=False,
        )

        assert mock_post.call_count == 3
        sent_ids = [call[1]["json"]["chat_id"] for call in mock_post.call_args_list]
        assert sent_ids == [111111, 222222, -1003333333333]


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
