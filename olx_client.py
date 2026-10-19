import json
import time
import logging
from urllib.parse import urlencode
from typing import Any, Dict, List, Optional
from curl_cffi.requests import AsyncSession

from config import (
    OLX_CLIENT_ID,
    OLX_CLIENT_SECRET,
    OLX_REDIRECT_URI,
    DEFAULT_CITY_ID,
    DEFAULT_CONTACT_NAME,
    DEFAULT_CONTACT_PHONE,
    ADVERTISER_TYPE,
    TOKENS_FILE,
)

logger = logging.getLogger(__name__)

OLX_BASE_URL = "https://www.olx.ua"


class OLXClient:
    def __init__(self):
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at: float = 0
        self.cached_city_id: int = DEFAULT_CITY_ID
        self._load_tokens()

    def _load_tokens(self):
        if TOKENS_FILE.exists():
            try:
                data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
                self.access_token = data.get("access_token")
                self.refresh_token = data.get("refresh_token")
                self.expires_at = float(data.get("expires_at", 0))
            except Exception as e:
                logger.error(f"Помилка читання файла токенів OLX: {e}")

    def _save_tokens(self):
        try:
            data = {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "cached_city_id": DEFAULT_CITY_ID,
            }
            TOKENS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Помилка збереження токенів OLX: {e}")

    def get_authorization_url(self) -> str:
        """Генерує посилання для авторизації користувача в OLX."""
        params = {
            "client_id": OLX_CLIENT_ID,
            "response_type": "code",
            "state": "olx_bot_auth",
            "scope": "read write v2",
            "redirect_uri": OLX_REDIRECT_URI,
        }
        return f"{OLX_BASE_URL}/oauth/authorize/?{urlencode(params)}"

    def is_authorized(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    async def exchange_code_for_token(self, code: str) -> Dict[str, Any]:
        """Обмінює одноразовий authorization code на access_token та refresh_token."""
        payload = {
            "grant_type": "authorization_code",
            "client_id": OLX_CLIENT_ID,
            "client_secret": OLX_CLIENT_SECRET,
            "code": code.strip(),
            "scope": "read write v2",
            "redirect_uri": OLX_REDIRECT_URI,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://www.olx.ua",
            "Referer": "https://www.olx.ua/",
        }
        async with AsyncSession(impersonate="chrome120", timeout=25) as session:
            resp = await session.post(f"{OLX_BASE_URL}/api/open/oauth/token", json=payload, headers=headers)
            if resp.status_code != 200:
                short_err = resp.text[:300].replace("<", "&lt;").replace(">", "&gt;")
                raise RuntimeError(f"OLX OAuth ({resp.status_code}): {short_err}")
            data = resp.json()
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token")
            expires_in = int(data.get("expires_in", 86400))
            self.expires_at = time.time() + expires_in - 300
            self._save_tokens()
            return data

    async def refresh_access_token(self) -> str:
        """Оновлює access_token через refresh_token."""
        if not self.refresh_token:
            raise RuntimeError("Немає refresh_token. Виконайте повторну авторизацію через /auth.")

        payload = {
            "grant_type": "refresh_token",
            "client_id": OLX_CLIENT_ID,
            "client_secret": OLX_CLIENT_SECRET,
            "refresh_token": self.refresh_token,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        async with AsyncSession(impersonate="chrome120", timeout=25) as session:
            resp = await session.post(f"{OLX_BASE_URL}/api/open/oauth/token", json=payload, headers=headers)
            if resp.status_code != 200:
                short_err = resp.text[:300].replace("<", "&lt;").replace(">", "&gt;")
                raise RuntimeError(f"Не вдалося оновити токен OLX ({resp.status_code}): {short_err}")
            data = resp.json()
            self.access_token = data["access_token"]
            if data.get("refresh_token"):
                self.refresh_token = data["refresh_token"]
            expires_in = int(data.get("expires_in", 86400))
            self.expires_at = time.time() + expires_in - 300
            self._save_tokens()
            return self.access_token

    async def get_valid_token(self) -> str:
        if not self.access_token:
            if self.refresh_token:
                return await self.refresh_access_token()
            raise RuntimeError("Бот ще не підключений до вашого акаунту OLX. Натисніть /auth для підключення.")
        if time.time() >= self.expires_at and self.refresh_token:
            return await self.refresh_access_token()
        return self.access_token

    async def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        token = await self.get_valid_token()
        headers = kwargs.pop("headers", {})
        headers.update({
            "Authorization": f"Bearer {token}",
            "Version": "2.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Language": "uk",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        url = f"{OLX_BASE_URL}{endpoint}"
        async with AsyncSession(impersonate="chrome120", timeout=30) as session:
            resp = await session.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 401 and self.refresh_token:
                token = await self.refresh_access_token()
                headers["Authorization"] = f"Bearer {token}"
                resp = await session.request(method, url, headers=headers, **kwargs)

            if resp.status_code >= 400:
                short_err = resp.text[:400].replace("<", "&lt;").replace(">", "&gt;")
                raise RuntimeError(f"OLX API ({resp.status_code}): {short_err}")
            if not resp.text:
                return {}
            return resp.json()

    async def build_required_attributes(self, category_id: int) -> List[Dict[str, Any]]:
        """
        Запитує обов'язкові атрибути для обраної категорії OLX
        і автоматично заповнює їх (включно з 626 Колекціонування).
        """
        if category_id == 626:
            return [
                {"code": "subcategory", "value": "prochee-kollektsionirovanie"},
                {"code": "for_gaming", "value": "0"},
            ]

        attributes_payload: List[Dict[str, Any]] = []
        try:
            data = await self._request("GET", f"/api/partner/categories/{category_id}/attributes")
            attrs = data.get("data", [])
            for attr in attrs:
                code = attr.get("code")
                validation = attr.get("validation", {})
                is_required = validation.get("required", False)
                values = attr.get("values", [])

                if not code:
                    continue

                if code == "state":
                    attributes_payload.append({"code": "state", "value": "new"})
                    continue

                if is_required and values:
                    first_val = values[0].get("code") or values[0].get("value")
                    if first_val:
                        attributes_payload.append({"code": code, "value": first_val})
        except Exception as e:
            logger.warning(f"Не вдалося отримати атрибути категорії {category_id}: {e}")

        return attributes_payload

    async def get_adverts(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Повертає список усіх оголошень користувача з OLX API."""
        res = await self._request("GET", f"/api/partner/adverts?limit={limit}")
        return res.get("data", [])

    async def get_threads(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Повертає список діалогів (чатів) користувача на OLX."""
        res = await self._request("GET", f"/api/partner/threads?limit={limit}")
        return res.get("data", [])

    async def get_thread_messages(self, thread_id: int, limit: int = 15) -> List[Dict[str, Any]]:
        """Повертає історію повідомлень конкретного діалогу на OLX."""
        res = await self._request("GET", f"/api/partner/threads/{thread_id}/messages?limit={limit}")
        return res.get("data", [])

    async def send_thread_message(self, thread_id: int, text: str) -> Dict[str, Any]:
        """Відправляє повідомлення покупцю прямо в чат OLX і позначає діалог прочитаним."""
        try:
            await self._request("POST", f"/api/partner/threads/{thread_id}/commands", json={"command": "mark-as-read"})
        except Exception:
            pass
        res = await self._request(
            "POST",
            f"/api/partner/threads/{thread_id}/messages",
            json={"text": text.strip()},
        )
        return res.get("data", res)

    async def get_advert_statistics(self, advert_id: int) -> Dict[str, Any]:
        """Повертає перегляди оголошення, відкриття телефону та додавання в обране."""
        try:
            res = await self._request("GET", f"/api/partner/adverts/{advert_id}/statistics")
            return res.get("data", {"advert_views": 0, "phone_views": 0, "users_observing": 0})
        except Exception:
            return {"advert_views": 0, "phone_views": 0, "users_observing": 0}

    async def get_account_balance(self) -> Dict[str, Any]:
        """Повертає баланс гаманця та бонусів OLX."""
        res = await self._request("GET", "/api/partner/users/me/account-balance")
        return res.get("data", {})

    async def activate_advert_command(self, advert_id: int) -> Dict[str, Any]:
        """Надсилає команду активації оголошення (якщо є куплений пакет або кошти на балансі)."""
        return await self._request("POST", f"/api/partner/adverts/{advert_id}/commands", json={"command": "activate"})

    async def deactivate_advert_command(self, advert_id: int) -> Dict[str, Any]:
        """Деактивує активне оголошення."""
        return await self._request(
            "POST",
            f"/api/partner/adverts/{advert_id}/commands",
            json={"command": "deactivate", "is_success": False},
        )

    async def delete_advert(self, advert_id: int) -> Dict[str, Any]:
        """Видаляє неактивне/неоплачене оголошення."""
        return await self._request("DELETE", f"/api/partner/adverts/{advert_id}")

    async def create_advert(
        self,
        title: str,
        description: str,
        price_uah: int,
        images: List[str],
        category_id: int,
        advertiser_type: str = ADVERTISER_TYPE,
    ) -> Dict[str, Any]:
        """Створює оголошення в OLX через Partner API v2."""
        attributes = await self.build_required_attributes(category_id)

        payload: Dict[str, Any] = {
            "title": title[:70],
            "description": description,
            "category_id": category_id,
            "advertiser_type": advertiser_type,
            "contact": {
                "name": DEFAULT_CONTACT_NAME,
                "phone": DEFAULT_CONTACT_PHONE,
            },
            "location": {
                "city_id": DEFAULT_CITY_ID,
            },
            "price": {
                "value": price_uah,
                "currency": "UAH",
                "negotiable": False,
                "budget": False,
                "trade": False,
            },
            "images": [{"url": img_url} for img_url in images[:8]],
            "attributes": attributes,
        }

        result = await self._request("POST", "/api/partner/adverts", json=payload)
        return result.get("data", result)


olx_client = OLXClient()
