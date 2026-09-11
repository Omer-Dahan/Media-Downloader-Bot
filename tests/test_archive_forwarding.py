"""Tests for archive channel resolution, parsing, and message forwarding."""

import html
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from config.config import _parse_channel_id
from engine.base import BaseDownloader


def test_parse_channel_id():
    """Verify channel IDs are parsed correctly as int or str."""
    assert _parse_channel_id("-1001234567890") == -1001234567890
    assert _parse_channel_id("12345678") == 12345678
    assert _parse_channel_id(-1001234567890) == -1001234567890
    assert _parse_channel_id("@my_archive_channel") == "@my_archive_channel"
    assert _parse_channel_id("https://t.me/my_channel") == "https://t.me/my_channel"
    assert _parse_channel_id("") is None
    assert _parse_channel_id(None) is None


class DummyDownloader(BaseDownloader):
    """Subclass of BaseDownloader for testing archive forwarding."""

    def _start(self):
        pass

    def _download(self):
        return []

    def _setup_formats(self):
        pass


def test_forward_to_archive_custom_caption():
    """Verify _forward_to_archive uses custom caption when provided."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    downloader = DummyDownloader(client, bot_msg, "https://example.com/video")

    sent_msg = SimpleNamespace(id=999, media_group_id=None)

    with patch("engine.base.ARCHIVE_CHANNEL", -100999999):
        downloader._forward_to_archive(
            success=sent_msg,
            files=["/tmp/test.mp4"],
            custom_caption="Custom caption text",
        )

        client.copy_message.assert_called_once()
        call_kwargs = client.copy_message.call_args.kwargs
        assert call_kwargs["chat_id"] == -100999999
        assert call_kwargs["message_id"] == 999
        assert call_kwargs["caption"] == "Custom caption text"


def test_forward_to_archive_uses_subclass_get_archive_caption():
    """Verify _forward_to_archive calls _get_archive_caption on subclass if defined."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    downloader = DummyDownloader(client, bot_msg, "https://example.com/video")
    downloader._get_archive_caption = MagicMock(return_value="Caption from subclass method")

    sent_msg = SimpleNamespace(id=888, media_group_id=None)

    with patch("engine.base.ARCHIVE_CHANNEL", -100999999):
        downloader._forward_to_archive(
            success=sent_msg,
            files=["/tmp/test.mp4"],
        )

        downloader._get_archive_caption.assert_called_once_with(["/tmp/test.mp4"])
        client.copy_message.assert_called_once()
        assert client.copy_message.call_args.kwargs["caption"] == "Caption from subclass method"


def test_forward_to_archive_handles_channel_invalid_gracefully(caplog):
    """Verify that CHANNEL_INVALID from Telegram does not raise an exception or crash."""
    client = MagicMock()
    client.copy_message.side_effect = Exception("Telegram says: [400 CHANNEL_INVALID] - The provided channel is invalid.")

    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    downloader = DummyDownloader(client, bot_msg, "https://example.com/video")
    sent_msg = SimpleNamespace(id=777, media_group_id=None)

    with patch("engine.base.ARCHIVE_CHANNEL", -100999999):
        # Must not raise an exception
        downloader._forward_to_archive(
            success=sent_msg,
            files=["/tmp/test.mp4"],
        )

    # Verify informative warning was logged
    assert any("Archive channel (-100999999) is not accessible" in record.message for record in caplog.records)


def test_forward_to_archive_handles_split_parts_list():
    """Verify that when success is a list of split parts, all parts are copied."""
    client = MagicMock()
    bot_msg = MagicMock()
    bot_msg.chat.id = 12345
    bot_msg.chat.type = "private"
    bot_msg.id = 100

    downloader = DummyDownloader(client, bot_msg, "https://example.com/large_video")

    part1 = SimpleNamespace(id=101, media_group_id=None, caption="part 1")
    part2 = SimpleNamespace(id=102, media_group_id=None, caption="part 2")

    with patch("engine.base.ARCHIVE_CHANNEL", -100999999):
        downloader._forward_to_archive(
            success=[part1, part2],
            files=["/tmp/part1.mp4", "/tmp/part2.mp4"],
        )

        assert client.copy_message.call_count == 2
        call1_kwargs = client.copy_message.call_args_list[0].kwargs
        call2_kwargs = client.copy_message.call_args_list[1].kwargs
        assert call1_kwargs["message_id"] == 101
        assert call2_kwargs["message_id"] == 102
