"""Tests for FFmpeg thumbnail generation parameters."""

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import ffmpeg
from engine.base import BaseDownloader


class DummyDownloader(BaseDownloader):
    def _start(self):
        pass

    def _download(self):
        return []

    def _setup_formats(self):
        pass


def test_ffmpeg_thumbnail_command_includes_update_flag():
    """Verify that FFmpeg output stream configured for thumbnail includes the -update 1 flag."""
    input_stream = ffmpeg.input("sample.mp4", ss=10)
    filtered = input_stream.filter("scale", 300, -1)
    output_stream = filtered.output("thumb.png", vframes=1, update=1)

    cmd = ffmpeg.compile(output_stream)
    assert "-update" in cmd
    idx = cmd.index("-update")
    assert cmd[idx + 1] == "1"
    assert "-vframes" in cmd
    idx_v = cmd.index("-vframes")
    assert cmd[idx_v + 1] == "1"


def test_base_downloader_get_metadata_invokes_ffmpeg_with_quiet_and_update():
    """Verify get_metadata passes update=1 and runs with quiet=True."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    downloader = DummyDownloader(client, bot_msg, "https://example.com/video")

    dummy_video = Path(downloader._tempdir.name) / "test_video.mp4"
    dummy_video.write_bytes(b"dummy_video_data")

    mock_probe = {
        "format": {"duration": "120.0"},
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
    }

    with patch("ffmpeg.probe", return_value=mock_probe), \
         patch("ffmpeg.input") as mock_input:

        mock_filter = MagicMock()
        mock_output = MagicMock()
        mock_run = MagicMock()

        mock_input.return_value.filter.return_value = mock_filter
        mock_filter.output.return_value = mock_output
        mock_output.run = mock_run

        def fake_run(*args, **kwargs):
            out_file = mock_filter.output.call_args[0][0]
            Path(out_file).write_bytes(b"x" * 200)

        mock_run.side_effect = fake_run

        meta = downloader.get_metadata()

        # Verify output was called with update=1
        mock_filter.output.assert_called_once()
        _, output_kwargs = mock_filter.output.call_args
        assert output_kwargs.get("vframes") == 1
        assert output_kwargs.get("update") == 1

        # Verify run was called with quiet=True, overwrite_output=True
        mock_run.assert_called_once()
        _, run_kwargs = mock_run.call_args
        assert run_kwargs.get("quiet") is True
        assert run_kwargs.get("overwrite_output") is True

        # And verify thumbnail was returned in metadata
        assert meta["thumb"] is not None
        assert Path(meta["thumb"]).exists()
