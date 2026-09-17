"""Tests for Round 9 enhancements:

1. JavaScript runtime detection and n-challenge error translation.
2. RotatingFileHandler and per-request persistent logging.
3. JDownloader media link preservation (video + audio) and junk filtering.
"""

from datetime import datetime
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from engine.base import ClassifiedDownloadError
from engine.generic import (
    check_and_ensure_js_runtime,
    classify_download_error,
    ClassifiedMessage,
)
from engine.jdownloader_manager import JDownloaderManager, JDownloaderError
from engine.request_logger import (
    start_request_log,
    end_request_log,
    get_request_log,
    format_request_log_filename,
)
from main import get_user_friendly_error_message, report_error_to_archive


# ---------------------------------------------------------------------------
# Requirement 1: JavaScript Runtime Detection & Error Translation
# ---------------------------------------------------------------------------

def test_check_and_ensure_js_runtime_detects_runtime(caplog):
    """Verify check_and_ensure_js_runtime detects a supported runtime and logs info."""
    mock_info = SimpleNamespace(
        name="node", version="26.7.0", path="/usr/bin/node", supported=True
    )
    mock_runtime = MagicMock()
    mock_runtime.info = mock_info

    with patch("yt_dlp.utils._jsruntime.NodeJsRuntime", return_value=mock_runtime):
        with caplog.at_level(logging.INFO):
            result = check_and_ensure_js_runtime()

    assert result["status"] == "ok"
    assert any("Found supported JavaScript runtime for yt-dlp" in r.message for r in caplog.records)


def test_check_and_ensure_js_runtime_warning_when_missing(caplog):
    """Verify warning is logged when no supported JS runtime is found in PATH or common dirs."""
    mock_runtime = MagicMock()
    mock_runtime.info = None

    with patch("yt_dlp.utils._jsruntime.NodeJsRuntime", return_value=mock_runtime), \
         patch("yt_dlp.utils._jsruntime.DenoJsRuntime", return_value=mock_runtime), \
         patch("yt_dlp.utils._jsruntime.QuickJsRuntime", return_value=mock_runtime), \
         patch("yt_dlp.utils._jsruntime.BunJsRuntime", return_value=mock_runtime), \
         patch("shutil.which", return_value=None):
        with caplog.at_level(logging.WARNING):
            result = check_and_ensure_js_runtime()

    assert result["status"] == "missing"
    assert any("No supported JavaScript runtime found in PATH" in r.message for r in caplog.records)
    assert any("The page needs to be reloaded" in r.message for r in caplog.records)


def test_check_and_ensure_js_runtime_warning_when_unsupported(caplog):
    """Verify warning is logged when a runtime is found but its version is unsupported."""
    mock_info = SimpleNamespace(
        name="node", version="18.0.0", path="/usr/bin/node", supported=False
    )
    mock_runtime = MagicMock()
    mock_runtime.info = mock_info
    mock_none_runtime = MagicMock()
    mock_none_runtime.info = None

    with patch("yt_dlp.utils._jsruntime.NodeJsRuntime", return_value=mock_runtime), \
         patch("yt_dlp.utils._jsruntime.DenoJsRuntime", return_value=mock_none_runtime), \
         patch("yt_dlp.utils._jsruntime.QuickJsRuntime", return_value=mock_none_runtime), \
         patch("yt_dlp.utils._jsruntime.BunJsRuntime", return_value=mock_none_runtime):
        with caplog.at_level(logging.WARNING):
            result = check_and_ensure_js_runtime()

    assert result["status"] == "unsupported"
    assert any("not supported by yt-dlp" in r.message for r in caplog.records)


def test_check_and_ensure_js_runtime_augments_path(tmp_path):
    """Verify common directories that exist on disk are prepended to os.environ['PATH']."""
    dummy_local_bin = tmp_path / "fake_local_bin"
    dummy_local_bin.mkdir()

    original_path = os.environ.get("PATH", "")
    try:
        with patch("engine.generic.os.path.expanduser") as mock_expand:
            mock_expand.side_effect = lambda p: str(dummy_local_bin) if "~/.local/bin" in p else p
            check_and_ensure_js_runtime()
            assert str(dummy_local_bin) in os.environ.get("PATH", "").split(os.pathsep)
    finally:
        os.environ["PATH"] = original_path


