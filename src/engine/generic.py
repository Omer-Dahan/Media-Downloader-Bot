from typing import Any
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
import shutil

import yt_dlp

from config import AUDIO_FORMAT, ARCHIVE_CHANNEL, ENABLE_ARIA2
from utils import is_youtube
from database.model import (
    get_format_settings,
    get_quality_settings,
    get_total_credits,
    CreditsExhaustedException,
)
from engine.base import BaseDownloader, ClassifiedDownloadError
from engine.helper import extract_metadata_from_info
from engine.network_errors import NetworkError, is_network_error

# Get absolute path to cookies file (relative to src directory)
_SCRIPT_DIR = Path(__file__).parent.parent
COOKIES_PATH = _SCRIPT_DIR / "youtube-cookies.txt"

# Track if we've already tried updating yt-dlp in this session
_ytdlp_update_attempted = False

# File to store update info for notification after restart
UPDATE_FLAG_FILE = _SCRIPT_DIR / ".ytdlp_updated"


def check_and_ensure_js_runtime() -> dict[str, Any]:
    """
    Check if a supported JavaScript runtime (node, deno, quickjs/qjs, bun) is available for yt-dlp.
    Augments PATH with common installation directories (~/.local/bin, /usr/local/bin, ~/.deno/bin, etc.)
    and logs a detailed warning if no supported runtime is found.
    """
    import shutil

    # Check common search paths used on Linux servers and user installations
    common_search_dirs = [
        Path(os.path.expanduser("~/.local/bin")),
        Path("/usr/local/bin"),
        Path(os.path.expanduser("~/.deno/bin")),
        Path(os.path.expanduser("~/.bun/bin")),
        Path(os.path.expanduser("~/.nvm/current/bin")),
    ]
    if sys.platform == "win32":
        common_search_dirs.extend([
            Path(r"C:\Program Files\nodejs"),
            Path(r"C:\Program Files (x86)\nodejs"),
        ])

    current_path_entries = set(os.environ.get("PATH", "").split(os.pathsep))
    added_dirs = []
    for d in common_search_dirs:
        if d.exists() and d.is_dir() and str(d) not in current_path_entries:
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            current_path_entries.add(str(d))
            added_dirs.append(str(d))

    if added_dirs:
        logging.info("Augmented PATH with common tool directories: %s", added_dirs)

    # Check runtimes using yt-dlp internal jsruntime detection
    candidates = []
    try:
        from yt_dlp.utils._jsruntime import (
            DenoJsRuntime,
            NodeJsRuntime,
            BunJsRuntime,
            QuickJsRuntime,
        )
        candidates = [
            ("node", NodeJsRuntime()),
            ("deno", DenoJsRuntime()),
            ("quickjs", QuickJsRuntime()),
            ("bun", BunJsRuntime()),
        ]
    except Exception as e:
        logging.debug("Could not import yt-dlp _jsruntime classes: %s", e)

    supported_runtimes = []
    unsupported_runtimes = []

    for name, r in candidates:
        try:
            info = r.info
            if info:
                if info.supported:
                    supported_runtimes.append(info)
                else:
                    unsupported_runtimes.append(info)
        except Exception:
            pass

    # Fallback to shutil.which if _jsruntime failed
    if not supported_runtimes and not unsupported_runtimes:
        for bin_name in ("node", "deno", "qjs", "quickjs", "bun"):
            found_path = shutil.which(bin_name)
            if found_path:
                logging.info("Found JavaScript runtime binary at %s", found_path)
                return {"status": "ok", "path": found_path}

    if supported_runtimes:
        best = supported_runtimes[0]
        logging.info(
            "Found supported JavaScript runtime for yt-dlp: %s (version: %s, path: %s)",
            best.name,
            best.version,
            best.path,
        )
        return {"status": "ok", "runtime": best}

    if unsupported_runtimes:
        first_unsupp = unsupported_runtimes[0]
        logging.warning(
            "JavaScript runtime '%s' found at %s (version %s), but this version is not supported "
            "by yt-dlp. YouTube downloads may fail with 'The page needs to be reloaded'. "
            "Please upgrade Node.js (>= 22.0.0) or Deno (>= 2.3.0).",
            first_unsupp.name,
            first_unsupp.path,
            first_unsupp.version,
        )
        return {"status": "unsupported", "runtime": first_unsupp}

    logging.warning(
        "No supported JavaScript runtime found in PATH or common search paths. "
        "Checked: deno, node, quickjs (qjs), bun. "
        "Search paths checked: %s. "
        "yt-dlp requires a JavaScript runtime (such as Node.js >= 22.0.0 or Deno >= 2.3.0) "
        "to solve YouTube n-challenge signatures. Without it, YouTube downloads will fail with "
        "'The page needs to be reloaded'. Please install Node.js or Deno on the server "
        "and ensure it is accessible in PATH.",
        [str(d) for d in common_search_dirs],
    )
    return {"status": "missing", "runtime": None}


