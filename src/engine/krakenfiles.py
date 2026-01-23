import requests
from bs4 import BeautifulSoup
from engine.helper import safe_truncate
from engine.direct import DirectDownload

def krakenfiles_download(client, bot_message, url: str):
    session = requests.Session()

    def _extract_form_data(url: str) -> list[tuple[str, str]]:
        try:
            resp = session.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")

            if post_url := soup.xpath('//form[@id="dl-form"]/@action'):
                post_url = f"https://krakenfiles.com{post_url[0]}"
            else:
                raise ValueError("לא נמצא קישור להורדה.")
            if token := soup.xpath('//input[@id="dl-token"]/@value'):
                data = {"token": token[0]}
            else:
                raise ValueError("לא נמצא טוקן להורדה.")

            return list(zip(post_url, data))

        except requests.RequestException as e:
            raise ValueError(f"נכשל לטעון את הדף: {str(e)}")
        except Exception as e:
            raise ValueError(f"נכשל לעבד את הדף: {str(e)}")

    def _get_download_url(form_data: list[tuple[str, str]]) -> str:
        for post_url, data in form_data:
            try:
                response = session.post(post_url, data=data)
                response.raise_for_status()

                json_data = response.json()
                if "url" in json_data:
                    return json_data["url"]

            except requests.RequestException as e:
                bot_message.edit_text(f"שגיאה בשליחת טופס: {str(e)}")
            except ValueError as e:
                bot_message.edit_text(f"שגיאה בעיבוד תגובה: {str(e)}")

        raise ValueError("לא ניתן לקבל קישור הורדה")

    def _download(url: str):
        try:
            bot_message.edit_text("מעבד את קישור ההורדה...")
            form_data = _extract_form_data(url)
            download_url = _get_download_url(form_data)

            bot_message.edit_text("מתחיל הורדה...")
            downloader = DirectDownload(client, bot_message, download_url)
            downloader.start()

        except ValueError as e:
            truncated_error = safe_truncate(str(e), limit=3500)
            bot_message.edit_text(
                f"ההורדה נכשלה!❌\nאירעה שגיאה: `{truncated_error}`\n"
                "אנא בדוק את הקישור ונסה שוב."
            )


    _download(url)