def test_classify_download_error_js_runtime_and_n_challenge():
    """Verify n challenge and solver script errors are translated to informative Hebrew."""
    err1 = (
        "[youtube] oZIF91nmp0k: n challenge solving failed: Some formats may be missing. "
        "Ensure you have a supported JavaScript runtime and challenge solver script distribution installed."
    )
    msg1 = classify_download_error(err1)
    assert isinstance(msg1, ClassifiedMessage)
    assert msg1.is_safe is True
    assert "runtime של JavaScript" in msg1
    assert "Node.js" in msg1
    assert "Deno" in msg1

    err2 = "Ensure you have a supported JavaScript runtime and challenge solver script distribution installed."
    msg2 = classify_download_error(err2)
    assert msg2.is_safe is True
    assert "runtime של JavaScript" in msg2

    err3 = "Signature solving failed: Some formats may be missing."
    msg3 = classify_download_error(err3)
    assert msg3.is_safe is True
    assert "runtime של JavaScript" in msg3


def test_classify_download_error_page_needs_to_be_reloaded():
    """Verify 'The page needs to be reloaded' is translated to informative Hebrew."""
    err = "ERROR: [youtube] oZIF91nmp0k: The page needs to be reloaded."
    msg = classify_download_error(err)
    assert isinstance(msg, ClassifiedMessage)
    assert msg.is_safe is True
    assert "The page needs to be reloaded" not in msg
    assert "runtime של JavaScript" in msg
    assert "Node.js" in msg


def test_main_user_friendly_error_message_for_js_runtime():
    """Verify get_user_friendly_error_message formats the classified JS runtime error for user."""
    raw_msg = (
        "שגיאת פענוח ביוטיוב: חסר בשרת runtime של JavaScript (כגון Node.js או Deno) "
        "הנדרש לפענוח חתימות יוטיוב (n challenge solver).\n"
        "יש להתקין בשרת Node.js (גרסה 22 ומעלה) או Deno."
    )
    err = ClassifiedDownloadError(raw_msg, is_safe=True)
    res = get_user_friendly_error_message(err)
    assert res.startswith("❌ ")
    assert "runtime של JavaScript" in res
    assert "Node.js" in res


def test_report_error_to_archive_includes_js_runtime_error():
    """Verify report_error_to_archive reports the translated Hebrew error to the archive channel."""
    client = MagicMock()
    user = SimpleNamespace(id=12345678, first_name="Tester", username="testuser")
    url = "https://www.youtube.com/watch?v=oZIF91nmp0k"
    error = ClassifiedDownloadError(
        "שגיאת פענוח ביוטיוב: חסר בשרת runtime של JavaScript (כגון Node.js או Deno) הנדרש לפענוח חתימות יוטיוב.",
        is_safe=True,
    )

    with patch("main.ARCHIVE_CHANNEL", -100123456789):
        report_error_to_archive(client, user, url, error)

    client.send_message.assert_called_once()
    caption = client.send_message.call_args.kwargs["text"]
    assert "runtime של JavaScript" in caption
    assert "oZIF91nmp0k" in caption


# ---------------------------------------------------------------------------
# Requirement 2: Persistent Log Files (RotatingFileHandler & request_logger)
# ---------------------------------------------------------------------------

def test_rotating_file_handler_setup(tmp_path):
    """Verify RotatingFileHandler writes logs to the configured path with 10MB/5 backups."""
    from config import setup_file_logging

    log_path = tmp_path / "test_bot.log"
    handler = setup_file_logging(
        log_path=str(log_path),
        max_bytes=10 * 1024 * 1024,
        backup_count=5,
    )
    assert handler is not None
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 5
    assert handler.encoding == "utf-8"

    # Ensure root logger passes INFO records to handler
    logging.getLogger().setLevel(logging.INFO)
    logging.info("Test message for file log verification")
    handler.flush()

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "Test message for file log verification" in content

    # Clean up handler from root logger
    logging.getLogger().removeHandler(handler)