def _ensure_node_in_path():
    """Backwards-compatible wrapper for check_and_ensure_js_runtime."""
    return check_and_ensure_js_runtime()


# Run this check immediately when module loads
check_and_ensure_js_runtime()


def get_ytdlp_version() -> str:
    """Get the current installed version of yt-dlp."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in result.stdout.split("\n"):
            if line.startswith("Version:"):
                return line.split(":")[1].strip()
    except Exception as e:
        logging.error("Failed to get yt-dlp version: %s", e)
    return "unknown"


def check_ytdlp_update_available() -> tuple[bool, str, str]:
    """Check if a yt-dlp update is available.
    Returns (update_available, current_version, latest_version)
    """
    current_version = get_ytdlp_version()

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "index", "versions", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Parse output to get latest version
        output = result.stdout + result.stderr
        # Look for "Available versions:" or version numbers
        import re

        versions = re.findall(r"(\d+\.\d+\.\d+)", output)
        if versions:
            latest_version = versions[0]  # First match is usually the latest
            return current_version != latest_version, current_version, latest_version
    except Exception as e:
        logging.error("Failed to check for yt-dlp updates: %s", e)

    return False, current_version, "unknown"


def save_update_info(old_version: str, new_version: str):
    """Save update info to file for notification after restart."""
    try:
        import json
        from datetime import datetime

        update_info = {
            "old_version": old_version,
            "new_version": new_version,
            "timestamp": datetime.now().isoformat(),
        }
        UPDATE_FLAG_FILE.write_text(json.dumps(update_info, ensure_ascii=False))
        logging.info("Saved update info to %s", UPDATE_FLAG_FILE)
    except Exception as e:
        logging.error("Failed to save update info: %s", e)


def get_base_ytdlp_opts(tempdir_name: str) -> dict:
    """Returns the base yt-dlp options generally used across extraction helpers."""
    import pathlib

    output = pathlib.Path(tempdir_name, "%(title).70s.%(ext)s").as_posix()
    return {
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
    }


def run_ytdlp_pip_upgrade() -> tuple[bool, str]:
    """Runs pip install --upgrade yt-dlp.
    Returns (success_bool, pip_output_string)."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
        capture_output=True,
        text=True,
        timeout=120,  # 2 minutes timeout for download+install
    )
    output = result.stdout + result.stderr
    is_success = "Successfully installed" in output and "yt-dlp" in output
    return is_success, output


def auto_update_ytdlp() -> bool:
    """Check for and install yt-dlp updates automatically on startup.

    Returns True if an update was installed and bot should restart, False otherwise.
    """
    global _ytdlp_update_attempted
    # Don't check again if already checked in this process (e.g. triggered by error)
    if _ytdlp_update_attempted:
        return False

    current_version = get_ytdlp_version()

    # First check if update is available via pip index (fast)
    update_available, _, latest = check_ytdlp_update_available()

    if not update_available:
        logging.info("✅ yt-dlp is up to date (%s)", current_version)
        _ytdlp_update_attempted = True
        return False

    logging.info(
        "🔍 New yt-dlp version found: %s -> %s. Updating...", current_version, latest
    )

    try:
        _ytdlp_update_attempted = True
        is_success, _ = run_ytdlp_pip_upgrade()

        # Verify with actual installed version check
        new_version = get_ytdlp_version()

        if is_success and new_version != current_version:
            logging.info(
                "✅ yt-dlp updated successfully! %s → %s", current_version, new_version
            )
            save_update_info(current_version, new_version)
            # On startup, we should restart to ensure the new library is loaded
            restart_bot()
            return True
        logging.info(
            "✅ yt-dlp check finished, no version change detected (current: %s)",
            new_version,
        )
        return False

    except subprocess.TimeoutExpired:
        logging.warning("⚠️ yt-dlp update check timed out")
        return False
    except Exception as e:
        logging.error("❌ Failed to update yt-dlp: %s", e)
        return False


