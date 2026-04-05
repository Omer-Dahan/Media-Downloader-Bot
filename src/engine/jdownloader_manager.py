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

try:
    import myjdapi
    MYJD_AVAILABLE = True
except ImportError:
    myjdapi = None  # type: ignore
    MYJD_AVAILABLE = False

from config import (
    JDOWNLOADER_EMAIL,
    JDOWNLOADER_PASSWORD,
    JDOWNLOADER_DEVICE_NAME,
    JDOWNLOADER_DOWNLOAD_DIR,
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
        if not MYJD_AVAILABLE:
            raise JDownloaderConnectionError(
                "ספריית myjdapi אינה מותקנת. התקן אותה עם: pip install myjdownloader"
            )
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
        except Exception as e:
            # Resolve exception types dynamically to avoid NameError when myjdapi unavailable
            conn_exc = getattr(getattr(myjdapi, "exception", None), "MYJDConnectionException", None)
            dev_exc = getattr(getattr(myjdapi, "exception", None), "MYJDDeviceNotFoundException", None)
            if conn_exc and isinstance(e, conn_exc):
                logging.error("Failed to connect to my.jdownloader.org: %s", e)
                raise JDownloaderConnectionError(
                    "שגיאת התחברות ל-my.jdownloader.org. בדוק אימייל וסיסמה."
                ) from e
            if dev_exc and isinstance(e, dev_exc):
                logging.error("JDownloader device not found: %s", e)
                raise JDownloaderConnectionError(
                    f"לא נמצא מכשיר JDownloader בשם '{JDOWNLOADER_DEVICE_NAME}'. "
                    "ודא ש-JDownloader2 רץ ומחובר."
                ) from e
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

    # Video file extensions to accept from JDownloader packages
    VIDEO_EXTENSIONS = (
        ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
        ".wmv", ".m4v", ".ts", ".m2ts", ".mpeg", ".mpg",
        ".f4v", ".3gp", ".ogv", ".m3u8",
    )

    # Known non-media extensions to always reject
    JUNK_EXTENSIONS = (
        ".webmanifest", ".html", ".htm", ".css", ".js",
        ".woff", ".woff2", ".ttf", ".eot", ".map", 
        ".php", ".asp", ".jsp",
    )

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
            # Snapshot existing linkgrabber package UUIDs BEFORE adding our link
            existing_uuids = self._get_linkgrabber_uuids()

            self._device.linkgrabber.add_links([{
                "autostart": False,   # keep in linkgrabber until we filter
                "links": url,
                "packageName": f"TGBot_{user_id}",
                "destinationFolder": JDOWNLOADER_DOWNLOAD_DIR,
            }])

            # Retry loop: wait up to 60s for JDownloader to crawl the URL
            # and create a NEW package that wasn't there before
            package_id = None
            deadline = time.time() + 60
            while time.time() < deadline:
                time.sleep(2)
                package_id = self._find_new_package(existing_uuids)
                if package_id is not None:
                    break
            else:
                raise JDownloaderError(
                    "JDownloader לא הצליח לעבד את הקישור תוך 60 שניות. "
                    "ייתכן שהאתר לא נתמך או שהקישור שגוי."
                )

            # Filter to video-only links inside the package, then start
            self._filter_video_links_in_package(package_id)
            self._device.linkgrabber.move_to_downloadlist(
                package_ids=[package_id],
                link_ids=[],
            )

            # Register for tracking
            self._register_download(user_id, package_id)

            logging.info(
                "Added link to JDownloader for user %s: %s (package: %s)",
                user_id, url, package_id,
            )
            return package_id

        except JDownloaderError:
            raise
        except Exception as e:
            logging.error("Failed to add link to JDownloader: %s", e)
            raise JDownloaderError(f"שגיאה בהוספת הקישור ל-JDownloader: {e}") from e

    def _get_linkgrabber_uuids(self) -> set[int]:
        """Return a set of all current linkgrabber package UUIDs."""
        try:
            packages = self._device.linkgrabber.query_packages([{}])
            return {p["uuid"] for p in (packages or []) if "uuid" in p}
        except Exception as e:
            logging.warning("Could not snapshot linkgrabber UUIDs: %s", e)
            return set()

    def _find_new_package(self, existing_uuids: set[int]) -> int | None:
        """Find a package UUID in the linkgrabber that wasn't in the snapshot."""
        try:
            packages = self._device.linkgrabber.query_packages([{"name": True}])
            if packages:
                for pkg in packages:
                    uid = pkg.get("uuid")
                    if uid and uid not in existing_uuids:
                        logging.info("Found new JD package: uuid=%s name='%s'", uid, pkg.get("name"))
                        return uid
        except Exception as e:
            logging.error("Error searching for new package: %s", e)
        return None

    def _filter_video_links_in_package(self, package_id: int) -> None:
        """
        Remove all non-video links from a linkgrabber package.
        This ensures JDownloader only downloads video files.
        """
        try:
            links = self._device.linkgrabber.query_links([{
                "packageUUIDs": [package_id],
                "name": True,
                "url": True,
            }])
            if not links:
                return  # nothing to filter

            links_to_remove = [
                lnk.get("uuid")
                for lnk in links
                if lnk.get("uuid") and not self._is_video_link(lnk)
            ]

            remaining = len(links) - len(links_to_remove)

            if remaining == 0:
                # No recognised video links — but before letting everything through,
                # remove known junk files (manifest, json, html, etc.)
                junk_to_remove = [
                    lnk.get("uuid")
                    for lnk in links
                    if lnk.get("uuid") and self._is_junk_link(lnk)
                ]

                if junk_to_remove:
                    logging.info(
                        "No video links found — removing %d junk links from JD package %s",
                        len(junk_to_remove), package_id,
                    )
                    self._device.linkgrabber.remove_links(
                        link_ids=junk_to_remove,
                        package_ids=[],
                    )

                surviving = len(links) - len(junk_to_remove)
                if surviving == 0:
                    raise JDownloaderError(
                        "לא נמצאו קבצים מוכרים בקישור. "
                        "JDownloader מצא תוכן אך ללא פורמט נתמך."
                    )

                names = [lnk.get("name", "?") for lnk in links if lnk.get("uuid") not in junk_to_remove]
                logging.warning(
                    "No video-extension links found in JD package %s — kept %d non-junk links. "
                    "Link names: %s", package_id, surviving, names
                )
                return

            if links_to_remove:
                logging.info(
                    "Removing %d non-video links from JD package %s",
                    len(links_to_remove), package_id,
                )
                self._device.linkgrabber.remove_links(
                    link_ids=links_to_remove,
                    package_ids=[],
                )

            remaining = len(links) - len(links_to_remove)
            if remaining == 0:
                raise JDownloaderError(
                    "לא נמצאו קבצים מוכרים בקישור. "
                    "JDownloader מצא תוכן אך ללא פורמט נתמך."
                )

            logging.info("%d video link(s) kept in JD package %s", remaining, package_id)

        except JDownloaderError:
            raise
        except Exception as e:
            logging.warning("Could not filter video links in package %s: %s", package_id, e)
            # Non-fatal: proceed without filtering

    @classmethod
    def _is_video_link(cls, link: dict) -> bool:
        """Return True if a linkgrabber link looks like a video file."""
        name = (link.get("name") or "").lower()
        url  = (link.get("url")  or "").lower()
        return any(
            name.endswith(ext) or ext in url
            for ext in cls.VIDEO_EXTENSIONS
        )

    @classmethod
    def _is_junk_link(cls, link: dict) -> bool:
        """Return True if a linkgrabber link is a known junk/non-media file."""
        name = (link.get("name") or "").lower()
        return any(name.endswith(ext) for ext in cls.JUNK_EXTENSIONS)


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
                # Use cleanup to remove links AND delete files from disk for THIS package ONLY.
                self._device.downloads.cleanup(
                    action="DELETE_ALL",
                    mode="REMOVE_LINKS_AND_DELETE_FILES",
                    selection_type="NONE",
                    package_ids=[package_id]
                )
            else:
                self._device.downloads.remove_links(package_ids=[package_id])
            logging.info("Removed JD package %s (delete_files=%s)", package_id, delete_files)
        except Exception as e:
            logging.error("Failed to remove JD package %s: %s", package_id, e)
        finally:
            self._unregister_download(user_id)


