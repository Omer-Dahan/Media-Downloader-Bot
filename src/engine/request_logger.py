"""
Request Logger - Per-request log capture using Context Variables.

Captures all logs from a request's lifecycle for detailed error reporting
and persists them to disk in logs/requests/<date>_<user>_<hash>.log.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import os
from pathlib import Path
import re
import time
from contextvars import ContextVar
from io import StringIO


@dataclass
class RequestLogContext:
    url: str
    user_id: int | str
    start_time: datetime
    buffer: StringIO


# Context variables to hold the current request context and log buffer.
# Each worker thread/task carries its own buffer; a single shared handler
# (registered once below) routes records into whichever buffer is active in
# the current context.
_request_context: ContextVar[RequestLogContext | None] = ContextVar(
    "request_context", default=None
)
_request_buffer: ContextVar[StringIO | None] = ContextVar(
    "request_buffer", default=None
)


class RequestLogHandler(logging.Handler):
    """Logging handler that writes to the current request's buffer."""

    def emit(self, record):
        ctx = _request_context.get()
        buf = ctx.buffer if ctx is not None else _request_buffer.get()
        if buf is not None:
            try:
                msg = self.format(record)
                buf.write(msg + "\n")
            except Exception:
                pass  # Don't let logging errors break the app


# Register a single handler on the root logger at import time. This avoids
# accumulating one handler per request (which caused duplicate log lines and a
# slow handler leak) - the per-request isolation comes from the ContextVar.
_shared_handler = RequestLogHandler()
_shared_handler.setFormatter(
    logging.Formatter("[%(asctime)s %(levelname)s] %(message)s", datefmt="%H:%M:%S")
)
_shared_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_shared_handler)


def format_request_log_filename(url: str, user_id: int | str, dt: datetime | None = None) -> str:
    """
    Format filename for per-request log file: <date>_<user>_<hash>.log.
    """
    if dt is None:
        dt = datetime.now()
    date_str = dt.strftime("%Y%m%d")
    user_str = str(user_id)
    url_hash = hashlib.md5(
        str(url).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:8]
    return f"{date_str}_{user_str}_{url_hash}.log"


def start_request_log(url: str, user_id: int) -> None:
    """
    Start capturing logs for a new request by setting up a fresh buffer in the
    current context.

    Args:
        url: The URL being processed
        user_id: The user ID making the request
    """
    buf = StringIO()
    ctx = RequestLogContext(
        url=str(url),
        user_id=user_id,
        start_time=datetime.now(),
        buffer=buf,
    )
    _request_context.set(ctx)
    _request_buffer.set(buf)

    # Write request header
    buf.write("=== Request Start ===\n")
    buf.write(f"URL: {url}\n")
    buf.write(f"User: {user_id}\n")
    buf.write(f"{'='*20}\n")


def get_request_log() -> str:
    """
    Get captured logs for current request.
    Applies redaction for sensitive data.

    Returns:
        The captured log content with sensitive data redacted
    """
    ctx = _request_context.get()
    buf = ctx.buffer if ctx is not None else _request_buffer.get()
    if buf is None:
        return ""
    content = buf.getvalue()
    return _redact_sensitive(content)


