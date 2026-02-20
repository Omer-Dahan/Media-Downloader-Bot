import logging
import os
import re
import threading
import time
import typing
from io import BytesIO
from typing import Any

import psutil
import pyrogram.errors
from apscheduler.schedulers.background import BackgroundScheduler
from pyrogram import Client, enums, filters, types

from config import (
    APP_HASH,
    APP_ID,
    ARCHIVE_CHANNEL,
    AUTHORIZED_USER,
    BOT_TOKEN,
    ENABLE_ARIA2,
    ENABLE_FFMPEG,
    M3U8_SUPPORT,
    ENABLE_VIP,
    OWNER,
    PROVIDER_TOKEN,
    TOKEN_PRICE,
    TORRENT_MIN_CREDITS,
    BotText,
)
from database.model import (
    check_quota,
    credit_account,
    get_format_settings,
    get_free_quota,
    get_paid_quota,
    get_quality_settings,
    get_subtitles_settings,
    get_title_length_settings,
    init_user,
    set_user_settings,
    CreditsExhaustedException,
    get_total_credits,
)
from engine import direct_entrance, youtube_entrance, youtube_entrance_with_quality, get_youtube_video_info, special_download_entrance, torrent_entrance, is_magnet_link, jdownloader_entrance, TORRENT_AVAILABLE, JDOWNLOADER_AVAILABLE
from engine.base import cancellation_events, _resume_state_cache
from engine.concurrency import concurrency_manager
from engine.generic import check_and_send_update_notification, auto_update_ytdlp
from utils import extract_url_and_name, is_youtube, format_system_stats
from engine.helper import sizeof_fmt
from admin import admin_panel_command, admin_callback_handler, admin_text_handler, _admin_state

# Temporary storage for YouTube URLs (maps hash to URL)
_youtube_url_cache: dict[str, str] = {}

# Users waiting to provide torrent input (after /torrent command)
_torrent_waiting_users: set[int] = set()

logging.info("Authorized users are %s", AUTHORIZED_USER)
logging.getLogger("apscheduler.executors.default").propagate = False


def create_app(name: str, workers: int = 64) -> Client:
    return Client(
        name,
        APP_ID,
        APP_HASH,
        bot_token=BOT_TOKEN,
        workers=workers,
        # max_concurrent_transmissions=max(1, WORKERS // 2),
        # https://github.com/pyrogram/pyrogram/issues/1225#issuecomment-1446595489
    )


app = create_app("main")


def report_error_to_archive(client: Client, user: types.User, url: str, error: Exception | str):
    import html
    from engine.request_logger import get_request_log
    
    if not ARCHIVE_CHANNEL:
        return
    
    try:
        user_display = str(user.id)
        if user:
            name = user.first_name or ""
            if user.username:
                name = f"{name} @{user.username}".strip()
            if name:
                user_display = name
        
        # Get and escape log content
        log_content = get_request_log()
        log_escaped = html.escape(log_content) if log_content else "אין לוג זמין"
        
        # Handle 4096 char limit - leave room for header (~500 chars)
        MAX_LOG_LEN = 3000
        send_log_as_file = False
        if len(log_escaped) > MAX_LOG_LEN:
            log_display = log_escaped[:MAX_LOG_LEN] + "\n... (נקטע, ראה קובץ מצורף)"
            send_log_as_file = True
        else:
            log_display = log_escaped
        
        caption = (
            f"❌ <b>דיווח שגיאה</b>\n"
            f"👤 משתמש: {html.escape(user_display)}\n"
            f"🆔 {user.id}\n"
            f"<blockquote expandable>🔗 קישור: {html.escape(url)}</blockquote>\n"
            f"⚠️ שגיאה: {html.escape(str(error))}\n\n"
            f"<blockquote expandable>📋 לוג מפורט:\n<pre>{log_display}</pre></blockquote>"
        )
        
        client.send_message(
            chat_id=ARCHIVE_CHANNEL,
            text=caption,
            parse_mode=enums.ParseMode.HTML,
            link_preview_options=types.LinkPreviewOptions(is_disabled=True)
        )
        
        # Send full log as file if truncated
        if send_log_as_file and log_content:
            f = BytesIO(log_content.encode('utf-8'))
            f.name = f"error_log_{user.id}.txt"
            client.send_document(ARCHIVE_CHANNEL, f)
            f.close()
            
    except Exception as e:
        logging.error("Failed to report error to archive channel: %s", e)


# Register admin panel handlers
@app.on_message(filters.command(["adminpanel"]))
def adminpanel_handler(client: Client, message: types.Message):
    admin_panel_command(client, message)

@app.on_callback_query(filters.regex(r"^admin:"))
def admin_callback(client: Client, callback_query: types.CallbackQuery):
    admin_callback_handler(client, callback_query)