def check_and_send_update_notification(client):
    """Check if bot was restarted after update and send notification.

    Call this from main.py after bot starts.
    """
    if not UPDATE_FLAG_FILE.exists():
        return

    try:
        import json

        update_info = json.loads(UPDATE_FLAG_FILE.read_text())
        UPDATE_FLAG_FILE.unlink()  # Delete the flag file

        old_ver = update_info.get("old_version", "unknown")
        new_ver = update_info.get("new_version", "unknown")
        timestamp = update_info.get("timestamp", "unknown")

        message = (
            f"🔄 **עדכון yt-dlp הושלם!**\n\n"
            f"📦 גרסה קודמת: `{old_ver}`\n"
            f"📦 גרסה חדשה: `{new_ver}`\n"
            f"⏰ זמן עדכון: {timestamp}\n\n"
            f"✅ הבוט הופעל מחדש בהצלחה!"
        )

        if ARCHIVE_CHANNEL:
            client.send_message(chat_id=ARCHIVE_CHANNEL, text=message)
            logging.info("Sent update notification to archive channel")
        else:
            logging.info("No archive channel configured, skipping notification")

    except Exception as e:
        logging.error("Failed to send update notification: %s", e)
        # Clean up flag file even if notification fails
        try:
            UPDATE_FLAG_FILE.unlink()
        except Exception as del_error:
            logging.warning("Failed to delete update flag file: %s", del_error)


def restart_bot():
    """Restart the bot process."""
    logging.info("Restarting bot process...")
    try:
        from utils.process_lock import release_process_lock

        release_process_lock()
    except Exception:
        pass
    try:
        # Use os.execv to replace the current process with a new one
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logging.error("Failed to restart bot: %s", e)
        # Fallback: try to exit gracefully so supervisor/systemd can restart
        logging.info("Attempting graceful exit for external restart...")
        os._exit(1)


def try_update_ytdlp() -> bool:
    """Try to update yt-dlp to the latest version.
    If an update is available, installs it and restarts the bot.
    Returns True if update was installed (bot will restart), False otherwise.
    """
    # Simply call the auto_update_ytdlp function which now handles both cases robustly
    return auto_update_ytdlp()


def is_extraction_error(error_msg: str) -> bool:
    """Check if the error is an extraction error that might be fixed by updating."""
    extraction_errors = [
        "Unable to extract",
        "unable to extract",
        "Unsupported URL",
        "This video is not available",
        "Video unavailable",
        "ExtractorError",
    ]
    return any(err in str(error_msg) for err in extraction_errors)


def match_filter(info_dict):
    if info_dict.get("is_live"):
        raise NotImplementedError("לא ניתן להוריד שידור חי")
    return None  # Allow download for non-live videos



