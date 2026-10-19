import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReactionTypeEmoji,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from config import (
    ADMIN_USER_ID,
    ADVERTISER_TYPE,
    BOT_TOKEN,
    CATEGORY_PRESETS,
    DEFAULT_CITY_NAME,
    DEFAULT_CONTACT_NAME,
    DEFAULT_CONTACT_PHONE,
    PORT,
    STATE_FILE,
    STATUS_LABELS,
    WEBAPP_URL,
)
from makerworld import extract_model_id, fetch_makerworld_model
from ai_helper import generate_olx_copy
from olx_client import olx_client
from webapp_html import DASHBOARD_HTML

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("olx_makerworld_bot")

router = Router()


class AdvertDraftState(StatesGroup):
    waiting_for_price = State()
    waiting_for_new_title = State()
    waiting_for_new_description = State()
    waiting_for_auth_code = State()
    waiting_for_olx_reply = State()


def load_tracked_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            data.setdefault("owner_chat_id", None)
            data.setdefault("forum_group_id", None)
            data.setdefault("adverts", {})
            data.setdefault("threads", {})
            data.setdefault("thread_topics", {})
            data.setdefault("buyer_topics", {})
            data.setdefault("topic_to_thread", {})
            return data
        except Exception:
            pass
    return {
        "owner_chat_id": None,
        "forum_group_id": None,
        "adverts": {},
        "threads": {},
        "thread_topics": {},
        "buyer_topics": {},
        "topic_to_thread": {},
    }


def save_tracked_state(state_obj: Dict[str, Any]):
    try:
        STATE_FILE.write_text(
            json.dumps(state_obj, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Помилка збереження STATE_FILE: {e}")


def register_owner_chat(chat_id: int):
    st = load_tracked_state()
    if chat_id > 0 and st.get("owner_chat_id") != chat_id:
        st["owner_chat_id"] = chat_id
        save_tracked_state(st)


async def ensure_conversation_topic(
    bot: Bot,
    thread_id: str,
    advert_id: str,
    title: str,
    price: str,
    url: str,
    interlocutor_id: Optional[str] = None,
) -> Optional[int]:
    """
    Створює одну єдину гілку (Forum Topic) під конкретного покупця (interlocutor_id).
    Якщо цей самий покупець пише кілька повідомлень або пише по іншому оголошенню —
    усі його повідомлення потрапляють у ЙОГО єдину гілку, а у кожному повідомленні
    чітко вказується, про яке оголошення мова!
    """
    st = load_tracked_state()
    forum_id = st.get("forum_group_id")
    if not forum_id:
        return None

    thread_topics = st.setdefault("thread_topics", {})
    buyer_topics = st.setdefault("buyer_topics", {})
    topic_to_thread = st.setdefault("topic_to_thread", {})

    buyer_key = str(interlocutor_id) if interlocutor_id else str(thread_id)

    # Перевіряємо, чи вже є гілка для цього покупця (або цього треду)
    existing_topic_id = buyer_topics.get(buyer_key) or thread_topics.get(str(thread_id))
    if existing_topic_id:
        thread_topics[str(thread_id)] = int(existing_topic_id)
        buyer_topics[buyer_key] = int(existing_topic_id)
        # Прив'язуємо гілку до найсвіжішого thread_id цього покупця, щоб текстова відповідь летіла туди
        topic_to_thread[str(existing_topic_id)] = str(thread_id)
        save_tracked_state(st)
        return int(existing_topic_id)

    topic_name = f"👤 Покупець #{buyer_key[-5:]} | {title[:42]} ({price} грн)"[:120]
    try:
        topic = await bot.create_forum_topic(chat_id=int(forum_id), name=topic_name)
        topic_id = topic.message_thread_id
        thread_topics[str(thread_id)] = topic_id
        buyer_topics[buyer_key] = topic_id
        topic_to_thread[str(topic_id)] = str(thread_id)
        save_tracked_state(st)

        card_text = (
            f"🛍 <b>Діалог із покупцем OLX (ID покупця: <code>{buyer_key}</code>)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Звернувся щодо оголошення:</b>\n<b>{title}</b>\n"
            f"💵 <b>Ціна:</b> <b>{price} грн</b> (ID: <code>#{advert_id}</code>)\n"
            f"🔗 <a href='{url}'>Відкрити це оголошення на OLX</a>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✍️ <i>Щоб відповісти цьому покупцю на OLX — просто напишіть будь-яке текстове повідомлення прямо в цю гілку!</i>"
        )
        card_msg = await bot.send_message(
            chat_id=int(forum_id),
            message_thread_id=topic_id,
            text=card_text,
            disable_web_page_preview=True,
        )
        try:
            await bot.pin_chat_message(
                chat_id=int(forum_id),
                message_id=card_msg.message_id,
                disable_notification=True,
            )
        except Exception:
            pass
        return topic_id
    except Exception as e:
        logger.warning(f"Не вдалося створити гілку діалогу #{thread_id} у групі {forum_id}: {e}")
        return None


def is_allowed_user(user_id: int) -> bool:
    if not ADMIN_USER_ID:
        return True
    return str(user_id) == str(ADMIN_USER_ID)


CURRENT_WEBAPP_URL = WEBAPP_URL


def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постійна зручна панель кнопок внизу екрана Telegram."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📊 Мої оголошення та статуси"),
                KeyboardButton(text="💬 Повідомлення OLX"),
            ],
            [
                KeyboardButton(text="⚠️ Потребують уваги / Оплати"),
                KeyboardButton(
                    text="📱 Відкрити Mini App",
                    web_app=WebAppInfo(url=CURRENT_WEBAPP_URL),
                ),
            ],
        ],
        resize_keyboard=True,
    )