def private_use(func):
    def wrapper(client: Client, message: types.Message):
        chat_id = getattr(message.from_user, "id", None)

        # message type check
        if message.chat.type != enums.ChatType.PRIVATE and not getattr(message, "text", "").lower().startswith("/ytdl"):
            logging.debug("%s, it's annoying me...🙄️ ", message.text)
            return

        # authorized users check
        if AUTHORIZED_USER:
            users = [int(i) for i in AUTHORIZED_USER.split(",")]
        else:
            users = []

        if users and chat_id and chat_id not in users:
            message.reply_text("הבוט הזה פרטי ולא זמין עבורך.", quote=True)
            return

        return func(client, message)

    return wrapper


@app.on_message(filters.command(["start"]))
def start_handler(client: Client, message: types.Message):
    from_id = message.chat.id
    user = message.from_user
    init_user(from_id, first_name=user.first_name if user else None, username=user.username if user else None)
    logging.info("%s welcome to youtube-dl bot!", message.from_user.id)
    client.send_chat_action(from_id, enums.ChatAction.TYPING)
    # Use ReplyKeyboardRemove to clear any old keyboard from previous bot versions
    client.send_message(
        from_id,
        BotText.start,
        disable_web_page_preview=True,
        reply_markup=types.ReplyKeyboardRemove(),
    )


@app.on_message(filters.command(["help"]))
def help_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    init_user(chat_id)
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    markup = types.InlineKeyboardMarkup(
        [[types.InlineKeyboardButton("לצ'אט איתי 💬", url="https://t.me/YD_IL")]]
    )
    client.send_message(chat_id, BotText.help, disable_web_page_preview=True, reply_markup=markup)


@app.on_message(filters.command(["about"]))
def about_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    init_user(chat_id)
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    client.send_message(chat_id, BotText.about)


@app.on_message(filters.command(["ping"]))
def ping_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    init_user(chat_id)
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)

    def send_message_and_measure_ping():
        start_time = int(round(time.time() * 1000))
        reply: types.Message | typing.Any = client.send_message(chat_id, "בודק פינג...")

        end_time = int(round(time.time() * 1000))
        ping_time = int(round(end_time - start_time))
        message.reply_text(f"פינג: {ping_time:.2f} מילישניות", quote=True)
        time.sleep(0.5)
        client.edit_message_text(chat_id=reply.chat.id, message_id=reply.id, text="בדיקת הפינג הושלמה.")
        time.sleep(1)
        client.delete_messages(chat_id=reply.chat.id, message_ids=reply.id)

    thread = threading.Thread(target=send_message_and_measure_ping)
    thread.start()


@app.on_message(filters.command(["buy"]))
def buy(client: Client, message: types.Message):
    buy_text = """💳 **מערכת הקרדיטים**

**איך זה עובד?**
• כל **200MB** עולה **קרדיט 1**
• קובץ 400MB = **2 קרדיטים**
• קובץ 1GB = **5 קרדיטים**
• פלייליסט מחויב לפי גודל הקבצים

**מה קורה כשנגמרים?**
הקרדיטים שלכם לא מתאפסים - הם נשארים עד שתרכשו עוד!

💬 **לרכישת קרדיטים נוספים:**
שלחו לי הודעה פרטית ואפתח לכם גישה בתשלום קטן 🚀"""
    
    markup = types.InlineKeyboardMarkup(
        [
            [types.InlineKeyboardButton("לרכישת קרדיטים 💬", url="https://t.me/YD_IL")],
        ]
    )
    message.reply_text(buy_text, reply_markup=markup)


@app.on_callback_query(filters.regex(r"buy.*"))
def send_invoice(client: Client, callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    data = callback_query.data
    _, count, price = data.split("-")
    price = int(float(price) * 100)
    client.send_invoice(
        chat_id,
        f"{count} קרדיטים להורדות",
        "אנא בצע תשלום דרך Stripe",
        f"{count}",
        "USD",
        [types.LabeledPrice(label="VIP", amount=price)],
        provider_token=os.getenv("PROVIDER_TOKEN"),
        protect_content=True,
        start_parameter="no-forward-placeholder",
    )


@app.on_pre_checkout_query()
def pre_checkout(client: Client, query: types.PreCheckoutQuery):
    client.answer_pre_checkout_query(query.id, ok=True)


@app.on_message(filters.successful_payment)
def successful_payment(client: Client, message: types.Message):
    who = message.chat.id
    amount = message.successful_payment.total_amount  # in cents
    quota = int(message.successful_payment.invoice_payload)
    ch = message.successful_payment.provider_payment_charge_id
    free, paid = credit_account(who, amount, quota, ch)
    if paid > 0:
        message.reply_text(f"התשלום בוצע בהצלחה! יש לך {free} קרדיטים חינמיים ו-{paid} קרדיטים בתשלום.")
    else:
        message.reply_text("משהו השתבש. אנא פנה למנהל.")
    message.delete()


@app.on_message(filters.command(["stats"]))
def stats_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    init_user(chat_id)
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)

    try:
        from database.model import is_admin
        owner_stats, user_stats = format_system_stats(botStartTime)
        if message.from_user.id in OWNER:
            message.reply_text(owner_stats, quote=True)
        else:
            message.reply_text(user_stats, quote=True)
    except Exception as e:
        message.reply_text(f"❌ שגיאה: {e}", quote=True)


