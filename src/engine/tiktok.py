import logging
import pathlib
import subprocess
import requests
from typing import Optional, Tuple
from pyrogram import types

import yt_dlp

from config import ARCHIVE_CHANNEL, TIKTOK_COOKIES_FILE
from engine.base import BaseDownloader

# Check if gallery-dl is available for slideshow downloads
try:
    import gallery_dl
    GALLERY_DL_AVAILABLE = True
except ImportError:
    GALLERY_DL_AVAILABLE = False
    logging.info("gallery-dl not available, slideshow support will be limited")


def resolve_tiktok_url(url: str) -> str:
    """Resolve short TikTok URLs (vt.tiktok.com, vm.tiktok.com) to full URLs."""
    # Check if it's a short URL that needs resolving
    short_domains = ['vt.tiktok.com', 'vm.tiktok.com']
    
    if not any(domain in url for domain in short_domains):
        return url  # Already a full URL
    
    try:
        logging.info("TikTok: Resolving short URL: %s", url)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
        resolved_url = response.url
        
        # Clean up URL - remove query parameters for cleaner URL
        if "?" in resolved_url:
            resolved_url = resolved_url.split("?")[0]
        
        logging.info("TikTok: Resolved to: %s", resolved_url)
        return resolved_url
    except Exception as e:
        logging.warning("TikTok: Failed to resolve short URL, using original: %s", e)
        return url

# Try to import tiktokapipy, but make it optional
try:
    from tiktokapipy.api import TikTokAPI
    TIKTOKAPIPY_AVAILABLE = True
except Exception:
    TIKTOKAPIPY_AVAILABLE = False


def is_tiktok_slideshow(url: str) -> bool:
    """
    Check if a TikTok URL points to a slideshow (photo post) rather than a video.
    Slideshow URLs typically contain '/photo/' in the path.
    """
    return '/photo/' in url.lower()


