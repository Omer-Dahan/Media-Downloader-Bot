"""Tests for safe error message formatting in engine status updates."""

from unittest.mock import MagicMock, patch
import pytest

from engine.base import ClassifiedDownloadError, format_safe_error_message
from engine.googledrive import GoogleDriveDownload
from engine.jdownloader import JDownloaderDownload
from engine.jdownloader_manager import JDownloaderConnectionError, JDownloaderError
from engine.torrent import TorrentDownload
from engine.torrent_manager import (
    TorrentConcurrencyError,
    TorrentConnectionError,
    TorrentError,
)


def test_format_safe_error_message_unsafe_hides_raw_details():
    """Unsafe exceptions or exceptions without is_safe must use fallback and hide raw details."""
    raw_sensitive = "Connection refused to 10.0.0.1:8080 with token abc123xyz"
    err = Exception(raw_sensitive)

    result = format_safe_error_message(err, fallback="כשל כללי בהורדה", prefix="❌ ")
    assert raw_sensitive not in result
    assert result == "❌ כשל כללי בהורדה"

    err_explicit_unsafe = TorrentError(raw_sensitive, is_safe=False)
    result2 = format_safe_error_message(
        err_explicit_unsafe, fallback="כשל בהוספת הקישור", prefix="❌ "
    )
    assert raw_sensitive not in result2
    assert result2 == "❌ כשל בהוספת הקישור"


def test_format_safe_error_message_safe_shows_content():
    """Exceptions explicitly marked as is_safe=True must have their content shown."""
    safe_text = "הטורנט נעלם מהשרת"
    err = TorrentError(safe_text, is_safe=True)

    result = format_safe_error_message(err, fallback="כשל כללי בהורדה", prefix="❌ ")
    assert result == f"❌ {safe_text}"


