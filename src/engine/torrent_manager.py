"""
Torrent Manager - qBittorrent Web API interaction layer.

Handles all communication with qBittorrent including adding torrents,
tracking progress, managing concurrency limits, and cleanup.
"""
import logging
import threading
from pathlib import Path
from typing import Any

import qbittorrentapi

from config import (
    QBITTORRENT_HOST,
    QBITTORRENT_PORT,
    QBITTORRENT_USERNAME,
    QBITTORRENT_PASSWORD,
    TORRENT_MAX_PER_USER,
    TORRENT_MAX_GLOBAL,
)

# Module-level concurrency tracking
_active_torrents: dict[int, str] = {}  # user_id -> torrent_hash
_global_active_count: int = 0
_lock = threading.Lock()


class TorrentError(Exception):
    """Base exception for torrent operations."""
    pass


class TorrentConnectionError(TorrentError):
    """qBittorrent connection failed."""
    pass


class TorrentConcurrencyError(TorrentError):
    """Concurrency limit exceeded."""
    pass


class TorrentManager:
    """Manages interaction with qBittorrent Web API."""
    
    def __init__(self):
        """Initialize connection to qBittorrent."""
        try:
            self._client = qbittorrentapi.Client(
                host=QBITTORRENT_HOST,
                port=QBITTORRENT_PORT,
                username=QBITTORRENT_USERNAME,
                password=QBITTORRENT_PASSWORD,
            )
            # Test connection
            self._client.auth_log_in()
            logging.info("Connected to qBittorrent at %s:%s", QBITTORRENT_HOST, QBITTORRENT_PORT)
        except qbittorrentapi.LoginFailed as e:
            logging.error("qBittorrent login failed: %s", e)
            raise TorrentConnectionError("שגיאת התחברות ל-qBittorrent. בדוק שם משתמש וסיסמה.") from e
        except Exception as e:
            logging.error("Failed to connect to qBittorrent: %s", e)
            raise TorrentConnectionError(
                "לא ניתן להתחבר ל-qBittorrent. ודא שהשרת פועל עם Web UI מופעל."
            ) from e
    
    @staticmethod
    def can_start_torrent(user_id: int) -> tuple[bool, str]:
        """
        Check if user can start a new torrent download.
        
        Returns:
            (can_start, reason_if_blocked)
        """
        global _global_active_count
        
        with _lock:
            # Check per-user limit
            if user_id in _active_torrents:
                return False, "יש לך כבר הורדת טורנט פעילה. המתן לסיומה."
            
            # Check global limit
            if _global_active_count >= TORRENT_MAX_GLOBAL:
                return False, "השרת עמוס כרגע. נסה שוב בעוד מספר דקות."
            
            return True, ""
    
    @staticmethod
    def _register_torrent(user_id: int, torrent_hash: str):
        """Register active torrent for tracking."""
        global _global_active_count
        
        with _lock:
            _active_torrents[user_id] = torrent_hash
            _global_active_count += 1
            logging.info("Registered torrent %s for user %s. Global count: %d", 
                        torrent_hash[:8], user_id, _global_active_count)
    
    @staticmethod
    def _unregister_torrent(user_id: int):
        """Unregister torrent from tracking."""
        global _global_active_count
        
        with _lock:
            if user_id in _active_torrents:
                torrent_hash = _active_torrents.pop(user_id)
                _global_active_count = max(0, _global_active_count - 1)
                logging.info("Unregistered torrent %s for user %s. Global count: %d",
                            torrent_hash[:8], user_id, _global_active_count)
    
    def add_torrent(self, source: str | Path, user_id: int) -> str:
        """
        Add a torrent from magnet link or .torrent file.
        
        Args:
            source: Magnet link (str) or path to .torrent file
            user_id: Telegram user ID for tracking
            
        Returns:
            Torrent hash for tracking
        """
        # Check concurrency limits first
        can_start, reason = self.can_start_torrent(user_id)
        if not can_start:
            raise TorrentConcurrencyError(reason)
        
        try:
            # Check if source is magnet link
            if isinstance(source, str) and source.startswith("magnet:"):
                # Extract hash first to handle duplicates/errors better
                import re
                match = re.search(r'btih:([a-fA-F0-9]{40})', source, re.IGNORECASE)
                if not match:
                    match = re.search(r'btih:([a-fA-F0-9]{64})', source, re.IGNORECASE)
                
                if match:
                    torrent_hash = match.group(1).lower()
                    # Optional: Check existence
                    # existing = self._client.torrents_info(torrent_hashes=torrent_hash)
                else:
                    torrent_hash = None # Will try to extract after add if not found here (though unlikely for valid magnet)

                # Add torrent
                result = self._client.torrents_add(urls=source)
                
                if result == "Ok.":
                    pass # Good
                elif result == "Fails.":
                    # Check if it exists anyway (e.g. duplicate)
                    if torrent_hash and self._client.torrents_info(torrent_hashes=torrent_hash):
                        logging.info("Torrent %s already exists (add returned Fails)", torrent_hash)
                    else:
                        raise TorrentError("הוספת הטורנט נכשלה. קישור המגנט לא תקין או שהשרת חוסם אותו.")
                else:
                    raise TorrentError(f"הוספת הטורנט נכשלה: {result}")
                
                # If we didn't have hash, try to extract from source again provided it succeeded?
                # Actually if it succeeded, we rely on the pre-extracted hash.
                if not torrent_hash:
                     raise TorrentError("לא ניתן לחלץ hash מהמגנט לינק")

            else:
                # .torrent file
                file_path = Path(source)
                if not file_path.exists():
                    raise TorrentError("קובץ הטורנט לא נמצא")
                
                with open(file_path, 'rb') as f:
                    torrent_content = f.read()
                
                result = self._client.torrents_add(torrent_files=torrent_content)
                if result == "Ok.":
                    # Get hash from recently added torrents
                    torrents = self._client.torrents_info(sort='added_on', reverse=True, limit=1)
                    if torrents:
                        torrent_hash = torrents[0].hash
                    else:
                        raise TorrentError("לא ניתן למצוא את הטורנט שנוסף")
                else:
                    raise TorrentError(f"הוספת הטורנט נכשלה: {result}")
            
            # Register for tracking
            self._register_torrent(user_id, torrent_hash)
            
            logging.info("Added torrent %s for user %s", torrent_hash[:8], user_id)
            return torrent_hash
            
        except TorrentError:
            raise
        except Exception as e:
            logging.error("Failed to add torrent: %s", e)
            raise TorrentError(f"שגיאה בהוספת הטורנט: {e}") from e
    
    def get_status(self, torrent_hash: str) -> dict[str, Any]:
        """
        Get current status of a torrent.
        
        Returns:
            Dict with: progress (0-100), speed (bytes/s), eta (seconds), 
                      state (str), downloaded (bytes), total (bytes), name (str)
        """
        try:
            torrents = self._client.torrents_info(torrent_hashes=torrent_hash)
            if not torrents:
                return {"state": "missing", "error": "טורנט לא נמצא"}
            
            t = torrents[0]
            return {
                "progress": round(t.progress * 100, 1),
                "speed": t.dlspeed,  # bytes per second
                "eta": t.eta if t.eta != 8640000 else -1,  # -1 for infinity
                "state": t.state,
                "downloaded": t.downloaded,
                "total": t.total_size,
                "name": t.name,
                "num_seeds": t.num_seeds,
                "num_leechs": t.num_leechs,
            }
        except Exception as e:
            logging.error("Failed to get torrent status: %s", e)
            return {"state": "error", "error": str(e)}
    
    def is_complete(self, torrent_hash: str) -> bool:
        """Check if torrent download is complete."""
        status = self.get_status(torrent_hash)
        # Complete states: uploading, stalledUP, pausedUP, forcedUP, queuedUP
        return status.get("state", "").endswith("UP") or status.get("progress", 0) >= 100
    
    def is_stalled(self, torrent_hash: str) -> bool:
        """Check if torrent is stalled (no seeds, no progress)."""
        status = self.get_status(torrent_hash)
        state = status.get("state", "")
        # Stalled states
        return state in ("stalledDL", "metaDL", "missingFiles", "error")
    
    def get_output_path(self, torrent_hash: str) -> Path | None:
        """
        Get the path to downloaded content.
        
        Returns:
            Path to single file, or folder for multi-file torrents
        """
        try:
            torrents = self._client.torrents_info(torrent_hashes=torrent_hash)
            if not torrents:
                return None
            
            t = torrents[0]
            content_path = Path(t.content_path)
            
            if content_path.exists():
                return content_path
            
            # Fallback: construct from save_path + name
            save_path = Path(t.save_path) / t.name
            if save_path.exists():
                return save_path
            
            logging.warning("Torrent content path not found: %s", content_path)
            return None
            
        except Exception as e:
            logging.error("Failed to get torrent output path: %s", e)
            return None
    
    def remove_torrent(self, torrent_hash: str, user_id: int, delete_files: bool = True):
        """
        Remove torrent from qBittorrent and cleanup tracking.
        
        Args:
            torrent_hash: Hash of torrent to remove
            user_id: User ID for tracking cleanup
            delete_files: Whether to delete downloaded files
        """
        try:
            self._client.torrents_delete(
                torrent_hashes=torrent_hash,
                delete_files=delete_files
            )
            logging.info("Removed torrent %s (delete_files=%s)", torrent_hash[:8], delete_files)
        except Exception as e:
            logging.error("Failed to remove torrent %s: %s", torrent_hash[:8], e)
        finally:
            # Always unregister from tracking
            self._unregister_torrent(user_id)
    
    def pause_torrent(self, torrent_hash: str):
        """Pause a torrent download."""
        try:
            self._client.torrents_pause(torrent_hashes=torrent_hash)
        except Exception as e:
            logging.error("Failed to pause torrent: %s", e)
    
    def resume_torrent(self, torrent_hash: str):
        """Resume a paused torrent."""
        try:
            self._client.torrents_resume(torrent_hashes=torrent_hash)
        except Exception as e:
            logging.error("Failed to resume torrent: %s", e)