class TikTokDownload(BaseDownloader):
    """Downloader for TikTok videos (yt-dlp) and slideshows (gallery-dl)."""

    def _setup_formats(self) -> list | None:
        """TikTok doesn't need format setup like YouTube."""
        return [None]

    def _download_with_ytdlp_url(self, url: str) -> list:
        """Try downloading with yt-dlp first."""
        from engine.generic import get_base_ytdlp_opts
        ydl_opts = get_base_ytdlp_opts(self._tempdir.name)
        
        # Add cookies if available
        if TIKTOK_COOKIES_FILE and pathlib.Path(TIKTOK_COOKIES_FILE).exists():
            logging.info("TikTok: Using cookies file for yt-dlp: %s", TIKTOK_COOKIES_FILE)
            ydl_opts["cookiefile"] = TIKTOK_COOKIES_FILE
        
        # Add subtitle options if user has subtitles enabled
        if self._subtitles:
            logging.info("TikTok: Subtitles enabled - will download if available")
            ydl_opts["writesubtitles"] = True
            ydl_opts["writeautomaticsub"] = True
            ydl_opts["subtitleslangs"] = ["en", "en-orig", "en-US"]
            ydl_opts["subtitlesformat"] = "srt"

        try:
            logging.info("TikTok: Trying yt-dlp with URL: %s", url)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Use extract_info with download=True to get title and download in one operation
                info = ydl.extract_info(url, download=True)
                if info:
                    # Get all possible title fields
                    title_field = info.get('title', '') or ''
                    desc_field = info.get('description', '') or ''
                    fulltitle_field = info.get('fulltitle', '') or ''
                    
                    # Debug: log available title fields
                    logging.info("TikTok DEBUG: title len=%d, description len=%d, fulltitle len=%d", 
                                len(title_field), len(desc_field), len(fulltitle_field))
                    
                    # Choose the longest one (description is usually the full text for TikTok)
                    title = max([title_field, desc_field, fulltitle_field], key=len)
                    
                    if title:
                        self._video_title = title[:500]
                        logging.info("TikTok: Using longest title (%d chars): %s", len(title), title[:100])
                    else:
                        logging.warning("TikTok: No title found in info dict!")

            files = list(pathlib.Path(self._tempdir.name).glob("*"))
            if files:
                logging.info("TikTok: yt-dlp succeeded!")
                return [str(f) for f in files if f.is_file()]
        except Exception as e:
            logging.warning("TikTok: yt-dlp failed: %s", e)

        return []

    def _download_slideshow_with_gallery_dl(self, url: str) -> Tuple[list, Optional[str]]:
        """
        Download TikTok slideshow using gallery-dl.
        Returns tuple of (image_files, audio_file) where audio_file may be None.
        """
        if not GALLERY_DL_AVAILABLE:
            logging.warning("TikTok: gallery-dl not available for slideshow")
            return [], None

        try:
            logging.info("TikTok: Downloading slideshow with gallery-dl: %s", url)
            
            # Use gallery-dl programmatically
            from gallery_dl import config, job
            
            # Configure gallery-dl
            config.clear()
            config.set(("extractor",), "base-directory", self._tempdir.name)
            config.set(("extractor",), "directory", [])  # No subdirectories
            config.set(("extractor", "tiktok"), "videos", True)  # Include audio
            
            # Add cookies if available
            if TIKTOK_COOKIES_FILE and pathlib.Path(TIKTOK_COOKIES_FILE).exists():
                logging.info("TikTok: Using cookies for gallery-dl: %s", TIKTOK_COOKIES_FILE)
                config.set(("extractor",), "cookies", [TIKTOK_COOKIES_FILE])
            
            # Run the download job
            download_job = job.DownloadJob(url)
            download_job.run()
            
            # Collect downloaded files
            all_files = list(pathlib.Path(self._tempdir.name).glob("*"))
            
            image_files = []
            audio_file = None
            
            for f in all_files:
                if f.is_file():
                    ext = f.suffix.lower()
                    if ext in ['.jpg', '.jpeg', '.png', '.webp']:
                        image_files.append(str(f))
                    elif ext in ['.mp3', '.m4a', '.aac', '.wav', '.ogg']:
                        audio_file = str(f)
            
            if image_files:
                logging.info("TikTok: gallery-dl found %d images and audio=%s", 
                           len(image_files), bool(audio_file))
                # Sort images by name to maintain order
                image_files.sort()
                return image_files, audio_file
                
        except Exception as e:
            logging.warning("TikTok: gallery-dl slideshow download failed: %s", e)

        return [], None

    def _download_with_tiktokapipy_url(self, url: str) -> list:
        """Fallback download using tiktokapipy."""
        if not TIKTOKAPIPY_AVAILABLE:
            logging.warning("TikTok: tiktokapipy not available for fallback")
            return []

        try:
            logging.info("TikTok: Trying tiktokapipy fallback with URL: %s", url)
            
            # tiktokapipy has sync API
            with TikTokAPI() as api:
                video = api.video(url)
                
                if video is None:
                    logging.warning("TikTok: tiktokapipy could not get video info")
                    return []
                
                # Get video bytes
                video_bytes = video.video.download()
                
                if video_bytes:
                    # Save to temp file
                    title = getattr(video, 'desc', 'tiktok_video')[:70] or 'tiktok_video'
                    # Clean title for filename
                    title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
                    if not title:
                        title = 'tiktok_video'
                    
                    output_path = pathlib.Path(self._tempdir.name) / f"{title}.mp4"
                    output_path.write_bytes(video_bytes)
                    
                    logging.info("TikTok: tiktokapipy succeeded!")
                    return [str(output_path)]
                    
        except Exception as e:
            logging.warning("TikTok: tiktokapipy fallback failed: %s", e)

        return []

    def _download(self, formats=None) -> list:
        """Download TikTok content - videos with yt-dlp, slideshows with gallery-dl."""
        # Store original URL for caption/display (user wants original link, not resolved redirect)
        self._original_url = self._url
        
        # Resolve short URLs to full URLs for download only
        self._resolved_url = resolve_tiktok_url(self._url)
        
        # Initialize slideshow-related attributes
        self._is_slideshow = False
        self._slideshow_images = []
        self._slideshow_audio = None
        
        # Check if this is a slideshow (photo post)
        if is_tiktok_slideshow(self._resolved_url):
            logging.info("TikTok: Detected slideshow URL, using gallery-dl")
            self._is_slideshow = True
            
            images, audio = self._download_slideshow_with_gallery_dl(self._resolved_url)
            if images:
                self._slideshow_images = images
                self._slideshow_audio = audio
                self._format = "photo"
                # Return all files (images + audio if exists)
                all_files = images.copy()
                if audio:
                    all_files.append(audio)
                return all_files
            
            # Slideshow detected but gallery-dl failed, try yt-dlp as fallback
            logging.warning("TikTok: gallery-dl failed for slideshow, trying yt-dlp")
        
        # Try yt-dlp for videos (or as fallback for slideshows)
        files = self._download_with_ytdlp_url(self._resolved_url)
        
        if files:
            self._format = "video"
            return files
        
        # Fallback to tiktokapipy
        files = self._download_with_tiktokapipy_url(self._resolved_url)
        
        if files:
            self._format = "video"
            return files
        
        # All methods failed - report to archive channel and show error to user
        error_msg = "הורדה מ-TikTok נכשלה!"
        self._bot_msg.edit_text(
            f"❌ {error_msg}\n\n"
            "TikTok חוסם הורדות לפעמים. נסה שוב מאוחר יותר."
        )
        
        # Send error report to archive channel
        if ARCHIVE_CHANNEL:
            try:
                from engine.helper import get_user_display_name
                user_display = get_user_display_name(self._from_user)
                
                report = (
                    f"❌ **דיווח שגיאה**\n"
                    f"👤 משתמש: {user_display}\n"
                    f"🆔 {self._from_user}\n"
                    f"🔗 קישור ישיר: {self._original_url}\n"
                    f"🔗 קישור מפורט: {self._resolved_url}\n"
                    f"⚠️ שגיאה: {error_msg}"
                )
                self._client.send_message(
                    chat_id=ARCHIVE_CHANNEL, 
                    text=report,
                    link_preview_options=types.LinkPreviewOptions(is_disabled=True)
                )
            except Exception as e:
                logging.warning("Failed to send error to archive: %s", e)
        
        return []  # Return empty, error already reported

    def _get_archive_caption(self, files: list) -> str:
        """Create custom archive caption with both TikTok URLs."""
        from pathlib import Path
        from engine.helper import get_user_display_name
        
        user_display = get_user_display_name(self._from_user)
        
        filename = "Unknown"
        if files and len(files) > 0:
            filename = Path(files[0]).name
        
        # Check if URLs are different (short vs resolved)
        if self._original_url != self._resolved_url:
            return (
                f"👤 משתמש: {user_display}\n"
                f"🆔 {self._from_user}\n"
                f"📁 קובץ: {filename}\n"
                f"🔗 קישור ישיר: {self._original_url}\n"
                f"🔗 קישור מפורט: {self._resolved_url}"
            )
        else:
            return (
                f"👤 משתמש: {user_display}\n"
                f"🆔 {self._from_user}\n"
                f"📁 קובץ: {filename}\n"
                f"🔗 קישור: {self._original_url}"
            )

    def _start(self):
        """Start download and upload process with custom archive handling."""
        downloaded_files = self._download()
        if not downloaded_files:
            return
        
        from pathlib import Path
        from pyrogram import types
        
        success = None
        
        # Handle slideshow (images + audio) differently from video
        if self._is_slideshow and self._slideshow_images:
            logging.info("TikTok: Uploading slideshow with %d images", len(self._slideshow_images))
            
            # Create caption for the images
            caption = f"🖼️ תמונות מ-TikTok\n🔗 {self._original_url}"
            
            # Send images as album (media group)
            if len(self._slideshow_images) > 1:
                # Build media group for multiple images
                media_group = []
                for i, img_path in enumerate(self._slideshow_images[:10]):  # Telegram limit: 10 items
                    if i == 0:
                        media_group.append(types.InputMediaPhoto(media=img_path, caption=caption))
                    else:
                        media_group.append(types.InputMediaPhoto(media=img_path))
                
                try:
                    success = self._client.send_media_group(
                        chat_id=self._chat_id,
                        media=media_group
                    )
                    if isinstance(success, list) and len(success) > 0:
                        success = success[0]  # Get first message for archive
                except Exception as e:
                    logging.error("TikTok: Failed to send image album: %s", e)
            else:
                # Single image
                try:
                    success = self._client.send_photo(
                        chat_id=self._chat_id,
                        photo=self._slideshow_images[0],
                        caption=caption
                    )
                except Exception as e:
                    logging.error("TikTok: Failed to send single image: %s", e)
            
            # Send audio file separately if available
            if self._slideshow_audio:
                try:
                    audio_caption = "🎵 אודיו מה-Slideshow"
                    self._client.send_audio(
                        chat_id=self._chat_id,
                        audio=self._slideshow_audio,
                        caption=audio_caption
                    )
                    logging.info("TikTok: Sent slideshow audio")
                except Exception as e:
                    logging.warning("TikTok: Failed to send audio: %s", e)
            
            # Only mark as complete and deduct credits if upload succeeded
            if success:
                # Calculate file sizes for credit deduction
                file_sizes = [Path(f).stat().st_size for f in downloaded_files if Path(f).exists()]
                self._remaining_credits = self._record_usage(file_sizes)
                remaining_text = f" | קרדיטים נותרים: {self._remaining_credits}" if self._remaining_credits else ""
                self._bot_msg.edit_text(f"✅ הושלם בהצלחה{remaining_text}")
            else:
                self._bot_msg.edit_text("❌ שליחת התמונות נכשלה")
        
        else:
            # Regular video upload
            files = [Path(f) for f in downloaded_files]
            meta = self.get_metadata()
            success = self._upload(files=downloaded_files, meta=meta, skip_archive=True)
        
        # Custom archive handling for TikTok with both URLs
        if ARCHIVE_CHANNEL and success:
            try:
                msg_id = getattr(success, 'id', None)
                archive_caption = self._get_archive_caption(downloaded_files)
                
                logging.info("TikTok: Copying to archive with custom caption")
                self._client.copy_message(
                    chat_id=ARCHIVE_CHANNEL,
                    from_chat_id=self._chat_id,
                    message_id=msg_id,
                    caption=archive_caption
                )
                logging.info("TikTok: Forwarded to archive channel")
            except Exception as e:
                logging.error("TikTok: Failed to forward to archive: %s", e)