@app.on_message(filters.command(["settings"]))
def settings_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    init_user(chat_id)
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    
    markup = _build_settings_markup(chat_id)
    client.send_message(chat_id, BotText.settings, reply_markup=markup)


@app.on_message(filters.command(["direct"]))
def direct_download(client: Client, message: types.Message):
    chat_id = message.chat.id
    init_user(chat_id)
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    message_text = message.text
    url, new_name = extract_url_and_name(message_text)
    logging.info("Direct download using aria2/requests start %s", url)
    if url is None or not re.findall(r"^https?://", url.lower()):
        message.reply_text("שלח לי קישור תקין.", quote=True)
        return
    bot_msg = message.reply_text("בקשת הורדה ישירה התקבלה.", quote=True)
    try:
        direct_entrance(client, bot_msg, url)
    except ValueError as e:
        # If direct download fails, try JDownloader2 as fallback
        if "בוטלה" not in str(e):
            logging.info("Direct download failed for %s, falling back to JDownloader2: %s", url, e)
            try:
                jdownloader_entrance(client, bot_msg, url)
            except Exception as jd_e:
                report_error_to_archive(client, message.from_user, url, jd_e)
                message.reply_text(str(jd_e), quote=True)
                bot_msg.delete()
        else:
            raise


@app.on_message(filters.command(["spdl"]))
def spdl_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    init_user(chat_id)
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    message_text = message.text
    url, new_name = extract_url_and_name(message_text)
    logging.info("spdl start %s", url)
    if url is None or not re.findall(r"^https?://", url.lower()):
        message.reply_text("משהו לא בסדר 🤔.\nבדוק את הקישור ושלח שוב.", quote=True)
        return
    bot_msg = message.reply_text("בקשת הורדה מיוחדת התקבלה.", quote=True)
    try:
        special_download_entrance(client, bot_msg, url)
    except ValueError as e:
        report_error_to_archive(client, message.from_user, url, e)
        message.reply_text(e.__str__(), quote=True)
        bot_msg.delete()
        return


@app.on_message(filters.command(["ytdl"]) & filters.group)
def ytdl_handler(client: Client, message: types.Message):
    # for group only
    init_user(message.from_user.id)
    client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)
    message_text = message.text
    url, new_name = extract_url_and_name(message_text)
    logging.info("ytdl start %s", url)
    if url is None or not re.findall(r"^https?://", url.lower()):
        message.reply_text("בדוק את הקישור שלך.", quote=True)
        return

    bot_msg = message.reply_text("בקשת הורדה בקבוצה התקבלה.", quote=True)
    try:
        youtube_entrance(client, bot_msg, url)
    except ValueError as e:
        report_error_to_archive(client, message.from_user, url, e)
        message.reply_text(e.__str__(), quote=True)
        bot_msg.delete()
        return


@app.on_message(filters.command(["torrent"]))
@private_use
def torrent_command_handler(client: Client, message: types.Message):
    """Handle /torrent command - puts user in waiting state for magnet/file."""
    chat_id = message.from_user.id
    user = message.from_user
    init_user(chat_id, first_name=user.first_name if user else None, username=user.username if user else None)
    
    # Check if torrent feature is available at all
    if not TORRENT_AVAILABLE:
        message.reply_text(
            "🧲 **הורדת טורנטים אינה זמינה בגרסה זו**\n\n"
            "qBittorrent אינו מותקן או מוגדר בשרת.",
            quote=True,
        )
        return

    # Check 500+ credits requirement
    total_credits = get_total_credits(chat_id)
    if total_credits < TORRENT_MIN_CREDITS:
        message.reply_text(
            f"⚡ **הורדת טורנטים זמינה למנויים פרימיום בלבד**\n\n"
            f"נדרשים מינימום {TORRENT_MIN_CREDITS} קרדיטים.\n"
            f"יש לך כרגע: {total_credits} קרדיטים.\n\n"
            "💬 לרכישת קרדיטים, צור קשר:",
            reply_markup=types.InlineKeyboardMarkup([
                [types.InlineKeyboardButton("לרכישת קרדיטים 💬", url="https://t.me/YD_IL")]
            ]),
            quote=True
        )
        return
    
    # Put user in waiting state
    _torrent_waiting_users.add(chat_id)
    
    message.reply_text(
        "🧲 **מצב הורדת טורנט**\n\n"
        "שלח לי כעת:\n"
        "• **מגנט לינק** - הדבק את הקישור\n"
        "• **קובץ .torrent** - העלה כקובץ\n\n"
        "💡 לביטול, שלח כל הודעה אחרת או המתן.",
        quote=True
    )


def check_link(url: str, uid: int = None):
    if re.findall(r"^https://(www\.)?youtube\.com/channel/", url) or "list" in url:
        # Playlist/channel allowed for all users with credits
        if uid is not None:
            total_credits = get_total_credits(uid)
            if total_credits and total_credits >= 1:
                return None  # Allow playlist download
        # No credits - return block marker
        return "PLAYLIST_NO_CREDITS"

    if not M3U8_SUPPORT and (re.findall(r"m3u8|\.m3u8|\.m3u$", url.lower())):
        return "קישורי m3u8 מושבתים."

