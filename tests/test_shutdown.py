"""Tests for graceful shutdown, signal handling, child process cleanup, and lock release."""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils.process_lock import acquire_process_lock, release_process_lock
from utils.shutdown import (
    ShutdownManager,
    custom_idle,
    is_shutting_down,
    patch_kurigram_idle,
    shutdown_manager,
    terminate_child_processes,
)


@pytest.fixture(autouse=True)
def cleanup_shutdown_state(tmp_path):
    """Ensure clean shutdown_manager state and lock release before and after each test."""
    shutdown_manager.reset_state()
    release_process_lock()
    yield
    shutdown_manager.reset_state()
    shutdown_manager.restore_signal_handlers()
    release_process_lock()


def test_sigterm_triggers_graceful_shutdown(caplog):
    """Verify that receiving SIGTERM initiates the graceful shutdown flow and logs."""
    caplog.set_level(logging.INFO)
    mock_scheduler = MagicMock()
    mock_scheduler.running = True
    mock_client = MagicMock()
    mock_client.is_initialized = True

    shutdown_manager.register(client=mock_client, scheduler=mock_scheduler)

    assert not shutdown_manager.is_shutting_down()

    # Simulate SIGTERM signal handler execution
    shutdown_manager._signal_handler(signal.SIGTERM, None)

    # 1. Flag is set
    assert shutdown_manager.is_shutting_down()
    assert is_shutting_down()

    # 2. Log message emitted documenting receipt of signal
    assert any("Stop signal received (SIGTERM)" in r.message for r in caplog.records)

    # 3. Scheduler was stopped
    mock_scheduler.shutdown.assert_called_once_with(wait=False)

    # Clean up watchdog
    shutdown_manager.shutdown_completed()


def test_sigint_triggers_graceful_shutdown(caplog):
    """Verify that SIGINT also triggers graceful shutdown properly."""
    caplog.set_level(logging.INFO)
    mock_scheduler = MagicMock()
    mock_scheduler.running = True

    shutdown_manager.register(scheduler=mock_scheduler)
    shutdown_manager._signal_handler(signal.SIGINT, None)

    assert shutdown_manager.is_shutting_down()
    assert any("Stop signal received (SIGINT)" in r.message for r in caplog.records)
    mock_scheduler.shutdown.assert_called_once_with(wait=False)

    shutdown_manager.shutdown_completed()


def test_duplicate_signal_does_not_trigger_duplicate_shutdown(caplog):
    """Verify that receiving multiple signals does not restart or duplicate shutdown."""
    caplog.set_level(logging.INFO)
    mock_scheduler = MagicMock()
    mock_scheduler.running = True

    shutdown_manager.register(scheduler=mock_scheduler)
    shutdown_manager._signal_handler(signal.SIGTERM, None)
    assert mock_scheduler.shutdown.call_count == 1

    # Second signal arrives
    shutdown_manager._signal_handler(signal.SIGTERM, None)
    # Scheduler shutdown is still called only once
    assert mock_scheduler.shutdown.call_count == 1

    shutdown_manager.shutdown_completed()


def test_child_processes_identified_and_terminated():
    """Verify child processes created by this process are found and terminated."""
    # Spawn a child subprocess simulating ffmpeg or yt-dlp
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    assert child.poll() is None

    pids = terminate_child_processes(timeout=1.0)

    assert child.pid in pids
    # Process should be terminated
    child.wait(timeout=2.0)
    assert child.poll() is not None


def test_stubborn_child_process_killed_with_sigkill():
    """Verify that a child process ignoring SIGTERM is forcefully killed with SIGKILL."""
    # Process ignoring SIGTERM
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    child = subprocess.Popen([sys.executable, "-c", script])
    assert child.poll() is None

    pids = terminate_child_processes(timeout=0.5)

    assert child.pid in pids
    child.wait(timeout=3.0)
    assert child.poll() is not None


def test_watchdog_forces_exit_on_timeout(tmp_path, caplog):
    """Verify hard backup watchdog forces immediate exit and logs when shutdown hangs."""
    caplog.set_level(logging.INFO)
    lock_file = tmp_path / ".watchdog_test.lock"
    assert acquire_process_lock(lock_file) is True
    assert lock_file.exists()

    mgr = ShutdownManager(timeout=0.1)

    with patch("os._exit") as mock_exit:
        # Trigger watchdog
        mgr.start_watchdog(timeout=0.1)
        # Give watchdog thread time to fire
        time.sleep(0.3)

        # 1. os._exit must be called with code 0
        mock_exit.assert_called_once_with(0)

        # 2. Critical log was emitted explaining the timeout
        assert any("Graceful shutdown timed out" in r.message for r in caplog.records)

        # 3. Process lock must have been released before os._exit
        assert not lock_file.exists()


def test_process_lock_released_on_normal_shutdown(tmp_path):
    """Verify that process lock is cleanly released during shutdown completion."""
    lock_file = tmp_path / ".bot_clean.lock"
    assert acquire_process_lock(lock_file) is True
    assert lock_file.exists()

    # Mark shutdown completed and release lock
    shutdown_manager.shutdown_completed()
    release_process_lock()

    assert not lock_file.exists()

    # Verify another instance can acquire immediately without lock leak
    assert acquire_process_lock(lock_file) is True
    release_process_lock()


