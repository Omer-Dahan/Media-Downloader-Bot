"""
JDownloader Download Engine - Download manager for files via JDownloader2.

Extends BaseDownloader to provide JDownloader2 download with progress tracking,
stall detection, timeouts, and proper cleanup.
Uses my.jdownloader.org API for remote control.
"""
import logging
import time
from pathlib import Path

from config import (
    JDOWNLOADER_POLL_INTERVAL,
    JDOWNLOADER_STALL_TIMEOUT,
    JDOWNLOADER_GLOBAL_TIMEOUT,
)
from engine.base import BaseDownloader
from engine.jdownloader_manager import (
    JDownloaderManager,
    JDownloaderError,
    JDownloaderConnectionError,
    JDownloaderConcurrencyError,
)
from engine.archive_manager import needs_archive, create_zip, create_split_archive, split_file


def sizeof_fmt(num: int) -> str:
    """Format bytes to human readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}PB"


def eta_fmt(seconds: int) -> str:
    """Format seconds to human readable ETA."""
    if seconds < 0:
        return "∞"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{int(m)}m {int(s)}s"
    h, remainder = divmod(seconds, 3600)
    m, _ = divmod(remainder, 60)
    return f"{int(h)}h {int(m)}m"


class JDownloaderDownload(BaseDownloader):
    """JDownloader2 download handler.

    Extends BaseDownloader for consistent progress reporting and upload handling.
    Acts as a last-resort fallback when all other download engines fail.
    """

    def __init__(self, client, bot_msg, url):
        """
        Initialize JDownloader download.

        Args:
            client: Pyrogram client
            bot_msg: Bot message for status updates
            url: URL to download
        """
        super().__init__(client, bot_msg, url)
        self._manager: JDownloaderManager | None = None
        self._package_id: int | None = None
        self._user_id: int = bot_msg.chat.id
        self._start_time: float = 0
        self._last_speed: float = 0
        self._stall_start: float = 0

    def _setup_formats(self):
        """Not used for JDownloader downloads."""
        pass

    def _download(self, formats=None):
        """Not used directly - JDownloader download is handled in _start."""
        pass

    def _format_progress_message(self, status: dict) -> str:
        """Build progress message for Telegram."""
        state = status.get("state", "unknown")
        name = status.get("name", "Unknown")
        progress = status.get("progress", 0)
        speed = status.get("speed", 0)
        eta = status.get("eta", -1)
        downloaded = status.get("downloaded", 0)
        total = status.get("total", 0)

        # State emoji
        state_emojis = {
            "downloading": "📥",
            "waiting": "⏳",
            "finished": "✅",
            "error": "❌",
            "missing": "❓",
        }
        state_emoji = state_emojis.get(state, "🔄")

        # Progress bar (moon phases)
        filled = int(progress / 10)
        bar = "🌕" * filled + "🌑" * (10 - filled)

        # Build message
        lines = [
            f"🔧 **JDownloader2**",
            f"📦 {name[:40]}{'...' if len(name) > 40 else ''}",
            "",
            f"{bar} {progress:.1f}%",
        ]

        if total > 0:
            lines.append(f"📊 {sizeof_fmt(downloaded)}/{sizeof_fmt(total)}")

        if speed > 0:
            lines.append(f"⚡ {sizeof_fmt(speed)}/s")
            self._last_speed = speed

        if eta > 0:
            lines.append(f"⏱️ ETA: {eta_fmt(eta)}")

        lines.append(f"\n{state_emoji} סטטוס: {state}")

        return "\n".join(lines)

    def _poll_progress(self) -> bool:
        """
        Poll JDownloader progress and update message.

        Returns:
            True if download is complete, False otherwise
        """
        if not self._manager or self._package_id is None:
            return False

        status = self._manager.get_status(self._package_id)
        state = status.get("state", "")

        # Update Telegram message
        msg = self._format_progress_message(status)
        self.edit_text(msg)

        # Check completion
        if state == "finished" or status.get("progress", 0) >= 100:
            return True

        # Check for errors
        if state == "error":
            error_msg = status.get("error", "שגיאה לא ידועה")
            raise JDownloaderError(f"שגיאת הורדה ב-JDownloader: {error_msg}")

        if state == "missing":
            raise JDownloaderError("ההורדה נעלמה מ-JDownloader.")

        # Check stall
        speed = status.get("speed", 0)
        if speed == 0 and state == "downloading":
            if self._stall_start == 0:
                self._stall_start = time.time()
            elif time.time() - self._stall_start > JDOWNLOADER_STALL_TIMEOUT:
                raise JDownloaderError(
                    f"ההורדה תקועה כבר {JDOWNLOADER_STALL_TIMEOUT // 60} דקות ללא התקדמות."
                )
        else:
            self._stall_start = 0

        return False

    def _handle_output(self, output_path: Path) -> list[Path]:
        """
        Prepare output for upload - ZIP if folder, split if too large.

        Returns:
            List of file paths ready for upload
        """
        if output_path.is_dir():
            # Multiple files - check if needs archiving
            files = list(output_path.rglob("*"))
            files = [f for f in files if f.is_file()]

            if not files:
                raise JDownloaderError("לא נמצאו קבצים בתיקיית ההורדה")

            if len(files) == 1:
                return [files[0]]

            # Multiple files - create ZIP
            if needs_archive(output_path):
                total_size = sum(f.stat().st_size for f in files)
                if total_size > 2 * 1024 * 1024 * 1024:  # > 2GB
                    return create_split_archive(output_path, Path(self._tempdir.name))
                return [create_zip(output_path, Path(self._tempdir.name))]

            return files

        # Single file
        file_size = output_path.stat().st_size
        if file_size > 2 * 1024 * 1024 * 1024:  # > 2GB
            return split_file(output_path, Path(self._tempdir.name))

        return [output_path]

    def _start(self):
        """Main JDownloader download flow."""
        user_id = self._user_id

        self.edit_text("🔧 **JDownloader2**\n\n⏳ מתחבר ל-JDownloader...")

        # 1. Connect to JDownloader
        try:
            self._manager = JDownloaderManager()
        except JDownloaderConnectionError as e:
            self.edit_text(f"❌ {e}")
            raise
        except Exception as e:
            msg = "❌ לא ניתן להתחבר ל-JDownloader2. ודא שהתוכנה פועלת."
            self.edit_text(msg)
            raise JDownloaderError(msg) from e

        # 2. Check concurrency
        can_start, reason = JDownloaderManager.can_start_download(user_id)
        if not can_start:
            self.edit_text(f"⚠️ {reason}")
            raise JDownloaderConcurrencyError(reason)

        # 3. Add link
        self.edit_text("🔧 **JDownloader2**\n\n📎 מוסיף קישור להורדה...")
        try:
            self._package_id = self._manager.add_link(self._url, user_id)
        except JDownloaderConcurrencyError:
            raise
        except JDownloaderError as e:
            self.edit_text(f"❌ {e}")
            raise
        except Exception as e:
            msg = "❌ לא ניתן להוסיף את הקישור ל-JDownloader2."
            self.edit_text(msg)
            raise JDownloaderError(msg) from e

        self._start_time = time.time()
        logging.info("JDownloader download started - package: %s, user: %s", self._package_id, user_id)

        # Wrap everything in try/finally to ensure cleanup
        try:
            # 4. Poll progress
            while True:
                # Check cancellation
                self.check_for_cancel()

                # Check global timeout
                elapsed = time.time() - self._start_time
                if elapsed > JDOWNLOADER_GLOBAL_TIMEOUT:
                    raise JDownloaderError(
                        f"ההורדה חרגה ממגבלת הזמן ({JDOWNLOADER_GLOBAL_TIMEOUT // 3600} שעות)."
                    )

                # Poll
                if self._poll_progress():
                    break

                time.sleep(JDOWNLOADER_POLL_INTERVAL)

            # 5. Download complete - get output
            self.edit_text("🔧 **JDownloader2**\n\n✅ ההורדה הסתיימה!\n📦 מכין קבצים להעלאה...")

            output_path = self._manager.get_output_path(self._package_id)
            if not output_path or not output_path.exists():
                raise JDownloaderError("לא נמצאו קבצים שהורדו. בדוק את תיקיית ההורדות של JDownloader.")

            # 6. Handle output (zip/split if needed)
            try:
                files = self._handle_output(output_path)
            except Exception as e:
                raise JDownloaderError(f"שגיאה בעיבוד הקבצים: {e}") from e

            if not files:
                raise JDownloaderError("לא נמצאו קבצים להעלאה.")

            # 7. Upload
            logging.info("JDownloader download complete - %d files to upload", len(files))
            self._upload(files=[str(f) for f in files])

            # 8. Cleanup from JDownloader
            try:
                self._manager.cleanup_finished(self._package_id)
            except Exception as e:
                logging.warning("Failed to cleanup JD package: %s", e)

        finally:
            # Always unregister to free concurrency slot
            JDownloaderManager._unregister_download(user_id)
