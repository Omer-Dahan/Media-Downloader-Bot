import requests
from bs4 import BeautifulSoup
from engine.helper import handle_download_error
from engine.direct import DirectDownload


def krakenfiles_download(client, bot_message, url: str):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    )

    def _extract_form_data(url: str) -> tuple[str, dict]:
        """Parse the krakenfiles page and return (post_url, form_data)."""
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")

            form = soup.find("form", id="dl-form")
            action = form.get("action") if form else None
            if not action:
                raise ValueError("לא נמצא קישור להורדה.")
            # action may be absolute (server subdomain) or relative to the site root
            if action.startswith("http"):
                post_url = action
            else:
                post_url = f"https://krakenfiles.com{action}"

            token_input = soup.find("input", id="dl-token")
            token = token_input.get("value") if token_input else None
            if not token:
                raise ValueError("לא נמצא טוקן להורדה.")

            return post_url, {"token": token}

        except requests.RequestException as e:
            raise ValueError(f"נכשל לטעון את הדף: {str(e)}")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"נכשל לעבד את הדף: {str(e)}")

    def _get_download_url(post_url: str, data: dict) -> str:
        try:
            response = session.post(post_url, data=data, timeout=30)
            response.raise_for_status()

            json_data = response.json()
            if isinstance(json_data, dict) and json_data.get("url"):
                return json_data["url"]
        except requests.RequestException as e:
            raise ValueError(f"שגיאה בשליחת טופס: {str(e)}")
        except ValueError as e:
            raise ValueError(f"שגיאה בעיבוד תגובה: {str(e)}")

        raise ValueError("לא ניתן לקבל קישור הורדה")

    def _download(url: str):
        try:
            bot_message.edit_text("מעבד את קישור ההורדה...")
            post_url, data = _extract_form_data(url)
            download_url = _get_download_url(post_url, data)

            bot_message.edit_text("מתחיל הורדה...")
            downloader = DirectDownload(client, bot_message, download_url)
            downloader.start()

        except ValueError as e:
            handle_download_error(bot_message, e)

    _download(url)
