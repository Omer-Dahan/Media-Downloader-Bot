"""
JDownloader Manager - my.jdownloader.org API interaction layer.

Handles all communication with JDownloader2 including adding links,
tracking progress, managing concurrency limits, and cleanup.
"""
import logging
import threading
import time
from pathlib import Path
from typing import Any

import myjdapi

from config import (
    JDOWNLOADER_EMAIL,
    JDOWNLOADER_PASSWORD,
    JDOWNLOADER_DEVICE_NAME,
    JDOWNLOADER_DOWNLOAD_DIR,
    JDOWNLOADER_MAX_PER_USER,
    JDOWNLOADER_MAX_GLOBAL,
)

# Module-level concurrency tracking
_active_downloads: dict[int, int] = {}  # user_id -> package_id
_global_active_count: int = 0
_lock = threading.Lock()


class JDownloaderError(Exception):
    """Base exception for JDownloader operations."""
    pass


class JDownloaderConnectionError(JDownloaderError):
    """JDownloader connection failed."""
    pass


class JDownloaderConcurrencyError(JDownloaderError):
    """Concurrency limit exceeded."""
    pass


class JDownloaderManager:
    """Manages interaction with JDownloader2 via my.jdownloader.org API."""

    def __init__(self):
        """Initialize connection to my.jdownloader.org."""
        if not JDOWNLOADER_EMAIL or not JDOWNLOADER_PASSWORD:
            raise JDownloaderConnectionError(
                "פרטי ההתחברות ל-JDownloader לא הוגדרו. בדוק JDOWNLOADER_EMAIL ו-JDOWNLOADER_PASSWORD ב-.env"
            )

        try:
            self._jd = myjdapi.Myjdapi()
            self._jd.set_app_key("MediaDownloaderBot")
            self._jd.connect(JDOWNLOADER_EMAIL, JDOWNLOADER_PASSWORD)
            self._device = self._jd.get_device(JDOWNLOADER_DEVICE_NAME)
            if not self._device:
                raise JDownloaderConnectionError(
                    f"לא נמצא מכשיר JDownloader בשם '{JDOWNLOADER_DEVICE_NAME}'. "
                    "ודא ש-JDownloader2 רץ ומחובר לחשבון my.jdownloader.org"
                )
            logging.info("Connected to JDownloader2 device: %s", JDOWNLOADER_DEVICE_NAME)
        except JDownloaderConnectionError:
            raise
        except myjdapi.exception.MYJDConnectionException as e:
            logging.error("Failed to connect to my.jdownloader.org: %s", e)
            raise JDownloaderConnectionError(
                "שגיאת התחברות ל-my.jdownloader.org. בדוק אימייל וסיסמה."
            ) from e
        except myjdapi.exception.MYJDDeviceNotFoundException as e:
            logging.error("JDownloader device not found: %s", e)
            raise JDownloaderConnectionError(
                f"לא נמצא מכשיר JDownloader בשם '{JDOWNLOADER_DEVICE_NAME}'. "
                "ודא ש-JDownloader2 רץ ומחובר."
            ) from e
        except Exception as e:
            logging.error("Failed to connect to JDownloader: %s", e)
            raise JDownloaderConnectionError(
                "לא ניתן להתחבר ל-JDownloader2. ודא שהתוכנה פועלת ומחוברת לאינטרנט."
            ) from e

    def _reconnect(self):
        """Reconnect to my.jdownloader.org (session refresh)."""
        try:
            self._jd.reconnect()
            self._device = self._jd.get_device(JDOWNLOADER_DEVICE_NAME)
        except Exception as e:
            logging.error("JDownloader reconnect failed: %s", e)
            raise JDownloaderConnectionError("איבדנו את החיבור ל-JDownloader.") from e

    @staticmethod
    def can_start_download(user_id: int) -> tuple[bool, str]:
        """
        Check if user can start a new JDownloader download.

        Returns:
            (can_start, reason_if_blocked)
        """
        global _global_active_count

        with _lock:
            if user_id in _active_downloads:
                return False, "יש לך כבר הורדת JDownloader פעילה. המתן לסיומה."

            if _global_active_count >= JDOWNLOADER_MAX_GLOBAL:
                return False, "השרת עמוס כרגע. נסה שוב בעוד מספר דקות."

            return True, ""

    @staticmethod
    def _register_download(user_id: int, package_id: int):
        """Register active download for tracking."""
        global _global_active_count

        with _lock:
            _active_downloads[user_id] = package_id
            _global_active_count += 1
            logging.info("Registered JD download %s for user %s. Global count: %d",
                        package_id, user_id, _global_active_count)

    @staticmethod
    def _unregister_download(user_id: int):
        """Unregister download from tracking."""
        global _global_active_count

        with _lock:
            if user_id in _active_downloads:
                package_id = _active_downloads.pop(user_id)
                _global_active_count = max(0, _global_active_count - 1)
                logging.info("Unregistered JD download %s for user %s. Global count: %d",
                            package_id, user_id, _global_active_count)

    def add_link(self, url: str, user_id: int) -> int:
        """
        Add a link to JDownloader for download.

        Args:
            url: URL to download
            user_id: Telegram user ID for tracking

        Returns:
            Package ID for tracking
        """
        # Check concurrency limits
        can_start, reason = self.can_start_download(user_id)
        if not can_start:
            raise JDownloaderConcurrencyError(reason)

        try:
            # Add link to linkgrabber
            self._device.linkgrabber.add_links([{
                "autostart": True,
                "links": url,
                "packageName": f"TGBot_{user_id}_{int(time.time())}",
                "destinationFolder": JDOWNLOADER_DOWNLOAD_DIR,
                "overwritePackagizerRules": True,
            }])

            # Wait for linkgrabber to process the link
            time.sleep(3)

            # Get the package from linkgrabber or downloads list
            package_id = self._find_package_for_url(url)

            if package_id is None:
                # Try to move from linkgrabber to downloads if needed
                self._move_linkgrabber_to_downloads()
                time.sleep(2)
                package_id = self._find_package_for_url(url)

            if package_id is None:
                raise JDownloaderError("לא ניתן למצוא את החבילה ב-JDownloader. ייתכן שהקישור לא נתמך.")

            # Register for tracking
            self._register_download(user_id, package_id)

            logging.info("Added link to JDownloader for user %s: %s (package: %s)", user_id, url, package_id)
            return package_id

        except JDownloaderError:
            raise
        except Exception as e:
            logging.error("Failed to add link to JDownloader: %s", e)
            raise JDownloaderError(f"שגיאה בהוספת הקישור ל-JDownloader: {e}") from e

    def _find_package_for_url(self, url: str) -> int | None:
        """Find the package ID for a given URL in linkgrabber or downloads."""
        try:
            # Check downloads list first
            packages = self._device.downloads.query_packages([{
                "bytesLoaded": True,
                "bytesTotal": True,
                "speed": True,
                "eta": True,
                "status": True,
                "finished": True,
                "running": True,
                "saveTo": True,
            }])
            if packages:
                # Return the most recently added package
                return packages[-1].get("uuid")

            # Check linkgrabber
            lg_packages = self._device.linkgrabber.query_packages([{
                "bytesLoaded": True,
                "bytesTotal": True,
                "status": True,
                "saveTo": True,
            }])
            if lg_packages:
                return lg_packages[-1].get("uuid")

        except Exception as e:
            logging.error("Error finding package for URL: %s", e)

        return None

    def _move_linkgrabber_to_downloads(self):
        """Move all packages from linkgrabber to downloads list."""
        try:
            lg_packages = self._device.linkgrabber.query_packages()
            if lg_packages:
                package_ids = [p.get("uuid") for p in lg_packages if p.get("uuid")]
                if package_ids:
                    self._device.linkgrabber.move_to_downloadlist(package_ids=package_ids)
                    logging.info("Moved %d packages from linkgrabber to downloads", len(package_ids))
        except Exception as e:
            logging.error("Error moving linkgrabber to downloads: %s", e)

    def get_status(self, package_id: int) -> dict[str, Any]:
        """
        Get current status of a download package.

        Returns:
            Dict with: progress (0-100), speed (bytes/s), eta (seconds),
                      state (str), downloaded (bytes), total (bytes), name (str)
        """
        try:
            # Try getting packages, reconnect if connection lost
            try:
                packages = self._device.downloads.query_packages([{
                    "bytesLoaded": True,
                    "bytesTotal": True,
                    "speed": True,
                    "eta": True,
                    "status": True,
                    "finished": True,
                    "running": True,
                    "saveTo": True,
                }])
            except (ConnectionError, myjdapi.exception.MYJDConnectionException) as e:
                logging.warning("JDownloader connection lost in get_status, reconnecting... (%s)", e)
                self._reconnect()
                packages = self._device.downloads.query_packages([{
                    "bytesLoaded": True,
                    "bytesTotal": True,
                    "speed": True,
                    "eta": True,
                    "status": True,
                    "finished": True,
                    "running": True,
                    "saveTo": True,
                }])

            if not packages:
                return {"state": "missing", "error": "חבילת ההורדה לא נמצאה"}

            # Find our package
            pkg = None
            for p in packages:
                if p.get("uuid") == package_id:
                    pkg = p
                    break

            if pkg is None:
                # Package might have finished and been removed, check by most recent
                pkg = packages[-1] if packages else None

            if pkg is None:
                return {"state": "missing", "error": "חבילת ההורדה לא נמצאה"}

            total = pkg.get("bytesTotal", 0) or 0
            downloaded = pkg.get("bytesLoaded", 0) or 0
            speed = pkg.get("speed", 0) or 0
            eta = pkg.get("eta", -1) or -1
            finished = pkg.get("finished", False)
            running = pkg.get("running", False)
            status_text = pkg.get("status", "")
            name = pkg.get("name", "Unknown")

            if finished:
                progress = 100.0
                state = "finished"
            elif running:
                progress = round((downloaded / total * 100), 1) if total > 0 else 0.0
                state = "downloading"
            else:
                progress = round((downloaded / total * 100), 1) if total > 0 else 0.0
                state = "waiting"

            return {
                "progress": progress,
                "speed": speed,
                "eta": eta,
                "state": state,
                "downloaded": downloaded,
                "total": total,
                "name": name,
                "status_text": status_text,
            }

        except Exception as e:
            logging.error("Failed to get JDownloader status: %s", e)
            return {"state": "error", "error": str(e)}

    def is_complete(self, package_id: int) -> bool:
        """Check if download is complete."""
        status = self.get_status(package_id)
        return status.get("state") == "finished" or status.get("progress", 0) >= 100

    def is_stalled(self, package_id: int) -> bool:
        """Check if download is stalled (no progress, not running)."""
        status = self.get_status(package_id)
        state = status.get("state", "")
        return state in ("waiting", "error", "missing")

    def get_output_path(self, package_id: int) -> Path | None:
        """
        Get the path to downloaded content.

        Returns:
            Path to download directory or specific file
        """
        try:
            packages = self._device.downloads.query_packages([{
                "saveTo": True,
            }])

            if not packages:
                return None

            # Find our package
            for p in packages:
                if p.get("uuid") == package_id:
                    save_to = p.get("saveTo")
                    if save_to:
                        path = Path(save_to)
                        if path.exists():
                            return path
                    break

            # Fallback: check the default download directory
            dl_dir = Path(JDOWNLOADER_DOWNLOAD_DIR)
            if dl_dir.exists():
                # Find the most recently modified item
                items = sorted(dl_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
                if items:
                    return items[0]

            return None

        except Exception as e:
            logging.error("Failed to get JDownloader output path: %s", e)
            return None

    def remove_download(self, package_id: int, user_id: int, delete_files: bool = False):
        """
        Remove download from JDownloader and cleanup tracking.

        Args:
            package_id: Package ID to remove
            user_id: User ID for tracking cleanup
            delete_files: Whether to delete downloaded files
        """
        try:
            if delete_files:
                self._device.downloads.remove_links(package_ids=[package_id])
            else:
                self._device.downloads.remove_links(package_ids=[package_id])
            logging.info("Removed JD package %s (delete_files=%s)", package_id, delete_files)
        except Exception as e:
            logging.error("Failed to remove JD package %s: %s", package_id, e)
        finally:
            self._unregister_download(user_id)

    def cleanup_finished(self, package_id: int):
        """Remove a finished package from JDownloader's download list."""
        try:
            self._device.downloads.cleanup(
                action="DELETE_FINISHED",
                mode="REMOVE_LINKS_AND_DELETE_FILES",
                selection_type="SELECTED",
                package_ids=[package_id]
            )
        except Exception as e:
            logging.error("Failed to cleanup JD package %s: %s", package_id, e)