def test_is_shutting_down_aborts_active_downloader():
    """Verify that BaseDownloader aborts with ValueError when system is shutting down."""
    from engine.base import BaseDownloader

    class DummyDownloader(BaseDownloader):
        def _download(self):
            pass

        def _setup_formats(self):
            pass

        def _start(self):
            pass

    mock_client = MagicMock()
    mock_msg = MagicMock()
    mock_msg.chat.id = 12345
    mock_msg.chat.type = MagicMock()
    mock_msg.id = 999

    downloader = DummyDownloader(mock_client, mock_msg, "https://example.com")

    # Before shutdown, check_for_cancel should pass
    downloader.check_for_cancel()

    # Initiate shutdown
    shutdown_manager.trigger_shutdown()
    assert is_shutting_down()

    # Now check_for_cancel must raise ValueError indicating shutdown
    with pytest.raises(ValueError, match="עקב כיבוי"):
        downloader.check_for_cancel()

    shutdown_manager.shutdown_completed()


def test_kurigram_idle_patched_and_coordinates():
    """Verify patch_kurigram_idle replaces idle in all locations and coordinates with manager."""
    import pyrogram
    import pyrogram.methods.utilities.idle as pyrogram_idle
    import pyrogram.methods.utilities.run as pyrogram_run

    result = patch_kurigram_idle()
    assert result is True

    assert pyrogram.idle is custom_idle
    assert pyrogram_idle.idle is custom_idle
    assert pyrogram_run.idle is custom_idle


def test_patch_kurigram_idle_failure_returns_false_and_logs(caplog):
    """Verify patch_kurigram_idle returns False and logs a warning on error."""
    caplog.set_level(logging.WARNING)
    with patch.dict(sys.modules, {"pyrogram": None}):
        result = patch_kurigram_idle()
        assert result is False
        assert any("Failed to patch Kurigram idle" in r.message for r in caplog.records)


def test_install_signal_handlers_logs_signals_and_idle_separately(caplog):
    """Verify install_signal_handlers logs signal handlers and idle patch status separately."""
    caplog.set_level(logging.INFO)
    mgr = ShutdownManager()

    # Case 1: Normal installation where idle patch succeeds
    mgr.install_signal_handlers()
    assert any("Graceful shutdown signal handlers installed for" in r.message for r in caplog.records)
    assert any("Kurigram idle coordination installed successfully" in r.message for r in caplog.records)
    mgr.restore_signal_handlers()

    # Case 2: Idle patch fails, must log clear warning
    caplog.clear()
    mgr2 = ShutdownManager()
    with patch("utils.shutdown.patch_kurigram_idle", return_value=False):
        mgr2.install_signal_handlers()
        assert any("Graceful shutdown signal handlers installed for" in r.message for r in caplog.records)
        assert any("Kurigram idle coordination was not installed" in r.message for r in caplog.records)
        assert any(
            r.levelno == logging.WARNING and "Kurigram idle coordination was not installed" in r.message
            for r in caplog.records
        )
        mgr2.restore_signal_handlers()


def test_custom_idle_terminates_on_trigger_shutdown():
    """Verify that custom_idle cleanly awaits and wakes up when shutdown is triggered."""
    patch_kurigram_idle()

    async def run_scenario():
        idle_task = asyncio.create_task(custom_idle())
        await asyncio.sleep(0.05)
        assert not idle_task.done()

        # Trigger shutdown
        shutdown_manager.trigger_shutdown()

        # Idle task should complete promptly
        await asyncio.wait_for(idle_task, timeout=1.0)
        assert idle_task.done()

    asyncio.run(run_scenario())
    shutdown_manager.shutdown_completed()


def test_watchdog_does_not_fire_if_shutdown_completed_in_time():
    """Verify that completing shutdown in time disarms the watchdog."""
    mgr = ShutdownManager(timeout=0.2)

    with patch("os._exit") as mock_exit:
        mgr.start_watchdog(timeout=0.2)
        # Immediately mark shutdown completed
        mgr.shutdown_completed()
        # Wait longer than timeout
        time.sleep(0.3)
        mock_exit.assert_not_called()


def test_watchdog_cleans_up_children_and_lock_on_forced_exit(tmp_path):
    """Verify that forced exit kills remaining children and releases lock."""
    lock_file = tmp_path / ".forced_exit.lock"
    assert acquire_process_lock(lock_file) is True
    assert lock_file.exists()

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    assert child.poll() is None

    mgr = ShutdownManager(timeout=0.1)

    with patch("os._exit") as mock_exit:
        mgr.start_watchdog(timeout=0.1)
        time.sleep(0.3)

        mock_exit.assert_called_once_with(0)
        assert not lock_file.exists()
        child.wait(timeout=2.0)
        assert child.poll() is not None


def test_signal_installation_and_restoration():
    """Verify that signals are registered and can be restored cleanly."""
    mgr = ShutdownManager()
    mgr.install_signal_handlers()

    assert signal.getsignal(signal.SIGTERM) == mgr._signal_handler
    assert signal.getsignal(signal.SIGINT) == mgr._signal_handler

    mgr.restore_signal_handlers()
    assert signal.getsignal(signal.SIGTERM) != mgr._signal_handler
