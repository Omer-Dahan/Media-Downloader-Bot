"""Tests for YouTube download error classification, aria2/gallery-dl fallbacks, and JDownloader stall detection."""

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine.generic import (
    YtDlpLogger,
    classify_download_error,
    is_gallery_dl_available,
    is_playlist_url,
    YoutubeDownload,
)
from engine.jdownloader import JDownloaderDownload, JDownloaderError
from engine.jdownloader_manager import JDownloaderManager
from engine import youtube_entrance, youtube_entrance_with_quality


def test_classify_download_error_bot_detection():
    """Verify bot detection and sign in errors are classified accurately."""
    err = "ERROR: [youtube] iL686rbf82M: Sign in to confirm you're not a bot. This helps protect our community."
    classified = classify_download_error(err)
    assert "נחסמה" in classified
    assert "זיהוי בוט" in classified
    assert "cookies" in classified


def test_classify_download_error_cookies_expired():
    """Verify cookie expiration errors are diagnosed directly."""
    err = "ERROR: [youtube] cookies file expired or corrupted session"
    classified = classify_download_error(err)
    assert "cookies" in classified
    assert "פג תוקפו" in classified or "אינו תקין" in classified


def test_classify_download_error_private_or_deleted():
    """Verify private or removed videos are diagnosed without blaming yt-dlp."""
    err = "ERROR: [youtube] abc12345: This video is private"
    classified = classify_download_error(err)
    assert "פרטי" in classified or "אינו זמין" in classified

    err2 = "ERROR: [youtube] abc12345: Video unavailable. This video has been removed by the uploader"
    classified2 = classify_download_error(err2)
    assert "אינו זמין" in classified2 or "נמחק" in classified2


def test_classify_download_error_geo_restriction():
    """Verify geo-blocked videos are diagnosed accurately."""
    err = "ERROR: [youtube] abc12345: The uploader has not made this video available in your country"
    classified = classify_download_error(err)
    assert "גיאוגרפית" in classified or "במדינה" in classified


def test_classify_download_error_format_not_available():
    """Verify format errors report format issues rather than false update messages."""
    err = "ERROR: requested format is not available"
    classified = classify_download_error(err)
    assert "הפורמט המבוקש אינו זמין" in classified


def test_is_playlist_url():
    """Verify multi-item source detection covering playlists, channels, handles, and custom URLs."""
    # Playlists
    assert is_playlist_url("https://www.youtube.com/playlist?list=PL12345") is True
    assert is_playlist_url("https://www.youtube.com/watch?v=abc&list=PL12345") is True
    assert is_playlist_url("https://youtu.be/abc?list=PL12345") is True
    assert is_playlist_url("https://www.youtube.com/playlist") is True

    # Channels
    assert is_playlist_url("https://www.youtube.com/channel/UC123456789") is True
    assert is_playlist_url("https://www.youtube.com/channel/UC123456789/videos") is True

    # Handles
    assert is_playlist_url("https://www.youtube.com/@some_creator") is True
    assert is_playlist_url("https://www.youtube.com/@some_creator/videos") is True
    assert is_playlist_url("https://www.youtube.com/@some_creator/shorts") is True

    # Custom and user URLs
    assert is_playlist_url("https://www.youtube.com/c/CreatorName") is True
    assert is_playlist_url("https://www.youtube.com/c/CreatorName/videos") is True
    assert is_playlist_url("https://www.youtube.com/user/CreatorName") is True
    assert is_playlist_url("https://www.youtube.com/user/CreatorName/videos") is True
    assert is_playlist_url("youtube.com/@creator") is True

    # Single videos (must NOT be treated as playlists)
    assert is_playlist_url("https://youtu.be/iL686rbf82M?si=JVN5_9OfDeESOB_C") is False
    assert is_playlist_url("https://www.youtube.com/watch?v=iL686rbf82M") is False
    assert is_playlist_url("https://youtu.be/iL686rbf82M") is False
    assert is_playlist_url("") is False
    assert is_playlist_url(None) is False


