"""Network error detection and handling utilities."""
import logging
import socket
from dataclasses import dataclass
from typing import Optional

import requests


@dataclass
class NetworkError(Exception):
    """Exception raised when a network error occurs during download."""
    url: str
    downloaded_bytes: int = 0
    total_bytes: int = 0
    quality: Optional[str] = None  # For YouTube quality selection
    partial_file_path: Optional[str] = None  # Path to partial download file
    original_error: Optional[Exception] = None
    
    def __str__(self):
        return f"שגיאת רשת בהורדה מ-{self.url}: הורדו {self.downloaded_bytes}/{self.total_bytes} בתים"


# Known network error patterns in yt-dlp error messages
YTDLP_NETWORK_PATTERNS = [
    "Connection reset",
    "Connection refused",
    "Connection timed out",
    "timed out",
    "Unable to download",
    "urlopen error",
    "Network is unreachable",
    "No route to host",
    "Name or service not known",
    "Temporary failure in name resolution",
    "Read timed out",
    "Connection aborted",
    "RemoteDisconnected",
    "IncompleteRead",
    "ConnectionResetError",
    "socket.timeout",
    "HTTPSConnectionPool",
]


def is_network_error(exception: Exception) -> bool:
    """Check if the given exception is a network-related error.
    
    Args:
        exception: The exception to check
        
    Returns:
        True if this is a network error, False otherwise
    """
    # Direct network exception types
    network_exceptions = (
        ConnectionError,
        ConnectionResetError,
        ConnectionRefusedError,
        ConnectionAbortedError,
        TimeoutError,
        socket.timeout,
        socket.error,
        OSError,
    )
    
    if isinstance(exception, network_exceptions):
        # OSError can be many things, check errno for network-related ones
        if isinstance(exception, OSError):
            # Network-related error numbers
            network_errnos = {
                10053,  # WSAECONNABORTED
                10054,  # WSAECONNRESET 
                10060,  # WSAETIMEDOUT
                10061,  # WSAECONNREFUSED
                110,    # ETIMEDOUT (Linux)
                111,    # ECONNREFUSED (Linux)
                104,    # ECONNRESET (Linux)
            }
            if hasattr(exception, 'errno') and exception.errno in network_errnos:
                return True
            # Also check by error message for common network errors
            err_str = str(exception).lower()
            if any(keyword in err_str for keyword in ['connection', 'network', 'timed out', 'unreachable']):
                return True
            return False
        return True
    
    # Requests library exceptions
    if isinstance(exception, requests.exceptions.RequestException):
        request_network_types = (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ReadTimeout,
        )
        if isinstance(exception, request_network_types):
            return True
        # Check for network-related messages in other request exceptions
        err_str = str(exception).lower()
        if any(keyword in err_str for keyword in ['connection', 'network', 'timeout']):
            return True
    
    # Check error message for yt-dlp style network errors
    error_str = str(exception)
    for pattern in YTDLP_NETWORK_PATTERNS:
        if pattern.lower() in error_str.lower():
            return True
    
    return False


def format_network_error_message(downloaded_bytes: int = 0, total_bytes: int = 0) -> str:
    """Create a user-friendly Hebrew message for network errors.
    
    Args:
        downloaded_bytes: Number of bytes downloaded before error
        total_bytes: Total expected bytes (0 if unknown)
        
    Returns:
        Hebrew error message string
    """
    msg = "❌ **נראה שהחיבור לאינטרנט נתק**\n\n"
    msg += "בדוק את החיבור שלך ונסה שוב.\n"
    
    if downloaded_bytes > 0:
        from engine.helper import sizeof_fmt
        downloaded_str = sizeof_fmt(downloaded_bytes)
        if total_bytes > 0:
            total_str = sizeof_fmt(total_bytes)
            percent = int((downloaded_bytes / total_bytes) * 100)
            msg += f"\n📊 הורדו: {downloaded_str} מתוך {total_str} ({percent}%)"
        else:
            msg += f"\n📊 הורדו: {downloaded_str}"
    
    return msg


