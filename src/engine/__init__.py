from urllib.parse import urlparse
from typing import Any, Callable

from engine.generic import YoutubeDownload
from engine.direct import DirectDownload
from engine.pixeldrain import pixeldrain_download
from engine.instagram import InstagramDownload
from engine.krakenfiles import krakenfiles_download
from engine.reddit import RedditDownload
from engine.tiktok import TikTokDownload
from engine.torrent import TorrentDownload
from engine.jdownloader import JDownloaderDownload


def googledrive_disabled(client: Any, bot_message: Any, url: str) -> None:
    """Temporarily disabled Google Drive downloads."""
    bot_message.edit_text(
        "❌ **הורדה מ-Google Drive מושבתת זמנית**\n\n"
        "אנחנו עובדים על תיקון הבעיה.\n"
        "בינתיים, אפשר להוריד ידנית מהקישור."
    )


def youtube_entrance(client, bot_message, url):
    youtube = YoutubeDownload(client, bot_message, url)
    youtube.start()


def youtube_entrance_with_quality(client, bot_message, url, quality: str):
    """Start YouTube download with specific quality selection.
    
    Args:
        quality: One of '1080', '720', '480', '360', 'audio'
    """
    youtube = YoutubeDownload(client, bot_message, url, selected_quality=quality)
    youtube.start()


def get_youtube_video_info(url: str) -> dict | None:
    """Extract video info without downloading.
    
    Returns dict with 'title' and 'duration' or None on error.
    """
    return YoutubeDownload.extract_info(url)


def direct_entrance(client, bot_message, url):
    dl = DirectDownload(client, bot_message, url)
    dl.start()


# --- Handler for the Instagram class, to make the interface consistent ---
def instagram_handler(client: Any, bot_message: Any, url: str) -> None:
    """A wrapper to handle the InstagramDownload class."""
    downloader = InstagramDownload(client, bot_message, url)
    downloader.start()


# --- Handler for Reddit ---
def reddit_handler(client: Any, bot_message: Any, url: str) -> None:
    """A wrapper to handle the RedditDownload class."""
    downloader = RedditDownload(client, bot_message, url)
    downloader.start()


# --- Handler for TikTok ---
def tiktok_handler(client: Any, bot_message: Any, url: str) -> None:
    """A wrapper to handle the TikTokDownload class."""
    downloader = TikTokDownload(client, bot_message, url)
    downloader.start()


# --- Handler for Torrents ---
def torrent_entrance(client: Any, bot_message: Any, source: str, torrent_file_path: str = None) -> None:
    """Start torrent download from magnet link or .torrent file.
    
    Args:
        client: Pyrogram client
        bot_message: Bot message for updates
        source: Magnet link (str) or identifier
        torrent_file_path: Path to .torrent file if uploaded
    """
    from pathlib import Path
    file_path = Path(torrent_file_path) if torrent_file_path else None
    downloader = TorrentDownload(client, bot_message, source, file_path)
    downloader.start()


def is_magnet_link(text: str) -> bool:
    """Check if text is a magnet link."""
    import re
    # Match magnet links with v1 (40 hex) or v2 (64 hex) hashes
    pattern = r'^magnet:\?xt=urn:btih:[a-fA-F0-9]{40,64}'
    return bool(re.match(pattern, text.strip()))


# --- Handler for JDownloader2 (last-resort fallback) ---
def jdownloader_entrance(client: Any, bot_message: Any, url: str) -> None:
    """Start download via JDownloader2 as last-resort fallback."""
    dl = JDownloaderDownload(client, bot_message, url)
    dl.start()


DOWNLOADER_MAP: dict[str, Callable[[Any, Any, str], Any]] = {
    "pixeldrain.com": pixeldrain_download,
    "krakenfiles.com": krakenfiles_download,
    "instagram.com": instagram_handler,
    "reddit.com": reddit_handler,
    "redd.it": reddit_handler,
    "tiktok.com": tiktok_handler,
    "vt.tiktok.com": tiktok_handler,
    # Google services (TEMPORARILY DISABLED)
    "drive.google.com": googledrive_disabled,
    "docs.google.com": googledrive_disabled,
    "sheets.google.com": googledrive_disabled,
    "slides.google.com": googledrive_disabled,
    "photos.google.com": googledrive_disabled,
}

# Media extensions to detect direct download links
MEDIA_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v",  # video
    ".mp3", ".m4a", ".flac", ".wav", ".aac", ".ogg", ".wma",  # audio
    ".zip", ".rar", ".7z", ".tar", ".gz",  # archives
)


def is_direct_download_url(url: str) -> bool:
    """Check if URL contains a media file extension (likely a direct download link)."""
    url_lower = url.lower()
    return any(ext in url_lower for ext in MEDIA_EXTENSIONS)


def special_download_entrance(client: Any, bot_message: Any, url: str) -> Any:
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            raise ValueError(f"לא ניתן לחלץ hostname של הקישור: {url}")
    except (ValueError, TypeError) as e:
        raise ValueError(f"פורמט קישור לא תקין: {url}") from e

    # Handle the special case for YouTube URLs first.
    if hostname.endswith("youtube.com") or hostname == "youtu.be":
        raise ValueError("לקישורי יוטיוב, פשוט שלח את הקישור ישירות.")

    # Iterate through the map to find a matching handler.
    for domain_suffix, handler_function in DOWNLOADER_MAP.items():
        if hostname.endswith(domain_suffix):
            return handler_function(client, bot_message, url)

    # Check if URL contains media extension - use direct download
    if is_direct_download_url(url):
        return direct_entrance(client, bot_message, url)

    raise ValueError(f"לא נמצא מוריד מתאים עבור: {hostname}")
