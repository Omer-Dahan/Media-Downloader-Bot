import hashlib
import json
import logging
import re
import tempfile
import uuid
from abc import ABC, abstractmethod
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import final

import ffmpeg
import filetype
from pyrogram import enums, types
from tqdm import tqdm

from config import TG_NORMAL_MAX_SIZE, MAX_DOWNLOAD_SIZE, Types, ARCHIVE_CHANNEL
from database import Redis
from database.model import (
    add_bandwidth_used,
    check_quota,
    get_format_settings,
    get_free_quota,
    get_paid_quota,
    get_quality_settings,
    get_subtitles_settings,
    get_title_length_settings,
    get_user_stats,
    use_quota,
    use_quota_dynamic,
    get_total_credits,
    CreditsExhaustedException,
)
from engine.helper import debounce, sizeof_fmt
from engine.network_errors import NetworkError, is_network_error, format_network_error_message

cancellation_events = set()

# Global storage for resume state (maps state_hash to resume info)
_resume_state_cache: dict[str, dict] = {}


def generate_input_media(file_paths: list, cap: str) -> list:
    input_media = []
    for path in file_paths:
        mime = filetype.guess_mime(path)
        if mime and "video" in mime:
            input_media.append(types.InputMediaVideo(media=path, supports_streaming=True))
        elif mime and "image" in mime:
            input_media.append(types.InputMediaPhoto(media=path))
        elif mime and "audio" in mime:
            input_media.append(types.InputMediaAudio(media=path))
        else:
            input_media.append(types.InputMediaDocument(media=path))

    # Attach caption to LAST file in playlist (not first)
    input_media[-1].caption = cap
    input_media[-1].parse_mode = enums.ParseMode.HTML
    return input_media