def send_no_credits_message(message: types.Message):
    """Send credits exhausted message with purchase button."""
    markup = types.InlineKeyboardMarkup([
        [types.InlineKeyboardButton("💬 לרכישת קרדיטים", url="https://t.me/YD_IL")]
    ])
    message.reply_text(
        "❌ **הקרדיטים שלך נגמרו.**\n\n"
        "לרכישת קרדיטים נוספים, צור קשר עם יוצר הבוט. 👇",
        reply_markup=markup,
        quote=True
    )

@app.on_message(filters.incoming & filters.text)
@private_use
def download_handler(client: Client, message: types.Message):
    from engine.request_logger import start_request_log, end_request_log
    
    chat_id = message.from_user.id
    user = message.from_user
    init_user(chat_id, first_name=user.first_name if user else None, username=user.username if user else None)
    text = message.text
    
    # Check if admin is waiting for input
    if chat_id in _admin_state:
        admin_text_handler(client, message)
        return
    
    # Extract URL from anywhere in the message
    if not text:
        return
    
    # Check for magnet links - only if user sent /torrent first
    if is_magnet_link(text):
        if chat_id not in _torrent_waiting_users:
            # Not in torrent mode - ignore magnet or hint about /torrent
            message.reply_text(
                "🧲 זיהיתי מגנט לינק!\n\n"
                "להורדת טורנטים, שלח קודם את הפקודה /torrent",
                quote=True
            )
            return
        
        # Remove from waiting state
        _torrent_waiting_users.discard(chat_id)
        
        logging.info("Processing magnet link from user %s", chat_id)
        
        # Start torrent download
        bot_msg = message.reply_text("🧲 מגנט לינק התקבל, מתחיל הורדת טורנט...", quote=True)
        client.send_chat_action(chat_id, enums.ChatAction.UPLOAD_VIDEO)
        
        start_request_log(text, chat_id)
        try:
            torrent_entrance(client, bot_msg, text)
        except ValueError as e:
            report_error_to_archive(client, message.from_user, text, e)
        finally:
            end_request_log()
        return
    
    # Find URLs anywhere in the message text
    url_match = re.search(r"https?://[^\s<>\"']+", text)
    if not url_match:
        return
    
    url = url_match.group(0)
    # Clean up trailing punctuation that might have been captured
    url = url.rstrip(".,;:!?)")
    
    logging.info("Extracted URL from message: %s", url)
    
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    logging.info("start %s", url)
    
    # Start request logging
    start_request_log(url, chat_id)

    try:
        link_check_result = check_link(url, chat_id)
        
        # Handle playlist/channel - check credits
        if link_check_result == "PLAYLIST_NO_CREDITS":
            send_no_credits_message(message)
            return
        elif link_check_result:
            # Other error messages (like m3u8 disabled)
            message.reply_text(link_check_result, quote=True)
            return
        
        # Check concurrency limit
        if not concurrency_manager.acquire(chat_id):
            message.reply_text(
                "❌ **יש לך יותר מדי פעולות פעילות במקביל.**\n"
                "אנא המתן לסיום ההורדות הקיימות.",
                quote=True
            )
            return
        
        # Check if this is a YouTube link - show quality selection menu
        if is_youtube(url):
            # Generate a short hash for the URL
            import hashlib
            url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
            _youtube_url_cache[url_hash] = url
            
            # Get video info
            video_info = get_youtube_video_info(url)
            if video_info:
                title = video_info.get('title', 'Unknown')
                duration = video_info.get('duration', '0:00')
            else:
                title = 'סרטון יוטיוב'
                duration = 'לא ידוע'
            
            # Build quality selection keyboard
            markup = types.InlineKeyboardMarkup(
                [
                    [  # First row - high quality
                        types.InlineKeyboardButton("🎬 1080p HD", callback_data=f"ytq:1080:{url_hash}"),
                        types.InlineKeyboardButton("🎬 720p", callback_data=f"ytq:720:{url_hash}"),
                    ],
                    [  # Second row - lower quality
                        types.InlineKeyboardButton("🎬 480p", callback_data=f"ytq:480:{url_hash}"),
                        types.InlineKeyboardButton("🎬 360p", callback_data=f"ytq:360:{url_hash}"),
                    ],
                    [  # Third row - audio only
                        types.InlineKeyboardButton("🎵 שמע בלבד", callback_data=f"ytq:audio:{url_hash}"),
                    ],
                ]
            )
            
            # Send quality selection message
            message.reply_text(
                BotText.youtube_quality_select.format(title, duration),
                reply_markup=markup,
                quote=True
            )
            return
        
        # Non-YouTube links - check for special downloaders first (Reddit, Instagram, etc.)
        bot_msg: types.Message | Any = message.reply_text("הקישור נקלט, מתחיל לעבוד עליו 📥", quote=True)
        client.send_chat_action(chat_id, enums.ChatAction.UPLOAD_VIDEO)
        
        # Try special download handlers first (Reddit, Instagram, PixelDrain, etc.)
        try:
            special_download_entrance(client, bot_msg, url)
        except ValueError as inner_e:
            # No special handler found, fall back to yt-dlp
            if "לא נמצא מוריד" in str(inner_e):
                try:
                    youtube_entrance(client, bot_msg, url)
                except Exception as ytdlp_e:
                    # Last resort — try JDownloader2
                    logging.info("yt-dlp failed for %s, trying JDownloader2: %s", url, ytdlp_e)
                    jdownloader_entrance(client, bot_msg, url)
            # Other ValueErrors are handled by the downloader itself
    except pyrogram.errors.Flood as e:
        report_error_to_archive(client, message.from_user, url, e)
        f = BytesIO()
        f.write(str(e).encode())
        f.write(b"\xd7\x94\xd7\x91\xd7\xa7\xd7\xa9\xd7\x94 \xd7\xa9\xd7\x9c\xd7\x9a \xd7\x91\xd7\x98\xd7\x99\xd7\xa4\xd7\x95\xd7\x9c. \xd7\x90\xd7\xa0\xd7\x90 \xd7\x94\xd7\x9e\xd7\xaa\xd7\x9f \xd7\x91\xd7\xa1\xd7\x91\xd7\x9c\xd7\xa0\xd7\x95\xd7\xaa.")
        f.name = "אנא המתן.txt"
        message.reply_document(f, caption=f"המתנת הצפה! אנא המתן {e} שניות...", quote=True)
        f.close()
        client.send_message(OWNER, f"המתנת הצפה! 🙁 {e} שניות....")
        time.sleep(e.value)
    except CreditsExhaustedException:
        # User has no credits - show purchase message
        send_no_credits_message(message)
    except ValueError as e:
        report_error_to_archive(client, message.from_user, url, e)
        message.reply_text(str(e), quote=True)
    except Exception as e:
        report_error_to_archive(client, message.from_user, url, e)
        logging.error("Download failed", exc_info=True)
        message.reply_text(
            "❌ לא הצלחתי להוריד את הקישור הזה כרגע.\n"
            "נסה שוב עוד מעט או שלח קישור אחר.",
            quote=True
        )
    finally:
        end_request_log()


