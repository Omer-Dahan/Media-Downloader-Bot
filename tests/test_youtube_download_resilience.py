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
    """Verify playlist detection based on query params and path."""
    assert is_playlist_url("https://www.youtube.com/playlist?list=PL12345") is True
    assert is_playlist_url("https://www.youtube.com/watch?v=abc&list=PL12345") is True
    assert is_playlist_url("https://youtu.be/iL686rbf82M?si=JVN5_9OfDeESOB_C") is False
    assert is_playlist_url("") is False


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