def test_ytdlp_logger_captures_errors_and_warnings():
    """Verify YtDlpLogger captures logs and preserves error messages."""
    logger = YtDlpLogger()
    logger.warning("Test warning from yt-dlp")
    logger.error("Test error from yt-dlp: Sign in to confirm you're not a bot")

    assert len(logger.warnings) == 1
    assert "Test warning" in logger.warnings[0]
    assert len(logger.errors) == 1
    assert "Sign in to confirm you're not a bot" in logger.errors[0]


def test_youtube_download_never_triggers_aria2_fallback():
    """Verify that for YouTube downloads, aria2 fallback is never attempted even if ENABLE_ARIA2 is True."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    url = "https://youtu.be/iL686rbf82M"
    downloader = YoutubeDownload(client, bot_msg, url, selected_quality="1080")

    download_calls = []

    def mock_download(formats, _retry_after_update=False, _use_aria2=None):
        download_calls.append(_use_aria2)
        # Simulate download returning empty list
        downloader._last_download_error = "Sign in to confirm you're not a bot"
        return []

    downloader._download = mock_download
    downloader._upload = MagicMock()

    with patch("engine.generic.get_total_credits", return_value=10):
        with pytest.raises(ValueError) as exc_info:
            downloader._start(["bestvideo+bestaudio"])

    # _download should only have been called once, not retried with _use_aria2=False
    assert len(download_calls) == 1
    assert "זיהוי בוט" in str(exc_info.value)


def test_gallery_dl_fallback_skipped_for_youtube():
    """Verify gallery-dl fallback is completely bypassed for YouTube URLs."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    url = "https://youtu.be/iL686rbf82M"
    downloader = YoutubeDownload(client, bot_msg, url, selected_quality="720")

    assert downloader._try_gallery_dl() is None


def test_jdownloader_stall_detection_in_waiting_state():
    """Verify JDownloader download raises JDownloaderError when waiting for too long without progress."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    dl = JDownloaderDownload(client, bot_msg, "https://example.com/file")
    dl._manager = MagicMock()
    dl._package_name = "TGBot_12345_test"

    # Status returns waiting with 0 speed and 0 bytes
    dl._manager.get_status.return_value = {
        "state": "waiting",
        "speed": 0,
        "downloaded": 0,
        "total": 1000,
        "progress": 0.0,
        "status_text": "Waiting in queue",
        "name": "test_pkg",
    }

    # First poll initializes stall timer
    assert dl._poll_progress() is False
    assert dl._stall_start > 0

    # Simulate elapsed time beyond timeout
    with patch("engine.jdownloader.JDOWNLOADER_STALL_TIMEOUT", 1):
        dl._stall_start = time.time() - 2
        with pytest.raises(JDownloaderError) as exc_info:
            dl._poll_progress()

        assert "תקועה במצב המתנה" in str(exc_info.value)


def test_jdownloader_manager_detects_error_in_status_text():
    """Verify JDownloaderManager marks package as error when status_text has error keywords."""
    mgr = JDownloaderManager.__new__(JDownloaderManager)
    mgr._device = MagicMock()

    mock_pkg = {
        "name": "test_pkg",
        "saveTo": str(Path("/tmp/downloads") / "TGBot_123_abc"),
        "status": "Plugin defect: YouTube extractor error",
        "finished": False,
        "running": False,
        "bytesLoaded": 0,
        "bytesTotal": 0,
        "speed": 0,
    }

    mgr._device.downloads.query_packages.return_value = [mock_pkg]

    with patch("engine.jdownloader_manager.JDOWNLOADER_DOWNLOAD_DIR", "/tmp/downloads"):
        status = mgr.get_status("TGBot_123_abc")

    assert status["state"] == "error"
    assert "Plugin defect" in status["error"]


def test_unrecoverable_youtube_error_skips_jdownloader_fallback():
    """Verify that private or removed YouTube videos do not fall back to JDownloader."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.id = 100

    with patch("engine.YoutubeDownload") as mock_yt_cls:
        instance = mock_yt_cls.return_value
        instance.start.side_effect = ValueError("הסרטון אינו זמין (סרטון פרטי, נמחק, או דורש מנוי ערוץ).")

        with patch("engine.jdownloader_entrance") as mock_jd:
            youtube_entrance_with_quality(client, bot_msg, "https://youtu.be/private_vid", "1080")
            # JDownloader should NOT be called for unrecoverable errors
            mock_jd.assert_not_called()
            # Bot message should be edited with error directly
            bot_msg.edit_text.assert_called_once()
            assert "הסרטון אינו זמין" in bot_msg.edit_text.call_args[0][0]


