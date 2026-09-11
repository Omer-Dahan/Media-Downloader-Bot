"""Tests for process lock and duplicate instance prevention."""

import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils.process_lock import acquire_process_lock, release_process_lock


def test_process_lock_acquisition_and_release(tmp_path):
    """Test that process lock can be acquired and released properly."""
    lock_file = tmp_path / ".test.lock"

    # 1. First acquisition should succeed
    acquired = acquire_process_lock(lock_file)
    assert acquired is True
    assert lock_file.exists()

    # Verify our PID was written to the file
    with open(lock_file, "r") as f:
        pid_str = f.read().strip()
    assert pid_str == str(os.getpid())

    # 2. Releasing the lock
    release_process_lock()

    # 3. Should be able to acquire again after release
    acquired_again = acquire_process_lock(lock_file)
    assert acquired_again is True
    release_process_lock()


def test_process_lock_prevents_duplicate_instance(tmp_path):
    """Test that a second process/holder cannot acquire an already locked file."""
    import subprocess

    lock_file = tmp_path / ".test_dup.lock"

    # Acquire lock in this process
    assert acquire_process_lock(lock_file) is True

    # Attempt to acquire lock in a separate subprocess
    script = f"""
import sys
sys.path.insert(0, '{str(Path(__file__).resolve().parent.parent / "src")}')
from utils.process_lock import acquire_process_lock

# This should fail because the parent process holds the lock
acquired = acquire_process_lock('{str(lock_file)}')
sys.exit(0 if acquired else 42)
"""
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert proc.returncode == 42, f"Expected code 42 (lock refused), got {proc.returncode}"

    # Cleanup
    release_process_lock()
