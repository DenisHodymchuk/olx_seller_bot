import json
import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class MakerWorldModel:
    model_id: str
    url: str
    title: str
    description: str
    images: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    author: str = ""


def extract_model_id(url: str) -> Optional[str]:
    """Витягує числовий ID моделі з посилання MakerWorld."""
    match = re.search(r"makerworld\.com/(?:[a-z]{2}/)?models/(\d+)", url, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _clean_html_text(html_content: str) -> str:
    """Очищує HTML-теги з опису моделі MakerWorld, зберігаючи абзаци."""
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for br in soup.find_all(["br", "p", "div", "li"]):
        br.append("\n")
    text = soup.get_text()
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned.strip()


def _is_valid_static_photo(url_val: str) -> bool:
    """
    Перевіряє, чи є посилання статичним фото (JPG/PNG/WebP), а не важкою GIF-анімацією чи аватаркою.
    OLX та Telegram InputMediaPhoto не приймають .gif або файли > 5 МБ.
    """
    if not url_val or not isinstance(url_val, str):
        return False
    if not url_val.startswith("http"):
        return False
    lower = url_val.lower().split("?")[0]
    if lower.endswith(".gif"):
        return False
    if any(skip in lower for skip in ("avatar", "icon", "logo", "badge", "plate")):
        return False
    return True


def _extract_images_from_dict(data: dict) -> List[str]:
    """Шукає перші 4 високоякісні статичні зображення (JPG/PNG/WebP) моделі у структурі даних MakerWorld."""
    images: List[str] = []

    def add_url(url_val: str):
        if _is_valid_static_photo(url_val) and url_val not in images:
            images.append(url_val)

    # 1. Спочатку додаємо картинку з designExtension.design_pictures (це головна галерея моделі)
    design_ext = data.get("designExtension") or {}
    if isinstance(design_ext, dict):
        pictures = design_ext.get("design_pictures") or design_ext.get("pictures") or []
        for pic in pictures:
            if isinstance(pic, dict):
                add_url(pic.get("url") or pic.get("picUrl") or "")
            elif isinstance(pic, str):
                add_url(pic)

    # 2. Обкладинка моделі (якщо вона не .gif)
    cover = data.get("cover") or data.get("coverUrl") or data.get("picUrl")
    if isinstance(cover, str):
        add_url(cover)

    # 3. Фото з профілів друку (instances)
    for key in ("pictures", "images", "instances"):
        items = data.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    add_url(item.get("url") or item.get("cover") or item.get("picUrl") or "")
                    ext_pics = item.get("pictures") or []
                    for ep in ext_pics:
                        if isinstance(ep, dict):
                            add_url(ep.get("url") or "")
                elif isinstance(item, str):
                    add_url(item)

    return images[:8]  # Беремо до 8 статичних фото моделі для вибору в боті


async def fetch_makerworld_model(url: str) -> MakerWorldModel:
    """
    Завантажує дані моделі з MakerWorld (назву, опис, перші 4 статичні фото, теги)
    за допомогою curl_cffi з імітацією браузера Chrome.
    """
    model_id = extract_model_id(url)
    if not model_id:
        raise ValueError("Не вдалося розпізнати ID моделі у посиланні MakerWorld.")

    clean_url = f"https://makerworld.com/en/models/{model_id}"

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,uk;q=0.8",
        "Referer": "https://makerworld.com/",
    }

    async with AsyncSession(impersonate="chrome120", timeout=25) as session:
        resp = await session.get(clean_url, headers=headers)
        html = resp.text if resp.status_code == 200 else ""
        soup = BeautifulSoup(html, "html.parser") if html else None

        # Спроба №1: __NEXT_DATA__ у сторінці
        if soup:
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                try:
                    next_data = json.loads(next_script.string)
                    page_props = next_data.get("props", {}).get("pageProps", {})
                    design = (
                        page_props.get("design")
                        or page_props.get("model")
                        or page_props.get("data")
                        or {}
                    )
                    if isinstance(design, dict) and (design.get("title") or design.get("name")):
                        title = design.get("title") or design.get("name") or f"3D Model #{model_id}"
                        raw_desc = (
                            design.get("summary")
                            or design.get("description")
                            or design.get("content")
                            or ""
                        )
                        description = _clean_html_text(raw_desc)
                        images = _extract_images_from_dict(design)

                        # Якщо у design_pictures були тільки GIF і набралося менше 8 фото — добираємо JPG/PNG зі сторінки
                        if len(images) < 8 and html:
                            cdn_urls = re.findall(
                                r"https://[^\s\"'<>]+?(?:makerworld\.bblmw\.com|public-cdn\.bambulab\.com)[^\s\"'<>]+?\.(?:jpg|jpeg|png|webp)",
                                html,
                                re.IGNORECASE,
                            )
                            for img_url in cdn_urls:
                                if _is_valid_static_photo(img_url) and img_url not in images:
                                    images.append(img_url)
                                if len(images) >= 8:
                                    break

                        tags = design.get("tags") or []
                        if isinstance(tags, list):
                            tags = [t.get("name", str(t)) if isinstance(t, dict) else str(t) for t in tags]

                        return MakerWorldModel(
                            model_id=model_id,
                            url=clean_url,
                            title=title,
                            description=description,
                            images=images[:8],
                            tags=tags,
                        )
                except Exception as e:
                    logger.warning(f"Помилка розбору __NEXT_DATA__: {e}")

        # Спроба №2: Резервний парсинг через мета-теги OpenGraph + статичні фото JPG/PNG/WebP на сторінці
        if soup:
            og_title = soup.find("meta", property="og:title")
            og_desc = soup.find("meta", property="og:description")
            og_image = soup.find("meta", property="og:image")

            title = (
                og_title["content"].replace(" - MakerWorld", "").replace(" | MakerWorld", "").strip()
                if og_title and og_title.get("content")
                else (soup.title.string.strip() if soup.title and soup.title.string else f"3D Model #{model_id}")
            )
            title = re.sub(r"\s*-\s*Free 3D Print Model.*$", "", title, flags=re.IGNORECASE).strip()

            description = (
                og_desc["content"].strip()
                if og_desc and og_desc.get("content")
                else ""
            )
            images = []
            if og_image and og_image.get("content") and _is_valid_static_photo(og_image["content"]):
                images.append(og_image["content"])

            cdn_urls = re.findall(
                r"https://[^\s\"'<>]+?(?:makerworld\.bblmw\.com|public-cdn\.bambulab\.com)[^\s\"'<>]+?\.(?:jpg|jpeg|png|webp)",
                html,
                re.IGNORECASE,
            )
            for img_url in cdn_urls:
                if _is_valid_static_photo(img_url) and img_url not in images:
                    images.append(img_url)
                if len(images) >= 8:
                    break

            if title or images:
                return MakerWorldModel(
                    model_id=model_id,
                    url=clean_url,
                    title=title,
                    description=description,
                    images=images[:8],
                )

    raise RuntimeError(
        "Не вдалося автоматично отримати дані з MakerWorld. Спробуйте ще раз."
    )