@app.on_message(filters.document & filters.private)
@private_use
def torrent_file_handler(client: Client, message: types.Message):
    """Handle .torrent file uploads (premium feature)."""
    from engine.request_logger import start_request_log, end_request_log
    import tempfile
    
    # Check if it's a .torrent file
    document = message.document
    if not document:
        return
    
    file_name = document.file_name or ""
    if not file_name.lower().endswith(".torrent"):
        return  # Not a torrent file, ignore

    # Check if torrent feature is available at all
    if not TORRENT_AVAILABLE:
        message.reply_text(
            "🧲 **הורדת טורנטים אינה זמינה בגרסה זו**\n\n"
            "qBittorrent אינו מותקן או מוגדר בשרת.",
            quote=True,
        )
        return
    
    chat_id = message.from_user.id
    user = message.from_user
    init_user(chat_id, first_name=user.first_name if user else None, username=user.username if user else None)
    
    logging.info("Received .torrent file from user %s: %s", chat_id, file_name)
    
    # Check if user sent /torrent first
    if chat_id not in _torrent_waiting_users:
        message.reply_text(
            "📁 זיהיתי קובץ טורנט!\n\n"
            "להורדת טורנטים, שלח קודם את הפקודה /torrent",
            quote=True
        )
        return
    
    # Remove from waiting state
    _torrent_waiting_users.discard(chat_id)
    
    bot_msg = message.reply_text("📥 מוריד קובץ טורנט...", quote=True)
    client.send_chat_action(chat_id, enums.ChatAction.UPLOAD_VIDEO)
    
    start_request_log(f"torrent:{file_name}", chat_id)
    
    # Check concurrency limit
    if not concurrency_manager.acquire(chat_id):
        message.reply_text(
            "❌ **יש לך יותר מדי פעולות פעילות במקביל.**\n"
            "אנא המתן לסיום ההורדות הקיימות.",
            quote=True
        )
        end_request_log()
        return
    
    try:
        # Download the .torrent file
        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as tmp:
            tmp_path = tmp.name
        
        message.download(tmp_path)
        
        bot_msg.edit_text("🧲 קובץ טורנט התקבל, מתחיל הורדה...")
        
        # Start torrent download
        torrent_entrance(client, bot_msg, f"torrent:{file_name}", tmp_path)
        
    except ValueError as e:
        report_error_to_archive(client, message.from_user, file_name, e)
    except Exception as e:
        report_error_to_archive(client, message.from_user, file_name, e)
        logging.error("Torrent file handling failed", exc_info=True)
        bot_msg.edit_text(
            "❌ לא הצלחתי לעבד את קובץ הטורנט.\n"
            "ודא שהקובץ תקין ונסה שוב."
        )
    finally:
        end_request_log()
        concurrency_manager.release(chat_id)
        # Cleanup temp file
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass


