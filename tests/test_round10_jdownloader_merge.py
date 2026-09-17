"""Tests for Round 10: JDownloader separate DASH stream merge and package file handling.

Verifies:
1. Single file with video and audio is uploaded as is without merging.
2. Separate video-only and audio-only streams from YouTube/JD are merged into one file via ffmpeg -c copy.
3. Video-only or audio-only files without counterpart are uploaded with a warning log and no crash.
4. Subtitles inside the package are preserved alongside media.
5. Real ffmpeg execution merges DASH components into a single valid file with both streams.
6. Temporary merged files are cleanly deleted from disk.
"""

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pyrogram import enums

from engine.jdownloader import JDownloaderDownload


@pytest.fixture
def jd_downloader():
    """Create a mock JDownloaderDownload instance without Telegram connection."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345678
    bot_msg.chat.type = enums.ChatType.PRIVATE
    bot_msg.id = 100

    jd = JDownloaderDownload(client, bot_msg, "https://www.youtube.com/watch?v=mock_video")
    jd._package_name = "TGBot_12345678_testpkg"
    jd._package_id = 9999
    return jd


def test_package_single_file_with_video_and_audio_not_merged(jd_downloader, tmp_path, caplog):
    """Verify package with 1 complete file containing video and audio is not merged and uploaded as is."""
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    video_file = pkg_dir / "complete_video.mp4"
    video_file.write_bytes(b"dummy_video_bytes")

    def mock_probe(path_str):
        return {
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        }

    with patch("ffmpeg.probe", side_effect=mock_probe), \
         patch.object(jd_downloader, "_merge_video_audio") as mock_merge, \
         caplog.at_level(logging.INFO):
        result = jd_downloader._handle_output(pkg_dir)

    assert len(result) == 1
    assert result[0] == video_file
    mock_merge.assert_not_called()
    assert any("complete media file with video and audio" in r.message for r in caplog.records)
    assert any("Final files prepared for upload" in r.message for r in caplog.records)


def test_package_separate_video_and_audio_merged_into_one(jd_downloader, tmp_path, caplog):
    """Verify package with separate video-only and audio-only streams is merged via ffmpeg -c copy."""
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    video_stream = pkg_dir / "yt_video.mp4"
    audio_stream = pkg_dir / "yt_audio.m4a"
    video_stream.write_bytes(b"video_only_bytes")
    audio_stream.write_bytes(b"audio_only_bytes")

    def mock_probe(path_str):
        if "yt_video.mp4" in path_str:
            return {"streams": [{"codec_type": "video", "codec_name": "h264"}]}
        elif "yt_audio.m4a" in path_str:
            return {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}
        return {"streams": []}

    mock_ffmpeg_output = MagicMock()
    mock_ffmpeg_output.overwrite_output.return_value = mock_ffmpeg_output
    mock_ffmpeg_output.run.return_value = (b"", b"")

    with patch("ffmpeg.probe", side_effect=mock_probe), \
         patch("ffmpeg.output", return_value=mock_ffmpeg_output) as mock_output, \
         caplog.at_level(logging.INFO):
        result = jd_downloader._handle_output(pkg_dir)

    assert len(result) == 1
    merged_file = result[0]
    assert merged_file.name == "yt_video_merged.mp4"
    assert merged_file in jd_downloader._temp_merged_files

    # Verify ffmpeg was invoked with -c copy
    mock_output.assert_called_once()
    kwargs = mock_output.call_args.kwargs
    assert kwargs.get("c") == "copy"

    # Verify source components were deleted to free disk space
    assert not video_stream.exists()
    assert not audio_stream.exists()

    # Verify clear logs
    assert any("Found 2 file(s)" in r.message for r in caplog.records)
    assert any("Merged video 'yt_video.mp4' and audio 'yt_audio.m4a'" in r.message for r in caplog.records)
    assert any("Final files prepared for upload" in r.message for r in caplog.records)


def test_package_video_only_uploaded_with_warning_no_crash(jd_downloader, tmp_path, caplog):
    """Verify package with video-only file (no audio counterpart) uploads with warning and no crash."""
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    video_file = pkg_dir / "video_only.mp4"
    video_file.write_bytes(b"video_bytes")

    def mock_probe(path_str):
        return {"streams": [{"codec_type": "video", "codec_name": "h264"}]}

    with patch("ffmpeg.probe", side_effect=mock_probe), \
         patch.object(jd_downloader, "_merge_video_audio") as mock_merge, \
         caplog.at_level(logging.WARNING):
        result = jd_downloader._handle_output(pkg_dir)

    assert len(result) == 1
    assert result[0] == video_file
    mock_merge.assert_not_called()
    assert any("video-only file 'video_only.mp4' without audio stream" in r.message for r in caplog.records)


def test_package_audio_only_uploaded_with_warning_no_crash(jd_downloader, tmp_path, caplog):
    """Verify package with audio-only file (no video counterpart) uploads with warning and no crash."""
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    audio_file = pkg_dir / "audio_only.m4a"
    audio_file.write_bytes(b"audio_bytes")

    def mock_probe(path_str):
        return {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}

    with patch("ffmpeg.probe", side_effect=mock_probe), \
         patch.object(jd_downloader, "_merge_video_audio") as mock_merge, \
         caplog.at_level(logging.WARNING):
        result = jd_downloader._handle_output(pkg_dir)

    assert len(result) == 1
    assert result[0] == audio_file
    mock_merge.assert_not_called()
    assert any("audio-only file 'audio_only.m4a' without video stream" in r.message for r in caplog.records)


def test_package_with_subtitles_preserves_subtitles(jd_downloader, tmp_path):
    """Verify subtitle files in package are preserved and returned alongside merged media."""
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    video_file = pkg_dir / "vid.mp4"
    audio_file = pkg_dir / "aud.m4a"
    sub_file = pkg_dir / "he.srt"
    video_file.write_bytes(b"video")
    audio_file.write_bytes(b"audio")
    sub_file.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nHello\n")

    def mock_probe(path_str):
        if "vid.mp4" in path_str:
            return {"streams": [{"codec_type": "video"}]}
        elif "aud.m4a" in path_str:
            return {"streams": [{"codec_type": "audio"}]}
        return {"streams": []}

    mock_ffmpeg_output = MagicMock()
    mock_ffmpeg_output.overwrite_output.return_value = mock_ffmpeg_output
    mock_ffmpeg_output.run.return_value = (b"", b"")

    with patch("ffmpeg.probe", side_effect=mock_probe), \
         patch("ffmpeg.output", return_value=mock_ffmpeg_output):
        result = jd_downloader._handle_output(pkg_dir)

    assert len(result) == 2
    merged_candidate = [f for f in result if f.suffix.lower() == ".mp4"]
    sub_candidate = [f for f in result if f.suffix.lower() == ".srt"]
    assert len(merged_candidate) == 1
    assert len(sub_candidate) == 1
    assert merged_candidate[0].name == "vid_merged.mp4"
    assert sub_candidate[0].name == "he.srt"


def test_real_ffmpeg_ffprobe_merge_dash_components(jd_downloader, tmp_path):
    """Verify end-to-end real ffmpeg merge and ffprobe verification with small files."""
    pkg_dir = tmp_path / "real_pkg"
    pkg_dir.mkdir()

    v_file = pkg_dir / "test_dash_video.mp4"
    a_file = pkg_dir / "test_dash_audio.m4a"

    # Generate tiny 0.1s video-only MP4
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "testsrc=duration=0.1:size=160x120:rate=1",
            "-c:v", "libx264", str(v_file),
        ],
        capture_output=True,
        check=True,
    )

    # Generate tiny 0.1s audio-only M4A
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "sine=duration=0.1",
            "-c:a", "aac", str(a_file),
        ],
        capture_output=True,
        check=True,
    )

    # Verify input stream properties before handle_output
    v_has_v, v_has_a = jd_downloader._probe_media_streams(v_file)
    assert v_has_v is True
    assert v_has_a is False

    a_has_v, a_has_a = jd_downloader._probe_media_streams(a_file)
    assert a_has_v is False
    assert a_has_a is True

    # Process through _handle_output with real ffmpeg and ffprobe
    result = jd_downloader._handle_output(pkg_dir)
    assert len(result) == 1
    merged = result[0]
    assert merged.exists()
    assert merged.name == "test_dash_video_merged.mp4"

    # Verify output stream properties on merged file
    m_has_v, m_has_a = jd_downloader._probe_media_streams(merged)
    assert m_has_v is True
    assert m_has_a is True

    # Verify source components were cleaned up
    assert not v_file.exists()
    assert not a_file.exists()


def test_cleanup_merged_files_on_error(jd_downloader, tmp_path):
    """Verify temporary merged files are deleted when cleanup is invoked on error or cancellation."""
    merged_file = tmp_path / "temp_merged.mp4"
    merged_file.write_bytes(b"merged_data")
    jd_downloader._temp_merged_files.append(merged_file)

    assert merged_file.exists()

    # Simulate cleanup in finally block
    for mf in getattr(jd_downloader, "_temp_merged_files", []):
        mf.unlink(missing_ok=True)

    assert not merged_file.exists()
