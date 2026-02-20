import logging
import os
import re
import pathlib
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import filetype
import requests
from requests.exceptions import HTTPError
from urllib.parse import urlparse

from config import ENABLE_ARIA2
from engine.base import BaseDownloader
from engine.network_errors import NetworkError, is_network_error

# Common User-Agent to avoid being blocked by servers
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"


class DirectDownload(BaseDownloader):

    def _setup_formats(self) -> list | None:
        # direct download doesn't need to setup formats
        pass

    def _requests_download(self):
        """Download using curl_cffi (Chrome impersonation) with streaming support for large files."""
        logging.info("[DOWNLOAD METHOD: curl_cffi/requests] Starting download: %s", self._url)
        
        # Extract origin for Referer header
        parsed = urlparse(self._url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        
        # Full browser-like headers
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": origin,
            "Origin": origin,
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        
        # Try curl_cffi first (impersonates Chrome TLS fingerprint)
        try:
            from curl_cffi import requests as curl_requests
            logging.info("Using curl_cffi with Chrome impersonation")
            
            # Use streaming download for large files with increased timeout (15 minutes)
            response = curl_requests.get(
                self._url, 
                headers=headers, 
                impersonate="chrome",
                timeout=900,  # 15 minutes timeout for large files
                stream=True
            )
            
            if response.status_code >= 400:
                raise ValueError(f"ההורדה נכשלה: {response.status_code} - {response.reason}")
            
            # Get file size from headers if available
            total_size = int(response.headers.get('content-length', 0))
            
            # Create temporary file
            file = Path(self._tempdir.name).joinpath(uuid4().hex)
            
            # Download with progress tracking
            downloaded = 0
            chunk_size = 1024 * 1024  # 1MB chunks
            
            try:
                with open(file, "wb") as f:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            # Check for cancellation on every chunk
                            self.check_for_cancel()
                            
                            # Update progress if we know total size
                            if total_size > 0:
                                self.download_hook({
                                    "status": "downloading",
                                    "downloaded_bytes": downloaded,
                                    "total_bytes": total_size
                                })
            except ValueError:
                # Cancellation - close the connection immediately
                logging.info("Closing connection due to cancellation")
                response.close()
                raise
            finally:
                # Always try to close the response to stop any background downloading
                try:
                    response.close()
                except Exception:
                    pass
            
            file_size_mb = downloaded / (1024 * 1024)
            logging.info("Download complete via curl_cffi: %d bytes", downloaded)
            self._bot_msg.edit_text(f"✅ ההורדה הושלמה ({file_size_mb:.1f} MB)\n⏳ מעלה לטלגרם...")
            
        except ImportError:
            logging.warning("curl_cffi not available, falling back to requests")
            # Fallback to regular requests with streaming
            try:
                response = requests.get(self._url, stream=True, headers=headers, timeout=(10, 900))
                response.raise_for_status()
                
                # Get file size from headers
                total_size = int(response.headers.get('content-length', 0))
                
                # Create temporary file
                file = Path(self._tempdir.name).joinpath(uuid4().hex)
                
                # Download with progress
                downloaded = 0
                chunk_size = 1024 * 1024  # 1MB chunks
                
                try:
                    with open(file, "wb") as f:
                        for chunk in response.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                
                                # Check for cancellation on every chunk
                                self.check_for_cancel()
                                
                                if total_size > 0:
                                    self.download_hook({
                                        "status": "downloading",
                                        "downloaded_bytes": downloaded,
                                        "total_bytes": total_size
                                    })
                except ValueError:
                    # Cancellation - close the connection immediately
                    logging.info("Closing requests connection due to cancellation")
                    response.close()
                    raise
                finally:
                    try:
                        response.close()
                    except Exception:
                        pass
                
                logging.info("Download complete via requests: %d bytes", downloaded)
            except HTTPError as e:
                raise ValueError(f"ההורדה נכשלה: {e.response.status_code} - {e.response.reason}") from e
            except requests.exceptions.RequestException as e:
                # Check if this is a network error
                if is_network_error(e):
                    # Get partial file size
                    partial_bytes = 0
                    if 'file' in dir() and file.exists():
                        partial_bytes = file.stat().st_size
                    raise NetworkError(
                        url=self._url,
                        downloaded_bytes=partial_bytes,
                        total_bytes=total_size if 'total_size' in dir() else 0,
                        partial_file_path=str(file) if 'file' in dir() else None,
                        original_error=e
                    ) from e
                raise ValueError(f"שגיאת חיבור: {e}") from e
        
        # Detect file type and rename with proper extension
        ext = filetype.guess_extension(file)
        if ext is not None:
            new_name = file.with_suffix(f".{ext}")
            file.rename(new_name)
            file = new_name

        return [file.as_posix()]

    def _aria2_download(self):
        logging.info("[DOWNLOAD METHOD: aria2] Starting multi-connection download: %s", self._url)
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
        # filename = self._get_aria2_name()
        self._process = None
        try:
            self._bot_msg.edit_text("⚡ ההורדה באמצעות Aria2 מתחילה...")
            temp_dir = self._tempdir.name
            command = [
                "aria2c",
                "--max-tries=3",
                "--max-concurrent-downloads=8",
                "--max-connection-per-server=16",
                "--split=16",
                "--summary-interval=1",
                "--console-log-level=notice",
                "--show-console-readout=true",
                "--quiet=false",
                "--human-readable=true",
                f"--user-agent={ua}",
                "-d", temp_dir,
                self._url,
            ]

            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1
            )

            while True:
                line = self._process.stdout.readline()
                if not line:
                    if self._process.poll() is not None:
                        break
                    continue

                progress = self.__parse_progress(line)
                if progress:
                    self.download_hook(progress)
                elif "Download complete:" in line:
                    self.download_hook({"status": "complete"})

            self._process.wait(timeout=300)
            success = self._process.wait() == 0
            if not success:
                raise subprocess.CalledProcessError(
                    self._process.returncode, 
                    command, 
                    self._process.stderr.read()
                )
            if self._process.returncode != 0:
                raise subprocess.CalledProcessError(
                    self._process.returncode, 
                    command, 
                    stderr
                )

            # This will get [Path_object] if a file is found, or None if no files are found.
            files = [f] if (f := next((item for item in Path(temp_dir).glob("*") if item.is_file()), None)) is not None else None
            if files is None:
                logging.error(f"No files found in {temp_dir}")
                raise FileNotFoundError(f"No files found in {temp_dir}")
            else:
                logging.info("Successfully downloaded file: %s", files[0])

            return files

        except subprocess.TimeoutExpired:
            error_msg = "\u05d4\u05d4\u05d5\u05e8\u05d3\u05d4 \u05d4\u05d5\u05e4\u05e1\u05e7\u05d4 \u05e2\u05e7\u05d1 \u05d7\u05e8\u05d9\u05d2\u05d4 \u05de\u05d6\u05de\u05df \u05d4\u05d4\u05de\u05ea\u05e0\u05d4 (5 \u05d3\u05e7\u05d5\u05ea)."
            logging.error(error_msg)
            self.edit_text(f"ההורדה נכשלה!❌\n\n{error_msg}")
            return []
        except Exception as e:
            self.edit_text(f"ההורדה נכשלה!❌\n\n`{e}`")
            return []
        finally:
            if self._process:
                self._process.terminate()
                self._process = None

    def __parse_progress(self, line: str) -> dict | None:
        if "Download complete:" in line or "(OK):download completed" in line:
            return {"status": "complete"}

        progress_match = re.search(
            r'\[#\w+\s+(?P<progress>[\d.]+[KMGTP]?iB)/(?P<total>[\d.]+[KMGTP]?iB)\(.*?\)\s+CN:\d+\s+DL:(?P<speed>[\d.]+[KMGTP]?iB)\s+ETA:(?P<eta>[\dhms]+)',
            line
        )

        if progress_match:
            return {
                "status": "downloading",
                "downloaded_bytes": self.__parse_size(progress_match.group("progress")),
                "total_bytes": self.__parse_size(progress_match.group("total")),
                "_speed_str": f"{progress_match.group('speed')}/s",
                "_eta_str": progress_match.group("eta")
            }

        # Fallback check for summary lines
        if "Download Progress Summary" in line and "MiB" in line:
            return {"status": "progress", "details": line}

        return None

    def __parse_size(self, size_str: str) -> int:
        units = {
            "B": 1, 
            "K": 1024, "KB": 1024, "KIB": 1024,
            "M": 1024**2, "MB": 1024**2, "MIB": 1024**2,
            "G": 1024**3, "GB": 1024**3, "GIB": 1024**3,
            "T": 1024**4, "TB": 1024**4, "TIB": 1024**4
        }
        match = re.match(r"([\d.]+)([A-Za-z]*)", size_str.replace("i", "").upper())
        if match:
            number, unit = match.groups()
            unit = unit or "B"
            return int(float(number) * units.get(unit, 1))
        return 0

    def _download(self, formats=None) -> list:
        logging.info("[DirectDownload] ENABLE_ARIA2=%s", ENABLE_ARIA2)
        if ENABLE_ARIA2:
            return self._aria2_download()
        return self._requests_download()

    def _start(self):
        try:
            downloaded_files = self._download()
            self._upload(files=downloaded_files)
        except NetworkError as e:
            # Network error - show resume button
            logging.warning("Network error during download: %s", e)
            self.edit_text_with_resume_button(
                downloaded_bytes=e.downloaded_bytes,
                total_bytes=e.total_bytes,
                partial_file=e.partial_file_path,
                download_type="direct"
            )
        except ValueError as e:
            # Check if this is a cancellation
            if "בוטלה" in str(e):
                logging.info("Cancellation confirmed, updating message")
                try:
                    self._bot_msg.edit_text("🛑 ההורדה בוטלה בהצלחה")
                except Exception as edit_err:
                    logging.error("Failed to edit cancellation message: %s", edit_err)
            else:
                raise