def _build_settings_markup(chat_id):
    """Build toggle-style settings keyboard with current user settings."""
    # Get current settings
    quality = get_quality_settings(chat_id)  # returns 'high', 'medium', 'low'
    send_format = get_format_settings(chat_id)  # returns 'video', 'document', 'audio'
    subtitles_enabled = get_subtitles_settings(chat_id)  # returns True/False
    title_length = get_title_length_settings(chat_id)  # returns 100, 250, 500, 1000
    
    # Quality display mapping
    quality_display = {
        'high': '1080p',
        'medium': '720p',
        'low': '480p'
    }
    
    # Format display mapping
    format_display = {
        'video': 'וידאו',
        'document': 'קובץ',
        'audio': 'שמע'
    }
    
    # Build toggle buttons - each shows current state
    quality_btn = types.InlineKeyboardButton(
        f"🎥 איכות: {quality_display.get(quality, '1080p')}",
        callback_data="toggle_quality"
    )
    
    format_btn = types.InlineKeyboardButton(
        f"📤 שליחה: {format_display.get(send_format, 'וידאו')}",
        callback_data="toggle_format"
    )
    
    subtitles_btn = types.InlineKeyboardButton(
        f"📝 כתוביות: {'פעיל' if subtitles_enabled else 'כבוי'}",
        callback_data="toggle_subtitles"
    )
    
    title_btn = types.InlineKeyboardButton(
        f"📑 אורך כותרת: {title_length}",
        callback_data="toggle_title_len"
    )
    
    return types.InlineKeyboardMarkup(
        [
            [quality_btn],
            [format_btn],
            [subtitles_btn],
            [title_btn],
        ]
    )


@app.on_callback_query(filters.regex(r"^toggle_quality$"))
def toggle_quality_callback(client: Client, callback_query: types.CallbackQuery):
    """Toggle quality: 1080p → 720p → 480p → 1080p"""
    chat_id = callback_query.message.chat.id
    current = get_quality_settings(chat_id)
    
    # Cycle: high → medium → low → high
    cycle = {'high': 'medium', 'medium': 'low', 'low': 'high'}
    new_quality = cycle.get(current, 'high')
    
    display = {'high': '1080p', 'medium': '720p', 'low': '480p'}
    
    logging.info("Setting %s quality to %s", chat_id, new_quality)
    set_user_settings(chat_id, "quality", new_quality)
    callback_query.answer(f"✅ איכות: {display[new_quality]}")
    
    try:
        callback_query.message.edit_text(BotText.settings, reply_markup=_build_settings_markup(chat_id))
    except pyrogram.errors.MessageNotModified:
        pass


@app.on_callback_query(filters.regex(r"^toggle_format$"))
def toggle_format_callback(client: Client, callback_query: types.CallbackQuery):
    """Toggle format: וידאו ↔ קובץ"""
    chat_id = callback_query.message.chat.id
    current = get_format_settings(chat_id)
    
    # Toggle: video ↔ document
    new_format = 'document' if current == 'video' else 'video'
    display = {'video': 'וידאו', 'document': 'קובץ'}
    
    logging.info("Setting %s format to %s", chat_id, new_format)
    set_user_settings(chat_id, "format", new_format)
    callback_query.answer(f"✅ שליחה: {display[new_format]}")
    
    try:
        callback_query.message.edit_text(BotText.settings, reply_markup=_build_settings_markup(chat_id))
    except pyrogram.errors.MessageNotModified:
        pass


@app.on_callback_query(filters.regex(r"^toggle_subtitles$"))
def toggle_subtitles_callback(client: Client, callback_query: types.CallbackQuery):
    """Toggle subtitles: פעיל ↔ כבוי"""
    chat_id = callback_query.message.chat.id
    current = get_subtitles_settings(chat_id)
    
    # Toggle on/off
    new_value = 0 if current else 1
    
    logging.info("Setting %s subtitles to %s", chat_id, new_value)
    set_user_settings(chat_id, "subtitles", new_value)
    
    if new_value:
        callback_query.answer("✅ כתוביות: פעיל")
    else:
        callback_query.answer("❌ כתוביות: כבוי")
    
    try:
        callback_query.message.edit_text(BotText.settings, reply_markup=_build_settings_markup(chat_id))
    except pyrogram.errors.MessageNotModified:
        pass


@app.on_callback_query(filters.regex(r"^toggle_title_len$"))
def toggle_title_length_callback(client: Client, callback_query: types.CallbackQuery):
    """Toggle title length: 1000 → 500 → 250 → 100 → 1000"""
    chat_id = callback_query.message.chat.id
    current = get_title_length_settings(chat_id)
    
    # Cycle: 1000 → 500 → 250 → 100 → 1000
    cycle = {1000: 500, 500: 250, 250: 100, 100: 1000}
    new_length = cycle.get(current, 1000)
    
    logging.info("Setting %s title_length to %s", chat_id, new_length)
    set_user_settings(chat_id, "title_length", new_length)
    callback_query.answer(f"✅ אורך כותרת: {new_length} תווים")
    
    try:
        callback_query.message.edit_text(BotText.settings, reply_markup=_build_settings_markup(chat_id))
    except pyrogram.errors.MessageNotModified:
        pass


