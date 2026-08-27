"""Скачивание вложений из ВК и уборка за собой.

Claude не ходит в интернет за файлами ВК сам (ссылки временные и подписанные),
поэтому вложение сначала скачивается в workspace/media, а в промпт передаётся
локальный путь — дальше Claude читает его инструментом Read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# Расширения, которые Claude умеет прочитать инструментом Read.
READABLE_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp",
    "pdf", "txt", "md", "csv", "json", "xml", "yaml", "yml", "log",
    "py", "js", "ts", "html", "css", "sh", "sql",
}

# Кириллицу оставляем: имя файла из ВК иначе превращается в ряд подчёркиваний,
# и в чате видно «Смотрю: 20260810-152037-9-0-____________.txt».
_UNSAFE = re.compile(r"[^\w.-]", re.UNICODE)

_OVER_BUDGET = "не скачано: в одном сообщении слишком много вложений"


@dataclass(frozen=True)
class Attachment:
    """Что бот смог извлечь из одного вложения ВК."""

    kind: str
    # Локальный путь, если файл удалось скачать.
    path: Path | None = None
    # Текстовое описание для промпта, если файла нет (ссылка, стикер, видео).
    note: str | None = None


@dataclass(frozen=True)
class MessagePart:
    """Само сообщение или вложенное в него — пересланное либо ответ."""

    label: str  # пусто для самого сообщения
    text: str
    attachments: list[Attachment]


# Пересланное может тянуть за собой цепочку; ограничиваем, чтобы одно сообщение
# не развернулось в сотню кусков и не съело контекст.
MAX_PARTS = 12
MAX_DEPTH = 3

# Сколько файлов бот скачает по одному сообщению. Лимит на размер вложения сам
# по себе диск не бережёт: в сообщении с пересланными их набирается MAX_PARTS
# пачек по десятку, и одно сообщение утаскивало бы гигабайты.
#
# Двадцать, а не десять: ВК разрешает приложить к сообщению десять фотографий,
# и ровно так приходит «проверь все эти вина». На десяти бюджет съедался ровно
# фотографиями, и стоило дописать к ним подпись вторым сообщением (а бот их
# склеивает в один вопрос), как часть снимков молча превращалась в «слишком
# много вложений».
MAX_FILES_PER_MESSAGE = 20
# Общий вес всех файлов одного сообщения — кратно лимиту на одно вложение.
MESSAGE_BUDGET_FACTOR = 4


@dataclass
class _Budget:
    """Сколько ещё можно скачать в рамках одного сообщения."""

    files_left: int
    bytes_left: int


# Вложения, внутри которых лежит собственное содержимое: свой текст и свои
# вложения. Устроены как сообщение, поэтому и разбираются как сообщение — иначе
# от репоста остаётся заглушка «содержимое недоступно», хотя весь текст поста и
# картинки к нему приехали вместе с ним.
NESTED_LABELS = {
    "wall": "Запись со стены",
    "wall_reply": "Комментарий к записи",
}


def _walk(
    message: dict[str, Any],
    label: str,
    depth: int,
    out: list[tuple[str, dict]],
    expanded: set[int],
) -> None:
    out.append((label, message))
    if depth >= MAX_DEPTH or len(out) >= MAX_PARTS:
        return

    reply = message.get("reply_message")
    if isinstance(reply, dict):
        _walk(reply, "Ответ на сообщение", depth + 1, out, expanded)

    for forwarded in message.get("fwd_messages") or []:
        if len(out) >= MAX_PARTS:
            break
        if isinstance(forwarded, dict):
            _walk(forwarded, "Пересланное сообщение", depth + 1, out, expanded)

    for attachment in message.get("attachments") or []:
        if len(out) >= MAX_PARTS:
            break
        if not isinstance(attachment, dict):
            continue
        nested = NESTED_LABELS.get(attachment.get("type", ""))
        body = attachment.get(attachment.get("type", "")) if nested else None
        if not isinstance(body, dict):
            continue
        # Помечаем разобранное: иначе то же вложение вторым проходом превратится
        # ещё и в заглушку рядом с полным текстом.
        expanded.add(id(body))
        _walk(body, nested, depth + 1, out, expanded)

    # Репост репоста: у верхней записи текст пустой, всё содержимое ниже.
    for older in message.get("copy_history") or []:
        if len(out) >= MAX_PARTS:
            break
        if isinstance(older, dict):
            _walk(older, "Запись со стены", depth + 1, out, expanded)


def raw_parts(message: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], set[int]]:
    """Сообщение вместе с пересланными, ответом и записями со стены.

    Второй элемент — id разобранных вложений-записей: по нему видно, что
    заглушку вместо них рисовать уже не нужно.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    expanded: set[int] = set()
    _walk(message, "", 0, out, expanded)
    return out, expanded


