# Project Context: Download Bot

## Overview
This project is a Telegram Bot designed to download media from various platforms (YouTube, Instagram, TikTok, Reddit, direct links, and Torrents). It supports file processing, converting to compatible formats, splitting large files (up to 4GB+), and uploading them back to Telegram. It features a credit-based quota system, concurrency limits, and an admin panel.

## Directory Structure
- **`src/`**: Source code root.
  - **`main.py`**: The entry point of the application. Initializes the Pyrogram client, defines message handlers, and manages the main event loop.
  - **`admin.py`**: Handles the admin panel (stats, user management, credit/quota modification).
  - **`engine/`**: Contains the downloading and processing logic.
    - **`base.py`**: A robust base class (`BaseDownloader`) handling common logic: progress bars, quota deduction, file splitting, thumbnail generation, uploading, and error reporting to the archive channel.
    - **`__init__.py`**: Acts as a dispatcher, mapping URLs/domains to specific downloaders from the module.
    - **`concurrency.py`**: Manages download slots per user (limits concurrent tasks).
    - **Specific Downloaders**: `direct.py`, `googledrive.py`, `instagram.py`, `tiktok.py`, `torrent.py`, `youtube.py` (via `generic.py`), etc. implementation of specific logic.
    - **JDownloader2**: `jdownloader.py` + `jdownloader_manager.py` — Last-resort fallback via my.jdownloader.org API. When all other engines fail, any URL is sent to JDownloader2 for download.
  - **`database/`**: Database interactions.
    - **`model.py`**: Defines SQLAlchemy models (`User`, `Setting`, `Payment`) and helper functions for quota/credits management.
  - **`config/`**: Configuration constants (loaded from `.env`).

## Key Workflows

### 1. Message Handling (`main.py`)
- The bot listens for text messages containing URLs or specific commands.
- **Commands**:
  - `/start`, `/help`: Basic info.
  - `/settings`: User preferences (Quality, Format).
  - `/stats`: System and usage statistics.
  - `/adminpanel`: Owner-only dashboard.
  - `/direct`, `/spdl`: Forces specific download modes.
  - `/torrent`: Enters "waiting for magnet/file" mode.
- **URL Detection**: If a URL is found in a text message, `main.download_handler` is triggered.
  - Checks if the user is authorized/blocked.
  - Checks for available credits (`check_quota`).
  - Checks for available concurrency slots (`concurrency_manager.acquire`).
  - Dispatches to the appropriate engine.

### 2. Dispatching (`src/engine/__init__.py`)
- `special_download_entrance` is the main router.
- It checks the hostname against `DOWNLOADER_MAP`.
- Specific handlers (e.g., `instagram_handler`, `reddit_handler`) initialize the corresponding `Downloader` class.
- If no specific handler is found, checks for direct file extensions or falls back to `YoutubeDownload` (which wraps `yt-dlp`).

### 3. Downloading Engine (`src/engine/base.py`)
- **Initialization**: Sets up a temporary directory.
- **Downloading**: Abstract `_download` method implemented by subclasses.
- **Progress**: Uses hooks to update the Telegram message with a "moon phase" progress bar.
- **Metadata**: Extracts video duration, resolution, and generates thumbnails using `ffmpeg`.
- **Splitting**: If a video > 2GB (Telegram limit), `_split_video_if_needed` splits it into parts using `ffmpeg` (copy codec for speed).
- **Uploading**: Uploads the file(s) to the user. If split, uploads parts sequentially, updating captions.
- **Quota**: Calls `_record_usage` to deduct credits based on file size.
- **Error Handling**: Captures exceptions and reports them to a configured `ARCHIVE_CHANNEL` with logs.

### 4. Database & Quota (`src/database/model.py`)
- SQLite database (via SQLAlchemy).
- **User**: Tracks `free` and `paid` credits, and bandwidth usage.
- **Quota Logic**:
  - `free`: Daily/Refillable quota.
  - `paid`: Permanent credits purchased via Stripe.
  - `use_quota_dynamic`: Deducts 1 credit per 200MB.

### 5. Admin System (`src/admin.py`)
- Accessible via `/adminpanel`.
- Features: Server stats (CPU/RAM), Download stats (Global usage), User list management, Block/Unblock users, Add/Reset credits, Launch JDownloader 2.

## Key Attributes
- **Concurrency**: `src/engine/concurrency.py` ensures users don't spam downloads (limit 1 free, 6 paid).
- **Cancellation**: `cancellation_events` in `base.py` allows users to stop active downloads.
- **Logging**: Errors and large logs are sent to a private Telegram channel for debugging.
