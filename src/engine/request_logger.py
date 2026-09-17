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
    return saved_path
