import functools
import threading
import time


def debounce(wait_seconds):
    """
    Thread-safe debounce decorator for functions that take a message with chat.id and msg.id attributes.
    The function will only be called if it hasn't been called with the same chat.id and msg.id in the last 'wait_seconds'.
    """

    def decorator(func):
        last_called = {}
        lock = threading.Lock()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal last_called
            now = time.time()

            # Assuming the first argument is the message object with chat.id and msg.id
            bot_msg = args[0]._bot_msg
            key = (bot_msg.chat.id, bot_msg.id)

            with lock:
                if key not in last_called or now - last_called[key] >= wait_seconds:
                    last_called[key] = now
                    return func(*args, **kwargs)

        return wrapper

    return decorator


def sizeof_fmt(num: int, suffix="B"):
    """Format file size in human-readable format."""
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, "Yi", suffix)


def moon_progress_bar(percent: float, total_cells: int = 10) -> str:
    """
    Build a moon phase progress bar (RTL - right to left).

    Uses waxing phases for RTL visual (progress fills from right):
    🌑 empty → 🌒 quarter → 🌓 half → 🌔 three-quarter → 🌕 full

    Args:
        percent: Progress percentage (0-100)
        total_cells: Number of moon cells (default 10)

    Returns:
        String of moon emojis representing progress (RTL)
    """
    progress = max(0, min(100, percent)) / 100
    filled_cells = int(progress * total_cells)
    remainder = (progress * total_cells) - filled_cells

    # Calculate partial moon (using waxing phases: 🌒🌓🌔)
    partial_moon = ""
    if filled_cells < total_cells and remainder > 0:
        if remainder >= 0.67:
            partial_moon = "🌔"
            filled_cells += 1
        elif remainder >= 0.34:
            partial_moon = "🌓"
            filled_cells += 1
        else:
            partial_moon = "🌒"
            filled_cells += 1

    # RTL: full moons on right (start), partial in middle, empty on left (end)
    empty_count = total_cells - filled_cells

    # Correction: filled_cells includes the partial one if present in the previous logic count?
    # Logic above: if remainder, filled_cells += 1. So filled_cells includes the partial slot.
    # We want: (filled_cells - 1) Full Moons + 1 Partial
    # If no partial: filled_cells Full Moons.

    full_count = filled_cells - (1 if partial_moon else 0)

    return "🌕" * full_count + partial_moon + "🌑" * empty_count


def safe_truncate(text: str, limit: int = 4000) -> str:
    """
    Safely truncate text to stay within Telegram's message limit.
    Adds ellipsis if truncated.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def handle_download_error(bot_message, error: Exception):
    """Safely format and send a generic download error message."""
    truncated_error = safe_truncate(str(error), limit=3500)
    bot_message.edit_text(
        f"ההורדה נכשלה!❌\nאירעה שגיאה: `{truncated_error}`\n"
        "אנא בדוק את הקישור ונסה שוב."
    )


def extract_metadata_from_info(info: dict) -> dict:
    """Safely extract title and description from a yt-dlp info dictionary."""
    if not info:
        return {"title": "", "description": ""}

    # Standard title fields
    title = info.get("title") or info.get("fulltitle") or ""

    # Standard description field
    description = info.get("description", "") or ""

    # Fallback: if title is empty but description exists, use short part of description as title
    if not title and description:
        title = description.split("\n")[0][:100]

    return {"title": title[:1000].strip(), "description": description[:50000].strip()}


def get_user_display_name(user_id: int) -> str:
    """Safely get formatted user display name from stats."""
    from database.model import get_user_stats

    user_info = get_user_stats(user_id)
    if user_info:
        name = user_info.get("first_name") or ""
        if user_info.get("username"):
            name = f"{name} @{user_info['username']}".strip()
        return name if name else str(user_id)
    return str(user_id)


def create_telegraph_page(title, url, description):
    """
    Create a Telegra.ph page for long descriptions.

    Returns:
        The URL of the created page, or None on failure.
    """
    import requests
    import logging

    # 1. Prepare nodes for Telegraph (must be JSON nodes)
    nodes = []

    # Attempt to embed if it's YouTube
    if "youtube.com" in url or "youtu.be" in url:
        video_id = ""
        if "youtu.be" in url:
            video_id = url.split("/")[-1].split("?")[0]
        elif "v=" in url:
            video_id = url.split("v=")[1].split("&")[0]

        if video_id:
            nodes.append(
                {
                    "tag": "figure",
                    "children": [
                        {
                            "tag": "iframe",
                            "attrs": {
                                "src": f"https://www.youtube.com/embed/{video_id}"
                            },
                        }
                    ],
                }
            )

    # Add source link
    nodes.append(
        {
            "tag": "p",
            "children": [
                "🔗 ",
                {"tag": "a", "attrs": {"href": url}, "children": ["קישור מקור"]},
            ],
        }
    )
    nodes.append({"tag": "hr"})

    # Add description (split into paragraphs)
    if description:
        for line in description.split("\n"):
            if line.strip():
                nodes.append({"tag": "p", "children": [line.strip()]})

    try:
        # 1. Create a temporary account (or reuse)
        account_resp = requests.post(
            "https://api.telegra.ph/createAccount",
            json={"short_name": "YD_IL", "author_name": "Download Bot"},
            timeout=10,
        )

        if not account_resp.ok:
            logging.error("Telegraph createAccount failed: %s", account_resp.text)
            return None

        token = account_resp.json()["result"]["access_token"]

        # 2. Create the page
        create_resp = requests.post(
            "https://api.telegra.ph/createPage",
            json={
                "access_token": token,
                "title": title or "תיאור סרטון",
                "content": nodes,
                "return_content": False,
            },
            timeout=15,
        )

        if create_resp.ok and create_resp.json().get("ok"):
            return create_resp.json()["result"]["url"]
        logging.error("Telegraph createPage failed: %s", create_resp.text)

    except Exception as e:
        logging.error("Telegraph integration error: %s", e)

    return None