def test_torrent_status_message_safety():
    """Verify TorrentDownload._start error handling hides raw error when unsafe and shows when safe."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    dl = TorrentDownload(client, bot_msg, "magnet:?xt=urn:btih:mock")
    dl.edit_text = MagicMock()

    # 1. Unsafe TorrentConnectionError
    unsafe_conn_err = TorrentConnectionError(
        "internal connection refused 127.0.0.1", is_safe=False
    )
    with patch("engine.torrent.TorrentManager", side_effect=unsafe_conn_err):
        with pytest.raises(ClassifiedDownloadError):
            dl._start()
        dl.edit_text.assert_called_with("❌ כשל כללי בהורדה")

    # 2. Safe TorrentConnectionError
    dl.edit_text.reset_mock()
    safe_conn_err = TorrentConnectionError("לא ניתן להתחבר ל-qBittorrent", is_safe=True)
    with patch("engine.torrent.TorrentManager", side_effect=safe_conn_err):
        with pytest.raises(ClassifiedDownloadError):
            dl._start()
        dl.edit_text.assert_called_with("❌ לא ניתן להתחבר ל-qBittorrent")

    # 3. Unsafe TorrentConcurrencyError
    dl.edit_text.reset_mock()
    unsafe_conc_err = TorrentConcurrencyError("internal queue overflow", is_safe=False)
    mock_mgr = MagicMock()
    mock_mgr.add_torrent.side_effect = unsafe_conc_err
    with patch("engine.torrent.TorrentManager", return_value=mock_mgr):
        with pytest.raises(ClassifiedDownloadError):
            dl._start()
        dl.edit_text.assert_called_with("⏳ כשל כללי בהורדה")

    # 4. Safe TorrentConcurrencyError
    dl.edit_text.reset_mock()
    safe_conc_err = TorrentConcurrencyError("השרת עמוס כרגע", is_safe=True)
    mock_mgr = MagicMock()
    mock_mgr.add_torrent.side_effect = safe_conc_err
    with patch("engine.torrent.TorrentManager", return_value=mock_mgr):
        with pytest.raises(ClassifiedDownloadError):
            dl._start()
        dl.edit_text.assert_called_with("⏳ השרת עמוס כרגע")

    # 5. Unsafe TorrentError
    dl.edit_text.reset_mock()
    unsafe_torrent_err = TorrentError("internal libtorrent memory fault", is_safe=False)
    mock_mgr = MagicMock()
    mock_mgr.add_torrent.side_effect = unsafe_torrent_err
    with patch("engine.torrent.TorrentManager", return_value=mock_mgr):
        with pytest.raises(ClassifiedDownloadError):
            dl._start()
        dl.edit_text.assert_called_with("❌ כשל כללי בהורדה")


def test_jdownloader_status_message_safety():
    """Verify JDownloaderDownload._start hides raw error details when unsafe."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 101

    dl = JDownloaderDownload(client, bot_msg, "https://example.com/file.zip")
    dl.edit_text = MagicMock()

    # Unsafe connection error (standard JDownloaderConnectionError without is_safe=True)
    unsafe_conn_err = JDownloaderConnectionError("internal api dead at 10.0.0.5")
    with patch("engine.jdownloader.JDownloaderManager", side_effect=unsafe_conn_err):
        with pytest.raises(JDownloaderConnectionError):
            dl._start()
        dl.edit_text.assert_called_with("❌ כשל כללי בהורדה")

    # Safe connection error
    dl.edit_text.reset_mock()
    safe_conn_err = JDownloaderConnectionError("שגיאת חיבור ידועה")
    safe_conn_err.is_safe = True
    with patch("engine.jdownloader.JDownloaderManager", side_effect=safe_conn_err):
        with pytest.raises(JDownloaderConnectionError):
            dl._start()
        dl.edit_text.assert_called_with("❌ שגיאת חיבור ידועה")

    # Unsafe add_link error
    dl.edit_text.reset_mock()
    unsafe_add_err = JDownloaderError("secret query parameter error")
    mock_mgr = MagicMock()
    mock_mgr.add_link.side_effect = unsafe_add_err
    mock_cls = MagicMock(return_value=mock_mgr)
    mock_cls.can_start_download.return_value = (True, "")
    with patch("engine.jdownloader.JDownloaderManager", mock_cls):
        with pytest.raises(JDownloaderError):
            dl._start()
        dl.edit_text.assert_called_with("❌ כשל בהוספת הקישור")

    # Safe add_link error
    dl.edit_text.reset_mock()
    safe_add_err = JDownloaderError("קישור לא נתמך")
    safe_add_err.is_safe = True
    mock_mgr = MagicMock()
    mock_mgr.add_link.side_effect = safe_add_err
    mock_cls = MagicMock(return_value=mock_mgr)
    mock_cls.can_start_download.return_value = (True, "")
    with patch("engine.jdownloader.JDownloaderManager", mock_cls):
        with pytest.raises(JDownloaderError):
            dl._start()
        dl.edit_text.assert_called_with("❌ קישור לא נתמך")


def test_googledrive_status_message_safety():
    """Verify GoogleDriveDownload._start hides raw details on generic ValueError."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 102

    dl = GoogleDriveDownload(
        client, bot_msg, "https://drive.google.com/file/d/abc123xyz"
    )

    # Unsafe ValueError
    with patch.object(
        dl,
        "_download",
        side_effect=ValueError("internal traceback: file not found at /srv/data"),
    ):
        dl._start()
        bot_msg.edit_text.assert_called_with("כשל כללי בהורדה")

    # Safe ClassifiedDownloadError
    bot_msg.edit_text.reset_mock()
    with patch.object(
        dl,
        "_download",
        side_effect=ClassifiedDownloadError(
            "לא ניתן לחלץ מזהה קובץ מהקישור של Google Drive", is_safe=True
        ),
    ):
        dl._start()
        bot_msg.edit_text.assert_called_with(
            "לא ניתן לחלץ מזהה קובץ מהקישור של Google Drive"
        )
