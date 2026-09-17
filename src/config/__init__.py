# pylint: disable=wrong-import-position
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (parent of src directory)
env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)
# Also load from src directory if exists there
env_path_src = Path(__file__).parent.parent / ".env"
load_dotenv(env_path_src)

from config.config import *
from config.constant import *

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s %(filename)s:%(lineno)d %(levelname).1s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def setup_file_logging(
    log_path: str | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> RotatingFileHandler | None:
    """Configure a RotatingFileHandler on the root logger for persistent file logs."""
    target_path = (
        log_path
        or os.getenv("LOG_FILE")
        or os.getenv("BOT_LOG_FILE")
        or "logs/bot.log"
    )
    if max_bytes is None:
        try:
            max_bytes = int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024))
        except (ValueError, TypeError):
            max_bytes = 10 * 1024 * 1024
    if backup_count is None:
        try:
            backup_count = int(os.getenv("LOG_BACKUP_COUNT", 5))
        except (ValueError, TypeError):
            backup_count = 5

    try:
        log_file = Path(target_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            if isinstance(h, RotatingFileHandler) and getattr(h, "_is_bot_log", False):
                if log_path is None or getattr(h, "_bot_log_path", None) == str(log_file):
                    return h
                root_logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass

        handler = RotatingFileHandler(
            str(log_file),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler._is_bot_log = True
        handler._bot_log_path = str(log_file)
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s %(filename)s:%(lineno)d %(levelname).1s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler.setLevel(logging.INFO)
        root_logger.addHandler(handler)
        return handler
    except Exception as e:
        logging.error("Failed to setup RotatingFileHandler for %s: %s", target_path, e)
        return None


setup_file_logging()
