"""Graceful shutdown management for Media-Downloader-Bot.

Handles SIGTERM and SIGINT signals with a strict timeout, terminates child processes
(ffmpeg, yt-dlp, aria2, etc.), releases process locks, and includes an independent
hard backup watchdog to prevent systemd timeout SIGKILL.
"""

import asyncio
import inspect
import logging
import os
import signal
import sys
import threading
from typing import Any, List, Optional

import psutil

from utils.process_lock import release_process_lock

# Default timeout in seconds before forced exit (well below systemd 90s timeout)
DEFAULT_SHUTDOWN_TIMEOUT = 15.0


def terminate_child_processes(
    timeout: float = 2.0, force_kill: bool = False
) -> List[int]:
    """Find and terminate or kill all child processes spawned by this process.

    Returns list of handled child process PIDs.
    """
    try:
        current_proc = psutil.Process()
        children = current_proc.children(recursive=True)
    except Exception as e:
        logging.error("Failed to inspect child processes: %s", e)
        return []

    if not children:
        return []

    handled_pids = []
    for c in children:
        try:
            handled_pids.append(c.pid)
        except Exception:
            pass

    logging.info(
        "Found %d active child process(es): %s", len(handled_pids), handled_pids
    )

    if force_kill:
        for child in children:
            try:
                logging.info(
                    "Force killing child process PID %d (%s)...",
                    child.pid,
                    child.name(),
                )
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(children, timeout=1.0)
        return handled_pids

    for child in children:
        try:
            logging.info(
                "Sending SIGTERM to child process PID %d (%s)...",
                child.pid,
                child.name(),
            )
            child.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    gone, alive = psutil.wait_procs(children, timeout=timeout)
    if alive:
        for child in alive:
            try:
                logging.warning(
                    "Child process PID %d (%s) did not terminate in time, sending SIGKILL...",
                    child.pid,
                    child.name(),
                )
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(alive, timeout=1.0)

    return handled_pids


