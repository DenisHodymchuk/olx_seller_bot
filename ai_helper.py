import json
import re
import logging
from typing import Tuple
from google import genai
from google.genai import types
from config import GEMINI_API_KEY
from makerworld import MakerWorldModel

logger = logging.getLogger(__name__)


def _clean_olx_text(text: str) -> str:
    """
    Гарантовано очищує текст від:
    1) Зірочок **, решіток #, підкреслень __ (Markdown)
    2) Будь-яких емодзі / смайликів та спецсимволів Unicode, які OLX не підтримує або ріже.
    Залишає лише українські/латинські літери, цифри, базову пунктуацію та тире.
    """
    text = re.sub(r"[\*#_`~]+", "", text)
    text = text.replace("•", "-")
    text = re.sub(
        r"[^\w\s\d.,!?:;()\"'«»—–\-\+/%\n\rіїєґІЇЄҐ]",
        "",
        text,
        flags=re.UNICODE,
    )
    lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _fallback_template(model: MakerWorldModel) -> Tuple[str, str, str]:
    """Короткий, привабливий шаблон з гачком без смайлів та без зірочок."""
    clean_title = _clean_olx_text(model.title.strip())
    if len(clean_title) > 48:
        clean_title = clean_title[:48].strip()
    title_uk = f"{clean_title} - 3D фігурка іграшка декор"[:70]

    desc_uk = (
        f"Шукаєте оригінальний подарунок або круту річ, яка миттєво привертає увагу? "
        f"{clean_title} — це стильний 3D-виріб, який приємно тримати в руках і неможливо випустити!\n\n"
        f"Чому варто обрати:\n"
        f"- Чітка деталізація та яскравий вигляд наживо\n"
        f"- Можна обрати бажаний колір під ваш смак\n"
        f"- Чудово підходить на подарунок, для колекції чи декору столу\n\n"
        f"Матеріал та безпека:\n"
        f"Виготовлено з сертифікованого біорозкладного еко-пластику PLA (на основі натуральної сировини — кукурудзяного крохмалю). "
        f"Матеріал на 100% безпечний для дітей та дорослих, гіпоалергенний, нетоксичний і взагалі не має запаху.\n\n"
        f"Відправка по всій Україні (OLX Доставка, Нова Пошта, Укрпошта).\n"
        f"Пишіть у повідомлення або телефонуйте — швидко відповім і допоможу обрати колір!"
    )
    return title_uk, desc_uk, "collectibles"


async def generate_olx_copy(model: MakerWorldModel) -> Tuple[str, str, str]:
    """
    Генерує український продаючий заголовок (до 70 символів),
    короткий сильний опис з гачком для покупця OLX (БЕЗ зірочок Markdown і БЕЗ смайлів/емодзі)
    та рекомендований пресет категорії.
    Автоматично пробує резервні моделі Gemini при перевантаженні (503).
    """
    if not GEMINI_API_KEY or GEMINI_API_KEY == "your_gemini_api_key_here":
        return _fallback_template(model)

    prompt = f"""
Ти — топовий копірайтер для магазину 3D-іграшок та виробів з пластику на OLX.ua.
Покупці на OLX не люблять читати довгі тексти.
ВАЖЛИВО: OLX НЕ підтримує форматування Markdown (зірочки **, *) і НЕ підтримує смайлики/емодзі!

Вхідні дані моделі з MakerWorld:
- Назва: {model.title}
- Опис автора: {model.description[:1000]}
- Теги: {", ".join(model.tags[:12])}

Твоє завдання — створити КОРОТКИЙ, живий і дуже привабливий текст УКРАЇНСЬКОЮ мовою (обов'язково переклади англійську назву українською!) для покупця ГОТОВОГО виробу:

1. `title` (від 16 до 68 символів):
   - Зрозуміла УКРАЇНСЬКА назва виробу + ключові слова для пошуку (наприклад: "Міні гарбузики на Хелловін 3D фігурка - Іграшка декор сувенір").
   - НЕ залишай англійську назву без перекладу!
   - НЕ пиши слів "на замовлення" чи "послуги друку". Без капслоку, без зірочок і без смайлів.

2. `description` (КОРОТКИЙ текст — строго від 280 до 550 символів, 4 короткі абзаци з відступами):
   - КАТЕГОРИЧНО ЗАБОРОНЕНО використовувати будь-які емодзі/смайлики та будь-які зірочки `*`, `**`, решітки `#`!
   - Для списків використовуй звичайне тире `- `.
   - НЕ пиши жодного слова про 3D-принтери, слайсери, сопло, підтримки чи STL-файли.
   - Структура опису (коротко і по суті):
     1) ГАЧОК (1-2 речення): емоційний, чіпляючий початок про сам виріб українською мовою — чому від нього неможливо відірватися або чому це ідеальна річ на стіл чи на подарунок.
     2) ГОЛОВНІ ПЕРЕВАГИ (2-3 короткі рядки через дефіс `- `): що він вміє (рухомі деталі, антистрес-ефект, висока деталізація, можна обрати бажаний колір).
     3) МАТЕРІАЛ ТА БЕЗПЕКА (ОБОВ'ЯЗКОВО!): чітко вкажи, що виріб виготовлений із преміального біорозкладного еко-пластику PLA (на основі кукурудзяного крохмалю), він на 100% безпечний для дітей та дорослих, нетоксичний, гіпоалергенний та абсолютно без запаху.
     4) ЗАКЛИК ДО ДІЇ (1 речення): швидка відправка по Україні (OLX Доставка, Нова Пошта, Укрпошта) — пишіть у чат!

3. `category_preset`:
   - Обирай "collectibles" (за замовчуванням для більшості 3D моделей, іграшок, фігурок, підставок), або "toys_figures", "handmade_gifts", "decor_other".

Поверни результат СТРОГО у форматі JSON з ключами: "title", "description", "category_preset".
"""

    client = genai.Client(api_key=GEMINI_API_KEY)
    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite"]

    for model_name in models_to_try:
        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.55,
                ),
            )
            data = json.loads(response.text)
            title = _clean_olx_text(str(data.get("title", "")).strip())[:70]
            description = _clean_olx_text(str(data.get("description", "")).strip())
            category_preset = str(data.get("category_preset", "collectibles")).strip()

            if len(title) < 16:
                title = f"{title} - 3D модель фігурка декор"[:70]
            if len(description) < 80:
                _, fallback_desc, _ = _fallback_template(model)
                description = fallback_desc

            return title, description, category_preset
        except Exception as e:
            logger.warning(f"Модель {model_name} тимчасово недоступна ({e}), пробуємо наступну...")

    return _fallback_template(model)