def test_unrecoverable_youtube_error_skips_jdownloader_fallback_standard_entrance():
    """Verify standard youtube_entrance also avoids JDownloader for unrecoverable errors."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.id = 100

    with patch("engine.YoutubeDownload") as mock_yt_cls:
        instance = mock_yt_cls.return_value
        instance.start.side_effect = ValueError("הסרטון אינו זמין (סרטון פרטי, נמחק, או דורש מנוי ערוץ).")

        with patch("engine.jdownloader_entrance") as mock_jd:
            youtube_entrance(client, bot_msg, "https://youtu.be/deleted_vid")
            mock_jd.assert_not_called()
            bot_msg.edit_text.assert_called_once()
            assert "הסרטון אינו זמין" in bot_msg.edit_text.call_args[0][0]


def test_check_link_consistency_for_playlists_and_channels():
    """Verify check_link uses the unified is_playlist_url truth source for channels and playlists."""
    from main import check_link

    with patch("main.get_total_credits", return_value=0):
        # Channel URLs blocked without credits
        assert check_link("https://www.youtube.com/channel/UC12345", uid=1) == "PLAYLIST_NO_CREDITS"
        assert check_link("https://www.youtube.com/@my_handle", uid=1) == "PLAYLIST_NO_CREDITS"
        assert check_link("https://www.youtube.com/c/ChannelName", uid=1) == "PLAYLIST_NO_CREDITS"
        assert check_link("https://www.youtube.com/user/UserName/videos", uid=1) == "PLAYLIST_NO_CREDITS"
        assert check_link("https://www.youtube.com/playlist?list=PL123", uid=1) == "PLAYLIST_NO_CREDITS"

    with patch("main.get_total_credits", return_value=5):
        # Channel and playlist URLs allowed with credits
        assert check_link("https://www.youtube.com/channel/UC12345", uid=1) is None
        assert check_link("https://www.youtube.com/@my_handle", uid=1) is None
        assert check_link("https://www.youtube.com/c/ChannelName", uid=1) is None
        assert check_link("https://www.youtube.com/user/UserName/videos", uid=1) is None
        assert check_link("https://www.youtube.com/playlist?list=PL123", uid=1) is None


def test_youtube_ignoreerrors_distinguishes_multi_item_from_single_video():
    """Verify multi-item URLs use ignoreerrors='only_download' while single videos use False."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    captured_opts = {}

    def fake_ydl(opts=None):
        captured_opts.clear()
        if opts:
            captured_opts.update(opts)
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_ctx
        mock_ctx.__exit__.return_value = False
        mock_ctx.download.return_value = 0
        return mock_ctx

    # 1. Multi-item URL (channel) -> ignoreerrors must be 'only_download'
    channel_url = "https://www.youtube.com/@creator/videos"
    dl_channel = YoutubeDownload(client, bot_msg, channel_url)
    with patch("engine.generic.yt_dlp.YoutubeDL", side_effect=fake_ydl):
        try:
            dl_channel._download(["best"])
        except Exception:
            pass
    assert captured_opts.get("ignoreerrors") == "only_download"

    # 2. Multi-item URL (playlist) -> ignoreerrors must be 'only_download'
    playlist_url = "https://www.youtube.com/playlist?list=PL98765"
    dl_playlist = YoutubeDownload(client, bot_msg, playlist_url)
    with patch("engine.generic.yt_dlp.YoutubeDL", side_effect=fake_ydl):
        try:
            dl_playlist._download(["best"])
        except Exception:
            pass
    assert captured_opts.get("ignoreerrors") == "only_download"

    # 3. Single video URL -> ignoreerrors must be False so errors fail clearly
    single_url = "https://www.youtube.com/watch?v=iL686rbf82M"
    dl_single = YoutubeDownload(client, bot_msg, single_url)
    with patch("engine.generic.yt_dlp.YoutubeDL", side_effect=fake_ydl):
        try:
            dl_single._download(["best"])
        except Exception:
            pass
    assert captured_opts.get("ignoreerrors") is False


