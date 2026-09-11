"""Pytest configuration and test isolation."""

import os
import sys
from pathlib import Path

# Ensure in-memory database and test credentials before any project module is imported
os.environ.setdefault("DB_DSN", "sqlite:///:memory:")
os.environ.setdefault("APP_ID", "12345")
os.environ.setdefault("APP_HASH", "mock_app_hash")
os.environ.setdefault("BOT_TOKEN", "123456:mock_bot_token")
os.environ.setdefault("OWNER", "123456789")

# Add src to sys.path
src_dir = str(Path(__file__).resolve().parent.parent / "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)
