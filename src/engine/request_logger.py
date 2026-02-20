"""
Request Logger - Per-request log capture using Context Variables.

Captures all logs from a request's lifecycle for detailed error reporting.
"""
import html
import logging
import re
from contextvars import ContextVar
from io import StringIO

# Context variable to hold the current request's log buffer
_request_buffer: ContextVar[StringIO | None] = ContextVar('request_buffer', default=None)
_request_handler: ContextVar[logging.Handler | None] = ContextVar('request_handler', default=None)


class RequestLogHandler(logging.Handler):
    """Logging handler that writes to the current request's buffer."""
    
    def emit(self, record):
        buf = _request_buffer.get()
        if buf is not None:
            try:
                msg = self.format(record)
                buf.write(msg + '\n')
            except Exception:
                pass  # Don't let logging errors break the app


def start_request_log(url: str, user_id: int) -> None:
    """
    Start capturing logs for a new request.
    Creates a buffer and attaches a handler to the root logger.
    
    Args:
        url: The URL being processed
        user_id: The user ID making the request
    """
    buf = StringIO()
    _request_buffer.set(buf)
    
    # Create and configure handler
    handler = RequestLogHandler()
    handler.setFormatter(logging.Formatter('[%(asctime)s %(levelname)s] %(message)s', datefmt='%H:%M:%S'))
    handler.setLevel(logging.INFO)
    
    # Add handler to root logger
    logging.getLogger().addHandler(handler)
    _request_handler.set(handler)
    
    # Write request header
    buf.write(f"=== Request Start ===\n")
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
        (r'(token=)[^&\s\'"]+', r'\1[REDACTED]'),
        (r'(auth=)[^&\s\'"]+', r'\1[REDACTED]'),
        (r'(signature=)[^&\s\'"]+', r'\1[REDACTED]'),
        (r'(key=)[^&\s\'"]+', r'\1[REDACTED]'),
        (r'(secret=)[^&\s\'"]+', r'\1[REDACTED]'),
        (r'(password=)[^&\s\'"]+', r'\1[REDACTED]'),
        (r'(api_key=)[^&\s\'"]+', r'\1[REDACTED]'),
        (r'(access_token=)[^&\s\'"]+', r'\1[REDACTED]'),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def end_request_log() -> None:
    """
    Clean up request log context.
    Removes the handler from root logger and clears the buffer.
    """
    handler = _request_handler.get()
    if handler is not None:
        try:
            logging.getLogger().removeHandler(handler)
        except Exception:
            pass
    
    buf = _request_buffer.get()
    if buf is not None:
        try:
            buf.close()
        except Exception:
            pass
    
    # Reset context variables
    _request_buffer.set(None)
    _request_handler.set(None)