def test_jdownloader_waiting_in_queue_with_active_downloads_not_killed():
    """Verify that a package waiting in queue while other packages download is not killed."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.id = 100

    dl = JDownloaderDownload(client, bot_msg, "https://example.com/file")
    dl._manager = MagicMock()
    dl._package_name = "TGBot_12345_test"

    # Status returns waiting with active_downloads = 1 on the device
    dl._manager.get_status.return_value = {
        "state": "waiting",
        "speed": 0,
        "downloaded": 0,
        "total": 1000,
        "progress": 0.0,
        "status_text": "Waiting in queue",
        "name": "test_pkg",
        "active_downloads": 1,
    }

    # Simulate elapsed time beyond timeout
    dl._stall_start = time.time() - 1000
    with patch("engine.jdownloader.JDOWNLOADER_STALL_TIMEOUT", 10):
        # Because active_downloads > 0, poll progress must reset stall timer and NOT raise
        assert dl._poll_progress() is False
        assert dl._stall_start == 0


def test_jdownloader_manager_calculates_active_downloads():
    """Verify JDownloaderManager computes active_downloads from other packages."""
    mgr = JDownloaderManager.__new__(JDownloaderManager)
    mgr._device = MagicMock()

    our_pkg = {
        "name": "our_pkg",
        "saveTo": str(Path("/tmp/downloads") / "TGBot_our_pkg"),
        "status": "Waiting in queue",
        "finished": False,
        "running": False,
        "bytesLoaded": 0,
        "bytesTotal": 1000,
        "speed": 0,
    }

    active_pkg = {
        "name": "other_pkg",
        "saveTo": str(Path("/tmp/downloads") / "TGBot_other_pkg"),
        "status": "Downloading",
        "finished": False,
        "running": True,
        "bytesLoaded": 500,
        "bytesTotal": 1000,
        "speed": 50000,
    }

    mgr._device.downloads.query_packages.return_value = [our_pkg, active_pkg]

    with patch("engine.jdownloader_manager.JDOWNLOADER_DOWNLOAD_DIR", "/tmp/downloads"):
        status = mgr.get_status("TGBot_our_pkg")

    assert status["state"] == "waiting"
    assert status.get("active_downloads") == 1


def test_user_error_message_sanitization_and_no_path_leak():
    """Verify get_user_friendly_error_message displays classified messages but redacts raw errors."""
    from main import get_user_friendly_error_message, GENERIC_ERROR_MESSAGE
    from engine.generic import ClassifiedDownloadError

    # 1. Classified safe error
    safe_err = ClassifiedDownloadError("הסרטון אינו זמין (סרטון פרטי, נמחק, או דורש מנוי ערוץ).", is_safe=True)
    msg = get_user_friendly_error_message(safe_err)
    assert "הסרטון אינו זמין" in msg
    assert msg.startswith("❌")

    # 2. Classified safe error - bot detection
    safe_bot = ClassifiedDownloadError("ההורדה מיוטיוב נחסמה (זיהוי בוט / נדרש אימות).", is_safe=True)
    msg_bot = get_user_friendly_error_message(safe_bot)
    assert "זיהוי בוט" in msg_bot

    # 3. Unclassified ClassifiedDownloadError
    unclassified_err = ClassifiedDownloadError("ההורדה נכשלה: some internal failure", is_safe=False)
    assert get_user_friendly_error_message(unclassified_err) == GENERIC_ERROR_MESSAGE

    # 4. Raw file system path exception
    leak_err = FileNotFoundError("/home/vm/projects/media-downloader-bot/secret_cookie.txt not found")
    msg_leak = get_user_friendly_error_message(leak_err)
    assert msg_leak == GENERIC_ERROR_MESSAGE
    assert "/home/vm" not in msg_leak

    # 5. Database connection exception
    db_err = Exception("psycopg2.OperationalError: server closed the connection unexpectedly at /var/run/postgresql")
    msg_db = get_user_friendly_error_message(db_err)
    assert msg_db == GENERIC_ERROR_MESSAGE
    assert "postgresql" not in msg_db

    # 6. Internal python ValueError
    val_err = ValueError("invalid literal for int() with base 10: 'invalid_id'")
    msg_val = get_user_friendly_error_message(val_err)
    assert msg_val == GENERIC_ERROR_MESSAGE
    assert "invalid literal" not in msg_val

    # 7. User cancellation ValueError is preserved
    cancel_err = ValueError("ההורדה בוטלה על ידי המשתמש 🛑")
    msg_cancel = get_user_friendly_error_message(cancel_err)
    assert "ההורדה בוטלה על ידי המשתמש" in msg_cancel


def test_jdownloader_temporary_limit_or_problem_does_not_fail():
    """Verify JDownloaderManager does not treat temporary limit/problem messages as fatal errors."""
    mgr = JDownloaderManager.__new__(JDownloaderManager)
    mgr._device = MagicMock()

    temporary_statuses = [
        "Download limit reached, wait 15 min",
        "Connection problem, retrying in 30s",
        "Temporarily blocked by host, retry in 5m",
        "IP limit reached: countdown 10:00",
    ]

    for temp_status in temporary_statuses:
        mock_pkg = {
            "name": "pkg_temp",
            "saveTo": str(Path("/tmp/downloads") / "TGBot_temp"),
            "status": temp_status,
            "finished": False,
            "running": False,
            "bytesLoaded": 0,
            "bytesTotal": 1000,
            "speed": 0,
        }
        mgr._device.downloads.query_packages.return_value = [mock_pkg]

        with patch("engine.jdownloader_manager.JDOWNLOADER_DOWNLOAD_DIR", "/tmp/downloads"):
            status = mgr.get_status("TGBot_temp")

        # Must NOT be marked as error; must remain waiting
        assert status["state"] == "waiting", f"Failed for status: {temp_status}"
        assert status.get("error", "") == ""


def test_jdownloader_fatal_error_keywords_still_fail():
    """Verify JDownloaderManager detects actual fatal errors."""
    mgr = JDownloaderManager.__new__(JDownloaderManager)
    mgr._device = MagicMock()

    fatal_statuses = [
        "Plugin defect: YouTube extractor broken",
        "Offline: File was removed",
        "Download failed (CRC checksum mismatch)",
        "Account missing: Premium required",
    ]

    for fatal_status in fatal_statuses:
        mock_pkg = {
            "name": "pkg_fatal",
            "saveTo": str(Path("/tmp/downloads") / "TGBot_fatal"),
            "status": fatal_status,
            "finished": False,
            "running": False,
            "bytesLoaded": 0,
            "bytesTotal": 1000,
            "speed": 0,
        }
        mgr._device.downloads.query_packages.return_value = [mock_pkg]

        with patch("engine.jdownloader_manager.JDOWNLOADER_DOWNLOAD_DIR", "/tmp/downloads"):
            status = mgr.get_status("TGBot_fatal")

        assert status["state"] == "error", f"Failed for status: {fatal_status}"
        assert status["error"] == fatal_status
