import os


def get_env(name: str, default=None):
    val = os.getenv(name, default)
    if val is None:
        return None
    if isinstance(val, str):
        if val.lower() == "true":
            return True
        if val.lower() == "false":
            return False
        if val.isdigit() and name != "AUTHORIZED_USER":
            return int(val)
    return val


# general settings
WORKERS: int = get_env("WORKERS", 100)
APP_ID: int = get_env("APP_ID")
APP_HASH = get_env("APP_HASH")
BOT_TOKEN = get_env("BOT_TOKEN")
OWNER = [int(i) for i in str(get_env("OWNER")).split(",")]
# db settings
AUTHORIZED_USER: str = get_env("AUTHORIZED_USER", "")
DB_DSN = get_env("DB_DSN")

ENABLE_FFMPEG = get_env("ENABLE_FFMPEG")
AUDIO_FORMAT = get_env("AUDIO_FORMAT", "m4a")
M3U8_SUPPORT = get_env("M3U8_SUPPORT")
ENABLE_ARIA2 = get_env("ENABLE_ARIA2")

# payment settings
ENABLE_VIP = get_env("ENABLE_VIP")
PROVIDER_TOKEN = get_env("PROVIDER_TOKEN")
FREE_DOWNLOAD = get_env("FREE_DOWNLOAD", 3)
TOKEN_PRICE = get_env("TOKEN_PRICE", 10)  # 1 USD=10 downloads
FREE_BANDWIDTH = get_env("FREE_BANDWIDTH", 2147483648)  # 2GB in bytes
ARCHIVE_CHANNEL = int(get_env("ARCHIVE_CHANNEL")) if get_env("ARCHIVE_CHANNEL") else None  # Channel to forward downloads to


# For advance users
# Please do not change, if you don't know what these are.
TG_NORMAL_MAX_SIZE = 2000 * 1024 * 1024
MAX_DOWNLOAD_SIZE = 4 * 1024 * 1024 * 1024  # 4GB - maximum allowed download
CAPTION_URL_LENGTH_LIMIT = 150

# This will set the value for the tmpfile path(engine path). If not, will return None and use system’s default path.
# Please ensure that the directory exists and you have necessary permissions to write to it.
TMPFILE_PATH = get_env("TMPFILE_PATH")

# Instagram settings - session file for instaloader (optional, for age-restricted content)
# Export your session with: instaloader --login YOUR_USERNAME --sessionfile instagram_session
INSTAGRAM_SESSION_FILE = get_env("INSTAGRAM_SESSION_FILE")

# Instagram cookies file for yt-dlp (Netscape format, export from browser)
# Use browser extension like "Get cookies.txt LOCALLY" to export cookies
INSTAGRAM_COOKIES_FILE = get_env("INSTAGRAM_COOKIES_FILE")

# TikTok cookies file for yt-dlp/gallery-dl
TIKTOK_COOKIES_FILE = get_env("TIKTOK_COOKIES_FILE")

# qBittorrent settings
QBITTORRENT_HOST = get_env("QBITTORRENT_HOST", "localhost")
QBITTORRENT_PORT = get_env("QBITTORRENT_PORT", 8080)
QBITTORRENT_USERNAME = get_env("QBITTORRENT_USERNAME", "admin")
QBITTORRENT_PASSWORD = get_env("QBITTORRENT_PASSWORD", "adminadmin")

# Torrent feature settings
TORRENT_MIN_CREDITS = get_env("TORRENT_MIN_CREDITS", 500)        # Gate requirement
TORRENT_STALL_TIMEOUT = get_env("TORRENT_STALL_TIMEOUT", 600)    # 10 minutes
TORRENT_GLOBAL_TIMEOUT = get_env("TORRENT_GLOBAL_TIMEOUT", 7200) # 2 hours
TORRENT_MAX_PER_USER = get_env("TORRENT_MAX_PER_USER", 1)        # Active per user
TORRENT_MAX_GLOBAL = get_env("TORRENT_MAX_GLOBAL", 5)            # Global cap

# JDownloader2 settings (my.jdownloader.org)
JDOWNLOADER_EMAIL = get_env("JDOWNLOADER_EMAIL")
JDOWNLOADER_PASSWORD = get_env("JDOWNLOADER_PASSWORD")
JDOWNLOADER_DEVICE_NAME = get_env("JDOWNLOADER_DEVICE_NAME")
JDOWNLOADER_DOWNLOAD_DIR = get_env("JDOWNLOADER_DOWNLOAD_DIR", r"C:\Users\omer\Downloads\JDownloader")

# JDownloader feature settings
JDOWNLOADER_POLL_INTERVAL = get_env("JDOWNLOADER_POLL_INTERVAL", 5)       # Seconds between checks
JDOWNLOADER_STALL_TIMEOUT = get_env("JDOWNLOADER_STALL_TIMEOUT", 600)    # 10 minutes
JDOWNLOADER_GLOBAL_TIMEOUT = get_env("JDOWNLOADER_GLOBAL_TIMEOUT", 7200) # 2 hours
JDOWNLOADER_MAX_PER_USER = get_env("JDOWNLOADER_MAX_PER_USER", 1)
JDOWNLOADER_MAX_GLOBAL = get_env("JDOWNLOADER_MAX_GLOBAL", 3)