class ShutdownManager:
    """Coordinates graceful and forced shutdown of the bot."""

    def __init__(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT):
        env_timeout = os.getenv("SHUTDOWN_TIMEOUT")
        if env_timeout:
            try:
                self._timeout = float(env_timeout)
            except ValueError:
                self._timeout = timeout
        else:
            self._timeout = timeout

        self._is_shutting_down = threading.Event()
        self._shutdown_completed = threading.Event()
        self._lock = threading.Lock()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._async_stop_event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Optional[Any] = None
        self._scheduler: Optional[Any] = None
        self._installed = False
        self._original_handlers = {}

    @property
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, val: float):
        self._timeout = float(val)

    def is_shutting_down(self) -> bool:
        """Check if shutdown sequence has been initiated."""
        return self._is_shutting_down.is_set()

    def register(
        self,
        client: Optional[Any] = None,
        scheduler: Optional[Any] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        """Register application components for graceful shutdown."""
        if client is not None:
            self._client = client
        if scheduler is not None:
            self._scheduler = scheduler
        if loop is not None:
            self._loop = loop

    def _watchdog_worker(self, timeout: float):
        """Hard backup watchdog running in an OS thread, independent of asyncio loop."""
        finished = self._shutdown_completed.wait(timeout)
        if finished:
            return

        logging.critical(
            "Graceful shutdown timed out after %.1f seconds. "
            "Asyncio loop or cleanup is unresponsive. Forcing immediate exit via os._exit(0).",
            timeout,
        )

        # 1. Kill any remaining child processes immediately
        terminate_child_processes(force_kill=True)

        # 2. Crucial: Release process lock explicitly before os._exit because os._exit bypasses atexit!
        try:
            release_process_lock()
            logging.info("Process lock explicitly released during watchdog forced exit")
        except Exception as e:
            logging.error(
                "Failed to release process lock during watchdog forced exit: %s", e
            )

        # 3. Flush output
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            logging.shutdown()
        except Exception:
            pass

        # 4. Immediate exit with code 0 so systemd considers it successful stop
        os._exit(0)

    def start_watchdog(self, timeout: Optional[float] = None):
        """Start the hard backup watchdog thread."""
        with self._lock:
            if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
                t = timeout if timeout is not None else self._timeout
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog_worker,
                    args=(t,),
                    name="ShutdownWatchdog",
                    daemon=True,
                )
                self._watchdog_thread.start()
                logging.info("Shutdown watchdog thread started (timeout: %.1fs)", t)

    def trigger_shutdown(self, signum: Optional[int] = None):
        """Initiate graceful shutdown actions."""
        already_shutting_down = self._is_shutting_down.is_set()
        self._is_shutting_down.set()

        if already_shutting_down:
            logging.warning("Shutdown already in progress, skipping duplicate trigger")
            return

        # Start watchdog immediately
        self.start_watchdog()

        # Terminate child processes to unblock worker threads
        terminate_child_processes(timeout=2.0)

        # Stop scheduler if running
        if self._scheduler is not None:
            try:
                if getattr(self._scheduler, "running", False):
                    self._scheduler.shutdown(wait=False)
                    logging.info("Scheduler shutdown initiated")
            except Exception as e:
                logging.warning("Error stopping scheduler: %s", e)

        # Wake up idle loop
        self._wake_idle_loop()

    def _wake_idle_loop(self):
        """Signal the idle loop to exit."""
        if self._async_stop_event is not None:
            try:
                loop = self._loop or getattr(self._async_stop_event, "_loop", None)
                if loop and loop.is_running():
                    loop.call_soon_threadsafe(self._async_stop_event.set)
                else:
                    self._async_stop_event.set()
            except Exception as e:
                logging.warning("Failed to signal async stop event: %s", e)

    def _signal_handler(self, signum: int, frame: Any):
        """Signal handler for SIGTERM and SIGINT."""
        sig_name = (
            signal.Signals(signum).name
            if signum in signal.Signals.__members__.values()
            else str(signum)
        )
        logging.info(
            "Stop signal received (%s). Initiating graceful shutdown...",
            sig_name,
        )
        self.trigger_shutdown(signum=signum)

    def install_signal_handlers(self) -> None:
        """Install signal handlers for SIGTERM and SIGINT, and patch Kurigram idle."""
        with self._lock:
            if self._installed:
                return

            installed_sigs = []
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    self._original_handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, self._signal_handler)
                    installed_sigs.append(sig)
                except (ValueError, OSError) as e:
                    logging.warning("Could not register signal %s: %s", sig, e)

            self._installed = True

            if installed_sigs:
                sig_names = [
                    signal.Signals(s).name
                    if s in signal.Signals.__members__.values()
                    else str(s)
                    for s in installed_sigs
                ]
                logging.info(
                    "Graceful shutdown signal handlers installed for: %s",
                    ", ".join(sig_names),
                )
            else:
                logging.warning("No graceful shutdown signal handlers could be installed")

            idle_patched = patch_kurigram_idle()
            if idle_patched:
                logging.info("Kurigram idle coordination installed successfully")
            else:
                logging.warning(
                    "Kurigram idle coordination was not installed; graceful stop via idle will not function"
                )

    def restore_signal_handlers(self):
        """Restore original signal handlers (useful for tests)."""
        with self._lock:
            for sig, handler in self._original_handlers.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass
            self._original_handlers.clear()
            self._installed = False

    async def wait_for_stop(self):
        """Wait until shutdown is initiated (used by custom_idle)."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        if self._async_stop_event is None:
            self._async_stop_event = asyncio.Event()

        if self._is_shutting_down.is_set():
            return

        try:
            await self._async_stop_event.wait()
        except asyncio.CancelledError:
            pass

    def shutdown_completed(self):
        """Mark shutdown as completed to disarm watchdog and release resources."""
        self._shutdown_completed.set()
        logging.info("Graceful shutdown completed successfully")

    def reset_state(self):
        """Reset state (primarily for testing)."""
        with self._lock:
            self._is_shutting_down.clear()
            self._shutdown_completed.clear()
            self._watchdog_thread = None
            self._async_stop_event = None
            self._loop = None
            self._client = None
            self._scheduler = None


shutdown_manager = ShutdownManager()


def is_shutting_down() -> bool:
    """Global check if shutdown is in progress."""
    return shutdown_manager.is_shutting_down()


async def custom_idle():
    """Kurigram idle replacement coordinating with shutdown_manager."""
    await shutdown_manager.wait_for_stop()


def patch_kurigram_idle() -> bool:
    """Patch Kurigram idle references to coordinate with shutdown_manager.

    Returns True if patching succeeded, False otherwise.
    """
    try:
        import pyrogram
        import pyrogram.methods.utilities.idle as pyrogram_idle
        import pyrogram.methods.utilities.run as pyrogram_run

        pyrogram.idle = custom_idle
        pyrogram_idle.idle = custom_idle
        pyrogram_run.idle = custom_idle
        return True
    except Exception as e:
        logging.warning("Failed to patch Kurigram idle coordination: %s", e)
        return False
