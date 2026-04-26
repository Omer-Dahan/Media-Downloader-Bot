import re
from urllib.parse import urlparse
from engine.helper import handle_download_error
from engine.direct import DirectDownload


def pixeldrain_download(client, bot_message, url):
    FILE_URL_FORMAT = "https://pixeldrain.com/api/file/{}?download"
    USER_PAGE_PATTERN = re.compile(r"https://pixeldrain.com/u/(\w+)")

    def _extract_file_id(url):
        if match := USER_PAGE_PATTERN.match(url):
            return match.group(1)

        parsed = urlparse(url)
        if parsed.path.startswith("/file/"):
            return parsed.path.split("/")[-1]

        raise ValueError("פורמט קישור Pixeldrain לא תקין")

    def _get_download_url(file_id):
        return FILE_URL_FORMAT.format(file_id)

    def _download(url):
        try:
            file_id = _extract_file_id(url)
            download_url = _get_download_url(file_id)

            ddl = DirectDownload(client, bot_message, download_url)
            ddl.start()

        except ValueError as e:
            handle_download_error(bot_message, e)

    _download(url)