class YtDlpLogger:
    """Logger for yt-dlp that captures warnings and errors."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def debug(self, msg: str):
        pass

    def warning(self, msg: str):
        self.warnings.append(str(msg))
        logging.warning("[yt-dlp] %s", msg)

    def error(self, msg: str):
        self.errors.append(str(msg))
        logging.error("[yt-dlp] %s", msg)


class ClassifiedMessage(str):
    """A string subclass tracking whether a message is classified and safe for user display."""
    is_safe: bool = True

    def __new__(cls, content: str, is_safe: bool = True):
        obj = super().__new__(cls, content)
        obj.is_safe = is_safe
        return obj


def is_playlist_url(url: str) -> bool:
    """Check if the URL is a playlist or channel (multi-item source)."""
    if not url or not isinstance(url, str):
        return False
    try:
        normalized_url = url if "://" in url else f"https://{url}"
        parsed = urlparse(normalized_url)
        query = parsed.query or ""
        path = parsed.path or ""

        # Existing playlist behavior across platforms
        if "list=" in query:
            return True
        if path.startswith("/playlist") or "/playlist" in path:
            return True

        # Restrict channel / handle / custom URLs to YouTube domains
        hostname = (parsed.hostname or "").lower()
        is_yt_domain = hostname in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
            "music.youtube.com",
            "www.youtube-nocookie.com",
            "youtube-nocookie.com",
        } or is_youtube(normalized_url)

        if is_yt_domain:
            channel_prefixes = (
                "/channel/",
                "/@",
                "/c/",
                "/user/",
            )
            for prefix in channel_prefixes:
                if path.startswith(prefix) or prefix in path:
                    return True

        return False
    except Exception:
        return False


def is_gallery_dl_available() -> bool:
    """Check if gallery-dl is installed and executable."""
    import shutil
    if shutil.which("gallery-dl"):
        return True
    venv_bin = Path(sys.executable).parent / "gallery-dl"
    if venv_bin.is_file() and os.access(venv_bin, os.X_OK):
        return True
    try:
        import importlib.util
        return importlib.util.find_spec("gallery_dl") is not None
    except Exception:
        return False


def get_gallery_dl_cmd() -> list[str]:
    """Get the command to execute gallery-dl."""
    import shutil
    if shutil.which("gallery-dl"):
        return ["gallery-dl"]
    venv_bin = Path(sys.executable).parent / "gallery-dl"
    if venv_bin.is_file() and os.access(venv_bin, os.X_OK):
        return [str(venv_bin)]
    return [sys.executable, "-m", "gallery_dl"]


def classify_download_error(error_msg: str | None, url: str = "") -> ClassifiedMessage:
    """Classify the error message into a human-readable Hebrew message with accurate cause."""
    if not error_msg:
        return ClassifiedMessage("ההורדה נכשלה: לא התקבל קובץ מדיה.", is_safe=True)

    err_lower = error_msg.lower()

    # YouTube JavaScript runtime / n-challenge solver missing
    if any(k in err_lower for k in [
        "the page needs to be reloaded",
        "n challenge solving failed",
        "challenge solver script",
        "supported javascript runtime",
        "signature solving failed",
    ]):
        return ClassifiedMessage(
            "שגיאת פענוח ביוטיוב: חסר בשרת runtime של JavaScript (כגון Node.js או Deno) הנדרש לפענוח חתימות יוטיוב (n challenge solver).\n"
            "יש להתקין בשרת Node.js (גרסה 22 ומעלה) או Deno.",
            is_safe=True,
        )

    # Bot detection / Captcha / Sign-in required
    if any(k in err_lower for k in [
        "sign in to confirm you're not a bot",
        "confirm you're not a bot",
        "bot detection",
        "automated queries",
        "unusual traffic",
        "sign in to confirm your age",
        "requires authentication",
        "login required",
        "http error 429",
    ]):
        return ClassifiedMessage(
            "ההורדה מיוטיוב נחסמה (זיהוי בוט / נדרש אימות).\nיש לעדכן את קובץ ה-cookies בשרת או להמתין להסרת החסימה.",
            is_safe=True,
        )

    # Cookie specific errors
    if any(k in err_lower for k in ["cookie", "cookies"]) and not any(k in err_lower for k in ["not a bot"]):
        return ClassifiedMessage(
            "שגיאת אימות מול יוטיוב: קובץ ה-cookies אינו תקין או שפג תוקפו.\nיש לרענן את קובץ ה-cookies בשרת.",
            is_safe=True,
        )

    # Private / unavailable / deleted video
    if any(k in err_lower for k in [
        "this video is unavailable",
        "video unavailable",
        "this video is private",
        "private video",
        "has been removed",
        "members-only content",
        "who has blocked you",
    ]):
        return ClassifiedMessage(
            "הסרטון אינו זמין (סרטון פרטי, נמחק, או דורש מנוי ערוץ).",
            is_safe=True,
        )

    # Geo restriction
    if any(k in err_lower for k in [
        "not available in your country",
        "available in your country",
        "geographic restriction",
        "blocked in your country",
        "georestricted",
    ]):
        return ClassifiedMessage(
            "הסרטון חסום לצפייה במדינה שבה נמצא השרת (הגבלה גיאוגרפית).",
            is_safe=True,
        )

    # Live stream
    if any(k in err_lower for k in ["שידור חי", "live stream"]):
        return ClassifiedMessage(
            "לא ניתן להוריד שידור חי פעיל.",
            is_safe=True,
        )

    # Format issues
    if any(k in err_lower for k in [
        "requested format is not available",
        "no video formats found",
        "format not available",
    ]):
        return ClassifiedMessage(
            "ההורדה נכשלה: הפורמט המבוקש אינו זמין עבור סרטון זה.",
            is_safe=True,
        )

    # General extraction errors where yt-dlp might actually be outdated
    if is_extraction_error(error_msg):
        return ClassifiedMessage(
            "שגיאה בחילוץ המידע מהקישור. ייתכן ש-yt-dlp דורש עדכון.",
            is_safe=True,
        )

    return ClassifiedMessage(
        f"ההורדה נכשלה: {error_msg[:150]}",
        is_safe=False,
    )


class YoutubeDownload(BaseDownloader):

    def __init__(self, client, bot_msg, url, selected_quality: str = None):
        """Initialize YoutubeDownload.

        Args:
            selected_quality: Optional quality selected by user ('1080', '720', '480', '360', 'audio')
        """
        super().__init__(client, bot_msg, url)
        self._selected_quality = selected_quality
        # Override format immediately if audio is selected (important for cache hits)
        if selected_quality == "audio":
            self._format = "audio"
        # Include selected quality in cache key to prevent returning wrong quality from cache
        if selected_quality:
            self._quality = f"{self._quality}:{selected_quality}"

    @staticmethod
    def extract_info(url: str) -> dict | None:
        """Extract video info without downloading.

        Returns dict with 'title' and 'duration' or None on error.
        """
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            # Enable Node.js runtime for YouTube JS challenge solving
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs:github": {}},
        }
        # Setup cookies for youtube
        if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 100:
            ydl_opts["cookiefile"] = str(COOKIES_PATH)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    duration_seconds = info.get("duration", 0)
                    minutes = duration_seconds // 60
                    seconds = duration_seconds % 60
                    duration_str = f"{minutes}:{seconds:02d}"
                    return {
                        "title": info.get("title", "Unknown"),
                        "duration": duration_str,
                        "duration_seconds": duration_seconds,
                    }
        except Exception as e:
            logging.error("Failed to extract video info: %s", e)
        return None

    @staticmethod
    def get_format(m):
        return [
            f"bestvideo[ext=mp4][height={m}]+bestaudio[ext=m4a]",
            f"bestvideo[vcodec^=avc][height={m}]+bestaudio[acodec^=mp4a]/best[vcodec^=avc]/best",
        ]

    def _setup_formats(self) -> list | None:
        if not is_youtube(self._url):
            return [None]

        # If user selected a specific quality via buttons, use that
        if self._selected_quality:
            audio = AUDIO_FORMAT or "m4a"
            defaults = [
                "bestvideo[ext=mp4][vcodec!*=av01][vcodec!*=vp09]+bestaudio[ext=m4a]/bestvideo+bestaudio",
                "bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]/best[vcodec^=avc]/best",
                None,
            ]
            # Use height<=X to allow fallback to lower resolutions if exact match not available
            quality_map = {
                "1080": [
                    "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
                ],
                "720": [
                    "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best",
                ],
                "480": [
                    "bestvideo[ext=mp4][height<=480]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
                ],
                "360": [
                    "bestvideo[ext=mp4][height<=360]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best",
                ],
                "audio": [
                    f"bestaudio[ext={audio}]",
                    "bestaudio[ext=mp3]",
                    "bestaudio[ext=opus]",
                    "bestaudio[ext=webm]",
                    "bestaudio",
                    # Fallback to video+audio and extract audio
                    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                ],
            }
            formats = quality_map.get(self._selected_quality, defaults)
            # Set format type for upload
            if self._selected_quality == "audio":
                self._format = "audio"
                # For audio, don't add video defaults - return only audio formats
                return formats
            self._format = "video"
            return formats + defaults

        # Otherwise use user's default settings
        quality, format_ = get_quality_settings(self._chat_id), get_format_settings(
            self._chat_id
        )
        # quality: high, medium, low, custom
        # format: audio, video, document
        formats = []
        defaults = [
            # webm , vp9 and av01 are not streamable on telegram, so we'll extract only mp4
            "bestvideo[ext=mp4][vcodec!*=av01][vcodec!*=vp09]+bestaudio[ext=m4a]/bestvideo+bestaudio",
            "bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]/best[vcodec^=avc]/best",
            None,
        ]
        audio = AUDIO_FORMAT or "m4a"
        maps = {
            "high-audio": [f"bestaudio[ext={audio}]"],
            "high-video": defaults,
            "high-document": defaults,
            "medium-audio": [f"bestaudio[ext={audio}]"],  # no mediumaudio :-(
            "medium-video": self.get_format(720),
            "medium-document": self.get_format(720),
            "low-audio": [f"bestaudio[ext={audio}]"],
            "low-video": self.get_format(480),
            "low-document": self.get_format(480),
            "custom-audio": "",
            "custom-video": "",
            "custom-document": "",
        }

        if quality == "custom":
            pass
            # TODO not supported yet

        formats.extend(maps[f"{quality}-{format_}"])
        # extend default formats if not high*
        if quality != "high":
            formats.extend(defaults)
        return formats

    def _download(
        self, formats, _retry_after_update: bool = False, _use_aria2: bool = None
    ) -> list:
        output = Path(self._tempdir.name, "%(title).70s.%(ext)s").as_posix()

        yt_logger = YtDlpLogger()
        ydl_opts = {
            "logger": yt_logger,
            "progress_hooks": [lambda d: self.download_hook(d)],
            "outtmpl": output,
            "restrictfilenames": False,
            "quiet": True,
            "match_filter": match_filter,
            "concurrent_fragments": 16,
            "buffersize": 4194304,
            "retries": 6,
            "fragment_retries": 6,
            "skip_unavailable_fragments": True,
            "embed_metadata": True,
            "embed_thumbnail": True,
            "writethumbnail": True,
            # Ensure MP4 output for Telegram inline streaming support
            "merge_output_format": "mp4",
            # Only ignore errors during playlist batch downloading, not single videos
            "ignoreerrors": "only_download" if is_playlist_url(self._url) else False,
            # Enable Node.js runtime for YouTube JS challenge solving (signature + n parameter)
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs:github": {}},
        }

        # Use pre-calculated playlist limit from _start (to avoid repeated DB queries)
        if (
            hasattr(self, "_max_playlist_items")
            and self._max_playlist_items is not None
        ):
            ydl_opts["playlistend"] = self._max_playlist_items
            logging.info(
                "Playlist limited to %d items based on credits",
                self._max_playlist_items,
            )

        # Add subtitle options if user has subtitles enabled
        if self._subtitles:
            logging.info(
                "Subtitles enabled - will download English subtitles if available"
            )
            ydl_opts["writesubtitles"] = True
            ydl_opts["writeautomaticsub"] = True  # Include auto-generated subs
            ydl_opts["subtitleslangs"] = [
                "en",
                "en-orig",
                "en-US",
                "en-GB",
            ]  # English priority
            ydl_opts["subtitlesformat"] = "srt"

        use_aria2 = ENABLE_ARIA2 if _use_aria2 is None else _use_aria2
        aria2_actually_used = False
        if use_aria2 and not is_youtube(self._url):
            if shutil.which("aria2c"):
                logging.info(
                    "[DOWNLOAD METHOD: aria2] Using aria2c as external downloader with 16 connections"
                )
                ydl_opts["external_downloader"] = "aria2c"
                ydl_opts["external_downloader_args"] = {
                    "aria2c": ["-x16", "-s16", "-k1M", "--max-tries=3", "--retry-wait=3"]
                }
                aria2_actually_used = True
                # Show progress message since aria2 doesn't trigger yt-dlp progress hooks
                self.edit_text(
                    "⚡ **מוריד במהירות גבוהה...**\n\n🚀 הורדה מהירה עם 16 חיבורים מקבילים\n⏳ נא להמתין - ההעלאה תתחיל בסיום"
                )
            else:
                logging.warning(
                    "[DOWNLOAD METHOD: aria2] aria2c executable not found in PATH, using built-in"
                )
        else:
            if is_youtube(self._url):
                logging.info(
                    "[DOWNLOAD METHOD: yt-dlp] Using built-in downloader (YouTube fragmented stream)"
                )
            else:
                logging.info(
                    "[DOWNLOAD METHOD: yt-dlp] Using built-in yt-dlp downloader"
                )
        # Add MP3 conversion for audio-only downloads
        if self._selected_quality == "audio":
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        # setup cookies for youtube only
        if is_youtube(self._url):
            # use cookies from browser firstly
            if browsers := os.getenv("BROWSERS"):
                ydl_opts["cookiesfrombrowser"] = browsers.split(",")
            if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 100:
                ydl_opts["cookiefile"] = str(COOKIES_PATH)
            # try add extract_args if present
            if potoken := os.getenv("POTOKEN"):
                ydl_opts["extractor_args"] = {
                    "youtube": ["player_client=web,default", f"po_token=web+{potoken}"]
                }
                # for new version? https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
                # ydl_opts["extractor_args"] = {
                #     "youtube": [f"po_token=web.player+{potoken}", f"po_token=web.gvs+{potoken}"]
                # }
            else:
                # Use web client - android client has been blocked by Google.
                # The updated yt-dlp handles the 'n' challenge natively for web client.
                ydl_opts["extractor_args"] = {"youtube": ["player_client=web,default"]}
        else:
            # For non-YouTube sites, use impersonate to bypass Cloudflare and other anti-bot measures
            # This requires curl_cffi to be installed (yt-dlp[curl-cffi])
            logging.info(
                "[IMPERSONATE] Using browser impersonation for non-YouTube site"
            )
            ydl_opts["impersonate"] = "chrome"
            ydl_opts["extractor_args"] = {"generic": ["impersonate"]}

        files = None
        extraction_error_encountered = False
        last_error = None
        self._last_download_error = None

        for f in formats:
            try:
                ydl_opts["format"] = f
                logging.info("yt-dlp options: %s", ydl_opts)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    # Use extract_info with download=True to get title and download in one operation
                    info = ydl.extract_info(self._url, download=True)
                    if info:
                        meta = extract_metadata_from_info(info)
                        if meta.get("title"):
                            self._video_title = meta["title"]
                            self._video_description = meta["description"]
                            logging.info(
                                "Extracted title (%d chars): %s",
                                len(self._video_title),
                                self._video_title[:100],
                            )
                # Get media files only - exclude .part files, thumbnails, and subtitle files
                # Thumbnails (.jpg, .webp) should not count as successful downloads
                video_extensions = {
                    ".mp4",
                    ".mkv",
                    ".webm",
                    ".avi",
                    ".mov",
                    ".flv",
                    ".m4v",
                }
                audio_extensions = {
                    ".mp3",
                    ".m4a",
                    ".aac",
                    ".ogg",
                    ".opus",
                    ".wav",
                    ".flac",
                }
                media_extensions = video_extensions | audio_extensions

                all_files = [
                    f
                    for f in Path(self._tempdir.name).glob("*")
                    if not f.suffix.lower() == ".part"
                ]
                files = [f for f in all_files if f.suffix.lower() in media_extensions]

                if files:  # Only break if we got actual video/audio files
                    break
                elif all_files:
                    # We have files but no media - probably just thumbnails from a failed download
                    logging.warning(
                        "Found files but no media: %s (likely download failed)",
                        [f.name for f in all_files],
                    )
                # If info was None or empty without raising an exception, capture from logger
                if not files and yt_logger.errors:
                    last_error = yt_logger.errors[-1]
                    self._last_download_error = last_error
            except Exception as e:
                last_error = str(e)
                self._last_download_error = last_error
                # Check if this is a cancellation - don't try next format, just stop
                if "בוטלה" in last_error:
                    logging.info("Download cancelled by user, stopping format attempts")
                    raise
                # Check if file is too large for Telegram - don't try next format
                if "גדול מדי" in last_error:
                    logging.error("File too large for Telegram, stopping download")
                    raise
                logging.warning("Format %s failed: %s, trying next...", f, e)
                # Check if this is a network error - stop trying and show resume button
                if is_network_error(e):
                    # Get partial file size if any
                    partial_files = list(Path(self._tempdir.name).glob("*.part"))
                    partial_bytes = (
                        sum(p.stat().st_size for p in partial_files)
                        if partial_files
                        else 0
                    )
                    raise NetworkError(
                        url=self._url,
                        downloaded_bytes=partial_bytes,
                        total_bytes=0,
                        quality=(
                            self._selected_quality
                            if hasattr(self, "_selected_quality")
                            else None
                        ),
                        original_error=e,
                    ) from e
                # Check if this is an extraction error
                if is_extraction_error(last_error):
                    extraction_error_encountered = True
                # If the error is fatal (private, removed, geo-blocked, bot-blocked), subsequent formats will also fail
                err_lower = last_error.lower()
                if any(fatal in err_lower for fatal in [
                    "sign in to confirm you're not a bot",
                    "confirm you're not a bot",
                    "this video is private",
                    "private video",
                    "this video is unavailable",
                    "video unavailable",
                    "has been removed",
                    "not available in your country",
                    "geographic restriction",
                    "the page needs to be reloaded",
                    "n challenge solving failed",
                    "challenge solver script",
                    "supported javascript runtime",
                ]):
                    logging.warning("Encountered fatal non-format error: %s, stopping format attempts", last_error)
                    break
                continue

        # Scan yt-dlp warnings and errors to detect n-challenge / JS runtime failures
        all_yt_msgs = (yt_logger.errors or []) + (yt_logger.warnings or [])
        if self._last_download_error:
            all_yt_msgs.append(self._last_download_error)
        n_challenge_detected = False
        for msg in all_yt_msgs:
            m_lower = str(msg).lower()
            if any(k in m_lower for k in [
                "the page needs to be reloaded",
                "n challenge solving failed",
                "challenge solver script",
                "supported javascript runtime",
            ]):
                n_challenge_detected = True
                self._last_download_error = str(msg)
                break

        # If all formats failed due to extraction error, try auto-updating yt-dlp
        if not files and not self._last_download_error and yt_logger.errors:
            self._last_download_error = yt_logger.errors[-1]

        if not files and extraction_error_encountered and not _retry_after_update and not n_challenge_detected:
            logging.info("Extraction error detected, attempting yt-dlp auto-update...")
            if try_update_ytdlp():
                logging.info("Retrying download after yt-dlp update...")
                return self._download(
                    formats, _retry_after_update=True, _use_aria2=use_aria2
                )
            else:
                logging.warning("yt-dlp auto-update failed or already attempted")

        # Fallback: if aria2 was used and failed, retry with built-in yt-dlp downloader
        # Fallback: only if aria2 was actually used and failed, retry with built-in yt-dlp downloader
        if not files and aria2_actually_used and _use_aria2 is None:
            logging.warning(
                "[aria2 FALLBACK] aria2 failed, retrying with built-in yt-dlp downloader..."
            )
            return self._download(
                formats, _retry_after_update=_retry_after_update, _use_aria2=False
            )

        return files

    def _try_gallery_dl(self) -> list | None:
        """Try to download using gallery-dl as a fallback."""
        if is_youtube(self._url):
            return None

        if not is_gallery_dl_available():
            logging.warning("[GALLERY-DL] gallery-dl is not available on this system")
            return None

        output = Path(self._tempdir.name)

        try:
            cmd = get_gallery_dl_cmd() + [
                "--dest",
                str(output),
                "--no-mtime",  # Don't set modification time
                "-q",  # Quiet mode
                self._url,
            ]

            logging.info("[GALLERY-DL] Running: %s", " ".join(cmd))

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300  # 5 minute timeout
            )

            if result.returncode != 0:
                logging.warning(
                    "[GALLERY-DL] Failed with code %d: %s",
                    result.returncode,
                    result.stderr[:200] if result.stderr else "",
                )
                return None

            # Find downloaded files
            video_extensions = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".m4v"}
            audio_extensions = {
                ".mp3",
                ".m4a",
                ".aac",
                ".ogg",
                ".opus",
                ".wav",
                ".flac",
            }
            media_extensions = video_extensions | audio_extensions

            # Search recursively since gallery-dl may create subdirectories
            files = []
            for ext in media_extensions:
                files.extend(output.rglob(f"*{ext}"))

            if files:
                logging.info("[GALLERY-DL] Success! Found %d files", len(files))
                return files
            logging.warning("[GALLERY-DL] No media files found in output")
            return None

        except subprocess.TimeoutExpired:
            logging.error("[GALLERY-DL] Timed out after 5 minutes")
            return None
        except FileNotFoundError:
            logging.error("[GALLERY-DL] gallery-dl executable not found")
            return None
        except Exception as e:
            logging.error("[GALLERY-DL] Error: %s", e)
            return None

    def _start(self, formats=None):
        # start download and upload, no cache hit
        # user can choose format by clicking on the button(custom config)
        try:
            # Check credits once at start (not in _download which may be called multiple times)
            available_credits = get_total_credits(self._from_user)
            if available_credits == 0:
                raise CreditsExhaustedException("הקרדיטים שלך נגמרו.")

            # Store max items for playlist limiting (used in _download)
            self._max_playlist_items = (
                available_credits if available_credits != float("inf") else None
            )
            logging.info(
                "User %s has %s credits, max playlist items: %s",
                self._from_user,
                available_credits,
                self._max_playlist_items or "unlimited",
            )

            default_formats = self._setup_formats()
            if formats is not None:
                # formats according to user choice
                default_formats = formats + default_formats
            files = self._download(default_formats)

            # Debug: log what files are in tempdir
            all_files_in_temp = list(Path(self._tempdir.name).glob("*"))
            logging.info("Files returned from _download: %s", files)
            logging.info("All files in tempdir: %s", all_files_in_temp)

            if not files:
                # Fallback to gallery-dl only for non-YouTube URLs when gallery-dl is available
                if not is_youtube(self._url) and is_gallery_dl_available():
                    logging.info(
                        "[GALLERY-DL FALLBACK] yt-dlp failed, trying gallery-dl..."
                    )
                    self.edit_text("🔄 **yt-dlp נכשל, מנסה gallery-dl...**")
                    files = self._try_gallery_dl()
                elif is_youtube(self._url):
                    logging.debug("[GALLERY-DL] Skipping gallery-dl fallback for YouTube URL")
                else:
                    logging.warning("[GALLERY-DL] Skipping gallery-dl fallback: gallery-dl is not installed")

            if not files:
                error_desc = classify_download_error(self._last_download_error, self._url)
                raise ClassifiedDownloadError(
                    str(error_desc), is_safe=getattr(error_desc, "is_safe", False)
                )
            self._upload()
        except NetworkError as e:
            # Network error - show resume button
            logging.warning("Network error during YouTube download: %s", e)
            self.edit_text_with_resume_button(
                downloaded_bytes=e.downloaded_bytes,
                total_bytes=e.total_bytes,
                quality=e.quality,
                download_type="youtube",
            )
