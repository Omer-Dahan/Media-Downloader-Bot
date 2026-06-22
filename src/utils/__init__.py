import os
import platform
import re
import subprocess
import time
from urllib.parse import urlparse

import psutil

from engine.helper import sizeof_fmt


def setup_secure_dir(path: str) -> None:
    """יוצר תיקייה עם הרשאת הרצה מחוסמת למניעת הפעלת קבצים זדוניים."""
    os.makedirs(path, exist_ok=True)
    if platform.system() == "Windows":
        subprocess.run(
            ["icacls", path, "/deny", "Everyone:(X)"], capture_output=True, check=False
        )
    else:
        current = os.stat(path).st_mode
        os.chmod(path, current & ~0o111)  # הסר Execute מכולם


def get_system_stats() -> dict:
    """Get system resource usage statistics."""

    def safe(func, *args):
        try:
            return func(*args)
        except Exception:
            return None

    cpu_usage = safe(psutil.cpu_percent)
    disk_usage = safe(psutil.disk_usage, "/")
    swap = safe(psutil.swap_memory)
    memory = safe(psutil.virtual_memory)
    net_io = safe(psutil.net_io_counters)
    boot_time = safe(psutil.boot_time)

    # Cores
    try:
        p_cores = psutil.cpu_count(logical=False) or "N/A"
        t_cores = psutil.cpu_count(logical=True) or "N/A"
    except Exception:
        p_cores = t_cores = "N/A"

    return {
        "cpu_usage": f"{cpu_usage}%" if cpu_usage is not None else "N/A",
        "disk": disk_usage,
        "swap": swap,
        "memory": memory,
        "net_io": net_io,
        "boot_time": boot_time,
        "p_cores": p_cores,
        "t_cores": t_cores,
    }


def format_system_stats(bot_start_time=None) -> tuple[str, str]:
    """
    Format system stats into strings.
    Returns: (owner_stats_str, user_stats_str)
    """
    stats = get_system_stats()

    # Disk
    if stats["disk"]:
        total, used, free, disk_percent = stats["disk"]
        total_str = sizeof_fmt(total)
        used_str = sizeof_fmt(used)
        free_str = sizeof_fmt(free)
        disk_percent_str = f"{disk_percent}%"
    else:
        total_str = used_str = free_str = disk_percent_str = "N/A"

    # Memory
    if stats["memory"]:
        mem_total = sizeof_fmt(stats["memory"].total)
        mem_free = sizeof_fmt(stats["memory"].available)
        mem_used = sizeof_fmt(stats["memory"].used)
        mem_percent = f"{stats['memory'].percent}%"
    else:
        mem_total = mem_free = mem_used = mem_percent = "N/A"

    # Swap
    if stats["swap"]:
        swap_total = sizeof_fmt(stats["swap"].total)
        swap_percent = f"{stats['swap'].percent}%"
    else:
        swap_total = swap_percent = "N/A"

    # Net IO
    if stats["net_io"]:
        sent = sizeof_fmt(stats["net_io"].bytes_sent)
        recv = sizeof_fmt(stats["net_io"].bytes_recv)
    else:
        sent = recv = "N/A"

    # Uptime
    bot_uptime = timeof_fmt(time.time() - bot_start_time) if bot_start_time else "N/A"
    os_uptime = (
        timeof_fmt(time.time() - stats["boot_time"]) if stats["boot_time"] else "N/A"
    )

    # Base top info
    base_info = (
        f"<b>╭🖥️ **שימוש במעבד »**</b>  __{stats['cpu_usage']}__\n"
        f"<b>├💾 **שימוש בזיכרון »**</b>  __{mem_percent}__\n"
        f"<b>╰🗃️ **שימוש בדיסק »**</b>  __{disk_percent_str}__\n\n"
        f"<b>╭📤העלאה:</b> {sent}\n"
        f"<b>╰📥הורדה:</b> {recv}\n\n\n"
        f"<b>סה״כ זיכרון:</b> {mem_total}\n"
        f"<b>זיכרון פנוי:</b> {mem_free}\n"
        f"<b>זיכרון בשימוש:</b> {mem_used}\n"
    )

    owner_stats = (
        "\n\n⌬─────「 סטטיסטיקות 」─────⌬\n\n"
        + base_info
        + f"<b>סה״כ SWAP:</b> {swap_total} | <b>שימוש ב-SWAP:</b> {swap_percent}\n\n"
        f"<b>סה״כ שטח דיסק:</b> {total_str}\n"
        f"<b>בשימוש:</b> {used_str} | <b>פנוי:</b> {free_str}\n\n"
        f"<b>ליבות פיזיות:</b> {stats['p_cores']}\n"
        f"<b>סה״כ ליבות:</b> {stats['t_cores']}\n\n"
        f"<b>🤖זמן פעילות הבוט:</b> {bot_uptime}\n"
        f"<b>⏲️זמן פעילות המערכת:</b> {os_uptime}\n"
    )

    user_stats = (
        "\n\n⌬─────「 סטטיסטיקות 」─────⌬\n\n"
        + base_info
        + f"<b>סה״כ שטח דיסק:</b> {total_str}\n"
        f"<b>בשימוש:</b> {used_str} | <b>פנוי:</b> {free_str}\n\n"
        f"<b>🤖זמן פעילות הבוט:</b> {bot_uptime}\n"
    )

    return owner_stats, user_stats


def timeof_fmt(seconds: int | float):
    periods = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    result = ""
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result += f"{int(period_value)}{period_name}"
    return result


def is_youtube(url: str) -> bool:
    try:
        if not url or not isinstance(url, str):
            return False

        parsed = urlparse(url)
        return parsed.netloc.lower() in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
            "music.youtube.com",
            "www.youtube-nocookie.com",
            "youtube-nocookie.com",
        }

    except Exception:
        return False


def extract_url_and_name(message_text):
    # Regular expression to match the URL
    url_pattern = r"(https?://[^\s]+)"
    # Regular expression to match the new name after '-n'
    name_pattern = r"-n\s+(.+)$"

    # Find the URL in the message_text
    url_match = re.search(url_pattern, message_text)
    url = url_match.group(0) if url_match else None

    # Find the new name in the message_text
    name_match = re.search(name_pattern, message_text)
    new_name = name_match.group(1) if name_match else None

    return url, new_name
