"""
Request Logger - Per-request log capture using Context Variables.

Captures all logs from a request's lifecycle for detailed error reporting.
"""

import logging
import re
from contextvars import ContextVar
from io import StringIO

# Context variable to hold the current request's log buffer.
# Each worker thread/task carries its own buffer; a single shared handler
# (registered once below) routes records into whichever buffer is active in
# the current context.
_request_buffer: ContextVar[StringIO | None] = ContextVar(
    "request_buffer", default=None
)


class RequestLogHandler(logging.Handler):
    """Logging handler that writes to the current request's buffer."""

    def emit(self, record):
        buf = _request_buffer.get()
        if buf is not None:
            try:
                msg = self.format(record)
                buf.write(msg + "\n")
            except Exception:
                pass  # Don't let logging errors break the app


# Register a single handler on the root logger at import time. This avoids
# accumulating one handler per request (which caused duplicate log lines and a
# slow handler leak) — the per-request isolation comes from the ContextVar.
_shared_handler = RequestLogHandler()
_shared_handler.setFormatter(
    logging.Formatter("[%(asctime)s %(levelname)s] %(message)s", datefmt="%H:%M:%S")
)
_shared_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_shared_handler)


def start_request_log(url: str, user_id: int) -> None:
    """
    Start capturing logs for a new request by setting up a fresh buffer in the
    current context.

    Args:
        url: The URL being processed
        user_id: The user ID making the request
    """
    buf = StringIO()
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
    buf = _request_buffer.get()
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


def end_request_log() -> None:
    """
    Clean up request log context by closing and clearing the current buffer.
    The shared root-logger handler is intentionally left in place.
    """
    buf = _request_buffer.get()
    if buf is not None:
        try:
            buf.close()
        except Exception:
            pass

    # Reset context variable
    _request_buffer.set(None)
