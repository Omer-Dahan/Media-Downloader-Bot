"""Tests for size formatting utilities and imports."""

import pytest
from utils.formatters import sizeof_fmt as sizeof_fmt_module
from utils import sizeof_fmt as sizeof_fmt_utils
from engine.helper import sizeof_fmt as sizeof_fmt_helper


def test_sizeof_fmt_values():
    """Verify sizeof_fmt correctly formats bytes to human-readable units."""
    assert sizeof_fmt_module(0) == "0.0B"
    assert sizeof_fmt_module(1023) == "1023.0B"
    assert sizeof_fmt_module(1024) == "1.0KiB"
    assert sizeof_fmt_module(1024 * 1024) == "1.0MiB"
    assert sizeof_fmt_module(1024 * 1024 * 1024) == "1.0GiB"
    assert sizeof_fmt_module(5 * 1024 * 1024 * 1024) == "5.0GiB"


def test_sizeof_fmt_reexports():
    """Verify sizeof_fmt is consistently available across packages without circular imports."""
    assert sizeof_fmt_utils is sizeof_fmt_module
    assert sizeof_fmt_helper is sizeof_fmt_module
