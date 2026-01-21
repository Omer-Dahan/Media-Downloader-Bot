"""
Torrent Download Engine - Download manager for torrents via qBittorrent.

Extends BaseDownloader to provide torrent download with progress tracking,
stall detection, timeouts, and proper cleanup.
"""
import logging
import time
from pathlib import Path

from config import (
    TG_NORMAL_MAX_SIZE,
    TORRENT_STALL_TIMEOUT,
    TORRENT_GLOBAL_TIMEOUT,
)
from engine.base import BaseDownloader
from engine.torrent_manager import (
    TorrentManager,
    TorrentError,
    TorrentConnectionError,
    TorrentConcurrencyError,
)
from engine.archive_manager import needs_archive, create_zip, create_split_archive, split_file


def sizeof_fmt(num: int) -> str:
    """Format bytes to human readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def eta_fmt(seconds: int) -> str:
    """Format seconds to human readable ETA."""
    if seconds < 0:
        return "∞"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h {minutes}m"


class TorrentDownload(BaseDownloader):
    """
    Torrent download handler using qBittorrent.
    
    Extends BaseDownloader for consistent progress reporting and upload handling.
    """
    
    def __init__(self, client, bot_msg, source: str, torrent_file_path: Path = None):
        """
        Initialize torrent download.
        
        Args:
            client: Pyrogram client
            bot_msg: Bot message for status updates
            source: Magnet link or path to .torrent file
            torrent_file_path: Path to downloaded .torrent file (if from document)
        """
        # For BaseDownloader, we use the source as URL
        super().__init__(client, bot_msg, source)
        
        self._source = source
        self._torrent_file_path = torrent_file_path
        self._torrent_hash: str | None = None
        self._manager: TorrentManager | None = None
        self._start_time: float = 0
        self._last_progress_time: float = 0
        self._last_progress_value: float = 0
        self._user_id = bot_msg.chat.id
    
    def _setup_formats(self):
        """Not used for torrents."""
        pass
    
    def _download(self, formats=None):
        """Not used directly - torrent download is handled in _start."""
        pass
    
    def _format_progress_message(self, status: dict) -> str:
        """Build progress message for Telegram."""
        progress = status.get("progress", 0)
        speed = status.get("speed", 0)
        eta = status.get("eta", -1)
        name = status.get("name", "טורנט")
        state = status.get("state", "unknown")
        downloaded = status.get("downloaded", 0)
        total = status.get("total", 0)
        seeds = status.get("num_seeds", 0)
        peers = status.get("num_leechs", 0)
        
        # Build moon phase progress bar using shared function
        from engine.helper import moon_progress_bar
        bar = moon_progress_bar(progress)
        
        # State emoji
        state_emoji = {
            "downloading": "⬇️",
            "stalledDL": "⏸️",
            "metaDL": "🔍",
            "queuedDL": "⏳",
            "pausedDL": "⏸️",
            "error": "❌",
            "missingFiles": "❌",
        }.get(state, "⬇️")
        
        # State text
        state_text = {
            "downloading": "מוריד",
            "stalledDL": "ממתין לעמיתים",
            "metaDL": "מחפש מטא-דאטה",
            "queuedDL": "בתור",
            "pausedDL": "מושהה",
            "error": "שגיאה",
            "missingFiles": "קבצים חסרים",
        }.get(state, state)
        
        lines = [
            f"**{name[:50]}**" if len(name) > 50 else f"**{name}**",
            "",
            f"‏{bar} {progress:.1f}%",
            "",
            f"{state_emoji} **מצב:** {state_text}",
            f"⬇️ **מהירות:** {sizeof_fmt(speed)}/s",
            f"📦 **הורד:** {sizeof_fmt(downloaded)} / {sizeof_fmt(total)}",
            f"⏱️ **זמן משוער:** {eta_fmt(eta)}",
            f"👥 **עמיתים:** {seeds} seeds, {peers} peers",
        ]
        
        return "\n".join(lines)
    
    def _poll_progress(self) -> bool:
        """
        Poll torrent progress and update message.
        
        Returns:
            True if download is complete, False otherwise
        """
        current_time = time.time()
        
        # Check global timeout
        elapsed = current_time - self._start_time
        if elapsed > TORRENT_GLOBAL_TIMEOUT:
            raise TorrentError(
                f"ההורדה הופסקה - חרגת ממגבלת הזמן ({TORRENT_GLOBAL_TIMEOUT // 3600} שעות)"
            )
        
        # Get status from qBittorrent
        status = self._manager.get_status(self._torrent_hash)
        
        if status.get("state") == "error" or status.get("state") == "missingFiles":
            raise TorrentError(f"שגיאת טורנט: {status.get('error', status.get('state'))}")
        
        if status.get("state") == "missing":
            raise TorrentError("הטורנט נעלם מהשרת")
        
        # Check for stall (no progress for too long)
        current_progress = status.get("progress", 0)
        if current_progress > self._last_progress_value:
            self._last_progress_time = current_time
            self._last_progress_value = current_progress
        else:
            stall_duration = current_time - self._last_progress_time
            if stall_duration > TORRENT_STALL_TIMEOUT:
                raise TorrentError(
                    f"ההורדה נתקעה - אין התקדמות כבר {TORRENT_STALL_TIMEOUT // 60} דקות"
                )
        
        # Update status message (throttled in edit_text)
        try:
            progress_msg = self._format_progress_message(status)
            self.edit_text(progress_msg)
        except Exception as e:
            logging.warning("Failed to update progress message: %s", e)
        
        # Check if complete
        return self._manager.is_complete(self._torrent_hash)
    
    def _handle_output(self, output_path: Path) -> list[str]:
        """
        Prepare output for upload - ZIP if folder, split if too large.
        
        Returns:
            List of file paths ready for upload
        """
        if output_path.is_dir():
            # Flatten directory - get all files recursively
            files_to_process = []
            for item in output_path.rglob("*"):
                if item.is_file():
                    files_to_process.append(item)
        else:
            files_to_process = [output_path]
            
        final_files = []
        for file_path in files_to_process:
            # Check size
            # TG_NORMAL_MAX_SIZE is usually 2GB
            if file_path.stat().st_size > TG_NORMAL_MAX_SIZE:
                self.edit_text(f"✂️ מפצל קובץ גדול: {file_path.name}...")
                # Split specifically this large file
                parts = split_file(file_path, Path(self._tempdir.name))
                final_files.extend([str(p) for p in parts])
            else:
                final_files.append(str(file_path))
                
        return final_files
    
    def _start(self):
        """Main torrent download flow."""
        try:
            # Initialize connection to qBittorrent
            self.edit_text("🔗 מתחבר לשרת הטורנטים...")
            self._manager = TorrentManager()
            
            # Add torrent
            self.edit_text("📥 מוסיף טורנט...")
            source = self._torrent_file_path or self._source
            self._torrent_hash = self._manager.add_torrent(source, self._user_id)
            
            self._start_time = time.time()
            self._last_progress_time = self._start_time
            
            # Poll for progress
            while True:
                # Check for cancellation
                self.check_for_cancel()
                
                # Poll and check completion
                if self._poll_progress():
                    break
                
                # Wait before next poll
                time.sleep(3)
            
            # Download complete!
            self.edit_text("✅ ההורדה הושלמה! מכין להעלאה...")
            
            # Get output path
            output_path = self._manager.get_output_path(self._torrent_hash)
            if not output_path:
                raise TorrentError("לא ניתן למצוא את הקבצים שהורדו")
            
            # Prepare files for upload
            upload_files = self._handle_output(output_path)
            
            # Get torrent name and calculate total size
            status = self._manager.get_status(self._torrent_hash)
            torrent_name = status.get("name", "טורנט")
            total_size = sum(Path(f).stat().st_size for f in upload_files)
            
            # Custom meta for torrents - send as document with torrent-style caption
            # Handle caption length limit (1024 chars)
            import html
            safe_name = torrent_name.replace("*", "").replace("_", "").replace("`", "")
            
            caption_prefix = (
                f"🧲 <b>{html.escape(safe_name)}</b>\n\n"
                f"📦 גודל: {sizeof_fmt(total_size)}\n"
                f"📁 קבצים: {len(upload_files)}\n\n"
                f"<blockquote expandable>🔗 מקור: "
            )
            caption_suffix = "</blockquote>"
            
            # Calculate remaining space
            # Using 1000 instead of 1024 to be safe with emojis/unicode length differences
            max_len = 1000
            current_len = len(caption_prefix) + len(caption_suffix)
            available_len = max_len - current_len
            
            if len(self._source) > available_len:
                source_display = self._source[:available_len-3] + "..."
            else:
                source_display = self._source
                
            torrent_meta = {
                "caption": f"{caption_prefix}{source_display}{caption_suffix}",
                "thumb": None,  # No thumbnail for torrents
            }
            
            # If single video file, try to extract metadata (thumb, duration)
            # Check for ANY supported video files (excluding MKV as requested)
            supported_video_exts = {'.mp4', '.avi', '.mov', '.webm', '.flv', '.m4v'}
            detected_videos = [f for f in upload_files if Path(f).suffix.lower() in supported_video_exts]
            
            has_video = len(detected_videos) > 0
            
            if has_video:
                # Find the largest video file for metadata extraction
                try:
                    largest_video = max(detected_videos, key=lambda p: Path(p).stat().st_size)
                    vid_meta = self._extract_video_metadata(Path(largest_video))
                    if vid_meta:
                        torrent_meta.update(vid_meta)
                        logging.info("Extracted metadata for largest torrent video: %s", {k: v for k, v in vid_meta.items() if k != 'thumb'})
                except Exception as e:
                    logging.warning("Failed to extract torrent metadata: %s", e)
            
            # Force document format ONLY if no supported videos found
            if not has_video:
                self._format = "document"
            
            # Upload using BaseDownloader's upload method
            self._upload(files=upload_files, meta=torrent_meta)
            
        except TorrentConnectionError as e:
            logging.error("qBittorrent connection error: %s", e)
            self.edit_text(f"❌ {e}")
            
            # Alert admin via archive channel
            from config import ARCHIVE_CHANNEL
            if ARCHIVE_CHANNEL:
                try:
                    self._client.send_message(
                        chat_id=ARCHIVE_CHANNEL,
                        text=(
                            "🚨 **התראת שרת טורנטים**\n\n"
                            "❌ לא ניתן להתחבר ל-qBittorrent!\n"
                            f"👤 משתמש: {self._user_id}\n"
                            f"**>📝 מקור: {self._source}**\n\n"
                            "⚠️ יש לבדוק שהתוכנה פועלת עם Web UI מופעל."
                        )
                    )
                except Exception as alert_err:
                    logging.error("Failed to send archive alert: %s", alert_err)
            
            raise ValueError(str(e))
            
        except TorrentConcurrencyError as e:
            logging.info("Concurrency limit: %s", e)
            self.edit_text(f"⏳ {e}")
            raise ValueError(str(e))
            
        except TorrentError as e:
            logging.error("Torrent error: %s", e)
            self.edit_text(f"❌ {e}")
            raise ValueError(str(e))
            
        except ValueError as e:
            # Cancellation or other value errors
            error_msg = str(e)
            if "בוטלה" in error_msg or "cancelled" in error_msg.lower():
                try:
                    self.edit_text("🛑 ההורדה בוטלה בהצלחה")
                except Exception:
                    pass  # Message edit might fail, that's ok
            raise
            
        finally:
            # Always cleanup - remove from qBittorrent and tracking
            if self._manager and self._torrent_hash:
                try:
                    self._manager.remove_torrent(
                        self._torrent_hash, 
                        self._user_id,
                        delete_files=True
                    )
                except Exception as e:
                    logging.error("Cleanup failed: %s", e)


def process_large_mkv(file_path, temp_dir):
    """
    Handle large MKV files by zipping and splitting them to preserve audio/subs.
    Returns list of paths to the split parts (as strings).
    """
    import logging
    from pathlib import Path
    from engine.archive_manager import create_split_archive
    
    file_path = Path(file_path)
    temp_dir = Path(temp_dir)
    
    logging.info("Large MKV detected (%s), using ZIP split", file_path.name)
    parts = create_split_archive(file_path, temp_dir)
    
    # Try to delete original to save space
    try:
        file_path.unlink()
    except Exception as e:
        logging.warning("Failed to remove original MKV: %s", e)
        
    return [str(p) for p in parts]