class MediaStore:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        media_dir: Path,
        max_bytes: int,
        ttl_days: int,
        max_total_bytes: int = 0,
    ) -> None:
        self._session = session
        self._dir = media_dir
        self._max_bytes = max_bytes
        self._ttl_seconds = ttl_days * 24 * 3600
        self._max_total_bytes = max_total_bytes

    # --- разбор вложений -------------------------------------------------

    def new_budget(self) -> _Budget:
        """Бюджет на скачивание. Один на ход, даже если сообщений несколько."""
        return _Budget(
            files_left=MAX_FILES_PER_MESSAGE,
            bytes_left=self._max_bytes * MESSAGE_BUDGET_FACTOR,
        )

    async def collect_parts(
        self,
        peer_id: int,
        message_id: int,
        message: dict[str, Any],
        budget: _Budget | None = None,
    ) -> list[MessagePart]:
        """Разбирает сообщение вместе с пересланными, ответом и записями."""
        await asyncio.to_thread(self.enforce_total_cap)
        if budget is None:
            budget = self.new_budget()
        blocks, expanded = raw_parts(message)
        parts: list[MessagePart] = []
        for order, (label, raw) in enumerate(blocks):
            attachments = await self.collect(
                peer_id,
                message_id,
                raw.get("attachments") or [],
                part=order,
                budget=budget,
                expanded=expanded,
            )
            parts.append(
                MessagePart(
                    label=label,
                    text=(raw.get("text") or "").strip(),
                    attachments=attachments,
                )
            )
        return parts

    async def collect(
        self,
        peer_id: int,
        message_id: int,
        attachments: list[dict[str, Any]],
        part: int = 0,
        budget: _Budget | None = None,
        expanded: set[int] | None = None,
    ) -> list[Attachment]:
        if budget is None:
            budget = self.new_budget()
        result: list[Attachment] = []
        for index, attachment in enumerate(attachments):
            try:
                item = await self._one(
                    peer_id, message_id, f"{part}-{index}", attachment, budget, expanded or set()
                )
            except Exception as exc:  # noqa: BLE001 — вложение не должно ронять ответ
                log.warning("Не удалось обработать вложение %s: %s", attachment.get("type"), exc)
                item = Attachment(kind=attachment.get("type", "?"), note="не удалось загрузить")
            if item is not None:
                result.append(item)
        return result

    async def _one(
        self,
        peer_id: int,
        message_id: int,
        index: str,
        attachment: dict[str, Any],
        budget: _Budget,
        expanded: set[int],
    ) -> Attachment | None:
        kind = attachment.get("type", "")
        body = attachment.get(kind) or {}

        if kind in NESTED_LABELS:
            if id(body) in expanded:
                return None  # текст и картинки записи уже разобраны отдельным блоком
            return Attachment(
                kind=kind,
                note=f"{NESTED_LABELS[kind].lower()} — слишком глубоко вложена, не разобрана",
            )

        if kind == "photo":
            url = _largest_photo_url(body)
            if not url:
                return Attachment(kind="photo", note="не удалось определить размер")
            if budget.files_left <= 0 or budget.bytes_left <= 0:
                return Attachment(kind="photo", note=_OVER_BUDGET)
            path = await self._download(
                peer_id, message_id, index, url, default_ext="jpg", budget=budget
            )
            return Attachment(kind="photo", path=path)

        if kind == "doc":
            ext = (body.get("ext") or "").lower()
            title = body.get("title") or "документ"
            url = body.get("url")
            if ext not in READABLE_EXTENSIONS:
                return Attachment(kind="doc", note=f"файл {title} (.{ext}) — формат не читается")
            if not url:
                return Attachment(kind="doc", note=f"файл {title} — ВК не дал ссылку")
            if budget.files_left <= 0 or budget.bytes_left <= 0:
                return Attachment(kind="doc", note=_OVER_BUDGET)
            path = await self._download(
                peer_id, message_id, index, url, default_ext=ext, stem=title, budget=budget
            )
            return Attachment(kind="doc", path=path)

        if kind == "audio_message":
            transcript = (body.get("transcript") or "").strip()
            if transcript:
                return Attachment(kind="audio_message", note=f"расшифровка: {transcript}")
            return Attachment(
                kind="audio_message",
                note="голосовое сообщение (ВК не дал расшифровку, содержимое недоступно)",
            )

        if kind == "link":
            url = body.get("url", "")
            title = body.get("title") or ""
            return Attachment(kind="link", note=f"ссылка {url} {title}".strip())

        if kind in {"video", "market", "poll", "sticker", "graffiti", "audio"}:
            return Attachment(kind=kind, note=f"вложение типа {kind} — содержимое недоступно боту")

        return Attachment(kind=kind or "неизвестно", note="вложение неизвестного типа")

    # --- скачивание ------------------------------------------------------

    async def _download(
        self,
        peer_id: int,
        message_id: int,
        index: str,
        url: str,
        *,
        default_ext: str,
        stem: str | None = None,
        budget: _Budget,
    ) -> Path:
        # Ссылку даёт сам ВК, но качаем мы её из контейнера, который видит
        # внутреннюю сеть сервера. Чужой схемой (file://) или подменённым
        # ответом API это превращается в запрос куда угодно.
        if not url.lower().startswith("https://"):
            raise ValueError(f"вложение не по https: {url[:60]}")

        limit = min(self._max_bytes, budget.bytes_left)
        target_dir = self._dir / str(peer_id)
        target_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if stem:
            base = _UNSAFE.sub("_", Path(stem).stem)[:40] or "file"
        else:
            # Без этого имя выходило вида «…-0-0-jpg.jpg» — расширение дважды.
            base = "photo"
        path = target_dir / f"{stamp}-{message_id}-{index}-{base}.{default_ext}"

        # Любой сбой посреди загрузки не должен оставлять обрезанный файл: Claude
        # прочитал бы его как настоящий и молча ответил по половине данных.
        try:
            async with self._session.get(url) as response:
                response.raise_for_status()

                declared = response.content_length
                if declared is not None and declared > limit:
                    raise ValueError(f"вложение больше лимита ({declared} байт)")

                written = 0
                with path.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        written += len(chunk)
                        if written > limit:
                            raise ValueError("вложение больше лимита")
                        handle.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

        budget.files_left -= 1
        budget.bytes_left -= written
        log.info("Скачано вложение %s (%s байт)", path.name, written)
        return path

    # --- уборка ----------------------------------------------------------

    def _files(self) -> list[tuple[float, int, Path]]:
        """Все скачанные файлы: (время правки, размер, путь)."""
        found: list[tuple[float, int, Path]] = []
        for path in self._dir.rglob("*"):
            try:
                stat = path.stat()
            except OSError:
                continue  # файл исчез между обходом и stat — не наша забота
            if path.is_file():
                found.append((stat.st_mtime, stat.st_size, path))
        return found

    def _drop_empty_dirs(self) -> None:
        for directory in sorted(self._dir.rglob("*"), reverse=True):
            with contextlib.suppress(OSError):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()

    def _sweep(self, older_than: float | None) -> int:
        """Удаляет файлы старше отметки (None — вообще все) и пустые папки."""
        removed = 0
        for mtime, _size, path in self._files():
            if older_than is not None and mtime >= older_than:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        self._drop_empty_dirs()
        return removed

    def enforce_total_cap(self) -> int:
        """Держит папку вложений в пределах потолка, вытесняя самые старые.

        TTL один диск не убережёт: за неделю до уборки в него влезает сколько
        угодно фотографий, а рядом на сервере живёт чужой сервис, которому
        кончившееся место — отказ.
        """
        if self._max_total_bytes <= 0:
            return 0
        files = self._files()
        total = sum(size for _mtime, size, _path in files)
        if total <= self._max_total_bytes:
            return 0

        removed = 0
        for _mtime, size, path in sorted(files):  # от самых старых
            if total <= self._max_total_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        self._drop_empty_dirs()
        log.warning("Папка вложений переполнена, удалено самых старых файлов: %s", removed)
        return removed

    def purge_once(self) -> int:
        """Удаляет файлы старше TTL. Возвращает число удалённых."""
        if self._ttl_seconds <= 0:
            return 0
        removed = self._sweep(time.time() - self._ttl_seconds)
        if removed:
            log.info("Удалено старых вложений: %s", removed)
        return removed

    def purge_all(self) -> int:
        """Сносит все скачанные вложения — для команды /clear."""
        removed = self._sweep(None)
        log.info("Удалено вложений полностью: %s", removed)
        return removed

    async def purge_loop(self, interval_seconds: int = 3600) -> None:
        while True:
            try:
                await asyncio.to_thread(self.purge_once)
                await asyncio.to_thread(self.enforce_total_cap)
            except Exception as exc:  # noqa: BLE001
                log.warning("Уборка media не удалась: %s", exc)
            await asyncio.sleep(interval_seconds)


def _largest_photo_url(photo: dict[str, Any]) -> str | None:
    sizes = photo.get("sizes") or []
    if not sizes:
        return None
    best = max(sizes, key=lambda size: (size.get("width", 0), size.get("height", 0)))
    return best.get("url")
