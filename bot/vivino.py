"""Поиск вина на Vivino.

Vivino не даёт публичного API, но у сайта есть эндпоинт explore, которым живёт
его собственная страница поиска. Из него и берём данные.

Две вещи, на которых всё держится (обе выяснены на живых запросах, а не по
документации — её нет):

1. Параметр поиска называется **search_term**. С привычным `q` эндпоинт не
   ругается: он просто игнорирует его и отдаёт топ вин вообще без учёта
   названия. Именно так рождаются ответы «нашёл ваше вино, рейтинг 4.9» про
   совершенно другую бутылку.
2. Порядок выдачи Vivino сортирует слабо: на «Tocornal Sauvignon Blanc» первым
   идёт «Tocornal Cabernet Sauvignon», а нужное вино лежит десятым. Поэтому
   берём широкую страницу (per_page=24) и переупорядочиваем сами — по доле
   совпавших слов запроса, а при равенстве по числу оценок.

Ещё замеченное: фильтр min_rating выбрасывает как раз недорогие вина с полки
(у них рейтинг проставлен на уровне вина, а не урожая), поэтому фильтров по
умолчанию не ставим никаких. Кириллицу Vivino не ищет вообще — про это
отдельная подсказка в ответе.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://www.vivino.com/api/explore/explore"

# Перед сайтом стоит WAF, и он смотрит на User-Agent — причём совсем не так,
# как ожидаешь. Замер с VPS (три круга подряд, свежее соединение на запрос):
#
#   curl/8.5.0                      -> 200
#   Chrome/131 (полная строка)      -> 403, страница челленджа на 919 байт
#   собственное имя бота            -> 403
#   Python/aiohttp, пустой UA       -> 403
#
# То есть притворяться браузером из питона — верный способ получить отказ:
# TLS-отпечаток на браузер не похож, и WAF отправляет такой запрос на проверку
# JavaScript, которую питон не пройдёт никогда. А curl считается честным
# не-браузером, и его пускают.
#
# С домашнего IP картина обратная — там проходят оба, поэтому UA не один, а
# список: не пустил первый, пробуем следующий. Удачный запоминаем, чтобы не
# платить лишним запросом на каждый поиск.
_AGENTS = (
    "curl/8.5.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)
_agent_index = 0

_HEADERS = {
    "Accept": "application/json",
    # Иначе названия стран и сортов приезжают на языке рынка (голландском).
    "Accept-Language": "en-US,en;q=0.9",
}

# Сколько кандидатов просить у Vivino. Меньше — нужное вино не попадает в
# выдачу совсем; больше — ответ пухнет (одна страница из 24 записей это ~400 КБ).
PER_PAGE = 24

REQUEST_TIMEOUT = 25
# Сколько запросов держать в воздухе разом: на фото с полки прилетает десяток
# названий, но выкладывать их все одновременно ни к чему.
CONCURRENCY = 3
# Потолок на одну пачку: фото с полки даёт ~10 названий, больше — уже перебор.
MAX_QUERIES = 12

_TYPES = {
    1: "красное",
    2: "белое",
    3: "игристое",
    4: "розовое",
    7: "десертное",
    24: "креплёное",
}

_COUNTRIES = {
    "fr": "Франция", "it": "Италия", "es": "Испания", "pt": "Португалия",
    "de": "Германия", "at": "Австрия", "cl": "Чили", "ar": "Аргентина",
    "us": "США", "au": "Австралия", "nz": "Новая Зеландия", "za": "ЮАР",
    "ru": "Россия", "ge": "Грузия", "am": "Армения", "md": "Молдавия",
    "gr": "Греция", "hu": "Венгрия", "il": "Израиль", "rs": "Сербия",
    "hr": "Хорватия", "si": "Словения", "ro": "Румыния", "bg": "Болгария",
    "uy": "Уругвай", "br": "Бразилия", "ch": "Швейцария", "lb": "Ливан",
}

_YEAR = re.compile(r"\b(1[89]\d{2}|20[0-3]\d)\b")
# Слова, которые есть почти на каждой этикетке и ничего не сужают: если считать
# их совпадениями, «сухое красное вино» совпадёт с чем угодно.
_NOISE = {
    "wine", "wines", "vino", "vin", "vinho", "doc", "docg", "igt", "igp", "aoc",
    "aop", "dop", "do", "reserve", "red", "white", "dry", "sec", "brut", "de",
    "la", "le", "el", "di", "du", "des", "the", "and", "ml", "cl", "vol",
}


@dataclass(frozen=True)
class WineHit:
    name: str
    wine_id: int
    rating: float | None
    ratings_count: int
    kind: str | None
    place: str | None
    grape: str | None
    price: str | None
    vintage_note: str | None
    match: float  # доля слов запроса, нашедшихся в названии

    @property
    def url(self) -> str:
        return f"https://www.vivino.com/w/{self.wine_id}"


def _tokens(text: str) -> set[str]:
    """Слова в сравнимом виде: без диакритики, без регистра, без мусорных слов.

    «Château» и «Chateau» обязаны совпадать — иначе половина французских вин
    считается несовпавшей.
    """
    flat = unicodedata.normalize("NFKD", text or "")
    flat = "".join(ch for ch in flat if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]+", flat.lower())
    return {word for word in words if len(word) > 1 and word not in _NOISE}


def _price(match: dict[str, Any]) -> str | None:
    price = match.get("price") or {}
    amount = price.get("amount")
    if not amount:
        return None
    currency = (price.get("currency") or {}).get("code") or ""
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency, currency + " ")
    return f"{symbol}{amount:g}"


# У части вин в поле сорта лежит не сорт, а цвет: «White», «Red». Повторять
# его рядом с «белое»/«красное» незачем.
_NOT_A_GRAPE = {"white", "red", "rose", "rosé", "sparkling", "dessert", "fortified"}


def _grape(value: Any) -> str | None:
    if not isinstance(value, str) or value.strip().lower() in _NOT_A_GRAPE:
        return None
    return value.strip() or None


def _place(wine: dict[str, Any]) -> str | None:
    region = wine.get("region") or {}
    country = region.get("country") or {}
    code = (country.get("code") or "").lower()
    name = _COUNTRIES.get(code) or country.get("name") or country.get("native_name")
    region_name = region.get("name")
    if name and region_name:
        return f"{name}, {region_name}"
    return name or region_name


async def _request(
    session: aiohttp.ClientSession, term: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    global _agent_index

    params: dict[str, Any] = {
        "search_term": term,
        "order_by": "relevance",
        "order": "desc",
        "page": 1,
        "per_page": PER_PAGE,
    }
    if extra:
        params.update(extra)

    last_status = 0
    # Начинаем с того UA, которым получилось в прошлый раз, и по кругу.
    for offset in range(len(_AGENTS)):
        index = (_agent_index + offset) % len(_AGENTS)
        headers = dict(_HEADERS, **{"User-Agent": _AGENTS[index]})
        async with session.get(
            API_URL,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as response:
            if response.status in (401, 403, 429, 503):
                last_status = response.status
                await response.read()  # соединение должно освободиться до следующей попытки
                log.info("Vivino ответил %s на UA %r", response.status, _AGENTS[index])
                continue
            response.raise_for_status()
            # content_type=None: Vivino отдаёт JSON, но иногда без заголовка.
            data = await response.json(content_type=None)
        _agent_index = index
        return data.get("explore_vintage") or {}

    raise RuntimeError(f"Vivino отказал (код {last_status}) на всех вариантах запроса")


async def search(session: aiohttp.ClientSession, query: str, limit: int = 3) -> list[WineHit]:
    """Ищет вино по названию и возвращает несколько лучших кандидатов.

    Кандидатов именно несколько: выдача Vivino неточная, и решать, та ли это
    бутылка, должен тот, кто видит этикетку.
    """
    query = (query or "").strip()
    if not query:
        return []

    # Год в запросе только мешает поиску (на «Chateau Margaux 2015» нужное вино
    # пропадает из выдачи совсем), поэтому ищем без него, а потом используем
    # его, чтобы показать оценку именно этого урожая.
    year_match = _YEAR.search(query)
    year = int(year_match.group(1)) if year_match else None
    term = _YEAR.sub(" ", query).strip() if year else query
    term = re.sub(r"\s{2,}", " ", term) or query

    explore = await _request(session, term)
    wanted = _tokens(term)

    best: dict[int, dict[str, Any]] = {}
    for item in explore.get("matches") or []:
        vintage = item.get("vintage") or {}
        wine = vintage.get("wine") or {}
        stats = vintage.get("statistics") or {}
        wine_id = wine.get("id")
        if not isinstance(wine_id, int):
            continue
        winery = (wine.get("winery") or {}).get("name") or ""
        title = " — ".join(part for part in (winery, wine.get("name") or "") if part)
        entry = best.get(wine_id)
        if entry is None:
            found = _tokens(title) & wanted
            entry = {
                "title": title,
                "wine": wine,
                "stats": stats,
                "match": len(found) / len(wanted) if wanted else 1.0,
                "price": _price(item),
                "vintages": {},
            }
            best[wine_id] = entry
        elif entry["price"] is None:
            entry["price"] = _price(item)
        vintage_year = vintage.get("year")
        if isinstance(vintage_year, int):
            entry["vintages"][vintage_year] = stats

    ranked = sorted(
        best.items(),
        key=lambda pair: (pair[1]["match"], pair[1]["stats"].get("wine_ratings_count") or 0),
        reverse=True,
    )

    hits: list[WineHit] = []
    for wine_id, entry in ranked[:limit]:
        wine = entry["wine"]
        stats = entry["stats"]
        style = wine.get("style") or {}
        count = int(stats.get("wine_ratings_count") or 0)
        hits.append(
            WineHit(
                name=entry["title"],
                wine_id=wine_id,
                rating=stats.get("wine_ratings_average"),
                ratings_count=count,
                kind=_TYPES.get(wine.get("type_id")),
                place=_place(wine),
                grape=_grape(style.get("varietal_name")),
                price=entry["price"],
                vintage_note=_vintage_note(entry["vintages"], year, count),
                match=float(entry["match"]),
            )
        )
    return hits


def _vintage_note(vintages: dict[int, dict[str, Any]], year: int | None, wine_count: int) -> str | None:
    """Оценка конкретного урожая — если её вообще имеет смысл показывать.

    У недорогих вин Vivino дублирует в урожай общую оценку вина: то же среднее,
    то же число голосов. Показывать такое отдельной строкой — врать про
    точность, поэтому дубликаты отсеиваем.
    """
    if year is None:
        return None
    stats = vintages.get(year)
    if not stats:
        return f"оценки за {year} год отдельно нет"
    count = int(stats.get("ratings_count") or 0)
    if not count or count == wine_count:
        return f"оценки за {year} год отдельно нет"
    return f"урожай {year}: {stats.get('ratings_average')} ({count})"


async def search_many(
    session: aiohttp.ClientSession, queries: list[str], limit: int = 3
) -> list[tuple[str, list[WineHit] | str]]:
    """Ищет сразу пачку названий. Второй элемент — либо находки, либо текст ошибки."""
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(query: str) -> list[WineHit] | str:
        async with semaphore:
            try:
                return await search(session, query, limit=limit)
            except asyncio.TimeoutError:
                return "Vivino не ответил вовремя"
            except Exception as exc:  # noqa: BLE001 — одно название не должно ронять пачку
                log.warning("Vivino не ответил на %r: %s", query, exc)
                return f"Vivino не ответил ({exc})"

    results = await asyncio.gather(*(one(query) for query in queries))
    return list(zip(queries, results))


def _ratings(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        word = "оценок"
    elif count % 10 == 1:
        word = "оценка"
    elif count % 10 in (2, 3, 4):
        word = "оценки"
    else:
        word = "оценок"
    return f"{count} {word}"


def _has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[а-яёА-ЯЁ]", text))


def format_hits(query: str, hits: list[WineHit] | str) -> str:
    """Находки по одному названию — в текст для агента."""
    if isinstance(hits, str):
        return f"«{query}»: {hits}."
    if not hits:
        hint = (
            " Vivino не ищет по-русски — напиши название латиницей, как на этикетке."
            if _has_cyrillic(query)
            else " Попробуй короче: производитель плюс название, без слов «вино», «сухое», объёма и года."
        )
        return f"«{query}»: ничего не найдено.{hint}"

    lines = [f"«{query}»:"]
    for index, hit in enumerate(hits, 1):
        if hit.rating:
            head = f"{hit.name} — {hit.rating} ({_ratings(hit.ratings_count)})"
        else:
            head = f"{hit.name} — рейтинга пока нет"
        parts = [head]
        for extra in (hit.kind, hit.grape, hit.place):
            if extra:
                parts.append(extra)
        if hit.price:
            parts.append(f"цена {hit.price}")
        if hit.vintage_note:
            parts.append(hit.vintage_note)
        parts.append(hit.url)
        mark = "" if hit.match >= 0.999 else f" [совпало не всё: {hit.match:.0%} слов запроса]"
        lines.append(f"{index}. " + ", ".join(parts) + mark)
    return "\n".join(lines)