def build_draft_keyboard(
    selected_cat_key: str,
    advertiser_type: str,
    has_price: bool,
    all_images: Optional[List[str]] = None,
    selected_indices: Optional[List[int]] = None,
) -> InlineKeyboardMarkup:
    buttons = []

    # Інтерактивний вибір конкретних фотографій (✅ №1, ✅ №2, ⬜ №3...)
    if all_images:
        if selected_indices is None:
            selected_indices = list(range(min(4, len(all_images))))
        photo_row = []
        for idx in range(len(all_images)):
            is_sel = idx in selected_indices
            mark = "✅" if is_sel else "⬜"
            photo_row.append(
                InlineKeyboardButton(
                    text=f"{mark} Фото {idx + 1}",
                    callback_data=f"pic:{idx}",
                )
            )
            if len(photo_row) == 4:
                buttons.append(photo_row)
                photo_row = []
        if photo_row:
            buttons.append(photo_row)

    adv_label = (
        "👤 Тип: Приватне (Безкоштовні ліміти ✅)"
        if advertiser_type == "private"
        else "🏢 Тип: Бізнес (Потребує пакету/оплати 💳)"
    )
    buttons.append([
        InlineKeyboardButton(text=adv_label, callback_data="toggle:adv_type")
    ])

    cat_row = []
    for key, info in CATEGORY_PRESETS.items():
        prefix = "✅ " if key == selected_cat_key else ""
        cat_row.append(
            InlineKeyboardButton(
                text=f"{prefix}{info['short']}",
                callback_data=f"cat:{key}",
            )
        )
        if len(cat_row) == 2:
            buttons.append(cat_row)
            cat_row = []
    if cat_row:
        buttons.append(cat_row)

    buttons.append([
        InlineKeyboardButton(text="✏️ Змінити заголовок", callback_data="edit:title"),
        InlineKeyboardButton(text="📝 Змінити опис", callback_data="edit:desc"),
    ])
    buttons.append([
        InlineKeyboardButton(text="💰 Змінити ціну", callback_data="edit:price"),
    ])

    if has_price:
        buttons.append([
            InlineKeyboardButton(text="🚀 ОПУБЛІКУВАТИ В OLX", callback_data="publish:now"),
        ])

    buttons.append([
        InlineKeyboardButton(text="❌ Скасувати", callback_data="draft:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_draft_preview(data: dict) -> str:
    title = data.get("title", "")
    description = data.get("description", "")
    price = data.get("price")
    cat_key = data.get("category_key", "collectibles")
    cat_name = CATEGORY_PRESETS.get(cat_key, CATEGORY_PRESETS["collectibles"])["name"]
    adv_type = data.get("advertiser_type", ADVERTISER_TYPE)
    adv_str = "👤 Приватна особа (безкоштовно в межах ліміту)" if adv_type == "private" else "🏢 Бізнес / Магазин"

    all_images = data.get("all_images") or data.get("images", [])
    sel_indices = data.get("selected_indices")
    if sel_indices is None:
        sel_indices = list(range(min(4, len(all_images))))
    nums_str = ", ".join(f"№{i + 1}" for i in sorted(sel_indices)) if sel_indices else "Жодного"

    price_str = f"<b>{price} грн</b>" if price else "⏳ <i>Не вказана (напишіть число у чат!)</i>"

    text = (
        f"📦 <b>Прев'ю оголошення OLX</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <b>Заголовок ({len(title)}/70):</b>\n{title}\n\n"
        f"💵 <b>Ціна:</b> {price_str}\n"
        f"📂 <b>Категорія:</b> {cat_name}\n"
        f"👤 <b>Тип публікації:</b> {adv_str}\n"
        f"🖼 <b>Вибрані фото ({len(sel_indices)} із {len(all_images)}):</b> <b>{nums_str}</b>\n"
        f"📍 <b>Локація та контакти:</b> м. {DEFAULT_CITY_NAME}, {DEFAULT_CONTACT_NAME} ({DEFAULT_CONTACT_PHONE})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Опис:</b>\n{description[:900]}{'...' if len(description) > 900 else ''}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )
    if not price:
        text += "👇 <b>Оберіть потрібні фото кнопками <code>✅ Фото 1..8</code> і напишіть у чат ціну в грн (наприклад: <code>350</code>)!</b>"
    else:
        text += "✅ <b>Все готово! Натисніть «🚀 ОПУБЛІКУВАТИ В OLX» або відмітьте потрібні фото нижче.</b>"
    return text


async def build_dashboard_view(filter_mode: str = "all") -> tuple[str, InlineKeyboardMarkup]:
    """Формує інтерактивну панель керування оголошеннями прямо в Telegram."""
    adverts = await olx_client.get_adverts(limit=30)

    # Синхронізуємо локальний кеш статусів, щоб монітор знав поточний стан
    st = load_tracked_state()
    tracked_map = st.get("adverts", {})
    for a in adverts:
        aid = str(a["id"])
        tracked_map[aid] = {
            "status": a.get("status"),
            "title": a.get("title", ""),
            "price": a.get("price", {}).get("value", ""),
            "url": a.get("url", ""),
        }
    st["adverts"] = tracked_map
    save_tracked_state(st)

    visible = [a for a in adverts if a.get("status") != "removed_by_user"]
    active_list = [a for a in visible if a.get("status") == "active"]
    pending_list = [a for a in visible if a.get("status") in ("new", "disabled")]
    limited_list = [a for a in visible if a.get("status") in ("limited", "removed_by_moderator", "unconfirmed")]
    outdated_list = [a for a in visible if a.get("status") == "outdated"]

    if filter_mode == "active":
        items = active_list
        header_title = f"✅ <b>Активні оголошення ({len(active_list)})</b>"
    elif filter_mode == "pending":
        items = pending_list
        header_title = f"⏳ <b>На модерації OLX ({len(pending_list)})</b>"
    elif filter_mode == "limited":
        items = limited_list
        header_title = f"⚠️ <b>Потребують оплати / Уваги ({len(limited_list)})</b>"
    else:
        items = visible[:12]
        header_title = f"📊 <b>Панель оголошень OLX (Всього: {len(visible)})</b>"

    summary = (
        f"{header_title}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Активні: <b>{len(active_list)}</b> | "
        f"⏳ Модерація: <b>{len(pending_list)}</b> | "
        f"💳 До оплати: <b>{len(limited_list)}</b> | "
        f"📦 Архів: <b>{len(outdated_list)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    if not items:
        summary += "\n<i>У цій вкладці наразі немає оголошень.</i>"
    else:
        lines = []
        for idx, a in enumerate(items[:10], 1):
            st_code = a.get("status", "")
            st_badge = STATUS_LABELS.get(st_code, st_code)
            price_val = a.get("price", {}).get("value", "?")
            title = a.get("title", "Без назви")
            url = a.get("url", "https://www.olx.ua/d/uk/myaccount/")
            aid = a.get("id")
            lines.append(
                f"{idx}. {st_badge}\n"
                f"<b><a href='{url}'>{title}</a></b> — <b>{price_val} грн</b> (<code>#{aid}</code>)"
            )
        summary += "\n\n".join(lines)

    kb_rows = [
        [
            InlineKeyboardButton(
                text=f"{'🔘 ' if filter_mode == 'all' else ''}Всі ({len(visible)})",
                callback_data="dash:all",
            ),
            InlineKeyboardButton(
                text=f"{'🔘 ' if filter_mode == 'active' else ''}✅ Активні ({len(active_list)})",
                callback_data="dash:active",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{'🔘 ' if filter_mode == 'pending' else ''}⏳ Модерація ({len(pending_list)})",
                callback_data="dash:pending",
            ),
            InlineKeyboardButton(
                text=f"{'🔘 ' if filter_mode == 'limited' else ''}💳 До оплати ({len(limited_list)})",
                callback_data="dash:limited",
            ),
        ],
    ]

    # Якщо є оголошення, що очікують оплати (limited), додаємо швидкі кнопки для них
    for a in limited_list[:3]:
        short_t = a.get("title", "")[:22]
        aid = a.get("id")
        kb_rows.append([
            InlineKeyboardButton(
                text=f"💳 Оплатити «{short_t}...»",
                url="https://www.olx.ua/d/uk/myaccount/waiting/",
            ),
            InlineKeyboardButton(
                text="🗑 Видалити",
                callback_data=f"del_adv:{aid}:{filter_mode}",
            ),
        ])

    kb_rows.append([
        InlineKeyboardButton(
            text="📱 Відкрити візуальний кабінет (Mini App)",
            web_app=WebAppInfo(url=CURRENT_WEBAPP_URL),
        ),
    ])
    kb_rows.append([
        InlineKeyboardButton(text="🔄 Оновити статуси з OLX", callback_data=f"dash:{filter_mode}"),
    ])

    return summary, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.message(Command("bind_group"))
async def cmd_bind_group(message: Message, bot: Bot):
    """Підключає супергрупу з увімкненими Гілками (Topics) як CRM-хаб для чатів із покупцями OLX."""
    if not is_allowed_user(message.from_user.id):
        return

    if message.chat.type not in ("supergroup", "group"):
        await message.answer(
            "ℹ️ Команду <code>/bind_group</code> потрібно написати <b>всередині вашої Telegram-групи</b>, де увімкнені <b>Гілки (Topics)</b>!\n\n"
            "<b>Як налаштувати за 30 секунд:</b>\n"
            "1. Створіть нову групу в Telegram.\n"
            "2. Зайдіть у Налаштування групи ➡️ увімкніть тумблер <b>«Гілки» (Topics)</b>.\n"
            "3. Додайте цього бота (<code>@bubaolx_seller_bot</code>) у групу та зробіть його <b>Адміністратором</b> (з правом «Керування гілками»).\n"
            "4. Напишіть у групі: <code>/bind_group</code>"
        )
        return

    st = load_tracked_state()
    st["forum_group_id"] = message.chat.id
    save_tracked_state(st)

    wait_msg = await message.answer(
        "✅ <b>Цю групу успішно підключено як CRM-хаб для чатів із покупцями OLX!</b>\n"
        "⏳ Перевіряю активні діалоги з покупцями на OLX..."
    )

    created_count = 0
    try:
        # Оновлюємо кеш оголошень, щоб знати назви та ціни товарів
        adverts = await olx_client.get_adverts(limit=30)
        adv_map = st.setdefault("adverts", {})
        for a in adverts:
            aid = str(a["id"])
            adv_map[aid] = {
                "status": a.get("status"),
                "title": a.get("title", f"Оголошення #{aid}"),
                "price": str(a.get("price", {}).get("value", "?")),
                "url": a.get("url", "https://www.olx.ua/d/uk/myaccount/"),
            }
        save_tracked_state(st)

        # Перевіряємо, чи є непрочитані повідомлення зараз (створюємо гілку тільки якщо є непрочитане або тільки коли напишуть нове!)
        threads = await olx_client.get_threads(limit=10)
        for t in threads[:10]:
            if int(t.get("unread_count", 0)) > 0:
                tid = str(t["id"])
                aid = str(t.get("advert_id", ""))
                buyer_id = str(t.get("interlocutor_id") or tid)
                adv_info = adv_map.get(aid, {})
                title = adv_info.get("title", f"Оголошення #{aid}")
                price_val = str(adv_info.get("price", "?"))
                url = adv_info.get("url", "https://www.olx.ua/d/uk/myaccount/answers/")

                topic_id = await ensure_conversation_topic(
                    bot=bot,
                    thread_id=tid,
                    advert_id=aid,
                    title=title,
                    price=price_val,
                    url=url,
                    interlocutor_id=buyer_id,
                )
                if topic_id:
                    created_count += 1
                    msgs = await olx_client.get_thread_messages(int(tid), limit=3)
                    if msgs:
                        last_m = msgs[-1]
                        who = "👤 <b>Покупець:</b>" if last_m.get("type") == "received" else "📤 <b>Ви:</b>"
                        txt = str(last_m.get("text", "")).replace("<", "&lt;").replace(">", "&gt;")
                        await bot.send_message(
                            chat_id=message.chat.id,
                            message_thread_id=topic_id,
                            text=f"{who} {txt}",
                            disable_web_page_preview=True,
                        )

        await wait_msg.edit_text(
            f"🎉 <b>Групу налаштовано для спілкування з покупцями!</b>\n"
            f"💬 Активних непрочитаних діалогів додано: <b>{created_count} шт.</b>\n\n"
            f"<b>Як це працює:</b>\n"
            f"• Старі завершені переписки не засмічують групу.\n"
            f"• Коли покупець пише вам на OLX — під <b>цю людину (покупця)</b> створюється <b>одна єдина гілка</b>.\n"
            f"• Навіть якщо ця сама людина напише 10 повідомлень підряд або спитає про інше ваше оголошення — все прийде в <b>її одну гілку</b> із вказанням назви товару!"
        )
    except Exception as e:
        await wait_msg.edit_text(
            f"✅ Групу прив'язано, але виникла помилка при перевірці гілок: <code>{e}</code>\n"
            f"Переконайтеся, що в налаштуваннях групи увімкнено <b>«Гілки» (Topics)</b>, а бот є <b>Адміністратором</b> із правом керування гілками!"
        )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    if not is_allowed_user(message.from_user.id):
        await message.answer("⛔ У вас немає доступу до цього бота.")
        return

    # Якщо /start викликали всередині супергрупи з гілками — автоматично прив'язуємо групу
    if message.chat.type in ("supergroup", "group"):
        await cmd_bind_group(message, bot)
        return

    register_owner_chat(message.chat.id)
    await state.clear()
    st = load_tracked_state()
    forum_status = "✅ Підключена" if st.get("forum_group_id") else "⚠️ Не підключена (напишіть /bind_group у групі з Гілками)"

    await message.answer(
        f"👋 Привіт, <b>{DEFAULT_CONTACT_NAME}</b>!\n\n"
        f"Я твій автоматичний бот та CRM-центр керування оголошеннями <b>OLX.ua</b>.\n\n"
        f"🔗 <b>Як створити оголошення:</b>\n"
        f"Просто скинь посилання на модель з <b>MakerWorld</b> — вибери потрібні фото, вкажи ціну та натисни «Опублікувати».\n\n"
        f"💬 <b>Спілкування з покупцями через Групу з Гілками (Topics):</b>\n"
        f"• Статус групи: {forum_status}\n"
        f"• Під <b>кожне нове спілкування з покупцем</b> бот створює окрему гілку, де зверху вказано, про яке саме оголошення пише людина. Ви просто пишете текст у гілці — і він летить покупцю в OLX!\n\n"
        f"👇 Використовуйте кнопки меню внизу для керування всіма оголошеннями та чатами!",
        reply_markup=get_main_reply_keyboard(),
    )


@router.message(Command("adverts"))
@router.message(F.text == "📊 Мої оголошення та статуси")
async def cmd_my_adverts(message: Message):
    if not is_allowed_user(message.from_user.id):
        return
    register_owner_chat(message.chat.id)
    wait_msg = await message.answer("🔄 Завантажую актуальні статуси ваших оголошень з OLX...")
    try:
        text, kb = await build_dashboard_view("all")
        await wait_msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        await wait_msg.edit_text(f"❌ Помилка отримання списку оголошень: <code>{e}</code>")


@router.message(F.text == "⚠️ Потребують уваги / Оплати")
async def cmd_limited_adverts(message: Message):
    if not is_allowed_user(message.from_user.id):
        return
    register_owner_chat(message.chat.id)
    wait_msg = await message.answer("🔍 Перевіряю неоплачені та зупинені оголошення...")
    try:
        text, kb = await build_dashboard_view("limited")
        await wait_msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        await wait_msg.edit_text(f"❌ Помилка: <code>{e}</code>")


@router.message(Command("messages"))
@router.message(F.text == "💬 Повідомлення OLX")
async def cmd_olx_messages(message: Message):
    if not is_allowed_user(message.from_user.id):
        return
    register_owner_chat(message.chat.id)
    wait_msg = await message.answer("💬 Завантажую останні діалоги з покупцями на OLX...")
    try:
        threads = await olx_client.get_threads(limit=8)
        if not threads:
            await wait_msg.edit_text("📭 У вас поки немає діалогів на OLX.")
            return

        st = load_tracked_state()
        adv_map = st.get("adverts", {})

        lines = ["💬 <b>Останні чати з покупцями OLX:</b>\n━━━━━━━━━━━━━━━━━━━━"]
        kb_rows = []

        for idx, t in enumerate(threads[:6], 1):
            tid = t["id"]
            aid = str(t.get("advert_id", ""))
            unread = int(t.get("unread_count", 0))
            adv_title = adv_map.get(aid, {}).get("title", f"Оголошення #{aid}")

            msgs = await olx_client.get_thread_messages(tid, limit=5)
            last_msg = msgs[-1] if msgs else {}
            last_text = str(last_msg.get("text", "")).replace("<", "&lt;").replace(">", "&gt;")[:90]
            sender_prefix = "👤 Покупець:" if last_msg.get("type") == "received" else "📤 Ви:"
            unread_badge = f" 🔴 <b>({unread} нових)</b>" if unread > 0 else ""

            lines.append(
                f"{idx}. 📌 <b>{adv_title[:42]}</b>{unread_badge}\n"
                f"   <i>{sender_prefix} {last_text}</i>"
            )
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"💬 Відкрити чат #{idx} ({adv_title[:18]}...)",
                    callback_data=f"th_view:{tid}",
                ),
                InlineKeyboardButton(
                    text="✍️ Відповісти",
                    callback_data=f"th_reply:{tid}",
                ),
            ])

        kb_rows.append([
            InlineKeyboardButton(text="🔄 Оновити чати", callback_data="th_refresh"),
        ])

        await wait_msg.edit_text(
            "\n\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Помилка завантаження чатів OLX: <code>{e}</code>")


@router.callback_query(F.data == "th_refresh")
async def cb_refresh_threads(callback: CallbackQuery):
    await callback.answer("Оновлюю чати...")
    try:
        threads = await olx_client.get_threads(limit=8)
        st = load_tracked_state()
        adv_map = st.get("adverts", {})
        lines = ["💬 <b>Останні чати з покупцями OLX:</b>\n━━━━━━━━━━━━━━━━━━━━"]
        kb_rows = []

        for idx, t in enumerate(threads[:6], 1):
            tid = t["id"]
            aid = str(t.get("advert_id", ""))
            unread = int(t.get("unread_count", 0))
            adv_title = adv_map.get(aid, {}).get("title", f"Оголошення #{aid}")
            msgs = await olx_client.get_thread_messages(tid, limit=5)
            last_msg = msgs[-1] if msgs else {}
            last_text = str(last_msg.get("text", "")).replace("<", "&lt;").replace(">", "&gt;")[:90]
            sender_prefix = "👤 Покупець:" if last_msg.get("type") == "received" else "📤 Ви:"
            unread_badge = f" 🔴 <b>({unread} нових)</b>" if unread > 0 else ""

            lines.append(
                f"{idx}. 📌 <b>{adv_title[:42]}</b>{unread_badge}\n"
                f"   <i>{sender_prefix} {last_text}</i>"
            )
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"💬 Чат #{idx} ({adv_title[:18]}...)",
                    callback_data=f"th_view:{tid}",
                ),
                InlineKeyboardButton(
                    text="✍️ Відповісти",
                    callback_data=f"th_reply:{tid}",
                ),
            ])

        kb_rows.append([
            InlineKeyboardButton(text="🔄 Оновити чати", callback_data="th_refresh"),
        ])
        await callback.message.edit_text(
            "\n\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )
    except Exception as e:
        await callback.answer(f"Помилка: {str(e)[:120]}", show_alert=True)


@router.callback_query(F.data.startswith("th_view:"))
async def cb_view_thread(callback: CallbackQuery):
    tid = int(callback.data.split(":", 1)[1])
    await callback.answer("Завантажую переписку...")
    try:
        msgs = await olx_client.get_thread_messages(tid, limit=12)
        if not msgs:
            await callback.answer("Повідомлень не знайдено", show_alert=True)
            return

        lines = [f"💬 <b>Історія діалогу (ID <code>{tid}</code>):</b>\n━━━━━━━━━━━━━━━━━━━━"]
        for m in msgs[-10:]:
            who = "👤 <b>Покупець</b>" if m.get("type") == "received" else "📤 <b>Ви</b>"
            dt = str(m.get("created_at", ""))[5:16]
            txt = str(m.get("text", "")).replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"{who} <i>({dt})</i>:\n{txt}")

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Написати відповідь покупцю", callback_data=f"th_reply:{tid}")],
                [InlineKeyboardButton(text="⬅️ Назад до списку чатів", callback_data="th_refresh")],
            ]
        )
        await callback.message.edit_text("\n\n".join(lines), reply_markup=kb)
    except Exception as e:
        await callback.answer(f"Помилка: {str(e)[:120]}", show_alert=True)


