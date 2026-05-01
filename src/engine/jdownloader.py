"""
JDownloader Download Engine - Download manager for files via JDownloader2.

Extends BaseDownloader to provide JDownloader2 download with progress tracking,
stall detection, timeouts, and proper cleanup.
Uses my.jdownloader.org API for remote control.
"""

import logging
import shutil
import time
import subprocess
import sys
import threading
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
from engine.archive_manager import (
    needs_archive,
    create_zip,
    create_split_archive,
    split_file,
)
from engine.helper import moon_progress_bar, sizeof_fmt
from utils import timeof_fmt


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
        self._package_name: str | None = None
        self._user_id: int = bot_msg.chat.id
        self._start_time: float = 0
        self._last_speed: float = 0
        self._stall_start: float = 0

    def _download_subtitles_background(self):
        """Fetch subtitles using yt-dlp in the background while JD downloads video."""
        if not self._subtitles:
            return

        logging.info(
            "Attempting to fetch subtitles for JD download via yt-dlp: %s", self._url
        )
        try:
            # We download subtitles to self._tempdir.name
            # BaseDownloader._upload will pick them up from there if we include them.

            # Construct yt-dlp command to only get subtitles
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--skip-download",
                "--writesubtitles",
                "--writeautomaticsub",
                "--subtitleslangs",
                "en,en-orig,en-US,en-GB,he",  # Added Hebrew as per bot context
                "--subtitlesformat",
                "srt",
                "--output",
                f"{self._tempdir.name}/%(title).70s.%(ext)s",
                "--quiet",
                "--no-warnings",
                self._url,
            ]

            def run_ytdlp():
                try:
                    subprocess.run(cmd, check=False, timeout=60)
                    logging.info("yt-dlp subtitle fetch completed for %s", self._url)
                except Exception as e:
                    logging.warning("Background subtitle fetch failed: %s", e)

            threading.Thread(target=run_ytdlp, daemon=True).start()

        except Exception as e:
            logging.warning("Failed to start background subtitle fetch: %s", e)

    def _setup_formats(self):
        """Not used for JDownloader downloads."""

    def _download(self, formats=None):
        """Not used directly - JDownloader download is handled in _start."""

    def _format_progress_message(self, status: dict) -> str:
        """Build progress message for Telegram."""
        state = status.get("state", "unknown")
        name = status.get("name", "Unknown")
        progress = status.get("progress", 0)
        speed = status.get("speed", 0)
        eta = status.get("eta", -1)
        downloaded = status.get("downloaded", 0)
        total = status.get("total", 0)

        # State text translation
        state_hebrew = {
            "downloading": "מוריד",
            "waiting": "ממתין",
            "finished": "הסתיים",
            "error": "שגיאה",
            "missing": "חסר",
        }
        state_text = state_hebrew.get(state, state)

        # Progress bar (moon phases)
        bar = moon_progress_bar(progress)

        # Format sizes
        size_progress = (
            f"{sizeof_fmt(downloaded)}/{sizeof_fmt(total)}"
            if total > 0
            else f"{sizeof_fmt(downloaded)}"
        )

        speed_str = f"{sizeof_fmt(speed)}/s" if speed > 0 else ""
        if speed > 0:
            self._last_speed = speed

        eta_str = timeof_fmt(eta) if eta > 0 else ""

        def more(title, value):
            return f"{title} {value}" if value else ""

        # Build message - matching RTL format from base.py
        text = f"""‏🔧 **JDownloader2**
‏📦 {name[:40]}{'...' if len(name) > 40 else ''}

‏━━━━━━━━━━━━━━━━━━
‏{bar} {progress:.1f}%
‏📊 {size_progress}
{more("‏⚡ מהירות:", speed_str)}
{more("‏⏱️ זמן משוער:", eta_str)}
‏━━━━━━━━━━━━━━━━━━
‏📥 סטטוס: {state_text}"""

        # Remove empty lines created by empty `more()` values
        return "\n".join([line for line in text.splitlines() if line.strip() != ""])

    def _poll_progress(self) -> bool:
        """
        Poll JDownloader progress and update message.

        Returns:
            True if download is complete, False otherwise
        """
        if not self._manager or not self._package_name:
            return False

        status = self._manager.get_status(self._package_name)
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
        Prepare output for upload.
        
        Returns:
            List of file paths ready for upload
        """
        if output_path.is_dir():
            files = list(output_path.rglob("*"))
            files = [f for f in files if f.is_file()]

            if not files:
                raise JDownloaderError("לא נמצאו קבצים בתיקיית ההורדה")

            return files

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
            self._package_id, self._package_name = self._manager.add_link(self._url, user_id)
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
        logging.info(
            "JDownloader download started - package: %s, user: %s",
            self._package_id,
            user_id,
        )

        # Start background subtitle fetch if enabled
        self._download_subtitles_background()

        # Wrap everything in try/finally to ensure cleanup
        _download_succeeded = False
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
            self.edit_text(
                "🔧 **JDownloader2**\n\n✅ ההורדה הסתיימה!\n📦 מכין קבצים להעלאה..."
            )

            output_path = self._manager.get_output_path(self._package_name)
            if not output_path or not output_path.exists():
                raise JDownloaderError(
                    "לא נמצאו קבצים שהורדו. בדוק את תיקיית ההורדות של JDownloader."
                )

            # 6. Handle output (zip/split if needed)
            try:
                files = self._handle_output(output_path)
            except Exception as e:
                raise JDownloaderError(f"שגיאה בעיבוד הקבצים: {e}") from e

            if not files:
                raise JDownloaderError("לא נמצאו קבצים להעלאה.")

            # 7. Build metadata from the actual file (get_metadata() searches self._tempdir
            #    which is empty for JDownloader — the file lives in JDOWNLOADER_DOWNLOAD_DIR)
            primary_file = files[0]
            meta = self._extract_video_metadata(primary_file)
            # Build caption using the filename as title (no _video_title set for JD downloads)
            import html as _html

            title = primary_file.stem[: self._title_length]
            duration_minutes = int(meta["duration"]) // 60
            duration_seconds = int(meta["duration"]) % 60
            duration_str = f"{duration_minutes}:{duration_seconds:02d} דקות"
            meta["caption"] = (
                f"🎬 <b>{_html.escape(title)}</b>\n\n"
                f"<blockquote expandable>🔗 מקור: {_html.escape(self._url)}</blockquote>\n"
                f"📐 רזולוציה: {meta['width']}x{meta['height']}\n"
                f"⏱️ אורך: {duration_str}\n"
                f"⬇️ הקובץ מוכן לצפייה והורדה\n"
                f"צפייה מהנה 👀✨"
            )

            logging.info(
                "JDownloader download complete - %d files to upload", len(files)
            )

            # Combine JDownloader files with subtitles from tempdir
            upload_files = [str(f) for f in files]
            subtitle_extensions = {".srt", ".vtt", ".ass", ".sub"}
            temp_subtitles = [
                str(f)
                for f in Path(self._tempdir.name).glob("*")
                if f.suffix.lower() in subtitle_extensions
            ]
            if temp_subtitles:
                logging.info(
                    "Including %d background subtitles in upload", len(temp_subtitles)
                )
                upload_files.extend(temp_subtitles)

            # 7.5 Release JDownloader slot early before starting the heavy upload
            try:
                JDownloaderManager._unregister_download(user_id, self._package_id)
                logging.info("Released JD slot for user %s before upload", user_id)
            except Exception:
                pass

            self._upload(files=upload_files, meta=meta)

            # 8. Cleanup from JDownloader and disk (success path - delete files)
            _download_succeeded = True
            try:
                self._manager.remove_download(
                    self._package_id, user_id, delete_files=True
                )
                logging.info(
                    "Removed JD package %s after successful upload", self._package_id
                )
            except Exception as e:
                logging.warning(
                    "Failed to remove JD package via API after success: %s", e
                )

            # Safety net: manually delete files from disk in case JD API didn't
            if output_path and output_path.exists():
                try:
                    if output_path.is_dir():
                        shutil.rmtree(output_path, ignore_errors=True)
                    else:
                        output_path.unlink(missing_ok=True)
                        # Also try to remove the parent dir if it's empty
                        parent = output_path.parent
                        if parent.exists() and not any(parent.iterdir()):
                            parent.rmdir()
                    logging.info("Manually deleted downloaded files: %s", output_path)
                except Exception as e:
                    logging.warning(
                        "Failed to manually delete files %s: %s", output_path, e
                    )

        finally:
            # On failure/cancellation - remove the stalled/errored package from JDownloader queue
            if (
                not _download_succeeded
                and self._manager
                and self._package_id is not None
            ):
                try:
                    self._manager.remove_download(
                        self._package_id, user_id, delete_files=True
                    )
                    logging.info(
                        "Removed failed JD package %s from queue", self._package_id
                    )
                except Exception as e:
                    logging.warning(
                        "Failed to remove failed JD package from queue: %s", e
                    )
            # Always unregister to free concurrency slot
            JDownloaderManager._unregister_download(user_id, self._package_id)
