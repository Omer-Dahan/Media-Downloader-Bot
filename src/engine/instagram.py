import logging
import pathlib
import re
from typing import Optional
from pyrogram import types

import yt_dlp

from config import ARCHIVE_CHANNEL, INSTAGRAM_SESSION_FILE, INSTAGRAM_COOKIES_FILE
from engine.base import BaseDownloader

# Try to import instaloader, but make it optional
try:
    import instaloader
    INSTALOADER_AVAILABLE = True
except ImportError:
    INSTALOADER_AVAILABLE = False
    logging.warning("instaloader not installed. Run: pip install instaloader")


class InstagramDownload(BaseDownloader):
    """Downloader for Instagram content using yt-dlp with instaloader fallback."""

    def extract_code(self) -> Optional[str]:
        """Extract the media code from Instagram URL."""
        patterns = [
            # Instagram stories highlights
            r"/stories/highlights/([a-zA-Z0-9_-]+)/",
            # Posts
            r"/p/([a-zA-Z0-9_-]+)/",
            # Reels
            r"/reel/([a-zA-Z0-9_-]+)/",
            # TV
            r"/tv/([a-zA-Z0-9_-]+)/",
            # Threads post (both with @username and without)
            r"(?:https?://)?(?:www\.)?(?:threads\.net)(?:/[@\w.]+)?(?:/post)?/([\w-]+)(?:/?\?.*)?$",
        ]

        for pattern in patterns:
            match = re.search(pattern, self._url)
            if match:
                if pattern == patterns[0]:  # stories highlights
                    return self._url
                else:
                    return match.group(1)

        return None

    def _setup_formats(self) -> list | None:
        """Instagram doesn't need format setup like YouTube."""
        return [None]

    def _download_with_ytdlp(self) -> list:
        """Download Instagram content using yt-dlp."""
        output = pathlib.Path(self._tempdir.name, "%(title).70s.%(ext)s").as_posix()
        ydl_opts = {
            "outtmpl": output,
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
            "format": "best[ext=mp4]/best",
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
            "retries": 3,
            "fragment_retries": 3,
            "progress_hooks": [self._ytdlp_progress_hook],
        }
        
        # Add cookies if configured
        if INSTAGRAM_COOKIES_FILE:
            import os
            if os.path.exists(INSTAGRAM_COOKIES_FILE):
                ydl_opts["cookiefile"] = INSTAGRAM_COOKIES_FILE
                logging.info("Instagram: Using cookies file for yt-dlp: %s", INSTAGRAM_COOKIES_FILE)
        else:
            # Try to extract cookies from browser automatically
            # Priority: Chrome, Firefox, Edge
            ydl_opts["cookiesfrombrowser"] = ("chrome",)
            logging.info("Instagram: Trying to extract cookies from Chrome browser")

        try:
            logging.info("Instagram: Trying yt-dlp with URL: %s", self._url)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Use extract_info with download=True to get title and download in one operation
                info = ydl.extract_info(self._url, download=True)
                if info:
                    # Get all possible title fields and choose the longest one
                    title_field = info.get('title', '') or ''
                    desc_field = info.get('description', '') or ''
                    fulltitle_field = info.get('fulltitle', '') or ''
                    title = max([title_field, desc_field, fulltitle_field], key=len)
                    if title:
                        self._video_title = title[:500]
                        logging.info("Instagram: Extracted title (%d chars): %s", len(title), title[:100] if len(title) > 100 else title)

            files = list(pathlib.Path(self._tempdir.name).glob("*"))
            if files:
                logging.info("Instagram: yt-dlp succeeded!")
                return [str(f) for f in files if f.is_file()]
        except Exception as e:
            logging.warning("Instagram: yt-dlp failed: %s", e)

        return []

    def _download_with_instaloader(self) -> list:
        """Fallback download using instaloader (supports login for restricted content)."""
        if not INSTALOADER_AVAILABLE:
            logging.warning("Instagram: instaloader not available for fallback")
            return []

        shortcode = self.extract_code()
        if not shortcode:
            logging.warning("Instagram: Could not extract shortcode from URL")
            return []

        try:
            logging.info("Instagram: Trying instaloader fallback with shortcode: %s", shortcode)
            
            L = instaloader.Instaloader(
                download_videos=True,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                dirname_pattern=self._tempdir.name,
                filename_pattern="{shortcode}",
            )

            # Load session if available (for age-restricted content)
            if INSTAGRAM_SESSION_FILE:
                try:
                    # Extract username from session file name (format: session-USERNAME)
                    import os
                    session_filename = os.path.basename(INSTAGRAM_SESSION_FILE)
                    if session_filename.startswith("session-"):
                        username = session_filename[8:]  # Remove "session-" prefix
                    else:
                        username = session_filename
                    
                    L.load_session_from_file(username, INSTAGRAM_SESSION_FILE)
                    logging.info("Instagram: Loaded session for user: %s", username)
                except Exception as e:
                    logging.warning("Instagram: Could not load session: %s", e)

            # Download the post
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            L.download_post(post, target="")

            files = list(pathlib.Path(self._tempdir.name).glob("*"))
            if files:
                logging.info("Instagram: instaloader succeeded!")
                # Filter out non-media files
                media_files = [str(f) for f in files if f.suffix.lower() in ('.mp4', '.jpg', '.jpeg', '.png', '.webp')]
                return media_files if media_files else [str(f) for f in files if f.is_file()]

        except Exception as e:
            logging.warning("Instagram: instaloader fallback failed: %s", e)

        return []

    def _ytdlp_progress_hook(self, d):
        """Progress hook for yt-dlp to update download status."""
        if d.get("status") == "downloading":
            try:
                self.download_hook({
                    "status": "downloading",
                    "downloaded_bytes": d.get("downloaded_bytes", 0),
                    "total_bytes": d.get("total_bytes") or d.get("total_bytes_estimate", 0),
                    "_speed_str": d.get("_speed_str", "N/A"),
                    "_eta_str": d.get("_eta_str", "N/A"),
                })
            except Exception:
                pass

    def _download(self, formats=None) -> list:
        """Download Instagram content, trying instaloader first (with session) then yt-dlp."""
        # Log session status
        if INSTAGRAM_SESSION_FILE:
            logging.info("Instagram: Session file configured: %s", INSTAGRAM_SESSION_FILE)
        else:
            logging.info("Instagram: No session file configured, some content may be restricted")
        
        # Try instaloader first (better for restricted content when logged in)
        files = self._download_with_instaloader()
        
        if files:
            return files

        # Fallback to yt-dlp if instaloader failed
        logging.info("Instagram: instaloader failed, trying yt-dlp fallback")
        files = self._download_with_ytdlp()

        if files:
            has_video = any(f.endswith(('.mp4', '.webm', '.mkv')) for f in files)
            has_image = any(f.endswith(('.jpg', '.jpeg', '.png', '.webp')) for f in files)
            
            if has_video:
                self._format = "video"
            elif has_image:
                self._format = "photo"
            else:
                self._format = "document"
            
            return files

        # Both methods failed
        error_msg = "הורדה מ-Instagram נכשלה!"
        self._bot_msg.edit_text(
            f"❌ {error_msg}\n\n"
            "התוכן מוגבל גיל או פרטי. נסה תוכן ציבורי אחר."
        )

        # Send error report to archive channel
        if ARCHIVE_CHANNEL:
            try:
                from database.model import get_user_stats
                user_info = get_user_stats(self._from_user)
                if user_info:
                    name = user_info.get('first_name') or ""
                    if user_info.get('username'):
                        name = f"{name} @{user_info['username']}".strip()
                    user_display = name if name else str(self._from_user)
                else:
                    user_display = str(self._from_user)

                report = (
                    f"❌ **דיווח שגיאה**\n"
                    f"👤 משתמש: {user_display}\n"
                    f"🆔 {self._from_user}\n"
                    f"🔗 קישור: {self._url}\n"
                    f"⚠️ שגיאה: {error_msg}"
                )
                self._client.send_message(
                    chat_id=ARCHIVE_CHANNEL,
                    text=report,
                    link_preview_options=types.LinkPreviewOptions(is_disabled=True)
                )
            except Exception as e:
                logging.warning("Failed to send error to archive: %s", e)

        return []

    def _get_archive_caption(self, files: list) -> str:
        """Create custom archive caption for Instagram."""
        from pathlib import Path
        from database.model import get_user_stats

        user_info = get_user_stats(self._from_user)
        if user_info:
            name = user_info.get('first_name') or ""
            if user_info.get('username'):
                name = f"{name} @{user_info['username']}".strip()
            user_display = name if name else str(self._from_user)
        else:
            user_display = str(self._from_user)

        filename = "Unknown"
        if files and len(files) > 0:
            filename = Path(files[0]).name

        return (
            f"👤 משתמש: {user_display}\n"
            f"🆔 {self._from_user}\n"
            f"📁 קובץ: {filename}\n"
            f"🔗 קישור: {self._url}"
        )

    def _start(self):
        """Start download and upload process with custom archive handling."""
        downloaded_files = self._download()
        if not downloaded_files:
            return

        from pathlib import Path

        files = [Path(f) for f in downloaded_files] if downloaded_files else list(Path(self._tempdir.name).glob("*"))
        meta = self.get_metadata()

        success = self._upload(files=downloaded_files, meta=meta, skip_archive=True)

        # Custom archive handling for Instagram
        if ARCHIVE_CHANNEL and success:
            try:
                msg_id = getattr(success, 'id', None)
                archive_caption = self._get_archive_caption(downloaded_files)

                logging.info("Instagram: Copying to archive with custom caption")
                self._client.copy_message(
                    chat_id=ARCHIVE_CHANNEL,
                    from_chat_id=self._chat_id,
                    message_id=msg_id,
                    caption=archive_caption
                )
                logging.info("Instagram: Forwarded to archive channel")
            except Exception as e:
                logging.error("Instagram: Failed to forward to archive: %s", e)