@app.on_callback_query(filters.regex(r"^ytq:"))
def youtube_quality_callback(client: Client, callback_query: types.CallbackQuery):
    """Handle YouTube quality selection callback."""
    from engine.request_logger import start_request_log, end_request_log
    
    chat_id = callback_query.message.chat.id
    data = callback_query.data
    
    # Parse callback data: ytq:quality:url_hash
    parts = data.split(":")
    if len(parts) != 3:
        callback_query.answer("❌ שגיאה בעיבוד הבקשה", show_alert=True)
        return
    
    _, quality, url_hash = parts
    
    # Get URL from cache
    url = _youtube_url_cache.get(url_hash)
    if not url:
        callback_query.answer("❌ הקישור פג תוקף, נא לשלוח שוב", show_alert=True)
        return
    
    # Remove from cache
    _youtube_url_cache.pop(url_hash, None)
    
    # Answer callback
    quality_names = {
        '1080': '1080p HD',
        '720': '720p',
        '480': '480p',
        '360': '360p',
        'audio': 'שמע בלבד'
    }
    if quality == 'audio':
        callback_query.answer("⏳ מתחיל להוריד שמע...")
    else:
        callback_query.answer(f"⏳ מתחיל הורדה באיכות {quality_names.get(quality, quality)}...")
    
    # Update the message to show download started
    try:
        if quality == 'audio':
            callback_query.message.edit_text("🔄 מוריד שמע...")
        else:
            callback_query.message.edit_text(f"🔄 מוריד באיכות {quality_names.get(quality, quality)}...")
    except Exception as e:
        logging.error("Failed to edit message: %s", e)
    
    # Start request logging
    start_request_log(url, chat_id)
    
    # Check concurrency limit
    if not concurrency_manager.acquire(chat_id):
        callback_query.message.edit_text(
            "❌ **יש לך יותר מדי פעולות פעילות במקביל.**\n"
            "אנא המתן לסיום ההורדות הקיימות."
        )
        end_request_log()
        return
    
    # Start download with selected quality
    try:
        client.send_chat_action(chat_id, enums.ChatAction.UPLOAD_VIDEO)
        try:
            youtube_entrance_with_quality(client, callback_query.message, url, quality)
        except CreditsExhaustedException:
            raise  # Let outer handler deal with credits
        except Exception as ytdlp_e:
            # yt-dlp failed — try JDownloader2 as last resort
            logging.info("YouTube download failed for %s, trying JDownloader2: %s", url, ytdlp_e)
            callback_query.message.edit_text("🔧 yt-dlp נכשל, מנסה דרך JDownloader2...")
            jdownloader_entrance(client, callback_query.message, url)
    except CreditsExhaustedException:
        # User has no credits - show purchase message with button
        markup = types.InlineKeyboardMarkup([
            [types.InlineKeyboardButton("💬 לרכישת קרדיטים", url="https://t.me/YD_IL")]
        ])
        callback_query.message.edit_text(
            "❌ הקרדיטים שלך נגמרו.\n"
            "לרכישת קרדיטים נוספים, צור קשר עם יוצר הבוט. 👇",
            reply_markup=markup
        )
    except Exception as e:
        # Get user for error reporting
        user = callback_query.from_user
        report_error_to_archive(client, user, url, e)
        logging.error("Download failed", exc_info=True)
        callback_query.message.edit_text(
            "❌ לא הצלחתי להוריד את הקישור הזה כרגע.\n"
            "נסה שוב עוד מעט או שלח קישור אחר."
        )
    finally:
        end_request_log()
        concurrency_manager.release(chat_id)


@app.on_callback_query(filters.regex(r"^cancel:"))
def cancel_callback(client: Client, callback_query: types.CallbackQuery):
    """Handle cancel button callback."""
    data = callback_query.data
    # data format: cancel:chat_id:message_id
    try:
        _, cid, mid = data.split(":")
    except ValueError:
        callback_query.answer("❌ שגיאה בזיהוי משימה לביטול", show_alert=True)
        return
    
    key = f"{cid}_{mid}"
    cancellation_events.add(key)
    
    callback_query.answer("🛑 מבטל...")


