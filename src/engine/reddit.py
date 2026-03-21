import logging
import pathlib
import requests
import yt_dlp

from RedDownloader import RedDownloader as RD
from engine.base import BaseDownloader
from engine.helper import extract_metadata_from_info


class RedditDownload(BaseDownloader):
    """Downloader for Reddit videos, images, and galleries using RedDownloader with yt-dlp fallback."""

    def _resolve_share_link(self, url: str) -> str:
        """Resolve Reddit share links (/s/) to actual post URLs."""
        if "/s/" in url:
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
                response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
                resolved_url = response.url
                # Clean up URL - remove query parameters for cleaner URL
                if "?" in resolved_url:
                    resolved_url = resolved_url.split("?")[0]
                logging.info("Resolved Reddit share link: %s -> %s", url, resolved_url)
                return resolved_url
            except Exception as e:
                logging.warning("Failed to resolve share link, using original: %s", e)
                return url
        return url

    def _setup_formats(self) -> list | None:
        pass

    def _download_with_ytdlp(self, url: str) -> list:
        """Fallback download using yt-dlp with Reddit-specific headers."""
        from engine.generic import get_base_ytdlp_opts
        ydl_opts = get_base_ytdlp_opts(self._tempdir.name)
        ydl_opts.update({
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            "extractor_args": {"reddit": ["player-client=android,web"]},
        })
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    self._format = "video"
                    meta = extract_metadata_from_info(info)
                    if meta.get("title"):
                        self._video_title = meta["title"]
                        self._video_description = meta["description"]
                        logging.info("Reddit: Extracted title (%d chars): %s", len(self._video_title), self._video_title[:100])
                    
            # Find downloaded files recursively
            return [str(f) for f in pathlib.Path(self._tempdir.name).rglob("*") if f.is_file()]
        except Exception as e:
            logging.error("yt-dlp fallback failed: %s", e)
            return []

    def _download(self, formats=None) -> list:
        """Download media from Reddit URL using RedDownloader, with yt-dlp fallback."""
        # Resolve share links first
        url = self._resolve_share_link(self._url)
        
        video_paths = []
        media_type = None
        
        # Try RedDownloader first
        try:
            downloader = RD.Download(
                url=url,
                output="reddit_media",
                destination=self._tempdir.name,
                quality=1080
            )
            
            try:
                media_type = downloader.GetMediaType()
                logging.info("Reddit media type: %s", media_type)
            except AttributeError:
                logging.warning("Could not get media type from RedDownloader")
            
            # Find all files in temp dir recursively, filtering for files only
            for file in pathlib.Path(self._tempdir.name).rglob("*"):
                if file.is_file():
                    video_paths.append(str(file))
                        
        except Exception as e:
            logging.warning("RedDownloader failed: %s", e)
        
        # If RedDownloader didn't get any files, try yt-dlp
        if not video_paths:
            logging.info("RedDownloader found no files, trying yt-dlp fallback...")
            video_paths = self._download_with_ytdlp(url)
        
        if not video_paths:
            self._bot_msg.edit_text("❌ הורדה מ-Reddit נכשלה!\n\nלא נמצא תוכן מדיה בפוסט.")
            return []
        
        # Determine format based on media type or file extension
        if media_type == "v":
            self._format = "video"
        elif media_type == "i":
            self._format = "photo"
        elif media_type == "g":
            self._format = "photo"
        elif media_type == "gif":
            self._format = "video"
        elif not hasattr(self, '_format') or self._format is None:
            # Detect from file extension
            ext = pathlib.Path(video_paths[0]).suffix.lower()
            if ext in [".mp4", ".webm", ".mov", ".avi"]:
                self._format = "video"
            elif ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
                self._format = "photo"
            else:
                self._format = "document"

        logging.info("Reddit downloaded files: %s", video_paths)
        return video_paths

    def _start(self):
        """Start download and upload process."""
        downloaded_files = self._download()
        if downloaded_files:
            self._upload(files=downloaded_files)
