"""Инструменты, которые бот выдаёт агенту сверх штатных.

Это внутрипроцессный MCP-сервер: каждый инструмент — обычная функция здесь, в
коде бота. Никакого Bash, никакого «выполни команду»: агент может ровно то, что
описано ниже, и ничего сверх. Модель угроз от этого не меняется — набор
по-прежнему задан списком.

Что добавилось:
- wine_search — рейтинги Vivino сразу пачкой (на фото с полки вин десяток);
- page_read — открыть страницу настоящим браузером: текст, картинки, ссылки;
- page_screenshot — снять страницу и посмотреть на неё глазами (через Read);
- save_image — забрать картинку со страницы к себе, чтобы разглядеть;
- send_photo / send_file — прислать человеку картинку или файл в переписку.

Всё, что ходит в сеть по адресу от агента, проверяется netcheck: во внутреннюю
сеть сервера (метаданные облака, соседи по хосту) хода нет ни у одного из них.
Всё, что пишется на диск, ложится в папку вложений — ту же, что чистится по
возрасту и по общему потолку.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

import aiohttp
from claude_agent_sdk import create_sdk_mcp_server, tool

from . import browser, formatting, images, vivino, wildberries
from .netcheck import url_problem

log = logging.getLogger(__name__)

SERVER_NAME = "bot"

# Имена, под которыми инструменты видны агенту и хуку-охраннику.
TOOL_NAMES = (
    "wine_search",
    "page_read",
    "page_screenshot",
    "save_image",
    "send_photo",
    "send_file",
)
FULL_TOOL_NAMES = tuple(f"mcp__{SERVER_NAME}__{name}" for name in TOOL_NAMES)

# Сколько текста страницы отдавать агенту. Больше — раздутый контекст на каждом
# следующем ходу разговора, а подробности всегда можно снять экраном.
PAGE_TEXT_LIMIT = 10_000
PAGE_IMAGES_SHOWN = 12
PAGE_LINKS_SHOWN = 12

BROWSER_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 90

_IMAGE_TYPES = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp",
    "image/avif": "avif", "image/svg+xml": "svg",
}
# Что ВК берёт как картинку в сообщении. Остальное отправляем документом.
_VK_PHOTO_EXT = images.VK_PHOTO_EXTENSIONS

_UNSAFE = re.compile(r"[^\w.-]", re.UNICODE)


class Sender(Protocol):
    """То, что нужно инструментам от клиента ВК."""

    async def send_message(self, peer_id: int, text: str, attachment: str | None = None) -> None: ...
    async def upload_photo(self, peer_id: int, path: Path) -> str: ...
    async def upload_doc(self, peer_id: int, path: Path, title: str | None = None) -> str: ...


@dataclass
class ToolContext:
    """Всё, что инструментам нужно знать про текущий ход разговора."""

    peer_id: int
    vk: Sender
    http: aiohttp.ClientSession
    media_dir: Path  # workspace/media — папка всех вложений
    max_bytes: int
    # Уборка папки вложений: зовём после каждой записи на диск.
    enforce_cap: Any = None

    @property
    def peer_dir(self) -> Path:
        return self.media_dir / str(self.peer_id)


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _fail(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _name_from_url(url: str, fallback: str) -> str:
    tail = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    tail = _UNSAFE.sub("_", tail).strip("._")
    return (tail or fallback)[:60]


def build_server(context: ToolContext) -> Any:
    """Собирает MCP-сервер, привязанный к этому разговору.

    Сервер создаётся на каждый ход: в нём зашит peer_id, и «отправь фото» обязано
    уходить именно тому, кто спрашивал, а не тому, кто спросил последним.
    """

    # --- вино ------------------------------------------------------------

    @tool(
        "wine_search",
        "Рейтинги вин на Vivino. Принимает СРАЗУ СПИСОК названий — если на фото "
        "полка или несколько бутылок, передай все названия одним вызовом, а не по "
        "одному. Название пиши латиницей, как на этикетке: производитель плюс "
        "название плюс сорт, без слов «вино», «сухое», без объёма и года "
        "(«Cono Sur Tocornal Sauvignon Blanc»). По-русски Vivino не ищет. "
        "На каждое название возвращает несколько кандидатов — сверь имя с "
        "этикеткой сам, выдача Vivino неточная.",
        {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Названия вин латиницей, до 12 штук за раз.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Сколько кандидатов показывать на каждое название (1-5, по умолчанию 3).",
                },
            },
            "required": ["queries"],
        },
    )
    async def wine_search(args: dict[str, Any]) -> dict[str, Any]:
        raw = args.get("queries")
        if isinstance(raw, str):
            raw = [raw]
        queries = [str(item).strip() for item in (raw or []) if str(item).strip()]
        if not queries:
            return _fail("Не переданы названия вин.")
        dropped = max(0, len(queries) - vivino.MAX_QUERIES)
        queries = queries[: vivino.MAX_QUERIES]
        try:
            # Модель иногда присылает число строкой, а иногда словом.
            limit = max(1, min(5, int(args.get("limit") or 3)))
        except (TypeError, ValueError):
            limit = 3

        pairs = await vivino.search_many(context.http, queries, limit=limit)
        blocks = [vivino.format_hits(query, hits) for query, hits in pairs]
        if any(isinstance(hits, list) and any(hit.price for hit in hits) for _q, hits in pairs):
            blocks.append(
                "Цены — с того рынка Vivino, который видит сервер (Европа). "
                "К российским ценникам отношения не имеют, в ответе на них не опирайся."
            )
        if dropped:
            blocks.append(f"Не искал {dropped} названий сверх лимита — спроси их отдельным вызовом.")
        return _ok("\n\n".join(blocks))

    # --- страницы --------------------------------------------------------

    @tool(
        "page_read",
        "Открыть страницу настоящим браузером (с JavaScript) и получить её текст, "
        "список картинок и ссылок. Бери его, когда WebFetch не открыл страницу или "
        "вернул явно неполное содержимое, когда нужен список картинок на странице "
        "(чтобы потом отправить их человеку) или когда на сайте нужно найти, куда "
        "нажимать дальше. Страницы выдачи поисковиков (google.com/search, "
        "yandex.ru/search) открывать бесполезно — они закрыты от автоматики, для "
        "поиска есть WebSearch. Часть сайтов может не пустить браузер вовсе: тогда "
        "так и скажи человеку, не пересказывая заглушку.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Полный адрес страницы (http или https)."},
            },
            "required": ["url"],
        },
    )
    async def page_read(args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        problem = await url_problem(url)
        if problem:
            return _fail(problem)
        special = await _special_site(context, url)
        if special is not None:
            return special
        try:
            page = await asyncio.wait_for(
                browser.render(url, want_details=True), timeout=BROWSER_TIMEOUT
            )
        except asyncio.TimeoutError:
            return _fail("Страница не открылась за отведённое время.")
        except browser.BrowserUnavailable as exc:
            return _fail(str(exc))
        except browser.BrowserError as exc:
            return _fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("page_read %s", url[:100])
            return _fail(f"Браузер не справился: {exc}")

        lines = [_page_header(page)]
        text = page.text[:PAGE_TEXT_LIMIT]
        if text:
            lines.append("Текст страницы:\n" + text)
            if len(page.text) > len(text) or page.text_truncated:
                lines.append("(текст обрезан — если нужно дальше, сними экран или уточни адрес)")
        else:
            lines.append("Текста на странице нет — возможно, всё содержимое картинками.")
        if page.images:
            shown = page.images[:PAGE_IMAGES_SHOWN]
            lines.append(
                "Картинки на странице (адрес, размер, подпись):\n"
                + "\n".join(
                    f"- {item.url} {item.width}x{item.height}" + (f" — {item.alt}" if item.alt else "")
                    for item in shown
                )
            )
        if page.links:
            lines.append(
                "Ссылки со страницы:\n"
                + "\n".join(f"- {text_} — {href}" for text_, href in page.links[:PAGE_LINKS_SHOWN])
            )
        return _ok("\n\n".join(lines))

    @tool(
        "page_screenshot",
        "Снять страницу браузером и посмотреть на неё глазами. Возвращает пути к "
        "картинкам-снимкам: открой их инструментом Read — и увидишь страницу так, "
        "как её видит человек. Нужен, когда важно, КАК выглядит: товар, фотография, "
        "график, карта, вёрстка, — и когда текстом страница не даётся. Если нужен "
        "просто текст со страницы, бери page_read: он быстрее, дешевле и отдаёт "
        "всё сразу. Длинную страницу за раз не снять: бери full_page, а чтобы "
        "досмотреть дальше — вызови ещё раз с start_at, его подскажет ответ. "
        "Каждый вызов заново открывает страницу, так что мотать до конца длинную "
        "статью без нужды не стоит.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Полный адрес страницы (http или https)."},
                "full_page": {
                    "type": "boolean",
                    "description": "true — снять и то, что ниже первого экрана (до трёх снимков подряд). "
                                   "По умолчанию снимается только первый экран.",
                },
                "start_at": {
                    "type": "integer",
                    "description": "С какой высоты в точках начинать снимок. Нужен, чтобы досмотреть "
                                   "длинную страницу: предыдущий ответ говорит, на чём остановился.",
                },
            },
            "required": ["url"],
        },
    )
    async def page_screenshot(args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        problem = await url_problem(url)
        if problem:
            return _fail(problem)
        special = await _special_site(context, url)
        if special is not None:
            return special
        full_page = bool(args.get("full_page"))
        try:
            start_at = max(0, int(args.get("start_at") or 0))
        except (TypeError, ValueError):
            start_at = 0
        host = _UNSAFE.sub("_", urlparse(url).hostname or "page")[:30]
        try:
            page = await asyncio.wait_for(
                browser.render(
                    url,
                    out_dir=context.peer_dir,
                    prefix=f"{_stamp()}-{host}",
                    shots=browser.MAX_SHOTS if full_page else 1,
                    full_page=full_page,
                    start_at=start_at,
                    want_details=True,
                ),
                timeout=BROWSER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return _fail("Страница не открылась за отведённое время.")
        except browser.BrowserUnavailable as exc:
            return _fail(str(exc))
        except browser.BrowserError as exc:
            return _fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("page_screenshot %s", url[:100])
            return _fail(f"Браузер не справился: {exc}")

        await _cleanup(context)
        if not page.shots:
            return _fail("Снимок не получился.")
        lines = [_page_header(page)]
        lines.append(
            "Снимки экрана (прочитай их инструментом Read, чтобы посмотреть):\n"
            + "\n".join(f"- {path}" for path in page.shots)
        )
        if page.page_height > page.shot_to:
            lines.append(
                f"Снято с {page.shot_from} по {page.shot_to} точку из {page.page_height}. "
                f"Дальше не влезло: если нужно посмотреть ниже, вызови ещё раз с "
                f"start_at={page.shot_to} и full_page=true."
            )
        excerpt = page.text[:600].strip()
        if excerpt:
            lines.append("Начало текста страницы:\n" + excerpt)
        return _ok("\n\n".join(lines))

    # --- файлы -----------------------------------------------------------

    @tool(
        "save_image",
        "Скачать картинку по её адресу к себе, чтобы посмотреть на неё "
        "инструментом Read или потом отправить человеку. Адрес картинки берётся "
        "из page_read.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Прямой адрес картинки."},
            },
            "required": ["url"],
        },
    )
    async def save_image(args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        try:
            path, size = await _download(context, url, images_only=True)
        except _DownloadError as exc:
            return _fail(str(exc))
        await _cleanup(context)
        return _ok(
            f"Картинка сохранена: {path} ({size // 1024} КБ). "
            "Посмотреть — Read по этому пути, отправить человеку — send_photo с тем же путём."
        )

    @tool(
        "send_photo",
        "Отправить человеку картинку прямо в переписку ВКонтакте. Источник — либо "
        "адрес картинки в интернете, либо путь к уже сохранённому файлу (снимок "
        "экрана, save_image, присланное фото). Пользуйся, когда просят прислать "
        "фото, показать, как что-то выглядит, или когда картинка объясняет лучше "
        "слов.",
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Адрес картинки в интернете или путь к файлу в рабочей папке.",
                },
                "caption": {
                    "type": "string",
                    "description": "Подпись к фото — одной живой фразой, без markdown. Можно опустить.",
                },
            },
            "required": ["source"],
        },
    )
    async def send_photo(args: dict[str, Any]) -> dict[str, Any]:
        return await _send(context, args, as_document=False)

    @tool(
        "send_file",
        "Отправить человеку файл документом в переписку ВКонтакте: pdf, документ, "
        "таблицу, архив, картинку, которую ВК не принял фотографией. Источник — "
        "адрес файла в интернете или путь к уже сохранённому файлу.",
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Адрес файла в интернете или путь к файлу в рабочей папке.",
                },
                "caption": {
                    "type": "string",
                    "description": "Сопроводительная фраза. Можно опустить.",
                },
            },
            "required": ["source"],
        },
    )
    async def send_file(args: dict[str, Any]) -> dict[str, Any]:
        return await _send(context, args, as_document=True)

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[wine_search, page_read, page_screenshot, save_image, send_photo, send_file],
    )


async def _special_site(context: ToolContext, url: str) -> dict[str, Any] | None:
    """Сайты, для которых есть дорога лучше браузера.

    Пока такой один: Wildberries закрыт от браузера по адресу сервера, но его
    служебный API отвечает. Возвращает None, если ничего особенного — тогда
    работает обычный путь через Chromium.
    """
    nm = wildberries.item_id(url)
    if nm is None:
        return None
    try:
        item = await asyncio.wait_for(
            wildberries.fetch_item(context.http, nm), timeout=BROWSER_TIMEOUT
        )
    except asyncio.TimeoutError:
        item = None
    except Exception as exc:  # noqa: BLE001
        log.warning("Wildberries %s: %s", nm, exc)
        item = None
    if item is None:
        return _fail(
            "Wildberries не пускает браузер с этого сервера, а его API не отдал товар "
            f"{nm}. Скажи человеку, что карточку открыть не вышло."
        )
    return _ok(wildberries.format_item(item))


def _page_header(page: browser.Rendered) -> str:
    head = f"Страница: {page.title or 'без заголовка'}\nАдрес: {page.url}"
    if page.status and page.status >= 400:
        head += f"\nСервер ответил кодом {page.status} — содержимое может быть заглушкой."
    if page.blocked:
        head += (
            f"\nСАЙТ НЕ ПУСТИЛ: {page.blocked}. Ниже — не содержимое страницы, а заглушка. "
            "Пересказывать её нельзя и выдумывать содержимое тоже: скажи человеку прямо, "
            "что сайт закрылся, и предложи ссылку или другой источник."
        )
    return head


async def _cleanup(context: ToolContext) -> None:
    if context.enforce_cap is None:
        return
    try:
        await asyncio.to_thread(context.enforce_cap)
    except Exception as exc:  # noqa: BLE001 — уборка не должна ронять инструмент
        log.warning("Уборка папки вложений не удалась: %s", exc)


class _DownloadError(RuntimeError):
    pass


async def _download(context: ToolContext, url: str, *, images_only: bool) -> tuple[Path, int]:
    """Скачивает файл по адресу в папку вложений этого собеседника."""
    problem = await url_problem(url)
    if problem:
        raise _DownloadError(problem)

    target_dir = context.peer_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with context.http.get(
            url,
            timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
            headers={"User-Agent": browser.USER_AGENT, "Accept": "*/*"},
        ) as response:
            if response.status >= 400:
                raise _DownloadError(f"Сервер ответил кодом {response.status} — файл не отдали.")
            # Хост мог оказаться внутренним после перенаправления.
            final = await url_problem(str(response.url))
            if final:
                raise _DownloadError(final)

            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if images_only and content_type not in _IMAGE_TYPES:
                raise _DownloadError(
                    f"По этому адресу не картинка, а {content_type or 'непонятно что'}."
                )
            declared = response.content_length
            if declared is not None and declared > context.max_bytes:
                raise _DownloadError(
                    f"Файл больше лимита ({declared // (1024 * 1024)} МБ) — не качаю."
                )

            extension = _IMAGE_TYPES.get(content_type) or ""
            name = _name_from_url(url, "file")
            if extension and not name.lower().endswith("." + extension):
                name = f"{name.rsplit('.', 1)[0]}.{extension}" if "." in name else f"{name}.{extension}"
            path = target_dir / f"{_stamp()}-{name}"

            written = 0
            try:
                with path.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        written += len(chunk)
                        if written > context.max_bytes:
                            raise _DownloadError("Файл больше лимита — не качаю.")
                        handle.write(chunk)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
    except _DownloadError:
        raise
    except asyncio.TimeoutError as exc:
        raise _DownloadError("Файл не скачался за отведённое время.") from exc
    except aiohttp.ClientError as exc:
        raise _DownloadError(f"Не смог скачать: {exc}") from exc
    if written == 0:
        path.unlink(missing_ok=True)
        raise _DownloadError("По этому адресу пусто.")
    log.info("Скачан файл для отправки: %s (%s байт)", path.name, written)
    return path, written


def _resolve_local(context: ToolContext, raw: str) -> Path:
    """Путь к уже лежащему файлу — только внутри папки вложений.

    Иначе «отправь файл /proc/1/environ» стало бы способом вынести токены в
    переписку в обход всех проверок на чтение.
    """
    path = Path(raw)
    if not path.is_absolute():
        path = (context.media_dir.parent / raw).resolve()
    else:
        path = path.resolve()
    root = context.media_dir.resolve()
    if not (path == root or root in path.parents):
        raise _DownloadError(
            "Отправлять можно только файлы из папки вложений: присланные, скачанные или снимки."
        )
    if not path.is_file():
        raise _DownloadError(f"Файла нет: {path}")
    if path.stat().st_size > context.max_bytes:
        raise _DownloadError("Файл больше лимита на отправку.")
    return path


async def _send(context: ToolContext, args: dict[str, Any], *, as_document: bool) -> dict[str, Any]:
    source = str(args.get("source") or "").strip()
    # Подпись пишет модель, а ВК markdown не понимает — снимаем разметку так же,
    # как с обычного ответа.
    caption = formatting.to_plain_text(str(args.get("caption") or ""))[:900]
    if not source:
        return _fail("Не указано, что отправлять.")

    try:
        if source.lower().startswith(("http://", "https://")):
            path, _size = await _download(context, source, images_only=not as_document)
        else:
            path = _resolve_local(context, source)
    except _DownloadError as exc:
        return _fail(str(exc))
    await _cleanup(context)

    changed: str | None = None
    if not as_document:
        # Половина сайтов отдаёт webp, а ВК его фотографией не берёт.
        path, changed = await asyncio.to_thread(images.prepare_for_vk, path)
    extension = path.suffix.lower().lstrip(".")
    as_document = as_document or extension not in _VK_PHOTO_EXT
    try:
        if as_document:
            attachment = await context.vk.upload_doc(context.peer_id, path, path.name)
        else:
            attachment = await context.vk.upload_photo(context.peer_id, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось загрузить %s в ВК: %s", path.name, exc)
        return _fail(_upload_hint(exc))

    try:
        await context.vk.send_message(context.peer_id, caption, attachment)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось отправить вложение: %s", exc)
        return _fail(f"ВК не принял отправку: {exc}")

    what = "файл" if as_document else "фото"
    note = f" ({changed})" if changed else ""
    return _ok(
        f"Отправил {what} ({path.name}){note} в переписку — человек его уже видит. "
        "Второй раз не отправляй и в ответе про это не отчитывайся отдельно."
    )


def _upload_hint(exc: Exception) -> str:
    """Самая частая причина отказа — права ключа ВК, и её видно по коду 15."""
    text = str(exc)
    if "error 15" in text or "no access" in text.lower():
        return (
            "ВК не разрешил загрузку: у ключа сообщества нет прав на фотографии и документы. "
            "Скажи об этом человеку — ключ надо перевыпустить, отметив «Фотографии» и «Документы»."
        )
    return f"Не удалось отправить: {text}"
