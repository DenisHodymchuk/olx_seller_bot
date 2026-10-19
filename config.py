import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()

OLX_CLIENT_ID = os.getenv("OLX_CLIENT_ID", "202769").strip()
OLX_CLIENT_SECRET = os.getenv("OLX_CLIENT_SECRET", "").strip()
OLX_REDIRECT_URI = os.getenv("OLX_REDIRECT_URI", "https://bubatgshop-production.up.railway.app/").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

DEFAULT_CITY_NAME = os.getenv("DEFAULT_CITY_NAME", "Хмельницький").strip()
DEFAULT_CITY_ID = int(os.getenv("DEFAULT_CITY_ID", "101"))  # 101 = м. Хмельницький в OLX.ua
DEFAULT_CONTACT_NAME = os.getenv("DEFAULT_CONTACT_NAME", "Денис").strip()
DEFAULT_CONTACT_PHONE = os.getenv("DEFAULT_CONTACT_PHONE", "0689531334").strip()
ADVERTISER_TYPE = os.getenv("ADVERTISER_TYPE", "private").strip()

PORT = int(os.getenv("PORT", "8080"))

TOKENS_FILE = BASE_DIR / "olx_tokens.json"
STATE_FILE = BASE_DIR / "adverts_state.json"

# URL для відкриття Telegram Mini App (якщо розгорнуто на Railway або локально)
WEBAPP_URL = os.getenv("WEBAPP_URL", OLX_REDIRECT_URI.rstrip("/") + "/app").strip()

CATEGORY_PRESETS = {
    "collectibles": {
        "name": "🏆 Колекціонування / 3D Моделі",
        "short": "🏆 Колекціонування",
        "category_id": 626,
    },
    "toys_figures": {
        "name": "🐉 Фігурки персонажів / Іграшки",
        "short": "🐉 Фігурки",
        "category_id": 3651,
    },
    "handmade_gifts": {
        "name": "🎁 Іграшки ручної роботи",
        "short": "🎁 Ручна робота",
        "category_id": 2354,
    },
    "decor_other": {
        "name": "🏺 Декор / Сувеніри",
        "short": "🏺 Декор і сувеніри",
        "category_id": 2355,
    },
    "printing_service": {
        "name": "🖨 3D друк / Послуги",
        "short": "🖨 3D Послуги",
        "category_id": 3550,
    },
}

STATUS_LABELS = {
    "active": "✅ Активне на OLX",
    "new": "⏳ На модерації OLX",
    "disabled": "⏳ Обробляється модерацією",
    "limited": "💳 Очікує оплати / Ліміт",
    "outdated": "📦 Неактивне (термін вийшов)",
    "removed_by_user": "🗑 Видалене вами",
    "removed_by_moderator": "⛔ Відхилено модератором!",
    "unconfirmed": "⚠️ Не підтверджено",
}
