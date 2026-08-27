"""Товар Wildberries в обход самого сайта.

Wildberries закрыт от браузера бота наглухо: и главная, и карточка товара
отвечают кодом 498 и пустым телом — блокировка по адресу сервера, снимок экрана
достаётся от заглушки. При этом их же служебный адрес `card.wb.ru`, которым
пользуется мобильное приложение, с того же адреса отвечает нормально. Ссылок на
Wildberries в переписке больше, чем на что угодно другое, поэтому для них
отдельная дорога: номер товара из ссылки -> карточка из API -> картинки из
их хранилища.

Номер хранилища (basket-NN) вычисляется по диапазонам номеров товара, и
диапазоны эти WB меняет. Вместо таблицы, которая устареет, просто спрашиваем
все хранилища разом и берём то, что ответило.

Картинки WB отдаёт только в webp, а ВК его фотографией не принимает — перевод
в jpg делает bot/images.py.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

CARD_API = "https://card.wb.ru/cards/v4/detail"
BASKETS = range(1, 41)
MAX_IMAGES = 10
TIMEOUT = 20

_HEADERS = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}

_HOST = re.compile(r"(^|\.)wildberries\.ru$", re.IGNORECASE)
_FROM_PATH = re.compile(r"/catalog/(\d{4,12})/")
_FROM_QUERY = re.compile(r"[?&]nm=(\d{4,12})")


@dataclass
class WbItem:
    nm: int
    name: str
    brand: str = ""
    supplier: str = ""
    rating: float | None = None
    feedbacks: int = 0
    price: float | None = None
    old_price: float | None = None
    in_stock: int = 0
    description: str = ""
    images: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.wildberries.ru/catalog/{self.nm}/detail.aspx"


def item_id(url: str) -> int | None:
    """Номер товара из ссылки Wildberries, если это она."""
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not _HOST.search(host):
        return None
    for pattern in (_FROM_PATH, _FROM_QUERY):
        found = pattern.search(url)
        if found:
            return int(found.group(1))
    return None


def _money(value: Any) -> float | None:
    """Цены приезжают в копейках целым числом."""
    try:
        return round(int(value) / 100, 2)
    except (TypeError, ValueError):
        return None


async def _find_basket(session: aiohttp.ClientSession, nm: int) -> tuple[int, dict[str, Any]] | None:
    """Хранилище, в котором лежит карточка товара, вместе с самой карточкой."""
    volume, part = nm // 100000, nm // 1000

    async def probe(number: int) -> tuple[int, dict[str, Any]] | None:
        url = (
            f"https://basket-{number:02d}.wbbasket.ru/"
            f"vol{volume}/part{part}/{nm}/info/ru/card.json"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as response:
                if response.status != 200:
                    return None
                return number, await response.json(content_type=None)
        except Exception:  # noqa: BLE001 — не ответившее хранилище просто не наше
            return None

    for found in await asyncio.gather(*(probe(number) for number in BASKETS)):
        if found is not None:
            return found
    return None


async def fetch_item(session: aiohttp.ClientSession, nm: int) -> WbItem | None:
    """Карточка товара: цена и рейтинг из API, описание и картинки из хранилища."""
    params = {"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30, "nm": nm}
    async with session.get(
        CARD_API, params=params, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=TIMEOUT)
    ) as response:
        if response.status != 200:
            log.info("card.wb.ru ответил %s на товар %s", response.status, nm)
            return None
        data = await response.json(content_type=None)

    products = (data.get("data") or {}).get("products") or data.get("products") or []
    if not products:
        return None
    product = products[0]

    prices = []
    for size in product.get("sizes") or []:
        price = size.get("price") or {}
        current, before = _money(price.get("product")), _money(price.get("basic"))
        if current:
            prices.append((current, before))
    # Сортировка только по текущей цене: у второго элемента пары бывает None,
    # и на двух размерах с одинаковым ценником сравнение None с числом уронило
    # бы разбор карточки целиком — а выглядело бы это как «WB не отдал товар».
    prices.sort(key=lambda pair: pair[0])

    item = WbItem(
        nm=nm,
        name=product.get("name") or "без названия",
        brand=product.get("brand") or "",
        supplier=product.get("supplier") or "",
        rating=product.get("reviewRating") or product.get("nmReviewRating") or product.get("rating"),
        feedbacks=int(product.get("feedbacks") or 0),
        price=prices[0][0] if prices else None,
        old_price=prices[0][1] if prices else None,
        in_stock=int(product.get("totalQuantity") or 0),
    )

    found = await _find_basket(session, nm)
    if found is not None:
        number, card = found
        item.description = (card.get("description") or "").strip()
        if not item.name or item.name == "без названия":
            item.name = card.get("imt_name") or item.name
        volume, part = nm // 100000, nm // 1000
        count = min(int(product.get("pics") or 1), MAX_IMAGES)
        item.images = [
            f"https://basket-{number:02d}.wbbasket.ru/"
            f"vol{volume}/part{part}/{nm}/images/big/{index}.webp"
            for index in range(1, count + 1)
        ]
    return item


def format_item(item: WbItem) -> str:
    """Карточка товара в текст для агента."""
    head = " ".join(part for part in (item.brand, item.name) if part)
    lines = [f"Товар Wildberries: {head}", f"Адрес: {item.url}"]

    if item.price:
        price = f"Цена: {item.price:g} ₽"
        if item.old_price and item.old_price > item.price:
            price += f" (было {item.old_price:g} ₽)"
        lines.append(price)
    if item.rating:
        lines.append(f"Оценка покупателей: {item.rating} по {item.feedbacks} отзывам")
    if item.supplier and item.supplier != item.brand:
        lines.append(f"Продавец: {item.supplier}")
    lines.append("В наличии" if item.in_stock else "Сейчас нет в наличии")
    if item.description:
        lines.append("Описание:\n" + item.description[:2000])
    if item.images:
        lines.append(
            "Фотографии товара (отправить человеку — send_photo с любым из адресов, "
            "webp переведётся в обычную картинку сам):\n"
            + "\n".join(f"- {url}" for url in item.images)
        )
    lines.append(
        "Данные взяты из служебного API Wildberries: сам сайт бота не пускает, "
        "так что снимок экрана этой страницы сделать нельзя, а эти данные — настоящие."
    )
    return "\n".join(lines)
