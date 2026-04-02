import os
import socket
import time
import urllib3
from dotenv import load_dotenv
from telebot import apihelper
from urllib3.exceptions import InsecureRequestWarning


load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "bot_data.db")
LOG_PATH = os.path.join(BASE_DIR, "bot.log")

urllib3.disable_warnings(category=InsecureRequestWarning)
socket.setdefaulttimeout(30)

apihelper.SESSION_TIME_TO_LIVE = 5 * 60
apihelper.READ_TIMEOUT = 30
apihelper.CONNECT_TIMEOUT = 30
apihelper.RETRY_DELAY = 1
apihelper.MAX_RETRIES = 5
apihelper.ENABLE_MIDDLEWARE = True

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
GENIUS_CLIENT_ID = os.getenv("GENIUS_CLIENT_ID")
GENIUS_CLIENT_SECRET = os.getenv("GENIUS_CLIENT_SECRET")
YANDEX_MUSIC_TOKEN = os.getenv("YANDEX_MUSIC_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

_admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {
    int(x.strip())
    for x in _admin_ids_raw.replace(";", ",").split(",")
    if x.strip().isdigit()
}

PER_PAGE = 8
BOT_START_TS = time.time()


def validate_tokens() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN не задан. Установите переменную окружения TELEGRAM_TOKEN")
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise ValueError("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET не заданы.")
