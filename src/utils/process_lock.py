"""Process lock utility to prevent duplicate bot instances using advisory file locking."""

import atexit
import logging
import os
import sys
from pathlib import Path
from typing import Optional

_lock_fd = None
_lock_file_path: Optional[Path] = None


def acquire_process_lock(lock_path: Optional[str | Path] = None) -> bool:
    """Attempt to acquire exclusive single-instance lock.

    Uses OS advisory file locking (fcntl.flock on Unix, msvcrt.locking on Windows).
    Returns True if acquired, False if another instance is already running.
    """
    global _lock_fd, _lock_file_path

    if _lock_fd is not None:
        return True  # Already acquired by this process

    if lock_path is None:
        # Place lock file in project root (one directory above src/utils)
        base_dir = Path(__file__).resolve().parent.parent.parent
        lock_path = base_dir / ".bot.lock"
    else:
        lock_path = Path(lock_path)

    _lock_file_path = lock_path

    try:
        # Open in read/write mode, create if missing
        _lock_fd = open(lock_path, "a+")

        if sys.platform != "win32":
            import fcntl

            try:
                fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                # Lock is already held by another running process
                _lock_fd.seek(0)
                other_pid = _lock_fd.read().strip()
                logging.error(
                    "❌ Another instance of the bot is already running (PID: %s). "
                    "Exiting to prevent duplicate session and AUTH_KEY_DUPLICATED.",
                    other_pid or "unknown",
                )
                _lock_fd.close()
                _lock_fd = None
                return False
        else:
            import msvcrt

            try:
                _lock_fd.seek(0)
                msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
            except (BlockingIOError, OSError):
                logging.error(
                    "❌ Another instance of the bot is already running. "
                    "Exiting to prevent duplicate session and AUTH_KEY_DUPLICATED."
                )
                _lock_fd.close()
                _lock_fd = None
                return False

        # Lock acquired: record our PID
        _lock_fd.seek(0)
        _lock_fd.truncate()
        _lock_fd.write(f"{os.getpid()}\n")
        _lock_fd.flush()

        # Clean up legacy .bot.pid if present to avoid confusion
        legacy_pid = lock_path.parent / ".bot.pid"
        if legacy_pid.exists():
            try:
                legacy_pid.unlink()
            except Exception:
                pass
        src_legacy_pid = lock_path.parent / "src" / ".bot.pid"
        if src_legacy_pid.exists():
            try:
                src_legacy_pid.unlink()
            except Exception:
                pass

        logging.info("🔒 Acquired exclusive process lock (PID: %d)", os.getpid())
        return True

    except Exception as e:
        logging.error("Failed to acquire process lock: %s", e)
        if _lock_fd is not None:
            try:
                _lock_fd.close()
            except Exception:
                pass
            _lock_fd = None
        return False


def release_process_lock() -> None:
    """Release the process lock and clean up the lock file."""
    global _lock_fd, _lock_file_path

    if _lock_fd is not None:
        try:
            if sys.platform != "win32":
                import fcntl

                try:
                    fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            _lock_fd.close()
        except Exception as e:
            logging.debug("Error closing lock file: %s", e)
        _lock_fd = None

    if _lock_file_path is not None and _lock_file_path.exists():
        try:
            _lock_file_path.unlink()
        except Exception:
            pass
        _lock_file_path = None


# Auto-release on clean process exit
atexit.register(release_process_lock)