@app.on_callback_query(filters.regex(r"^resume:"))
def resume_callback(client: Client, callback_query: types.CallbackQuery):
    """טיפול בכפתור המשך הורדה."""
    from engine.request_logger import start_request_log, end_request_log
    
    data = callback_query.data
    
    # Parse callback data: resume:state_hash
    parts = data.split(":")
    if len(parts) != 2:
        callback_query.answer("❌ שגיאה בעיבוד הבקשה", show_alert=True)
        return
    
    _, state_hash = parts
    
    # Get resume state from cache
    resume_state = _resume_state_cache.pop(state_hash, None)
    if not resume_state:
        callback_query.answer("❌ הבקשה פגה תוקף, נא לשלוח שוב", show_alert=True)
        return
    
    url = resume_state.get("url")
    download_type = resume_state.get("download_type", "generic")
    quality = resume_state.get("quality")
    chat_id = resume_state.get("chat_id")
    
    callback_query.answer("⏳ ממשיך הורדה...")
    
    try:
        callback_query.message.edit_text("🔄 ממשיך הורדה...")
    except Exception as e:
        logging.error("Failed to edit resume message: %s", e)
    
    # Start request logging
    start_request_log(url, chat_id)
    
    try:
        client.send_chat_action(chat_id, enums.ChatAction.UPLOAD_VIDEO)
        
        if download_type == "direct":
            direct_entrance(client, callback_query.message, url)
        elif download_type == "youtube":
            if quality and quality not in ['high', 'medium', 'low']:
                # Specific quality like '1080', '720', etc.
                youtube_entrance_with_quality(client, callback_query.message, url, quality)
            else:
                youtube_entrance(client, callback_query.message, url)
        else:
            # Try special handlers first, then fall back to yt-dlp
            try:
                special_download_entrance(client, callback_query.message, url)
            except ValueError as inner_e:
                if "לא נמצא מוריד" in str(inner_e):
                    youtube_entrance(client, callback_query.message, url)
    except Exception as e:
        user = callback_query.from_user
        report_error_to_archive(client, user, url, e)
        logging.error("Resume download failed", exc_info=True)
        callback_query.message.edit_text(
            "❌ לא הצלחתי להמשיך את ההורדה כרגע.\n"
            "נסה שוב עוד מעט או שלח קישור אחר."
        )
    finally:
        end_request_log()
        concurrency_manager.release(chat_id)


if __name__ == "__main__":
    # === Prevent duplicate instances ===
    _LOCKFILE = os.path.join(os.path.dirname(__file__), ".bot.pid")
    
    def _kill_old_instance():
        """Kill any previous bot instance that's still running."""
        if not os.path.exists(_LOCKFILE):
            return
        try:
            old_pid = int(open(_LOCKFILE).read().strip())
            if old_pid == os.getpid():
                return
            old_proc = psutil.Process(old_pid)
            # Verify it's actually our bot (not some other process reusing the PID)
            cmdline = " ".join(old_proc.cmdline())
            if "main.py" in cmdline:
                logging.warning("⚠️ Killing old bot instance (PID %d) to prevent database lock", old_pid)
                # Kill children first (sub-processes)
                for child in old_proc.children(recursive=True):
                    child.kill()
                old_proc.kill()
                old_proc.wait(timeout=5)
                logging.info("✅ Old instance killed successfully")
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            pass  # Process already gone or can't access
        except Exception as e:
            logging.warning("Could not kill old instance: %s", e)
    
    _kill_old_instance()
    
    # Write our PID
    with open(_LOCKFILE, "w") as f:
        f.write(str(os.getpid()))
    
    botStartTime = time.time()
    scheduler = BackgroundScheduler()
    # Daily reset removed - credits are now persistent
    # scheduler.add_job(reset_free, "cron", hour=0, minute=0)
    scheduler.start()
    
    # Auto-update yt-dlp on every startup
    auto_update_ytdlp()
    
    banner = f"""
▌ ▌         ▀▛▘     ▌       ▛▀▖              ▜            ▌
▝▞  ▞▀▖ ▌ ▌  ▌  ▌ ▌ ▛▀▖ ▞▀▖ ▌ ▌ ▞▀▖ ▌  ▌ ▛▀▖ ▐  ▞▀▖ ▝▀▖ ▞▀▌
 ▌  ▌ ▌ ▌ ▌  ▌  ▌ ▌ ▌ ▌ ▛▀  ▌ ▌ ▌ ▌ ▐▐▐  ▌ ▌ ▐  ▌ ▌ ▞▀▌ ▌ ▌
 ▘  ▝▀  ▝▀▘  ▘  ▝▀▘ ▀▀  ▝▀▘ ▀▀  ▝▀   ▘▘  ▘ ▘  ▘ ▝▀  ▝▀▘ ▝▀▘

By @BennyThink, VIP Mode: {ENABLE_VIP} 
    """
    print(banner)
    
    # Check if bot was restarted after yt-dlp update and send notification on startup
    @app.on_message(filters.command(["__startup_check__"]))
    def _dummy_startup_handler(client, message):
        pass  # Never actually called, just for registration
    
    # Use a thread to send the notification after a short delay
    def send_update_notification_on_startup():
        import time as t
        t.sleep(3)  # Wait for bot to fully connect
        try:
            check_and_send_update_notification(app)
        except Exception as e:
            logging.error("Failed to send update notification: %s", e)
        
        # Check JDownloader2 connection
        try:
            from engine.jdownloader_manager import JDownloaderManager, JDownloaderConnectionError
            from config import JDOWNLOADER_EMAIL
            if JDOWNLOADER_EMAIL:
                mgr = JDownloaderManager()
                logging.info("✅ JDownloader2 API connected successfully")
            else:
                logging.info("ℹ️ JDownloader2 not configured (JDOWNLOADER_EMAIL is empty)")
        except JDownloaderConnectionError as e:
            logging.warning("⚠️ JDownloader2 connection failed: %s", e)
        except Exception as e:
            logging.warning("⚠️ JDownloader2 check failed: %s", e)
    
    threading.Thread(target=send_update_notification_on_startup, daemon=True).start()
    
    app.run()
