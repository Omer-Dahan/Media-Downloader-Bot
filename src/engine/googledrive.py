"""Google Drive downloader using gdown library with curl_cffi fallback."""
import logging
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any


from engine.base import BaseDownloader


def googledrive_download(client: Any, bot_message: Any, url: str) -> None:
    """Download files from Google Drive using gdown library."""
    downloader = GoogleDriveDownload(client, bot_message, url)
    downloader.start()


class GoogleDriveDownload(BaseDownloader):
    """Downloader for Google Drive files using gdown with curl_cffi fallback."""
    
    # Video and audio extensions for smart format detection
    VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.m4v', '.wmv'}
    AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.flac'}
    
    # Pattern to extract file ID from various Google URL formats
    FILE_ID_PATTERNS = [
        # Google Drive
        r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)",  # /file/d/ID format
        r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)",  # open?id=ID format
        r"drive\.google\.com/uc\?id=([a-zA-Z0-9_-]+)",  # uc?id=ID format
        # Google Docs/Sheets/Slides
        r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)",  # Google Docs
        r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)",  # Google Sheets
        r"docs\.google\.com/presentation/d/([a-zA-Z0-9_-]+)",  # Google Slides
        r"sheets\.google\.com/.*?/d/([a-zA-Z0-9_-]+)",  # sheets.google.com
        r"slides\.google\.com/.*?/d/([a-zA-Z0-9_-]+)",  # slides.google.com
        # Google Photos
        r"photos\.google\.com/.*?/([a-zA-Z0-9_-]+)",  # Google Photos
        # Generic fallback
        r"/d/([a-zA-Z0-9_-]+)",  # Generic /d/ID format
        r"id=([a-zA-Z0-9_-]+)",  # Generic id= parameter
    ]
    
    # Export formats for Google Docs types
    EXPORT_FORMATS = {
        "document": ("application/pdf", ".pdf"),
        "spreadsheets": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
        "presentation": ("application/pdf", ".pdf"),
    }
    
    def _extract_file_id(self) -> str:
        """Extract file ID from Google Drive URL."""
        for pattern in self.FILE_ID_PATTERNS:
            match = re.search(pattern, self._url)
            if match:
                return match.group(1)
        raise ValueError("לא ניתן לחלץ מזהה קובץ מהקישור של Google Drive")
    
    def _get_doc_type(self) -> str | None:
        """Detect if this is a Google Docs/Sheets/Slides URL."""
        if "docs.google.com/document" in self._url:
            return "document"
        elif "docs.google.com/spreadsheets" in self._url or "sheets.google.com" in self._url:
            return "spreadsheets"
        elif "docs.google.com/presentation" in self._url or "slides.google.com" in self._url:
            return "presentation"
        return None
    
    def _setup_formats(self) -> list | None:
        """Google Drive doesn't need format setup."""
        return None
    
    def get_metadata(self):
        """Override to provide simple metadata for non-video files."""
        # For Google Drive, use simple metadata without ffprobe
        files = list(Path(self._tempdir.name).rglob("*"))
        files = [f for f in files if f.is_file() and not f.suffix.lower() == '.part']
        
        if not files:
            return {"caption": f"🔗 מקור:\n{self._url}"}
        
        file_path = files[0]
        filename = file_path.name
        file_ext = file_path.suffix.lower()
        file_size = file_path.stat().st_size
        file_size_mb = file_size / (1024 * 1024)
        
        # Only use ffprobe for actual video/audio files
        if file_ext in self.VIDEO_EXTENSIONS or file_ext in self.AUDIO_EXTENSIONS:
            # Use parent's method for media files
            return super().get_metadata()
        
        # Simple caption for documents (no resolution/duration)
        credits_line = ""
        if hasattr(self, '_remaining_credits') and self._remaining_credits is not None:
            credits_line = f"\nקרדיטים נותרים: {self._remaining_credits} 💳"
        
        caption = (
            f"📄 {filename}\n\n"
            f"🔗 מקור:\n{self._url}\n"
            f"📦 גודל: {file_size_mb:.1f} MB"
            f"{credits_line}"
        )
        
        return {"caption": caption, "thumb": None}
    
    def _download_with_curl(self, file_id: str, output_dir: Path) -> Path | None:
        """Try downloading with curl_cffi (Chrome impersonation)."""
        try:
            from curl_cffi import requests as curl_requests
            
            logging.info("[Google Drive] Trying curl_cffi with Chrome impersonation...")
            
            # Check if this is a Google Docs type that needs export
            doc_type = self._get_doc_type()
            if doc_type and doc_type in self.EXPORT_FORMATS:
                mime_type, ext = self.EXPORT_FORMATS[doc_type]
                download_url = f"https://docs.google.com/{doc_type}/d/{file_id}/export?format={ext.replace('.', '')}"
                logging.info("Using export URL for %s: %s", doc_type, download_url)
            else:
                # Regular file download
                download_url = f"https://drive.google.com/uc?id={file_id}&export=download&confirm=t"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            
            response = curl_requests.get(
                download_url,
                headers=headers,
                impersonate="chrome",
                timeout=300,
                allow_redirects=True,
            )
            
            if response.status_code >= 400:
                logging.error("curl_cffi failed with status %d", response.status_code)
                return None
            
            # Check if Google returned an HTML error page instead of the file
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type and len(response.content) < 50000:
                content_preview = response.content[:500].decode('utf-8', errors='ignore')
                if "error" in content_preview.lower() or "denied" in content_preview.lower():
                    logging.error("Google returned HTML error page")
                    return None
            
            # Verify content size
            if len(response.content) < 100:
                logging.error("Content too small: %d bytes", len(response.content))
                return None

            
            # Get filename from Content-Disposition header
            content_disp = response.headers.get("Content-Disposition", "")
            content_type = response.headers.get("Content-Type", "")
            filename = None
            
            # Try different patterns to extract filename
            # Pattern 1: filename*=UTF-8''encoded_name (RFC 5987)
            utf8_match = re.search(r"filename\*=UTF-8''([^;\s]+)", content_disp, re.IGNORECASE)
            if utf8_match:
                from urllib.parse import unquote
                filename = unquote(utf8_match.group(1))
                logging.info("Extracted UTF-8 filename: %s", filename)
            
            # Pattern 2: filename="name" or filename=name
            if not filename:
                basic_match = re.search(r'filename\s*=\s*"?([^";\r\n]+)"?', content_disp)
                if basic_match:
                    filename = basic_match.group(1).strip()
                    from urllib.parse import unquote
                    filename = unquote(filename)
                    logging.info("Extracted basic filename: %s", filename)
            
            # Fallback: generate filename based on doc type or file ID
            if not filename:
                if doc_type:
                    # Use default name for exported docs
                    _, ext = self.EXPORT_FORMATS[doc_type]
                    filename = f"google_{doc_type}_{file_id[:8]}{ext}"
                else:
                    # Use mimetypes to guess extension from Content-Type
                    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
                    filename = f"gdrive_{file_id[:8]}{ext}"
                logging.info("Generated fallback filename: %s", filename)
            
            output_path = output_dir / filename
            output_path.write_bytes(response.content)
            
            logging.info("curl_cffi download successful: %s (%d bytes)", filename, len(response.content))
            return output_path
            
        except ImportError:
            logging.warning("curl_cffi not available")
            return None
        except Exception as e:
            logging.error("curl_cffi download failed: %s", e)
            return None
    
    def _download(self, formats=None) -> list:
        """Download from Google Drive using gdown with curl_cffi fallback."""
        file_id = self._extract_file_id()
        logging.info("Extracted Google Drive file ID: %s", file_id)
        
        # Create unique subfolder in temp directory to avoid conflicts
        output_dir = Path(self._tempdir.name) / uuid.uuid4().hex
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Clean up any leftover .part files
        for part_file in Path(self._tempdir.name).rglob("*.part"):
            try:
                part_file.unlink()
            except Exception:
                pass
        
        # Show downloading message
        self.edit_text("📥 **מוריד מ-Google...**\n\n⏳ נא להמתין...")
        
        # Use curl_cffi directly (faster and more reliable than gdown)
        output_file = self._download_with_curl(file_id, output_dir)
        
        # Check if any method succeeded
        if output_file is None or not output_file.exists():
            # Last resort: check if any file was downloaded
            files_in_dir = list(output_dir.glob("*"))
            if files_in_dir:
                output_file = files_in_dir[0]
            else:
                raise ValueError(
                    "❌ **הורדה מ-Google נכשלה**\n\n"
                    "ייתכן שהקובץ:\n"
                    "• לא משותף ל'כל מי שיש לו את הקישור'\n"
                    "• נמחק או לא קיים\n"
                    "• חוסם הורדות אוטומטיות\n\n"
                    "בקש מבעל הקובץ לוודא שההגדרות מאפשרות הורדה."
                )
        
        # Detect file type and set format accordingly
        file_ext = output_file.suffix.lower()
        if file_ext in self.VIDEO_EXTENSIONS:
            self._format = "video"
            logging.info("Detected video file: %s, sending as video", file_ext)
        elif file_ext in self.AUDIO_EXTENSIONS:
            self._format = "audio"
            logging.info("Detected audio file: %s, sending as audio", file_ext)
        else:
            self._format = "document"
            logging.info("File type: %s, sending as document", file_ext)
        
        file_size_mb = output_file.stat().st_size / (1024 * 1024)
        logging.info("Google download complete: %s (%.1f MB)", output_file.name, file_size_mb)
        
        self.edit_text(f"✅ ההורדה הושלמה ({file_size_mb:.1f} MB)\n⏳ מעלה לטלגרם...")
        
        return [str(output_file)]
    
    def _start(self):
        """Start the download and upload process."""
        try:
            files = self._download()
            if files:
                # Record credits based on file sizes (for all file types, not just media)
                file_sizes = [Path(f).stat().st_size for f in files]
                self._remaining_credits = self._record_usage(file_sizes)
                self._upload(files=files)
        except ValueError as e:
            try:
                self._bot_msg.edit_text(str(e))
            except Exception:
                logging.error("Failed to send error message to user")
