import logging
import threading
from typing import Dict

from database.model import get_total_credits, OWNER

# Concurrency limits
MAX_CONCURRENT_FREE = 1
MAX_CONCURRENT_PAID = 6


class ConcurrencyManager:
    """Manages concurrent downloads per user."""

    def __init__(self):
        self._active_tasks: Dict[int, int] = {}  # user_id -> count
        self._lock = threading.Lock()

    def acquire(self, user_id: int) -> bool:
        """Attempt to acquire a slot for a download task.

        Returns:
            True if acquired, False if limit reached.
        """
        if user_id in OWNER:
            return True

        with self._lock:
            current = self._active_tasks.get(user_id, 0)

            # Check limits
            limit = MAX_CONCURRENT_FREE
            if get_total_credits(user_id) > 0:
                limit = MAX_CONCURRENT_PAID

            if current >= limit:
                logging.warning(
                    "User %s rejected: %d/%d active tasks", user_id, current, limit
                )
                return False

            self._active_tasks[user_id] = current + 1
            logging.info("User %s acquired slot: %d/%d", user_id, current + 1, limit)
            return True

    def release(self, user_id: int):
        """Release a slot."""
        if user_id in OWNER:
            return

        with self._lock:
            current = self._active_tasks.get(user_id, 0)
            if current > 0:
                self._active_tasks[user_id] = current - 1
                if self._active_tasks[user_id] == 0:
                    del self._active_tasks[user_id]
                logging.info(
                    "User %s released slot, remaining: %d",
                    user_id,
                    self._active_tasks.get(user_id, 0),
                )
            else:
                logging.warning(
                    "User %s tried to release slot but has 0 active tasks", user_id
                )


# Global instance
concurrency_manager = ConcurrencyManager()