def test_gitignore_contains_logs():
    """Verify .gitignore includes the logs/ directory."""
    gitignore_path = Path(".gitignore")
    assert gitignore_path.exists()
    lines = [line.strip() for line in gitignore_path.read_text(encoding="utf-8").splitlines()]
    assert "logs/" in lines


def test_request_logger_format_filename():
    """Verify request log filename follows <date>_<user>_<hash>.log format."""
    dt = datetime(2026, 9, 17, 14, 30, 0)
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    expected_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    filename = format_request_log_filename(url, 987654, dt)

    assert filename == f"20260917_987654_{expected_hash}.log"
    parts = filename.replace(".log", "").split("_")
    assert len(parts) == 3
    assert parts[0] == "20260917"
    assert parts[1] == "987654"
    assert parts[2] == expected_hash


def test_request_logger_always_writes_to_file_and_redacts_secrets(tmp_path):
    """Verify request_logger always writes log file with redacted secrets."""
    requests_dir = tmp_path / "requests"
    url = "https://example.com/video?token=super_secret_123&api_key=my_key_abc"
    user_id = 5551234

    logging.getLogger().setLevel(logging.INFO)
    with patch.dict(os.environ, {"REQUEST_LOG_DIR": str(requests_dir)}):
        start_request_log(url, user_id)

        # Log some messages through standard logging
        logging.info("Step 1: processing download for user")
        logging.info("Sensitive token in log: token=super_secret_123")
        logging.info("Sensitive key in log: api_key=my_key_abc")

        # Verify get_request_log in memory contains redacted content before ending
        in_memory_log = get_request_log()
        assert "[REDACTED]" in in_memory_log
        assert "super_secret_123" not in in_memory_log

        # End request log (persists to file)
        saved_path = end_request_log()

    assert saved_path is not None
    assert saved_path.exists()
    assert saved_path.parent == requests_dir

    file_content = saved_path.read_text(encoding="utf-8")
    assert "=== Request Start ===" in file_content
    assert "Step 1: processing download for user" in file_content
    assert "[REDACTED]" in file_content
    assert "super_secret_123" not in file_content
    assert "my_key_abc" not in file_content


# ---------------------------------------------------------------------------
# Requirement 3: JDownloader Link Filtering (Video + Audio vs Junk)
# ---------------------------------------------------------------------------

def test_jdownloader_extensions_classification():
    """Verify audio extensions are classified as media and junk extensions as junk."""
    # Video extensions
    assert JDownloaderManager._is_video_link({"name": "movie.mp4"}) is True
    assert JDownloaderManager._is_media_link({"name": "movie.mp4"}) is True
    assert JDownloaderManager._is_junk_link({"name": "movie.mp4"}) is False

    # Audio extensions
    for ext in (".m4a", ".opus", ".ogg", ".weba", ".mp3", ".aac", ".flac", ".wav"):
        link = {"name": f"audio_track{ext}"}
        assert JDownloaderManager._is_audio_link(link) is True, f"Failed for {ext}"
        assert JDownloaderManager._is_media_link(link) is True, f"Failed for {ext}"
        assert JDownloaderManager._is_junk_link(link) is False, f"Failed for {ext}"

    # Junk extensions
    for ext in (".html", ".json", ".xml", ".jpg", ".png", ".srt", ".vtt", ".torrent", ".part"):
        link = {"name": f"file{ext}"}
        assert JDownloaderManager._is_junk_link(link) is True, f"Failed for {ext}"
        assert JDownloaderManager._is_media_link(link) is False, f"Failed for {ext}"