class BaseDownloader(ABC):
    def __init__(self, client: Types.Client, bot_msg: Types.Message, url: str):
        self._client = client
        self._url = url
        # chat id is the same for private chat
        self._chat_id = self._from_user = bot_msg.chat.id
        if bot_msg.chat.type == enums.ChatType.GROUP or bot_msg.chat.type == enums.ChatType.SUPERGROUP:
            # if in group, we need to find out who send the message
            self._from_user = bot_msg.reply_to_message.from_user.id
        self._id = bot_msg.id
        self._tempdir = tempfile.TemporaryDirectory(prefix="ytdl-")
        self._bot_msg: Types.Message = bot_msg
        self._redis = Redis()
        self._quality = get_quality_settings(self._chat_id)
        self._format = get_format_settings(self._chat_id)
        self._subtitles = get_subtitles_settings(self._chat_id)
        self._title_length = get_title_length_settings(self._chat_id)  # Max chars for title in caption
        self._video_title = None  # Full title for caption

    def __del__(self):
        self._tempdir.cleanup()

    def _record_usage(self, file_sizes: list[int] | int = 0) -> int:
        """Record usage and deduct credits based on file sizes.
        
        Args:
            file_sizes: List of file sizes in bytes, or single file size, or 0
        
        Returns:
            Remaining credits after deduction
        """
        free, paid = get_free_quota(self._from_user), get_paid_quota(self._from_user)
        logging.info("User %s has %s free and %s paid quota", self._from_user, free, paid)
        if free + paid < 0:
            raise Exception("חריגה ממכסת השימוש")

        remaining = use_quota_dynamic(self._from_user, file_sizes)
        
        # Calculate total size for bandwidth tracking
        if isinstance(file_sizes, list):
            total_size = sum(file_sizes)
        else:
            total_size = file_sizes
        
        if total_size > 0:
            add_bandwidth_used(self._from_user, total_size)
        return remaining

    @staticmethod
    def __remove_bash_color(text):
        return re.sub(r"\u001b|\[0;94m|\u001b\[0m|\[0;32m|\[0m|\[0;33m", "", text)

    @staticmethod
    def __tqdm_progress(desc, total, finished, speed="", eta=""):
        def more(title, initial):
            if initial:
                return f"{title} {initial}"
            else:
                return ""

        # Calculate percentage
        if total > 0:
            percent = int((finished / total) * 100)
        else:
            percent = 0
        
        # Build moon phase progress bar using shared function
        from engine.helper import moon_progress_bar
        progress_bar = moon_progress_bar(percent)
        
        # Format file size progress
        from engine.helper import sizeof_fmt
        size_progress = f"{sizeof_fmt(finished)}/{sizeof_fmt(total)}"
        
        # Modern RTL style progress display with circle bar
        text = f"""‏📥 **{desc}**
‏━━━━━━━━━━━━━━━━━━
‏{progress_bar} {percent}%
‏📊 {size_progress}
{more("‏⚡ מהירות:", speed)}
{more("‏⏱️ זמן משוער:", eta)}
‏━━━━━━━━━━━━━━━━━━"""
        return text

    def download_hook(self, d: dict):
        self.check_for_cancel()
        if d["status"] == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)

            # Only block files over 4GB - files between 2-4GB will be split later
            if total > MAX_DOWNLOAD_SIZE:
                msg = f"גודל הקובץ {sizeof_fmt(total)} גדול מדי (מקסימום 4GB)"
                raise Exception(msg)

            # percent = remove_bash_color(d.get("_percent_str", "N/A"))
            speed = self.__remove_bash_color(d.get("_speed_str", "N/A"))
            eta = self.__remove_bash_color(d.get("_eta_str") or d.get("eta") or "N/A")
            text = self.__tqdm_progress("מוריד...", total, downloaded, speed, eta)
            self.edit_text(text)

    def upload_hook(self, current, total):
        self.check_for_cancel()
        
        import time
        
        # Initialize upload tracking on first call
        if not hasattr(self, '_upload_start_time'):
            self._upload_start_time = time.time()
            self._upload_last_bytes = 0
            self._upload_last_time = time.time()
            self._upload_speed = 0
        
        now = time.time()
        time_diff = now - self._upload_last_time
        
        # Update speed calculation every 0.5 seconds to smooth it out
        if time_diff >= 0.5:
            bytes_diff = current - self._upload_last_bytes
            self._upload_speed = bytes_diff / time_diff if time_diff > 0 else 0
            self._upload_last_bytes = current
            self._upload_last_time = now
        
        # Calculate ETA
        speed_str = ""
        eta_str = ""
        if self._upload_speed > 0:
            speed_str = sizeof_fmt(self._upload_speed) + "/s"
            remaining_bytes = total - current
            eta_seconds = int(remaining_bytes / self._upload_speed)
            if eta_seconds < 60:
                eta_str = f"{eta_seconds} שניות"
            elif eta_seconds < 3600:
                eta_str = f"{eta_seconds // 60}:{eta_seconds % 60:02d} דקות"
            else:
                hours = eta_seconds // 3600
                minutes = (eta_seconds % 3600) // 60
                eta_str = f"{hours}:{minutes:02d} שעות"
        
        text = self.__tqdm_progress("מעלה...", total, current, speed_str, eta_str)
        self.edit_text(text)

    def check_for_cancel(self):
        key = f"{self._chat_id}_{self._id}"
        if key in cancellation_events:
            cancellation_events.discard(key)
            raise ValueError("ההורדה בוטלה על ידי המשתמש 🛑")

    def edit_text(self, text: str):
        # Rate limit to avoid "Waiting for..." spam from Pyrogram
        import time
        now = time.time()
        
        if not hasattr(self, '_last_edit_time'):
            self._last_edit_time = 0
            
        if now - self._last_edit_time < 2:
            return

        self._last_edit_time = now

        # Add cancel button
        markup = types.InlineKeyboardMarkup(
            [[types.InlineKeyboardButton("❌ ביטול", callback_data=f"cancel:{self._chat_id}:{self._id}")]]
        )
        try:
            self._bot_msg.edit_text(text, reply_markup=markup)
        except Exception:
            # Ignore edit errors (e.g. message not modified)
            pass

    @abstractmethod
    def _setup_formats(self) -> list | None:
        pass

    def edit_text_with_resume_button(self, downloaded_bytes: int = 0, total_bytes: int = 0, 
                                      quality: str = None, partial_file: str = None,
                                      download_type: str = "generic"):
        """Display network error message with a resume download button.
        
        Args:
            downloaded_bytes: Number of bytes downloaded before error
            total_bytes: Total expected bytes
            quality: Quality setting (for YouTube)
            partial_file: Path to partial download file
            download_type: Type of download (direct/youtube/special)
        """
        import hashlib
        
        # Create resume state
        state_hash = hashlib.md5(f"{self._url}:{self._chat_id}:{self._id}".encode()).hexdigest()[:8]
        
        _resume_state_cache[state_hash] = {
            "url": self._url,
            "chat_id": self._chat_id,
            "from_user": self._from_user,
            "message_id": self._id,
            "downloaded_bytes": downloaded_bytes,
            "total_bytes": total_bytes,
            "quality": quality or self._quality,
            "format": self._format,
            "partial_file": partial_file,
            "download_type": download_type,
            "tempdir": self._tempdir.name if hasattr(self, '_tempdir') else None,
        }
        
        # Format error message
        text = format_network_error_message(downloaded_bytes, total_bytes)
        
        # Create resume button
        markup = types.InlineKeyboardMarkup([
            [types.InlineKeyboardButton("▶️ המשך הורדה", callback_data=f"resume:{state_hash}")],
        ])
        
        try:
            self._bot_msg.edit_text(text, reply_markup=markup)
        except Exception as e:
            logging.error("Failed to show resume button: %s", e)

    @abstractmethod
    def _download(self, formats) -> list:
        # responsible for get format and download it
        pass

    @property
    def _methods(self):
        return {
            "document": self._client.send_document,
            "audio": self._client.send_audio,
            "video": self._client.send_video,
            "animation": self._client.send_animation,
            "photo": self._client.send_photo,
        }

    def send_something(self, *, chat_id, files, _type, caption=None, thumb=None, **kwargs):
        action = enums.ChatAction.UPLOAD_DOCUMENT
        if _type == "video":
            action = enums.ChatAction.UPLOAD_VIDEO
        elif _type == "audio":
            action = enums.ChatAction.UPLOAD_AUDIO
        elif _type == "photo":
            action = enums.ChatAction.UPLOAD_PHOTO
            
        self._client.send_chat_action(chat_id, action)
        is_cache = kwargs.pop("cache", False)
        if len(files) > 1 and is_cache == False:
            inputs = generate_input_media(files, caption)
            return self._client.send_media_group(chat_id, inputs)
        else:
            file_arg_name = None
            if _type == "photo":
                file_arg_name = "photo"
            elif _type == "video":
                file_arg_name = "video"
            elif _type == "animation":
                file_arg_name = "animation"
            elif _type == "document":
                file_arg_name = "document"
            elif _type == "audio":
                file_arg_name = "audio"
            else:
                logging.error("Unknown _type encountered: %s", _type)
                return None

            send_args = {
                "chat_id": chat_id,
                file_arg_name: files[0],
                "caption": caption,
                "progress": self.upload_hook,
                "parse_mode": enums.ParseMode.HTML,
                **kwargs,
            }
            
            # Add supports_streaming for video to enable inline playback in Telegram
            if _type == "video":
                send_args["supports_streaming"] = True

            if _type in ["video", "animation", "document", "audio"] and thumb is not None:
                send_args["thumb"] = thumb

            return self._methods[_type](**send_args)

    def get_metadata(self):
        # Find video/audio files only (exclude subtitles, thumbnails, etc.)
        video_extensions = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.m4v'}
        audio_extensions = {'.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.flac'}
        allowed_extensions = video_extensions | audio_extensions
        
        all_files = list(Path(self._tempdir.name).glob("*"))
        media_files = [f for f in all_files if f.suffix.lower() in allowed_extensions]
        
        if not media_files:
            # Fallback to first file if no media files found
            video_path = all_files[0] if all_files else None
        else:
            video_path = media_files[0]
        
        filename = Path(video_path).name if video_path else "unknown"
        file_ext = Path(video_path).suffix.lower() if video_path else ""
        is_audio = file_ext in audio_extensions
        
        width = height = duration = 0
        try:
            video_streams = ffmpeg.probe(video_path, select_streams="v")
            for item in video_streams.get("streams", []):
                height = item["height"]
                width = item["width"]
            duration = int(float(video_streams["format"]["duration"]))
        except Exception as e:
            logging.error("Error while getting metadata: %s", e)
        try:
            thumb_path = Path(video_path).parent.joinpath(f"{uuid.uuid4().hex}-thumbnail.png")
            thumb = thumb_path.as_posix()
            # A thumbnail's width and height should not exceed 320 pixels.
            ffmpeg.input(video_path, ss=duration / 2).filter(
                "scale",
                "if(gt(iw,ih),300,-1)",  # If width > height, scale width to 320 and height auto
                "if(gt(iw,ih),-1,300)",
            ).output(thumb, vframes=1).run()
            # Verify thumbnail was created and is valid (at least 100 bytes)
            if not thumb_path.exists() or thumb_path.stat().st_size < 100:
                logging.warning("Thumbnail file not created or too small, setting to None")
                thumb = None
        except ffmpeg._run.Error as e:
            logging.warning("Failed to create thumbnail: %s", e)
            thumb = None

        # Format duration as minutes:seconds
        duration_minutes = duration // 60
        duration_seconds = duration % 60
        duration_str = f"{duration_minutes}:{duration_seconds:02d} דקות"
        
        # Extract title: prefer stored full title, otherwise use filename
        if self._video_title:
            title = self._video_title[:self._title_length]  # Use user's preferred title length
            logging.info("get_metadata: Using stored _video_title (%d chars, max=%d)", len(title), self._title_length)
        else:
            title = Path(video_path).stem if video_path else "Unknown"
            logging.info("get_metadata: Using filename as title: %s", title[:50])
        
        # Add remaining credits if available
        credits_line = ""
        if hasattr(self, '_remaining_credits') and self._remaining_credits is not None:
            credits_line = f"\nקרדיטים נותרים: {self._remaining_credits} 💳"
        
        # Different caption for audio vs video
        import html
        esc_title = html.escape(title)
        esc_url = html.escape(self._url)
        
        if is_audio:
            caption = f"🎵 <b>{esc_title}</b>\n\n<blockquote expandable>🔗 מקור: {esc_url}</blockquote>\n⏱️ אורך: {duration_str}\n⬇️ הקובץ מוכן להורדה{credits_line}\nשמיעה מהנה 🎧✨"
        else:
            caption = f"🎬 <b>{esc_title}</b>\n\n<blockquote expandable>🔗 מקור: {esc_url}</blockquote>\n📐 רזולוציה: {width}x{height}\n⏱️ אורך: {duration_str}\n⬇️ הקובץ מוכן לצפייה והורדה{credits_line}\nצפייה מהנה 👀✨"
        
        return dict(height=height, width=width, duration=duration, thumb=thumb, caption=caption)

    def _extract_video_metadata(self, video_path: Path):
        """Extract metadata (duration, resolution, thumb) from video file."""
        import ffmpeg
        import uuid
        
        duration = 0
        width = 0
        height = 0
        thumb = None
        
        try:
            probe = ffmpeg.probe(str(video_path))
            format_info = probe.get('format', {})
            duration = float(format_info.get('duration', 0))
            
            # Get video stream info
            streams = probe.get('streams', [])
            video_stream = next((stream for stream in streams if stream['codec_type'] == 'video'), None)
            if video_stream:
                width = int(video_stream.get('width', 0))
                height = int(video_stream.get('height', 0))
                
            # Generate thumbnail
            thumb_path = video_path.parent.joinpath(f"{uuid.uuid4().hex}-thumbnail.png")
            thumb = thumb_path.as_posix()
            
            (
                ffmpeg
                .input(str(video_path), ss=duration / 2)
                .filter("scale", "if(gt(iw,ih),300,-1)", "if(gt(iw,ih),-1,300)")
                .output(thumb, vframes=1)
                .run(quiet=True, overwrite_output=True)
            )
            
            if not thumb_path.exists() or thumb_path.stat().st_size < 100:
                thumb = None
                
        except Exception as e:
            logging.warning("Failed to extract video metadata: %s", e)
            
        return {"duration": duration, "width": width, "height": height, "thumb": thumb}

    def _forward_to_archive(self, success, files, skip_archive=False):
        """Forward success message to archive channel."""
        from database.model import get_user_stats
        from config import CAPTION_URL_LENGTH_LIMIT
        import html
        
        if not ARCHIVE_CHANNEL or not success or skip_archive:
            return

        try:
            if isinstance(success, list):
                if not success: return
                msg_id = success[0].id
                media_group_id = getattr(success[0], 'media_group_id', None)
            else:
                msg_id = getattr(success, 'id', None)
                media_group_id = getattr(success, 'media_group_id', None)
            
            # Get user info
            user_info = get_user_stats(self._from_user)
            if user_info:
                name = user_info.get('first_name') or ""
                if user_info.get('username'):
                    name = f"{name} @{user_info['username']}".strip()
                user_display = name if name else str(self._from_user)
            else:
                user_display = str(self._from_user)
            
            # Get filename
            filename = "Unknown"
            if files and len(files) > 0:
                filename = Path(files[0]).name
            
            # Truncate things for caption
            display_url = self._url
            if len(display_url) > CAPTION_URL_LENGTH_LIMIT:
                display_url = display_url[:CAPTION_URL_LENGTH_LIMIT] + "..."
            
            display_filename = filename
            if len(display_filename) > 200:
                display_filename = display_filename[:200] + "..."

            # Create archive caption
            archive_caption = (
                f"👤 משתמש: {html.escape(user_display)}\n"
                f"🆔 {self._from_user}\n"
                f"📁 קובץ: {html.escape(display_filename)}\n"
                f"<blockquote expandable>🔗 קישור: {html.escape(display_url)}</blockquote>"
            )
            
            if media_group_id and isinstance(success, list):
                logging.info("Sending media group (%d messages) to archive channel", len(success))
                archive_inputs = []
                for msg in success:
                    # Extract file_id and type from the sent message
                    if msg.photo:
                        media = types.InputMediaPhoto(media=msg.photo.file_id)
                    elif msg.video:
                        media = types.InputMediaVideo(
                            media=msg.video.file_id,
                            duration=msg.video.duration,
                            width=msg.video.width,
                            height=msg.video.height,
                            supports_streaming=True
                        )
                    elif msg.document:
                        media = types.InputMediaDocument(media=msg.document.file_id)
                    elif msg.audio:
                        media = types.InputMediaAudio(
                            media=msg.audio.file_id,
                            duration=msg.audio.duration,
                            performer=msg.audio.performer,
                            title=msg.audio.title
                        )
                    else:
                        continue
                        
                    archive_inputs.append(media)
                
                if archive_inputs:
                    # Set caption on the last item
                    archive_inputs[-1].caption = archive_caption
                    archive_inputs[-1].parse_mode = enums.ParseMode.HTML
                    self._client.send_media_group(chat_id=ARCHIVE_CHANNEL, media=archive_inputs)

            elif isinstance(success, list):
                # List of separate messages (e.g. split archive parts) - copy each
                logging.info("Copying %d split parts to archive channel", len(success))
                for msg in success:
                    # Combine archive user info with the part's caption
                    part_caption = msg.caption or ""
                    
                    # Create minimal header
                    header = (
                         f"👤 משתמש: {user_display}\n"
                         f"🆔 {self._from_user}\n"
                         f"**>🔗 קישור: {self._url}**"
                    )
                    
                    final_caption = f"{header}\n{part_caption}"
                    
                    self._client.copy_message(
                        chat_id=ARCHIVE_CHANNEL,
                        from_chat_id=self._chat_id,
                        message_id=msg.id,
                        caption=final_caption,
                        parse_mode=enums.ParseMode.HTML
                    )
            else:
                # Single file - copy message with new caption
                self._client.copy_message(
                    chat_id=ARCHIVE_CHANNEL,
                    from_chat_id=self._chat_id,
                    message_id=msg_id,
                    caption=archive_caption,
                    parse_mode=enums.ParseMode.HTML
                )
            
            logging.info("Forwarded to archive channel: %s", ARCHIVE_CHANNEL)
            
        except Exception as e:
            logging.error("Failed to forward to archive channel: %s", e)

    def _split_video_if_needed(self, video_path: Path) -> list[Path]:
        """Split video into ~1.9GB parts if larger than Telegram limit.
        
        Returns list of paths - original if small enough, or split parts.
        """
        import math
        import subprocess
        
        file_size = video_path.stat().st_size
        if file_size <= TG_NORMAL_MAX_SIZE:
            return [video_path]
        
        logging.info("Video %s is %s, needs splitting", video_path.name, sizeof_fmt(file_size))
        self.edit_text("⏳ הקובץ גדול מדי לטלגרם, מפצל לחלקים...")
        
        # Get video duration
        try:
            probe = ffmpeg.probe(str(video_path))
            duration = float(probe['format']['duration'])
        except Exception as e:
            logging.error("Failed to probe video: %s", e)
            raise ValueError(f"לא הצלחתי לקרוא את הסרטון: {e}")
        
        # Calculate number of parts (~1.9GB each to be safe)
        target_size = 1.9 * 1024 * 1024 * 1024  # 1.9GB
        num_parts = math.ceil(file_size / target_size)
        part_duration = duration / num_parts
        
        logging.info("Splitting %s into %d parts, each ~%.1f seconds", video_path.name, num_parts, part_duration)
        
        parts = []
        for i in range(num_parts):
            start_time = i * part_duration
            output_path = video_path.parent / f"{video_path.stem}_part{i+1}{video_path.suffix}"
            
            self.edit_text(f"⏳ מפצל חלק {i+1}/{num_parts}...")
            
            try:
                # Use ffmpeg to split without re-encoding (fast)
                (
                    ffmpeg
                    .input(str(video_path), ss=start_time, t=part_duration)
                    .output(str(output_path), c='copy', avoid_negative_ts='make_zero')
                    .overwrite_output()
                    .run(quiet=True)
                )
                parts.append(output_path)
                logging.info("Created part %d: %s (%s)", i+1, output_path.name, sizeof_fmt(output_path.stat().st_size))
            except ffmpeg.Error as e:
                logging.error("ffmpeg split failed: %s", e)
                # Clean up any created parts
                for p in parts:
                    try:
                        p.unlink()
                    except:
                        pass
                raise ValueError(f"נכשל בפיצול הסרטון: {e}")
        
        # Remove original file to save space
        try:
            video_path.unlink()
        except:
            pass
        
        return parts
    
    def _upload_split_video(self, parts: list[Path], meta: dict):
        """Upload split video parts with caption moving from first to last part.
        
        Flow:
        1. First part gets full caption
        2. After each next part uploads, previous part's caption is updated to just "חלק X/Y"
        3. Last part keeps the full caption
        """
        num_parts = len(parts)
        sent_messages = []  # Store sent message objects for editing later
        
        full_caption = meta.get("caption", "")
        
        for i, part_path in enumerate(parts):
            part_num = i + 1
            part_label = f"📎 חלק {part_num}/{num_parts}"
            
            self.edit_text(f"⬆️ מעלה חלק {part_num}/{num_parts}...")
            
            # First and middle parts: send with full caption initially
            # After sending next part, we'll edit previous to just show part number
            if part_num < num_parts:
                current_caption = f"{part_label}\n\n{full_caption}"
            else:
                # Last part: full caption stays
                current_caption = f"{part_label}\n\n{full_caption}"
            
            try:
                # Build send_video args - only include thumb if not None
                send_args = {
                    "chat_id": self._chat_id,
                    "video": str(part_path),
                    "caption": current_caption,
                    "supports_streaming": True,
                    "progress": self.upload_hook,
                    "parse_mode": enums.ParseMode.HTML,
                }
                
                # Add optional metadata if present
                if meta.get("thumb"):
                    send_args["thumb"] = meta["thumb"]
                if meta.get("duration"):
                    send_args["duration"] = meta["duration"] // num_parts
                if meta.get("width"):
                    send_args["width"] = meta["width"]
                if meta.get("height"):
                    send_args["height"] = meta["height"]
                
                msg = self._client.send_video(**send_args)
                sent_messages.append(msg)
                logging.info("Sent part %d/%d: message_id=%s", part_num, num_parts, msg.id)
                
                # After sending this part (not the first), edit previous parts to show only part number
                if part_num > 1:
                    for prev_idx, prev_msg in enumerate(sent_messages[:-1]):
                        prev_part_num = prev_idx + 1
                        try:
                            self._client.edit_message_caption(
                                chat_id=self._chat_id,
                                message_id=prev_msg.id,
                                caption=f"📎 חלק {prev_part_num}/{num_parts}"
                            )
                        except Exception as e:
                            logging.warning("Failed to edit caption for part %d: %s", prev_part_num, e)
                
            except Exception as e:
                logging.error("Failed to send part %d: %s", part_num, e)
                raise ValueError(f"נכשל בשליחת חלק {part_num}: {e}")
            finally:
                # Clean up part file
                try:
                    part_path.unlink()
                except:
                    pass
        
        # Forward all parts to archive channel
        if ARCHIVE_CHANNEL and sent_messages:
            try:
                from database.model import get_user_stats
                
                # Get user info
                user_info = get_user_stats(self._from_user)
                if user_info:
                    name = user_info.get('first_name') or ""
                    if user_info.get('username'):
                        name = f"{name} @{user_info['username']}".strip()
                    user_display = name if name else str(self._from_user)
                else:
                    user_display = str(self._from_user)
                
                for msg in sent_messages:
                    original_caption = msg.caption or ""
                    archive_caption = (
                        f"👤 משתמש: {user_display}\n"
                        f"🆔 {self._from_user}\n"
                        f"{original_caption}"
                    )
                    
                    self._client.copy_message(
                        chat_id=ARCHIVE_CHANNEL,
                        from_chat_id=self._chat_id,
                        message_id=msg.id,
                        caption=archive_caption
                    )
                logging.info("Forwarded %d split parts to archive channel", len(sent_messages))
            except Exception as e:
                logging.error("Failed to forward split parts to archive: %s", e)

        return sent_messages[-1] if sent_messages else None

    def _upload(self, files=None, meta=None, skip_archive=False):
        # Reset upload speed tracking for new upload
        if hasattr(self, '_upload_start_time'):
            del self._upload_start_time
        if hasattr(self, '_upload_last_bytes'):
            del self._upload_last_bytes
        if hasattr(self, '_upload_last_time'):
            del self._upload_last_time
        if hasattr(self, '_upload_speed'):
            del self._upload_speed
            
        if files is None:
            # Exclude .part files (incomplete downloads)
            files = [f for f in Path(self._tempdir.name).glob("*") if not f.suffix.lower() == '.part']
        if meta is None:
            meta = self.get_metadata()

        # Separate subtitle and thumbnail files from actual video/audio files
        subtitle_extensions = {'.srt', '.vtt', '.ass', '.sub'}
        thumbnail_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
        subtitle_files = [f for f in files if Path(f).suffix.lower() in subtitle_extensions]
        
        # Filter to only video/audio files (exclude subtitles and thumbnails)
        video_extensions = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.m4v'}
        audio_extensions = {'.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.flac'}
        allowed_extensions = video_extensions | audio_extensions
        
        media_files = [f for f in files if Path(f).suffix.lower() in allowed_extensions]
        
        # Use media files for main upload (if found)
        if media_files:
            files = media_files
        
        # Check if any video file is too large and needs splitting
        video_files = [f for f in files if Path(f).suffix.lower() in video_extensions]
        for video_file in video_files:
            video_path = Path(video_file)
            original_size = video_path.stat().st_size
            if original_size > TG_NORMAL_MAX_SIZE:
                # Video too large - check type
                if video_path.suffix.lower() == '.mkv':
                    logging.info("Large MKV detected (%s), using ZIP split to preserve audio", video_path.name)
                    # Use ZIP split logic from torrent.py
                    self.edit_text("✂️ ההורדה גדולה מדי לטלגרם, מכווץ ומפצל ל-ZIP...")
                    
                    try:
                        # Local import to avoid circular dependency
                        from engine.torrent import process_large_mkv
                        parts = process_large_mkv(video_path, Path(self._tempdir.name))
                        
                        # Remove original video from files list and add parts
                        # Convert all to string for comparison/removal
                        files = [f for f in files if str(f) != str(video_path)]
                        files.extend(parts)
                        
                        # Continue to normal upload flow (files list updated)
                        continue
                        
                    except Exception as e:
                        logging.error("Failed to split MKV archive: %s", e)
                        raise ValueError(f"שגיאה בפיצול הקובץ: {e}")

                else:
                    # Video too large (MP4 etc) - split as video and upload
                    logging.info("Video %s exceeds Telegram limit, using split upload", video_path.name)
                    
                    # Record credits BEFORE splitting (based on original file size)
                    self._remaining_credits = self._record_usage(original_size)
                    
                    parts = self._split_video_if_needed(video_path)
                    if len(parts) > 1:
                        # Use split upload flow
                        success = self._upload_split_video(parts, meta)
                        # Handle success message
                        remaining_text = ""
                        if hasattr(self, '_remaining_credits') and self._remaining_credits is not None:
                            remaining_text = f" | קרדיטים נותרים: {self._remaining_credits}"
                        self._bot_msg.edit_text(f"✅ הושלם בהצלחה - {len(parts)} חלקים{remaining_text}")
                        return success

        success = SimpleNamespace(document=None, video=None, audio=None, animation=None, photo=None)
        if self._format == "document":
            logging.info("Sending as document for %s", self._url)
            
            # Special handling for split archives (multiple documents) to show progress
            # send_media_group does not support progress callback
            if len(files) > 1:
                success = []
                total_files = len(files)
                for i, file_path in enumerate(files):
                    # Update status message
                    self.edit_text(f"⬆️ מעלה חלק {i+1} מתוך {total_files}...")
                    
                    # Add caption logic similar to split video
                    part_caption = None
                    if i == total_files - 1:
                         # Full caption on last part
                         part_caption = meta.get("caption")
                    else:
                         # Minimal caption for previous parts
                         part_caption = f"📎 חלק {i+1}/{total_files}: {Path(file_path).name}"
                    
                    msg = self._client.send_document(
                        chat_id=self._chat_id,
                        document=str(file_path),
                        caption=part_caption,
                        thumb=meta.get("thumb"),
                        force_document=True,
                        progress=self.upload_hook,
                        parse_mode=enums.ParseMode.HTML
                    )
                    success.append(msg)
            else:
                success = self.send_something(
                    chat_id=self._chat_id,
                    files=files,
                    _type="document",
                    thumb=meta.get("thumb"),
                    force_document=True,
                    caption=meta.get("caption"),
                )
        elif self._format == "photo":
            logging.info("Sending as photo for %s", self._url)
            success = self.send_something(
                chat_id=self._chat_id,
                files=files,
                _type="photo",
                caption=meta.get("caption"),
            )
        elif self._format == "audio":
            logging.info("Sending as audio for %s", self._url)
            try:
                success = self.send_something(
                    chat_id=self._chat_id,
                    files=files,
                    _type="audio",
                    caption=meta.get("caption"),
                )
            except ValueError as e:
                # Cache may have wrong file type, try sending as document
                logging.warning("Failed to send as audio (%s), trying as document", e)
                success = self.send_something(
                    chat_id=self._chat_id,
                    files=files,
                    _type="document",
                    caption=meta.get("caption"),
                )
        elif self._format == "video":
            logging.info("Sending as video for %s", self._url)
            attempt_methods = ["video", "animation", "audio", "photo"]
            video_meta = meta.copy()

            upload_successful = False  # Flag to track if any method succeeded
            cache_corrupted = False  # Track if this looks like a corrupted cache
            for method in attempt_methods:
                current_meta = video_meta.copy()

                if method == "photo":
                    current_meta.pop("thumb", None)
                    current_meta.pop("duration", None)
                    current_meta.pop("height", None)
                    current_meta.pop("width", None)
                elif method == "audio":
                    current_meta.pop("height", None)
                    current_meta.pop("width", None)

                try:
                    success_obj = self.send_something(
                        chat_id=self._chat_id,
                        files=files,
                        _type=method,
                        **current_meta
                    )

                    if method == "video":
                        success = success_obj
                    elif method == "animation":
                        success = success_obj
                    elif method == "photo":
                        success = success_obj
                    elif method == "audio":
                        success = success_obj

                    upload_successful = True # Set flag to True on success
                    break
                except Exception as e:
                    logging.error("Retry to send as %s, error: %s", method, e)
                    # Check for file type mismatch errors (indicates corrupted cache)
                    if "file id instead" in str(e) or "Expected" in str(e):
                        cache_corrupted = True

            # Check the flag after the loop
            if not upload_successful:
                if cache_corrupted:
                    raise ValueError("CACHE_CORRUPTED: Expected media file id, got wrong type")
                raise ValueError("שגיאה: לקישורים ישירים, נסה שוב עם `/direct`.")

        else:
            logging.error("Unknown upload format settings for %s", self._format)
            return

        video_key = self._calc_video_key()
        
        if isinstance(success, list):
            file_ids = []
            for msg in success:
                obj = msg.document or msg.video or msg.audio or msg.animation or msg.photo
                if obj:
                    file_ids.append(getattr(obj, "file_id", None))
            file_id_json = json.dumps(file_ids)
        else:
            obj = success.document or success.video or success.audio or success.animation or success.photo
            file_id_json = json.dumps([getattr(obj, "file_id", None)])

        mapping = {
            "file_id": file_id_json,
            "meta": json.dumps({k: v for k, v in meta.items() if k != "thumb"}, ensure_ascii=False),
        }

        self._redis.add_cache(video_key, mapping)
        
        # Forward to archive channel using helper
        self._forward_to_archive(success, files, skip_archive)
        
        # Send subtitle files if user has subtitles enabled and files were found
        if subtitle_files and self._subtitles:
            for sub_file in subtitle_files:
                try:
                    sub_path = Path(sub_file)
                    self._client.send_document(
                        chat_id=self._chat_id,
                        document=str(sub_path),
                        caption=f"📝 כתוביות: {sub_path.name}"
                    )
                    logging.info("Sent subtitle file: %s", sub_path.name)
                except Exception as e:
                    logging.error("Failed to send subtitle file %s: %s", sub_file, e)
        
        # change progress bar to done with remaining credits
        remaining_text = ""
        if hasattr(self, '_remaining_credits') and self._remaining_credits is not None:
            remaining_text = f" | קרדיטים נותרים: {self._remaining_credits}"
        self._bot_msg.edit_text(f"✅ הושלם בהצלחה{remaining_text}")
        return success

    def _get_video_cache(self):
        return self._redis.get_cache(self._calc_video_key())

    def _calc_video_key(self):
        h = hashlib.md5()
        h.update((self._url + self._quality + self._format).encode())
        key = h.hexdigest()
        return key

    @final
    def start(self):
        check_quota(self._from_user)
        self._remaining_credits = None  # Initialize
        if cache := self._get_video_cache():
            logging.info("Cache hit for %s", self._url)
            meta, file_id = json.loads(cache["meta"]), json.loads(cache["file_id"])
            meta["cache"] = True
            # For cached files, still deduct credits (minimal cost)
            self._remaining_credits = self._record_usage(0)
            try:
                self._upload(file_id, meta)
            except ValueError as e:
                # Cache might be corrupted (wrong file type) - delete and retry fresh download
                if "CACHE_CORRUPTED" in str(e):
                    logging.warning("Corrupted cache detected, deleting and retrying: %s", e)
                    self._redis.delete_cache(self._calc_video_key())
                    self._start()
                else:
                    raise
        else:
            self._start()
            # Calculate file sizes for each media file (for per-file credit charging)
            media_extensions = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.m4v', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.flac'}
            file_sizes = [f.stat().st_size for f in Path(self._tempdir.name).glob("*") if f.is_file() and f.suffix.lower() in media_extensions]
            # Record usage BEFORE success message (which is now in _start->_upload)
            # But since _start calls _upload, we need to record after and update message separately
            self._remaining_credits = self._record_usage(file_sizes)
            # Only update success message if files were actually downloaded
            if file_sizes:
                try:
                    remaining_text = f" | קרדיטים נותרים: {self._remaining_credits}"
                    self._bot_msg.edit_text(f"✅ הושלם בהצלחה{remaining_text}")
                except Exception:
                    pass  # Ignore edit errors
        
        # Send temporary notification with remaining credits (auto-delete after 5 seconds)
        if self._remaining_credits is not None:
            try:
                import time
                import threading
                
                notif_msg = self._client.send_message(
                    chat_id=self._chat_id,
                    text=f"💳 קרדיטים נותרים: {self._remaining_credits}",
                    disable_notification=True
                )
                
                # Delete notification after 5 seconds in background thread
                def delete_notification():
                    time.sleep(5)
                    try:
                        notif_msg.delete()
                    except Exception:
                        pass
                
                threading.Thread(target=delete_notification, daemon=True).start()
            except Exception as e:
                logging.warning("Failed to send credit notification: %s", e)

    @abstractmethod
    def _start(self):
        pass