@router.callback_query(F.data.startswith("th_reply:"))
async def cb_reply_thread(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":", 1)[1])
    await state.set_state(AdvertDraftState.waiting_for_olx_reply)
    await state.update_data(reply_thread_id=tid)
    await callback.message.answer(
        f"✍️ <b>Введіть текст відповіді для покупця (Діалог <code>#{tid}</code>):</b>\n"
        f"<i>(Або натисніть /cancel, щоб скасувати)</i>"
    )
    await callback.answer()


@router.message(AdvertDraftState.waiting_for_olx_reply, F.text)
async def handle_olx_reply_text(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.startswith("/"):
        await state.clear()
        return

    data = await state.get_data()
    tid = data.get("reply_thread_id")
    if not tid:
        await state.clear()
        return

    wait_msg = await message.answer("📤 Відправляю відповідь покупцю в OLX...")
    try:
        await olx_client.send_thread_message(int(tid), text)
        await state.clear()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💬 Переглянути діалог", callback_data=f"th_view:{tid}")],
                [InlineKeyboardButton(text="✍️ Написати ще", callback_data=f"th_reply:{tid}")],
            ]
        )
        await wait_msg.edit_text(
            f"✅ <b>Відповідь успішно надіслано покупцю в OLX!</b>\n\n"
            f"📤 <i>«{text}»</i>",
            reply_markup=kb,
        )
    except Exception as e:
        err_msg = str(e).replace("<", "&lt;").replace(">", "&gt;")
        await wait_msg.edit_text(f"❌ Не вдалося відправити повідомлення: <code>{err_msg[:350]}</code>")