def _redact_sensitive(text: str) -> str:
    """
    Remove tokens, auth params, signatures from logs.

    Args:
        text: The log text to redact

    Returns:
        Text with sensitive data replaced with [REDACTED]
    """
    patterns = [
        (r'(token=)[^&\s\'"]+', r"\1[REDACTED]"),
        (r'(auth=)[^&\s\'"]+', r"\1[REDACTED]"),
        (r'(signature=)[^&\s\'"]+', r"\1[REDACTED]"),
        (r'(key=)[^&\s\'"]+', r"\1[REDACTED]"),
        (r'(secret=)[^&\s\'"]+', r"\1[REDACTED]"),
        (r'(password=)[^&\s\'"]+', r"\1[REDACTED]"),
        (r'(api_key=)[^&\s\'"]+', r"\1[REDACTED]"),
        (r'(access_token=)[^&\s\'"]+', r"\1[REDACTED]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def cleanup_request_logs(
    log_dir: Path | str | None = None,
    max_days: int | None = None,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> list[Path]:
    """
    Clean up request log files according to age, count, and size retention limits.

    - Deletes files older than max_days (default 14 days, configurable via REQUEST_LOG_MAX_DAYS).
    - Deletes oldest files if remaining count exceeds max_files (configurable via REQUEST_LOG_MAX_FILES).
    - Deletes oldest files if total directory size exceeds max_bytes (configurable via REQUEST_LOG_MAX_BYTES).
    - Never touches bot.log or its rotated backups (bot.log.1, etc.).
    - Logs each deleted file and total count/bytes freed.

    Returns:
        List of deleted Path objects.
    """
    if log_dir is None:
        target_dir = Path(os.getenv("REQUEST_LOG_DIR") or "logs/requests")
    else:
        target_dir = Path(log_dir)

    if not target_dir.exists() or not target_dir.is_dir():
        return []

    if max_days is None:
        val = os.getenv("REQUEST_LOG_MAX_DAYS") or os.getenv("REQUEST_LOG_RETENTION_DAYS")
        try:
            max_days = int(val) if val is not None else 14
        except (ValueError, TypeError):
            max_days = 14

    if max_files is None:
        val = os.getenv("REQUEST_LOG_MAX_FILES") or os.getenv("REQUEST_LOG_MAX_COUNT")
        try:
            max_files = int(val) if val is not None else 1000
        except (ValueError, TypeError):
            max_files = 1000

    if max_bytes is None:
        val = os.getenv("REQUEST_LOG_MAX_BYTES") or os.getenv("REQUEST_LOG_MAX_SIZE")
        try:
            max_bytes = int(val) if val is not None else 100 * 1024 * 1024
        except (ValueError, TypeError):
            max_bytes = 100 * 1024 * 1024

    def _is_protected(p: Path) -> bool:
        name = p.name
        bot_log_name = Path(os.getenv("LOG_FILE") or "logs/bot.log").name
        if name == bot_log_name or name.startswith(f"{bot_log_name}."):
            return True
        if name == "bot.log" or name.startswith("bot.log."):
            return True
        if "bot.log" in name:
            return True
        return False

    candidates = []
    try:
        for entry in target_dir.iterdir():
            if not entry.is_file():
                continue
            if _is_protected(entry):
                continue
            if not entry.name.endswith(".log"):
                continue
            candidates.append(entry)
    except Exception as e:
        logging.warning("Failed to list request log directory %s: %s", target_dir, e)
        return []

    now = time.time()
    deleted: list[Path] = []
    total_freed_bytes = 0

    def _get_file_time_and_size(p: Path) -> tuple[float, int]:
        try:
            st = p.stat()
            ft = st.st_mtime
            match = re.match(r"^(\d{8})_", p.name)
            if match:
                try:
                    dt = datetime.strptime(match.group(1), "%Y%m%d")
                    fn_ts = dt.timestamp()
                    if fn_ts < ft:
                        ft = fn_ts
                except Exception:
                    pass
            return ft, st.st_size
        except Exception:
            return 0.0, 0

    file_info = []
    for f in candidates:
        ft, sz = _get_file_time_and_size(f)
        file_info.append((f, ft, sz))

    survivors: list[tuple[Path, float, int]] = []
    if max_days is not None and max_days >= 0:
        cutoff_seconds = max_days * 86400
        for f, ft, sz in file_info:
            age_seconds = now - ft
            if age_seconds > cutoff_seconds:
                try:
                    f.unlink()
                    deleted.append(f)
                    total_freed_bytes += sz
                    logging.info(
                        "Deleted expired request log '%s' (age: %.1f days, %d bytes)",
                        f.name,
                        age_seconds / 86400,
                        sz,
                    )
                except Exception as e:
                    logging.warning("Failed to delete expired request log %s: %s", f, e)
            else:
                survivors.append((f, ft, sz))
    else:
        survivors = file_info

    # Sort survivors by timestamp ascending (oldest first)
    survivors.sort(key=lambda item: item[1])

    if max_files is not None and max_files >= 0 and len(survivors) > max_files:
        to_remove_count = len(survivors) - max_files
        to_delete = survivors[:to_remove_count]
        survivors = survivors[to_remove_count:]
        for f, ft, sz in to_delete:
            try:
                f.unlink()
                deleted.append(f)
                total_freed_bytes += sz
                logging.info(
                    "Deleted request log '%s' exceeding max_files ceiling (%d bytes)",
                    f.name,
                    sz,
                )
            except Exception as e:
                logging.warning("Failed to delete excess request log %s: %s", f, e)

    if max_bytes is not None and max_bytes >= 0:
        current_bytes = sum(sz for _, _, sz in survivors)
        while survivors and current_bytes > max_bytes:
            f, ft, sz = survivors.pop(0)
            try:
                f.unlink()
                deleted.append(f)
                total_freed_bytes += sz
                current_bytes -= sz
                logging.info(
                    "Deleted request log '%s' exceeding max_bytes ceiling (%d bytes)",
                    f.name,
                    sz,
                )
            except Exception as e:
                logging.warning("Failed to delete excess request log %s: %s", f, e)

    if deleted:
        logging.info(
            "Request log cleanup completed for %s: deleted %d file(s), freed %d bytes",
            target_dir,
            len(deleted),
            total_freed_bytes,
        )

    return deleted


def end_request_log() -> Path | None:
    """
    Clean up request log context by closing and clearing the current buffer.
    Always saves the captured request log to disk with sensitive data redacted
    under logs/requests/<date>_<user>_<hash>.log.
    """
    ctx = _request_context.get()
    buf = ctx.buffer if ctx is not None else _request_buffer.get()
    saved_path = None

    if buf is not None:
        try:
            content = _redact_sensitive(buf.getvalue())
            request_log_dir = Path(os.getenv("REQUEST_LOG_DIR") or "logs/requests")
            request_log_dir.mkdir(parents=True, exist_ok=True)

            if ctx is not None:
                filename = format_request_log_filename(ctx.url, ctx.user_id, ctx.start_time)
            else:
                filename = format_request_log_filename("", "unknown", datetime.now())

            log_path = request_log_dir / filename
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(content)
            saved_path = log_path
        except Exception as e:
            logging.error("Failed to save request log to file: %s", e)
        finally:
            try:
                buf.close()
            except Exception:
                pass

    # Reset context variables
    _request_context.set(None)
    _request_buffer.set(None)

    # Run cleanup of request log directory
    try:
        cleanup_request_logs()
    except Exception as e:
        logging.warning("Request log cleanup failed: %s", e)

    return saved_path