def test_jdownloader_filter_links_keeps_video_and_audio(caplog):
    """Verify JDownloader keeps both video and audio links when processing packages."""
    mgr = JDownloaderManager.__new__(JDownloaderManager)
    mgr._device = MagicMock()

    mock_links = [
        {"uuid": 1, "name": "video_1080p.mp4", "url": "https://rr.googlevideo.com/videoplayback?id=1"},
        {"uuid": 2, "name": "audio_128k.m4a", "url": "https://rr.googlevideo.com/videoplayback?id=2"},
        {"uuid": 3, "name": "subtitles_en.srt", "url": "https://youtube.com/api/timedtext?lang=en"},
        {"uuid": 4, "name": "thumbnail.jpg", "url": "https://i.ytimg.com/vi/abc/maxresdefault.jpg"},
        {"uuid": 5, "name": "info.json", "url": "https://youtube.com/info.json"},
    ]
    mgr._device.linkgrabber.query_links.return_value = mock_links

    with caplog.at_level(logging.INFO):
        mgr._filter_video_links_in_package(package_id=1789595586502)

    # Verify remove_links was called with junk link UUIDs only (srt, jpg, json)
    mgr._device.linkgrabber.remove_links.assert_called_once()
    removed_ids = mgr._device.linkgrabber.remove_links.call_args.kwargs["link_ids"]
    assert sorted(removed_ids) == [3, 4, 5]

    # Verify both video and audio (UUIDs 1 and 2) were preserved
    assert 1 not in removed_ids
    assert 2 not in removed_ids

    # Verify logged messages contain names and URLs of deleted links
    removed_logs = [r.message for r in caplog.records if "Removing junk link from JD package" in r.message]
    assert len(removed_logs) == 3
    assert any("subtitles_en.srt" in m and "https://youtube.com/api/timedtext" in m for m in removed_logs)
    assert any("thumbnail.jpg" in m and "https://i.ytimg.com/vi/abc/maxresdefault.jpg" in m for m in removed_logs)
    assert any("info.json" in m and "https://youtube.com/info.json" in m for m in removed_logs)


def test_jdownloader_filter_links_all_junk_raises_error():
    """Verify package containing only junk links raises JDownloaderError."""
    mgr = JDownloaderManager.__new__(JDownloaderManager)
    mgr._device = MagicMock()

    mock_links = [
        {"uuid": 10, "name": "index.html", "url": "https://example.com/index.html"},
        {"uuid": 11, "name": "cover.jpg", "url": "https://example.com/cover.jpg"},
    ]
    mgr._device.linkgrabber.query_links.return_value = mock_links

    with pytest.raises(JDownloaderError) as exc_info:
        mgr._filter_video_links_in_package(package_id=999)

    assert "לא נמצאו קבצים מוכרים בקישור" in str(exc_info.value)


def test_youtube_download_captures_n_challenge_from_warning():
    """Verify YoutubeDownload._download detects n-challenge warning and classifies error."""
    from engine.generic import YoutubeDownload

    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    url = "https://www.youtube.com/watch?v=oZIF91nmp0k"
    downloader = YoutubeDownload(client, bot_msg, url)

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts
            # Simulate yt-dlp logger warning
            logger = opts.get("logger")
            if logger:
                logger.warning(
                    "[youtube] oZIF91nmp0k: n challenge solving failed: Some formats may be missing. "
                    "Ensure you have a supported JavaScript runtime and challenge solver script distribution installed."
                )

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def extract_info(self, url, download=True):
            return None

    with patch("engine.generic.yt_dlp.YoutubeDL", side_effect=MockYDL):
        files = downloader._download(["1080", "720"])

    assert not files
    assert downloader._last_download_error is not None
    assert "n challenge solving failed" in downloader._last_download_error

    classified = classify_download_error(downloader._last_download_error, url)
    assert classified.is_safe is True
    assert "runtime של JavaScript" in classified


def test_youtube_download_stops_format_loop_on_reload_error():
    """Verify format loop breaks immediately on fatal n-challenge / reload error."""
    from engine.generic import YoutubeDownload

    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    url = "https://www.youtube.com/watch?v=oZIF91nmp0k"
    downloader = YoutubeDownload(client, bot_msg, url)

    extract_call_count = 0

    class MockYDLError:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def extract_info(self, url, download=True):
            nonlocal extract_call_count
            extract_call_count += 1
            raise Exception("ERROR: [youtube] oZIF91nmp0k: The page needs to be reloaded.")

    with patch("engine.generic.yt_dlp.YoutubeDL", side_effect=MockYDLError):
        downloader._download(["1080", "720", "480", "360"])

    # Must have stopped after first attempt because 'the page needs to be reloaded' is fatal
    assert extract_call_count == 1
    assert "The page needs to be reloaded" in downloader._last_download_error