@router.message(F.text == "🔄 Перевірити статуси зараз")
async def cmd_force_check(message: Message, bot: Bot):
    if not is_allowed_user(message.from_user.id):
        return
    register_owner_chat(message.chat.id)
    wait_msg = await message.answer("🔄 Перевіряю зміни статусів та нові повідомлення...")
    changes = await check_status_changes_once(bot)
    new_msgs = await check_new_messages_once(bot)
    text, kb = await build_dashboard_view("all")
    prefix = f"✅ Перевірку завершено (змін статусів: {changes}, нових повідомлень: {new_msgs}).\n\n"
    await wait_msg.edit_text(prefix + text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("dash:"))
async def cb_dashboard_tab(callback: CallbackQuery):
    mode = callback.data.split(":", 1)[1]
    await callback.answer("Оновлюю дані з OLX...")
    try:
        text, kb = await build_dashboard_view(mode)
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        await callback.answer(f"Помилка: {str(e)[:120]}", show_alert=True)


@router.callback_query(F.data.startswith("del_adv:"))
async def cb_delete_advert(callback: CallbackQuery):
    parts = callback.data.split(":")
    advert_id = int(parts[1])
    mode = parts[2] if len(parts) > 2 else "all"
    try:
        await olx_client.delete_advert(advert_id)
        await callback.answer(f"🗑 Оголошення #{advert_id} видалено!", show_alert=True)
        text, kb = await build_dashboard_view(mode)
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        await callback.answer(f"❌ Не вдалося видалити: {str(e)[:140]}", show_alert=True)


