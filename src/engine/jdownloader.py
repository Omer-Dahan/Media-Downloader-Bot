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

import ffmpeg

from config import (
    JDOWNLOADER_POLL_INTERVAL,
    JDOWNLOADER_STALL_TIMEOUT,
    JDOWNLOADER_GLOBAL_TIMEOUT,
)
from engine.base import BaseDownloader, format_safe_error_message
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
        self._temp_merged_files: list[Path] = []

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

        # Check stall in both downloading (zero speed) and waiting (not starting) states
        speed = status.get("speed", 0)
        downloaded = status.get("downloaded", 0)
        other_active = status.get("active_downloads", 0)

        if not hasattr(self, "_last_downloaded_bytes"):
            self._last_downloaded_bytes = downloaded

        has_progress = (speed > 0) or (downloaded > self._last_downloaded_bytes)
        self._last_downloaded_bytes = downloaded

        # If package is waiting in queue while other packages are actively downloading on the device,
        # it is legitimately queued. Reset stall timer so it is not killed prematurely.
        if state == "waiting" and other_active > 0:
            self._stall_start = 0
        elif not has_progress and state in ("downloading", "waiting"):
            if self._stall_start == 0:
                self._stall_start = time.time()
            elif time.time() - self._stall_start > JDOWNLOADER_STALL_TIMEOUT:
                stall_minutes = max(1, JDOWNLOADER_STALL_TIMEOUT // 60)
                if state == "waiting":
                    raise JDownloaderError(
                        f"ההורדה ב-JDownloader2 תקועה במצב המתנה מעל {stall_minutes} דקות (ההורדה לא החלה)."
                    )
                else:
                    raise JDownloaderError(
                        f"ההורדה ב-JDownloader2 תקועה כבר {stall_minutes} דקות ללא התקדמות."
                    )
        elif has_progress:
            self._stall_start = 0

        return False

    def _probe_media_streams(self, file_path: Path) -> tuple[bool, bool]:
        """
        Check whether file contains video and/or audio streams using ffprobe.

        Returns:
            (has_video, has_audio)
        """
        try:
            probe = ffmpeg.probe(str(file_path))
            streams = probe.get("streams", [])
            has_video = any(
                s.get("codec_type") == "video"
                and not bool(int(s.get("disposition", {}).get("attached_pic", 0) or 0))
                for s in streams
            )
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            return has_video, has_audio
        except Exception as e:
            err_details = getattr(e, "stderr", b"")
            err_details = (
                err_details.decode("utf-8", "ignore")
                if isinstance(err_details, bytes)
                else str(err_details)
            )
            logging.warning(
                "ffprobe could not analyze %s: %s | %s",
                file_path.name,
                e,
                err_details,
            )
            # Fallback based on extension if probe fails
            audio_extensions = {
                ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".weba", ".wma"
            }
            video_extensions = {
                ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".m4v", ".ts", ".3gp"
            }
            ext = file_path.suffix.lower()
            return ext in video_extensions, ext in audio_extensions

    def _merge_video_audio(self, video_path: Path, audio_path: Path) -> Path:
        """
        Merge separate video-only and audio-only files into a single container using ffmpeg -c copy.

        Returns:
            Path to merged media file
        """
        output_path = video_path.parent / f"{video_path.stem}_merged{video_path.suffix}"
        logging.info(
            "Merging video '%s' and audio '%s' into '%s' using ffmpeg -c copy",
            video_path.name,
            audio_path.name,
            output_path.name,
        )

        try:
            (
                ffmpeg.output(
                    ffmpeg.input(str(video_path)),
                    ffmpeg.input(str(audio_path)),
                    str(output_path),
                    c="copy",
                )
                .overwrite_output()
                .run(quiet=True)
            )
        except ffmpeg.Error as e:
            err_details = getattr(e, "stderr", b"")
            err_details = (
                err_details.decode("utf-8", "ignore")
                if isinstance(err_details, bytes)
                else str(err_details)
            )
            logging.warning(
                "ffmpeg copy merge to %s failed: %s | %s. Retrying with .mkv container.",
                output_path.suffix,
                e,
                err_details,
            )
            output_path.unlink(missing_ok=True)
            output_path = video_path.parent / f"{video_path.stem}_merged.mkv"
            try:
                (
                    ffmpeg.output(
                        ffmpeg.input(str(video_path)),
                        ffmpeg.input(str(audio_path)),
                        str(output_path),
                        c="copy",
                    )
                    .overwrite_output()
                    .run(quiet=True)
                )
            except ffmpeg.Error as e2:
                err_details2 = getattr(e2, "stderr", b"")
                err_details2 = (
                    err_details2.decode("utf-8", "ignore")
                    if isinstance(err_details2, bytes)
                    else str(err_details2)
                )
                output_path.unlink(missing_ok=True)
                logging.error(
                    "ffmpeg copy merge to .mkv failed: %s | %s", e2, err_details2
                )
                raise JDownloaderError(f"מיזוג הוידאו והאודיו נכשל: {e2}") from e2

        self._temp_merged_files.append(output_path)

        # Delete unmerged source files to free up disk space immediately
        try:
            video_path.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)
            logging.info(
                "Deleted unmerged source components: %s, %s",
                video_path.name,
                audio_path.name,
            )
        except Exception as e:
            logging.warning("Failed to delete unmerged source components: %s", e)

        return output_path

    def _handle_output(self, output_path: Path) -> list[Path]:
        """
        Prepare output for upload.

        Inspects package files using ffprobe:
        - Single file with both video and audio: uploaded as is.
        - Video-only and audio-only files: merged into a single file with ffmpeg -c copy.
        - Video-only or audio-only file without counterpart: uploaded with a warning log.
        - Preserves subtitle files.

        Returns:
            List of file paths ready for upload
        """
        if output_path.is_dir():
            all_files = [f for f in output_path.rglob("*") if f.is_file()]
            files = [
                f for f in all_files if f.suffix.lower() not in {".part", ".tmp"}
            ]
            if not files and all_files:
                files = all_files

            if not files:
                raise JDownloaderError("לא נמצאו קבצים בתיקיית ההורדה")
        else:
            files = [output_path]

        logging.info(
            "Found %d file(s) in JDownloader package '%s': %s",
            len(files),
            self._package_name or output_path.name,
            [f.name for f in files],
        )

        subtitle_extensions = {".srt", ".vtt", ".ass", ".sub"}
        subtitle_files = [f for f in files if f.suffix.lower() in subtitle_extensions]
        media_candidates = [
            f for f in files if f.suffix.lower() not in subtitle_extensions
        ]

        if not media_candidates:
            logging.info(
                "No media files found in package, returning subtitle files: %s",
                [f.name for f in subtitle_files],
            )
            return subtitle_files

        # Probe media files to detect video and audio streams
        probed_files: list[tuple[Path, bool, bool]] = []
        for mf in media_candidates:
            has_v, has_a = self._probe_media_streams(mf)
            logging.info(
                "Probed package file '%s': video=%s, audio=%s",
                mf.name,
                has_v,
                has_a,
            )
            probed_files.append((mf, has_v, has_a))

        both = [f for f, v, a in probed_files if v and a]
        video_only = [f for f, v, a in probed_files if v and not a]
        audio_only = [f for f, v, a in probed_files if a and not v]

        final_media: list[Path] = []

        if both:
            logging.info(
                "Package contains complete media file with video and audio: %s",
                both[0].name,
            )
            final_media = [both[0]]
        elif video_only and audio_only:
            merged = self._merge_video_audio(video_only[0], audio_only[0])
            logging.info(
                "Merged video '%s' and audio '%s' into '%s'",
                video_only[0].name,
                audio_only[0].name,
                merged.name,
            )
            final_media = [merged]
        elif video_only:
            logging.warning(
                "Package '%s' contains video-only file '%s' without audio stream. Uploading video without sound.",
                self._package_name or output_path.name,
                video_only[0].name,
            )
            final_media = [video_only[0]]
        elif audio_only:
            logging.warning(
                "Package '%s' contains audio-only file '%s' without video stream. Uploading audio only.",
                self._package_name or output_path.name,
                audio_only[0].name,
            )
            final_media = [audio_only[0]]
        else:
            logging.warning(
                "Package '%s': No standard audio/video streams recognized in files: %s",
                self._package_name or output_path.name,
                [f.name for f in media_candidates],
            )
            final_media = [media_candidates[0]]

        upload_list = final_media + subtitle_files
        logging.info(
            "Final files prepared for upload for package '%s': %s",
            self._package_name or output_path.name,
            [f.name for f in upload_list],
        )
        return upload_list

    def _start(self):
        """Main JDownloader download flow."""
        user_id = self._user_id

        self.edit_text("🔧 **JDownloader2**\n\n⏳ מתחבר ל-JDownloader...")

        # 1. Connect to JDownloader
        try:
            self._manager = JDownloaderManager()
        except JDownloaderConnectionError as e:
            self.edit_text(format_safe_error_message(e, prefix="❌ "))
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
            self.edit_text(format_safe_error_message(e, fallback="כשל בהוספת הקישור", prefix="❌ "))
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
        output_path: Path | None = None
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

            # 6. Handle output (merge DASH streams if needed, zip/split)
            try:
                files = self._handle_output(output_path)
            except Exception as e:
                raise JDownloaderError(f"שגיאה בעיבוד הקבצים: {e}") from e

            if not files:
                raise JDownloaderError("לא נמצאו קבצים להעלאה.")

            # 7. Build metadata from the actual file (get_metadata() searches self._tempdir
            #    which is empty for JDownloader: the file lives in JDOWNLOADER_DOWNLOAD_DIR)
            primary_file = files[0]
            meta = self._extract_video_metadata(primary_file)
            # Build caption using the filename as title (no _video_title set for JD downloads)
            import html as _html

            audio_extensions = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}
            is_audio = primary_file.suffix.lower() in audio_extensions

            title = primary_file.stem
            if title.endswith("_merged"):
                title = title[:-7]
            title = title[: self._title_length]
            duration_minutes = int(meta["duration"]) // 60
            duration_seconds = int(meta["duration"]) % 60
            duration_str = f"{duration_minutes}:{duration_seconds:02d} דקות"
            if is_audio:
                # Audio-only file: upload as audio (no resolution/thumbnail).
                self._format = "audio"
                meta["caption"] = (
                    f"🎵 <b>{_html.escape(title)}</b>\n\n"
                    f"<blockquote expandable>🔗 מקור: {_html.escape(self._url)}</blockquote>\n"
                    f"⏱️ אורך: {duration_str}\n"
                    f"⬇️ הקובץ מוכן להורדה\n"
                    f"שמיעה מהנה 🎧✨"
                )
            else:
                meta["caption"] = (
                    f"🎬 <b>{_html.escape(title)}</b>\n\n"
                    f"<blockquote expandable>🔗 מקור: {_html.escape(self._url)}</blockquote>\n"
                    f"📐 רזולוציה: {meta['width']}x{meta['height']}\n"
                    f"⏱️ אורך: {duration_str}\n"
                    f"⬇️ הקובץ מוכן לצפייה והורדה\n"
                    f"צפייה מהנה 👀✨"
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
                for ts in temp_subtitles:
                    if ts not in upload_files:
                        upload_files.append(ts)

            logging.info(
                "Final upload file(s) for package '%s': %s",
                self._package_name or str(self._package_id),
                [Path(f).name for f in upload_files],
            )

            # 7.5 Release JDownloader slot early before starting the heavy upload
            try:
                JDownloaderManager._unregister_download(user_id, self._package_id)
                logging.info("Released JD slot for user %s before upload", user_id)
            except Exception:
                pass

            self._upload(files=upload_files, meta=meta)

            logging.info(
                "Upload completed successfully for package '%s' (%d file(s) uploaded)",
                self._package_id,
                len(upload_files),
            )

            # 8. Cleanup from JDownloader and disk (success path: delete files)
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
                        parent = output_path.parent
                        if parent.exists() and not any(parent.iterdir()):
                            parent.rmdir()
                    logging.info("Manually deleted downloaded files: %s", output_path)
                except Exception as e:
                    logging.warning(
                        "Failed to manually delete files %s: %s", output_path, e
                    )

            # Clean up temporary merged files
            for mf in getattr(self, "_temp_merged_files", []):
                try:
                    p = Path(mf)
                    if p.exists():
                        p.unlink(missing_ok=True)
                        logging.info("Deleted temporary merged file: %s", p)
                except Exception as e:
                    logging.warning("Failed to delete temporary merged file %s: %s", mf, e)

        finally:
            # On failure/cancellation: remove the stalled/errored package from JDownloader queue
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

            # Clean up temporary merged files on error/cancel
            for mf in getattr(self, "_temp_merged_files", []):
                try:
                    p = Path(mf)
                    if p.exists():
                        p.unlink(missing_ok=True)
                        logging.info("Cleaned up temporary merged file on exit: %s", p)
                except Exception:
                    pass

            if not _download_succeeded and output_path and output_path.exists():
                try:
                    if output_path.is_dir():
                        shutil.rmtree(output_path, ignore_errors=True)
                    else:
                        output_path.unlink(missing_ok=True)
                        parent = output_path.parent
                        if parent.exists() and not any(parent.iterdir()):
                            parent.rmdir()
                except Exception as e:
                    logging.warning("Failed to clean up output files on exit: %s", e)

            # Always unregister to free concurrency slot
            JDownloaderManager._unregister_download(user_id, self._package_id)