@router.message(Command("auth"))
async def cmd_auth(message: Message, state: FSMContext):
    if not is_allowed_user(message.from_user.id):
        return
    register_owner_chat(message.chat.id)

    auth_url = olx_client.get_authorization_url()
    await state.set_state(AdvertDraftState.waiting_for_auth_code)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Авторизувати бота в OLX", url=auth_url)]
        ]
    )
    await message.answer(
        "🔐 <b>Підключення акаунту OLX</b>\n\n"
        "1. Натисніть кнопку нижче та підтвердіть доступ у своєму акаунті OLX.\n"
        "2. Скопіюйте посилання, на яке вас перекинуло (де є <code>?code=...</code>), і надішліть його сюди в чат.",
        reply_markup=kb,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🗑 Поточну чернетку скасовано. Надішліть нове посилання на MakerWorld!")


@router.message(AdvertDraftState.waiting_for_auth_code, F.text)
async def handle_auth_code_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if "makerworld.com" in text.lower():
        await state.clear()
        await handle_makerworld_link(message, state)
        return

    match = re.search(r"[?&]code=([^&\s]+)", text)
    code = match.group(1) if match else text

    status_msg = await message.answer("⏳ Обмінюю код авторизації на токен OLX...")
    try:
        await olx_client.exchange_code_for_token(code)
        await state.clear()
        await status_msg.edit_text(
            "✅ <b>Акаунт OLX успішно підключено!</b>\n"
            "Тепер просто скиньте посилання на будь-яку модель з MakerWorld."
        )
    except Exception as e:
        err_msg = str(e).replace("<", "&lt;").replace(">", "&gt;")
        await status_msg.edit_text(
            f"❌ Не вдалося обміняти код:\n<code>{err_msg[:500]}</code>\n\n"
            "Натисніть /auth, перейдіть за посиланням ще раз (оскільки код одноразовий) і надішліть новий код."
        )


@router.message(F.text.contains("makerworld.com"))
async def handle_makerworld_link(message: Message, state: FSMContext):
    if not is_allowed_user(message.from_user.id):
        return
    register_owner_chat(message.chat.id)

    url_match = re.search(r"https?://[^\s]+makerworld\.com[^\s]+", message.text)
    url = url_match.group(0) if url_match else message.text.strip()

    if not extract_model_id(url):
        await message.answer(
            "⚠️ Це посилання на MakerWorld не містить ID конкретної моделі.\n"
            "Приклад правильного посилання: <code>https://makerworld.com/en/models/123456</code>"
        )
        return

    status_msg = await message.answer("🔍 Завантажую фото, назву та опис із <b>MakerWorld</b>...")

    try:
        model = await fetch_makerworld_model(url)
    except Exception as e:
        logger.exception("Помилка завантаження з MakerWorld")
        err_msg = str(e).replace("<", "&lt;").replace(">", "&gt;")
        await status_msg.edit_text(f"❌ Помилка завантаження з MakerWorld:\n<code>{err_msg}</code>")
        return

    all_imgs = model.images[:8]
    default_sel = list(range(min(4, len(all_imgs))))

    await status_msg.edit_text(
        f"✅ Модель знайдено: <b>{model.title}</b> (знайдено {len(all_imgs)} фото).\n"
        f"🤖 Генерую чистий та короткий український опис із гачком для OLX..."
    )

    title_uk, desc_uk, cat_preset = await generate_olx_copy(model)
    if cat_preset not in CATEGORY_PRESETS:
        cat_preset = "collectibles"

    draft_data = {
        "model_id": model.model_id,
        "source_url": model.url,
        "title": title_uk,
        "description": desc_uk,
        "all_images": all_imgs,
        "selected_indices": default_sel,
        "images": [all_imgs[i] for i in default_sel],
        "category_key": cat_preset,
        "advertiser_type": "private",
        "price": None,
    }
    await state.set_data(draft_data)
    await state.set_state(AdvertDraftState.waiting_for_price)

    await status_msg.delete()

    if all_imgs:
        try:
            media_group = [
                InputMediaPhoto(
                    media=img_url,
                    caption=(
                        f"📸 Фото моделі №1–№{len(all_imgs)} (за замовчуванням вибрані перші {len(default_sel)}, ви можете перемикати їх кнопками нижче!)"
                        if idx == 0
                        else None
                    ),
                )
                for idx, img_url in enumerate(all_imgs)
            ]
            await message.answer_media_group(media=media_group)
        except Exception as e:
            logger.warning(f"Не вдалося відправити медіагрупу прев'ю, пробуємо по одному: {e}")
            # Якщо одне з фото в альбомі недоступне для Telegram, відправляємо тільки валідні
            valid_imgs = []
            for img_url in all_imgs:
                try:
                    await message.answer_photo(photo=img_url, caption=f"Фото №{len(valid_imgs) + 1}")
                    valid_imgs.append(img_url)
                except Exception:
                    pass
            if valid_imgs:
                all_imgs = valid_imgs
                default_sel = list(range(min(4, len(all_imgs))))
                draft_data["all_images"] = all_imgs
                draft_data["selected_indices"] = default_sel
                draft_data["images"] = [all_imgs[i] for i in default_sel]
                await state.set_data(draft_data)

    preview_text = format_draft_preview(draft_data)
    kb = build_draft_keyboard(cat_preset, "private", has_price=False, all_images=all_imgs, selected_indices=default_sel)
    await message.answer(preview_text, reply_markup=kb)


@router.message(AdvertDraftState.waiting_for_price, F.text)
async def handle_price_input(message: Message, state: FSMContext):
    # Якщо користувач пише всередині гілки покупця у групі — це відповідь покупцю, а не ціна чернетки!
    if message.chat.type in ("supergroup", "group") and message.message_thread_id:
        st = load_tracked_state()
        if str(message.message_thread_id) in st.get("topic_to_thread", {}):
            await handle_forum_topic_reply(message)
            return

    text = message.text.strip()
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        await message.answer("💡 Будь ласка, введіть ціну числом у гривнях (наприклад: <code>350</code>).")
        return

    price_uah = int(digits)
    if price_uah <= 0 or price_uah > 1_000_000:
        await message.answer("⚠️ Вкажіть реалістичну ціну в гривнях (від 1 до 1 000 000).")
        return

    data = await state.get_data()
    data["price"] = price_uah
    await state.set_data(data)

    preview_text = format_draft_preview(data)
    kb = build_draft_keyboard(
        data.get("category_key", "collectibles"),
        data.get("advertiser_type", "private"),
        has_price=True,
        all_images=data.get("all_images"),
        selected_indices=data.get("selected_indices"),
    )
    await message.answer(preview_text, reply_markup=kb)


@router.message(AdvertDraftState.waiting_for_new_title, F.text)
async def handle_new_title(message: Message, state: FSMContext):
    new_title = message.text.strip()
    if len(new_title) < 16 or len(new_title) > 70:
        await message.answer(
            f"⚠️ За правилами OLX заголовок має бути від 16 до 70 символів (зараз: {len(new_title)}). Введіть ще раз:"
        )
        return

    data = await state.get_data()
    data["title"] = new_title
    await state.set_data(data)
    await state.set_state(AdvertDraftState.waiting_for_price)

    preview_text = format_draft_preview(data)
    kb = build_draft_keyboard(
        data.get("category_key", "collectibles"),
        data.get("advertiser_type", "private"),
        has_price=bool(data.get("price")),
        all_images=data.get("all_images"),
        selected_indices=data.get("selected_indices"),
    )
    await message.answer(preview_text, reply_markup=kb)


@router.message(AdvertDraftState.waiting_for_new_description, F.text)
async def handle_new_description(message: Message, state: FSMContext):
    new_desc = message.text.strip()
    if len(new_desc) < 40:
        await message.answer("⚠️ Опис занадто короткий для OLX (мінімум 40–80 символів). Введіть детальніший опис:")
        return

    data = await state.get_data()
    data["description"] = new_desc
    await state.set_data(data)
    await state.set_state(AdvertDraftState.waiting_for_price)

    preview_text = format_draft_preview(data)
    kb = build_draft_keyboard(
        data.get("category_key", "collectibles"),
        data.get("advertiser_type", "private"),
        has_price=bool(data.get("price")),
        all_images=data.get("all_images"),
        selected_indices=data.get("selected_indices"),
    )
    await message.answer(preview_text, reply_markup=kb)


@router.callback_query(F.data.startswith("pic:"))
async def cb_toggle_photo(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    if not data.get("title"):
        await callback.answer("Чернетка застаріла. Скиньте посилання заново.", show_alert=True)
        return

    all_imgs = data.get("all_images") or data.get("images", [])
    sel_indices = list(data.get("selected_indices") or list(range(min(4, len(all_imgs)))))

    if idx in sel_indices:
        if len(sel_indices) <= 1:
            await callback.answer("⚠️ Має залишитися хоча б 1 вибране фото!", show_alert=True)
            return
        sel_indices.remove(idx)
        action_msg = f"Фото №{idx + 1} прибрано"
    else:
        sel_indices.append(idx)
        sel_indices.sort()
        action_msg = f"Фото №{idx + 1} додано"

    data["selected_indices"] = sel_indices
    data["images"] = [all_imgs[i] for i in sel_indices if i < len(all_imgs)]
    await state.set_data(data)

    preview_text = format_draft_preview(data)
    kb = build_draft_keyboard(
        data.get("category_key", "collectibles"),
        data.get("advertiser_type", "private"),
        has_price=bool(data.get("price")),
        all_images=all_imgs,
        selected_indices=sel_indices,
    )
    await callback.message.edit_text(preview_text, reply_markup=kb)
    await callback.answer(f"{action_msg} (вибрано: {len(sel_indices)} шт.)")


@router.callback_query(F.data == "toggle:adv_type")
async def cb_toggle_adv_type(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("title"):
        await callback.answer("Чернетка застаріла. Скиньте посилання заново.", show_alert=True)
        return

    current = data.get("advertiser_type", "private")
    new_type = "business" if current == "private" else "private"
    data["advertiser_type"] = new_type
    await state.set_data(data)

    preview_text = format_draft_preview(data)
    kb = build_draft_keyboard(
        data.get("category_key", "collectibles"),
        new_type,
        has_price=bool(data.get("price")),
        all_images=data.get("all_images"),
        selected_indices=data.get("selected_indices"),
    )
    await callback.message.edit_text(preview_text, reply_markup=kb)
    await callback.answer(
        "Встановлено: Приватна особа (безкоштовні ліміти)"
        if new_type == "private"
        else "Встановлено: Бізнес (потребує купленого пакета в OLX)"
    )


@router.callback_query(F.data.startswith("cat:"))
async def cb_change_category(callback: CallbackQuery, state: FSMContext):
    cat_key = callback.data.split(":", 1)[1]
    if cat_key not in CATEGORY_PRESETS:
        await callback.answer("Невідома категорія")
        return

    data = await state.get_data()
    if not data.get("title"):
        await callback.answer("Чернетка застаріла. Скиньте посилання заново.", show_alert=True)
        return

    data["category_key"] = cat_key
    await state.set_data(data)

    preview_text = format_draft_preview(data)
    kb = build_draft_keyboard(
        cat_key,
        data.get("advertiser_type", "private"),
        has_price=bool(data.get("price")),
        all_images=data.get("all_images"),
        selected_indices=data.get("selected_indices"),
    )
    await callback.message.edit_text(preview_text, reply_markup=kb)
    await callback.answer(f"Категорію змінено: {CATEGORY_PRESETS[cat_key]['short']}")


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit_field(callback: CallbackQuery, state: FSMContext):
    field_name = callback.data.split(":", 1)[1]
    if field_name == "title":
        await state.set_state(AdvertDraftState.waiting_for_new_title)
        await callback.message.answer("✏️ Надішліть новий заголовок оголошення (від 16 до 70 символів):")
    elif field_name == "desc":
        await state.set_state(AdvertDraftState.waiting_for_new_description)
        await callback.message.answer("📝 Надішліть новий текст опису оголошення:")
    elif field_name == "price":
        await state.set_state(AdvertDraftState.waiting_for_price)
        await callback.message.answer("💰 Надішліть нову ціну в гривнях (наприклад: <code>420</code>):")
    await callback.answer()


@router.callback_query(F.data == "draft:cancel")
async def cb_cancel_draft(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🗑 Чернетку оголошення скасовано. Надішліть нове посилання на MakerWorld!")
    await callback.answer()


@router.callback_query(F.data.startswith("activate_cmd:"))
async def cb_activate_advert(callback: CallbackQuery):
    advert_id = int(callback.data.split(":", 1)[1])
    try:
        await olx_client.activate_advert_command(advert_id)
        await callback.answer("✅ Команду активації з балансу/пакета OLX надіслано!", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ Відповідь OLX: {str(e)[:150]}", show_alert=True)


@router.callback_query(F.data == "publish:now")
async def cb_publish_advert(callback: CallbackQuery, state: FSMContext, bot: Bot):
    register_owner_chat(callback.message.chat.id)
    data = await state.get_data()
    if not data or not data.get("title"):
        await callback.answer("Чернетка не знайдена. Скиньте посилання заново.", show_alert=True)
        return

    price = data.get("price")
    if not price:
        await callback.answer("Спочатку вкажіть ціну в гривнях!", show_alert=True)
        return

    if not olx_client.is_authorized():
        await callback.answer("Спочатку підключіть акаунт OLX через команду /auth!", show_alert=True)
        return

    await callback.answer("Публікую оголошення на OLX...")
    await callback.message.edit_text("🚀 <b>Публікую оголошення в OLX... Зачекайте кілька секунд.</b>")

    cat_key = data.get("category_key", "collectibles")
    cat_info = CATEGORY_PRESETS.get(cat_key, CATEGORY_PRESETS["collectibles"])
    category_id = cat_info["category_id"]
    adv_type = data.get("advertiser_type", "private")

    try:
        created = await olx_client.create_advert(
            title=data["title"],
            description=data["description"],
            price_uah=int(price),
            images=data.get("images", []),
            category_id=category_id,
            advertiser_type=adv_type,
        )

        advert_id = created.get("id", "Невідомо")
        advert_url = created.get("url", "https://www.olx.ua/d/uk/myaccount/waiting/")
        status = created.get("status", "unknown")

        # Зберігаємо нове оголошення у систему фонового відслідковування статусів
        st = load_tracked_state()
        if callback.message.chat.id > 0:
            st["owner_chat_id"] = callback.message.chat.id
        st.setdefault("adverts", {})[str(advert_id)] = {
            "status": status,
            "title": data["title"],
            "price": str(price),
            "url": advert_url,
        }
        save_tracked_state(st)

        if status == "limited":
            pay_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💳 Оплатити / Активувати в OLX",
                            url="https://www.olx.ua/d/uk/myaccount/waiting/",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⚡ Списати з пакета/балансу OLX",
                            callback_data=f"activate_cmd:{advert_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔄 Змінити категорію і спробувати безкоштовно",
                            callback_data="cat:handmade_gifts" if cat_key != "handmade_gifts" else "cat:collectibles",
                        )
                    ],
                ]
            )
            await callback.message.edit_text(
                f"⚠️ <b>Оголошення створено (ID <code>{advert_id}</code>), але має статус <code>limited</code> (Очікує оплати / Ліміт)!</b>\n\n"
                f"📌 <b>Назва:</b> {data['title']}\n"
                f"📂 <b>Категорія:</b> {cat_info['name']}\n\n"
                f"📡 <b>Авто-відслідковування увімкнено:</b> як тільки ви оплатите це оголошення в OLX — я автоматично надішлю вам сповіщення!",
                reply_markup=pay_kb,
            )
            return

        await state.clear()
        status_human = STATUS_LABELS.get(status, f"{status} ✅")

        await callback.message.edit_text(
            f"🎉 <b>Оголошення успішно створено в OLX!</b>\n\n"
            f"🆔 <b>ID оголошення:</b> <code>{advert_id}</code>\n"
            f"📌 <b>Назва:</b> {data['title']}\n"
            f"📂 <b>Категорія:</b> {cat_info['short']}\n"
            f"💵 <b>Ціна:</b> {price} грн\n"
            f"📡 <b>Статус:</b> <b>{status_human}</b>\n"
            f"🔗 <a href='{advert_url}'>Відкрити оголошення на OLX</a>\n\n"
            f"💬 <i>Коли хтось напише по цьому оголошенню — бот автоматично створить окрему гілку діалогу у вашій групі з назвою та ціною цього товару!</i>"
        )
    except Exception as e:
        logger.exception("Помилка при публікації в OLX")
        err_msg = str(e).replace("<", "&lt;").replace(">", "&gt;")
        kb = build_draft_keyboard(
            cat_key,
            adv_type,
            has_price=True,
            all_images=data.get("all_images"),
            selected_indices=data.get("selected_indices"),
        )
        await callback.message.edit_text(
            f"❌ <b>Помилка при публікації в OLX:</b>\n<code>{err_msg[:450]}</code>\n\n"
            f"Спробуйте змінити фото, категорію або заголовок нижче:",
            reply_markup=kb,
        )


# ======================== ПРЯМА ВІДПОВІДЬ ПОКУПЦЮ ПРОСТО ТЕКСТОМ У ГІЛЦІ ГРУПИ ========================

@router.message(F.chat.type.in_({"supergroup", "group"}), F.message_thread_id, F.text)
async def handle_forum_topic_reply(message: Message):
    """
    Якщо власник просто пише будь-яке текстове повідомлення всередині гілки покупця у групі,
    бот автоматично відправляє цей текст відповідному покупцю на OLX без зайвих кнопок!
    """
    if not is_allowed_user(message.from_user.id):
        return
    if message.from_user.is_bot:
        return

    text = message.text.strip()
    if not text or text.startswith("/") or "makerworld.com" in text.lower():
        return

    st = load_tracked_state()
    forum_id = st.get("forum_group_id")
    if not forum_id or int(forum_id) != int(message.chat.id):
        return

    topic_id_str = str(message.message_thread_id)
    topic_to_thread = st.get("topic_to_thread", {})
    olx_thread_id = topic_to_thread.get(topic_id_str)

    # Додатковий пошук у thread_topics, якщо зворотній словник ще не оновлено
    if not olx_thread_id:
        for tid_k, top_v in st.get("thread_topics", {}).items():
            if str(top_v) == topic_id_str:
                olx_thread_id = str(tid_k)
                break

    if not olx_thread_id:
        return

    try:
        await olx_client.send_thread_message(int(olx_thread_id), text)
        try:
            await message.react([ReactionTypeEmoji(emoji="👍")])
        except Exception as re_err:
            logger.warning(f"Не вдалося поставити реакцію: {re_err}")
    except Exception as e:
        err_msg = str(e).replace("<", "&lt;").replace(">", "&gt;")
        await message.reply(f"❌ Не вдалося відправити в OLX: <code>{err_msg[:300]}</code>")


# ======================== ФОНОВИЙ МОНІТОРИНГ СТАТУСІВ ТА ЧАТІВ OLX (24/7) ========================

async def send_status_notification(
    bot: Bot,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    """
    Відправляє сповіщення про зміну статусу оголошення (активація, очікування оплати тощо)
    у загальний чат групи (General) або в особистий чат з ботом.
    """
    st = load_tracked_state()
    forum_id = st.get("forum_group_id")
    owner_chat_id = st.get("owner_chat_id")

    if forum_id:
        try:
            await bot.send_message(
                chat_id=int(forum_id),
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return
        except Exception as e:
            logger.warning(f"Помилка відправки статусу в групу {forum_id}: {e}")

    if owner_chat_id:
        await bot.send_message(
            chat_id=int(owner_chat_id),
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


async def send_buyer_message_to_topic(
    bot: Bot,
    thread_id: str,
    advert_id: str,
    title: str,
    price_val: str,
    url: str,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    interlocutor_id: Optional[str] = None,
):
    """
    Відправляє повідомлення від покупця в єдину гілку (Topic) саме цього покупця (interlocutor_id)!
    Якщо гілки для цього покупця ще немає — автоматично створює її і закріплює картку товару зверху.
    """
    st = load_tracked_state()
    forum_id = st.get("forum_group_id")
    owner_chat_id = st.get("owner_chat_id")
    buyer_key = str(interlocutor_id) if interlocutor_id else str(thread_id)

    if forum_id:
        topic_id = await ensure_conversation_topic(
            bot=bot,
            thread_id=thread_id,
            advert_id=advert_id,
            title=title,
            price=price_val,
            url=url,
            interlocutor_id=buyer_key,
        )
        if topic_id:
            try:
                await bot.send_message(
                    chat_id=int(forum_id),
                    message_thread_id=int(topic_id),
                    text=text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                return
            except Exception as e:
                logger.warning(f"Помилка відправки у гілку діалогу {topic_id}, перестворюємо: {e}")
                st = load_tracked_state()
                st.get("thread_topics", {}).pop(str(thread_id), None)
                st.get("buyer_topics", {}).pop(buyer_key, None)
                st.get("topic_to_thread", {}).pop(str(topic_id), None)
                save_tracked_state(st)
                topic_id = await ensure_conversation_topic(
                    bot=bot,
                    thread_id=thread_id,
                    advert_id=advert_id,
                    title=title,
                    price=price_val,
                    url=url,
                    interlocutor_id=buyer_key,
                )
                if topic_id:
                    await bot.send_message(
                        chat_id=int(forum_id),
                        message_thread_id=int(topic_id),
                        text=text,
                        reply_markup=reply_markup,
                        disable_web_page_preview=True,
                    )
                    return
        else:
            # Якщо гілку не вдалося створити (наприклад, бот ще не має права "Керування гілками"),
            # все одно відправляємо повідомлення у General цієї групи, щоб воно не загубилося!
            try:
                await bot.send_message(
                    chat_id=int(forum_id),
                    text=(
                        f"{text}\n\n"
                        f"⚠️ <i>Підказка: щоб бот створив окрему гілку під цього покупця, надайте боту право Адміністратора <b>«Керування гілками» (Manage Topics)</b> у налаштуваннях групи!</i>"
                    ),
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                return
            except Exception as e:
                logger.warning(f"Помилка відправки в General групи {forum_id}: {e}")

    if owner_chat_id:
        await bot.send_message(
            chat_id=int(owner_chat_id),
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


async def check_status_changes_once(bot: Bot) -> int:
    """
    Перевіряє актуальні статуси всіх оголошень користувача в OLX API.
    Якщо статус змінився (наприклад: new -> active, disabled -> limited, limited -> active),
    надсилає миттєве сповіщення!
    """
    if not olx_client.is_authorized():
        return 0

    st = load_tracked_state()
    chat_id = st.get("owner_chat_id") or st.get("forum_group_id")
    tracked: Dict[str, Dict[str, Any]] = st.get("adverts", {})

    adverts = await olx_client.get_adverts(limit=30)
    if not adverts:
        return 0

    changes_count = 0
    is_initial_sync = len(tracked) == 0

    for a in adverts:
        aid = str(a["id"])
        new_status = a.get("status", "")
        title = a.get("title", "Без назви")
        price_val = str(a.get("price", {}).get("value", "?"))
        url = a.get("url", "https://www.olx.ua/d/uk/myaccount/")

        old_entry = tracked.get(aid)
        old_status = old_entry.get("status") if old_entry else None

        tracked[aid] = {
            "status": new_status,
            "title": title,
            "price": price_val,
            "url": url,
        }

        if is_initial_sync or not chat_id:
            continue

        if old_status and old_status != new_status:
            if old_status == "disabled" and new_status == "new":
                continue

            changes_count += 1
            old_label = STATUS_LABELS.get(old_status, old_status)
            new_label = STATUS_LABELS.get(new_status, new_status)

            if new_status == "active":
                msg_text = (
                    f"🟢 <b>Оголошення активовано і вже на OLX!</b>\n\n"
                    f"📌 <b>{title}</b>\n"
                    f"💵 Ціна: <b>{price_val} грн</b> (<code>#{aid}</code>)\n"
                    f"🔄 Було: {old_label} ➡️ Стало: <b>{new_label}</b>\n\n"
                    f"🔗 <a href='{url}'>Переглянути на сайті OLX</a>"
                )
                kb = None
            elif new_status == "limited":
                msg_text = (
                    f"⚠️ <b>Увага! Оголошення НЕ опублікувалось — очікує оплати!</b>\n\n"
                    f"📌 <b>{title}</b>\n"
                    f"💵 Ціна: <b>{price_val} грн</b> (<code>#{aid}</code>)\n"
                    f"🔄 Статус змінився на: <b>{new_label}</b>\n\n"
                    f"💡 Як тільки ви оплатите його в OLX, я автоматично повідомлю про активацію!"
                )
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="💳 Перейти до оплати в OLX",
                                url="https://www.olx.ua/d/uk/myaccount/waiting/",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🗑 Видалити це оголошення",
                                callback_data=f"del_adv:{aid}:limited",
                            )
                        ],
                    ]
                )
            elif new_status == "removed_by_moderator":
                msg_text = (
                    f"⛔ <b>Оголошення відхилено модератором OLX!</b>\n\n"
                    f"📌 <b>{title}</b> (<code>#{aid}</code>)\n"
                    f"🔄 Статус: <b>{new_label}</b>"
                )
                kb = None
            else:
                msg_text = (
                    f"🔔 <b>Зміна статусу оголошення OLX:</b>\n\n"
                    f"📌 <b>{title}</b> (<code>#{aid}</code>)\n"
                    f"🔄 {old_label} ➡️ <b>{new_label}</b>"
                )
                kb = None

            try:
                await send_status_notification(
                    bot=bot,
                    text=msg_text,
                    reply_markup=kb,
                )
            except Exception as e:
                logger.warning(f"Не вдалося надіслати сповіщення про зміну статусу: {e}")

    st = load_tracked_state()
    st["adverts"] = tracked
    save_tracked_state(st)
    return changes_count


async def check_new_messages_once(bot: Bot) -> int:
    """
    Перевіряє всі діалоги (threads) користувача в OLX API.
    Якщо покупець написав нове повідомлення — знаходить або створює одну єдину гілку
    саме під цього покупця (interlocutor_id), де зверху і в повідомленні видно про яке оголошення мова,
    і надсилає туди всі нові повідомлення від нього!
    """
    if not olx_client.is_authorized():
        return 0

    st = load_tracked_state()
    has_dest = st.get("owner_chat_id") or st.get("forum_group_id")
    tracked_threads: Dict[str, Any] = st.get("threads", {})
    adv_map: Dict[str, Any] = st.get("adverts", {})

    threads = await olx_client.get_threads(limit=15)
    if not threads:
        return 0

    is_initial_sync = len(tracked_threads) == 0
    new_notifications = 0

    for t in threads[:10]:
        tid = str(t["id"])
        total_count = int(t.get("total_count", 0))
        unread_count = int(t.get("unread_count", 0))
        aid = str(t.get("advert_id", ""))
        buyer_id = str(t.get("interlocutor_id") or tid)

        prev = tracked_threads.get(tid)
        prev_total = int(prev.get("total_count", 0)) if isinstance(prev, dict) else 0
        last_notified_msg_id = prev.get("last_msg_id") if isinstance(prev, dict) else None

        if is_initial_sync:
            tracked_threads[tid] = {
                "total_count": total_count,
                "unread_count": unread_count,
                "last_msg_id": last_notified_msg_id,
                "advert_id": aid,
                "interlocutor_id": buyer_id,
            }
            continue

        if total_count > prev_total:
            msgs = await olx_client.get_thread_messages(int(tid), limit=10)
            incoming = [m for m in msgs if m.get("type") == "received"]
            if incoming:
                latest_in = incoming[-1]
                msg_id = latest_in.get("id")
                if msg_id != last_notified_msg_id and has_dest:
                    last_notified_msg_id = msg_id
                    new_notifications += 1
                    adv_info = adv_map.get(aid, {})
                    adv_title = adv_info.get("title", f"Оголошення #{aid}")
                    adv_price = str(adv_info.get("price", "?"))
                    adv_url = adv_info.get("url", "https://www.olx.ua/d/uk/myaccount/answers/")

                    msg_text_clean = str(latest_in.get("text", "")).replace("<", "&lt;").replace(">", "&gt;")
                    phone_info = f" (📞 <code>{latest_in['phone']}</code>)" if latest_in.get("phone") else ""

                    notify_text = f"👤 <b>Покупець{phone_info}:</b> {msg_text_clean}"
                    try:
                        await send_buyer_message_to_topic(
                            bot=bot,
                            thread_id=tid,
                            advert_id=aid,
                            title=adv_title,
                            price_val=adv_price,
                            url=adv_url,
                            text=notify_text,
                            reply_markup=None,
                            interlocutor_id=buyer_id,
                        )
                    except Exception as e:
                        logger.warning(f"Не вдалося надіслати сповіщення про нове повідомлення OLX: {e}")

        tracked_threads[tid] = {
            "total_count": total_count,
            "unread_count": unread_count,
            "last_msg_id": last_notified_msg_id,
            "advert_id": aid,
            "interlocutor_id": buyer_id,
        }

    st = load_tracked_state()
    st["threads"] = tracked_threads
    save_tracked_state(st)
    return new_notifications


async def background_status_monitor(bot: Bot):
    """Фоновий процес, який кожні 30 секунд перевіряє нові повідомлення від покупців та статуси оголошень на OLX."""
    logger.info("📡 Запущено фоновий моніторинг чатів та статусів OLX (інтервал: 30 сек)")
    tick = 0
    while True:
        try:
            await check_new_messages_once(bot)
            if tick % 2 == 0:
                await check_status_changes_once(bot)
            tick += 1
        except Exception as e:
            logger.warning(f"Помилка у фоновому моніторингу: {e}")
        await asyncio.sleep(30)


# ======================== ВЕБ-СЕРВЕР (OAuth2 + Telegram Mini App) ========================

async def oauth_callback_handler(request: web.Request) -> web.Response:
    code = request.query.get("code")
    error = request.query.get("error")

    if error:
        return web.Response(
            text=f"<h2>Помилка авторизації OLX: {error}</h2>",
            content_type="text/html",
            charset="utf-8",
        )

    if code:
        try:
            await olx_client.exchange_code_for_token(code)
            return web.Response(
                text=(
                    "<html><body style='font-family:sans-serif;text-align:center;padding:40px;'>"
                    "<h1>✅ Бот успішно підключений до вашого OLX Магазину!</h1>"
                    "<p>Тепер ви можете повернутися в Telegram і скидати посилання з MakerWorld.</p>"
                    "</body></html>"
                ),
                content_type="text/html",
                charset="utf-8",
            )
        except Exception as e:
            return web.Response(
                text=(
                    f"<html><body style='font-family:sans-serif;text-align:center;padding:40px;'>"
                    f"<h2>⚠️ Отримано код, але виникла помилка обміну:</h2><pre>{e}</pre>"
                    f"<p>Скопіюйте цей код і відправте боту в Telegram вручну: <b>{code}</b></p>"
                    f"</body></html>"
                ),
                content_type="text/html",
                charset="utf-8",
            )

    return web.Response(
        text=DASHBOARD_HTML,
        content_type="text/html",
        charset="utf-8",
    )


async def webapp_page_handler(request: web.Request) -> web.Response:
    return web.Response(
        text=DASHBOARD_HTML,
        content_type="text/html",
        charset="utf-8",
    )


async def api_adverts_handler(request: web.Request) -> web.Response:
    try:
        adverts = await olx_client.get_adverts(limit=30)
        return web.json_response({"ok": True, "adverts": adverts})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_delete_advert_handler(request: web.Request) -> web.Response:
    advert_id = int(request.match_info["id"])
    try:
        await olx_client.delete_advert(advert_id)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=400)


async def api_activate_advert_handler(request: web.Request) -> web.Response:
    advert_id = int(request.match_info["id"])
    try:
        await olx_client.activate_advert_command(advert_id)
        return web.json_response({"ok": True, "message": "✅ Запит на активацію надіслано!"})
    except Exception as e:
        return web.json_response({"ok": False, "message": f"❌ {e}"}, status=400)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", oauth_callback_handler)
    app.router.add_get("/callback", oauth_callback_handler)
    app.router.add_get("/app", webapp_page_handler)
    app.router.add_get("/api/adverts", api_adverts_handler)
    app.router.add_post("/api/adverts/{id}/delete", api_delete_advert_handler)
    app.router.add_post("/api/adverts/{id}/activate", api_activate_advert_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Веб-сервер та Mini App запущено: http://localhost:{PORT}/app")


async def start_https_tunnel(bot: Bot):
    """
    Автоматично піднімає та підтримує безкоштовний HTTPS-тунель (через SSH localhost.run)
    для роботи Mini App з локального ПК. Якщо тунель оновлює адресу — автоматично
    перезапускає його і оновлює кнопку Menu Button у Telegram!
    """
    global CURRENT_WEBAPP_URL
    from aiogram.types import MenuButtonWebApp

    async def _apply_new_webapp_url(new_url: str):
        global CURRENT_WEBAPP_URL
        if CURRENT_WEBAPP_URL == new_url:
            return
        CURRENT_WEBAPP_URL = new_url
        logger.info(f"🚀 Актуальний HTTPS URL для Telegram Mini App: {CURRENT_WEBAPP_URL}")
        try:
            # Оновлюємо головну синю кнопку Menu зліва від поля вводу в Telegram!
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="📊 Кабінет OLX",
                    web_app=WebAppInfo(url=CURRENT_WEBAPP_URL),
                )
            )
        except Exception as e:
            logger.warning(f"Не вдалося оновити MenuButtonWebApp: {e}")

    first_url_ready = asyncio.Event()

    async def _tunnel_Supervisor():
        while True:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ServerAliveInterval=30",
                    "-o",
                    "ServerAliveCountMax=3",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-R",
                    f"80:localhost:{PORT}",
                    "nokey@localhost.run",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                async def _read_stream(stream):
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", errors="ignore")
                        m = re.search(r"https://[a-z0-9-]+\.lhr\.life", text)
                        if m:
                            await _apply_new_webapp_url(m.group(0) + "/app")
                            first_url_ready.set()

                t1 = asyncio.create_task(_read_stream(proc.stdout))
                t2 = asyncio.create_task(_read_stream(proc.stderr))
                await proc.wait()
                t1.cancel()
                t2.cancel()
            except Exception as e:
                logger.warning(f"Помилка SSH-тунелю, перепідключення через 3 сек: {e}")
            await asyncio.sleep(3)

    asyncio.create_task(_tunnel_Supervisor())
    try:
        await asyncio.wait_for(first_url_ready.wait(), timeout=8.0)
    except asyncio.TimeoutError:
        pass


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "your_telegram_bot_token_here":
        logger.error("❌ Не вказано BOT_TOKEN у файлі .env! Заповніть файл .env перед запуском.")
        return

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    await start_web_server()
    await start_https_tunnel(bot)
    asyncio.create_task(background_status_monitor(bot))
    logger.info("🤖 Telegram-бот успішно запущений!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
