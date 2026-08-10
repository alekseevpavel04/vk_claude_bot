"""Скачивание вложений из ВК и уборка за собой.

Claude не ходит в интернет за файлами ВК сам (ссылки временные и подписанные),
поэтому вложение сначала скачивается в workspace/media, а в промпт передаётся
локальный путь — дальше Claude читает его инструментом Read.
"""

from __future__ import annotations

import asyncio
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

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class Attachment:
    """Что бот смог извлечь из одного вложения ВК."""

    kind: str
    # Локальный путь, если файл удалось скачать.
    path: Path | None = None
    # Текстовое описание для промпта, если файла нет (ссылка, стикер, видео).
    note: str | None = None


class MediaStore:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        media_dir: Path,
        max_bytes: int,
        ttl_days: int,
    ) -> None:
        self._session = session
        self._dir = media_dir
        self._max_bytes = max_bytes
        self._ttl_seconds = ttl_days * 24 * 3600

    # --- разбор вложений -------------------------------------------------

    async def collect(
        self, peer_id: int, message_id: int, attachments: list[dict[str, Any]]
    ) -> list[Attachment]:
        result: list[Attachment] = []
        for index, attachment in enumerate(attachments):
            try:
                item = await self._one(peer_id, message_id, index, attachment)
            except Exception as exc:  # noqa: BLE001 — вложение не должно ронять ответ
                log.warning("Не удалось обработать вложение %s: %s", attachment.get("type"), exc)
                item = Attachment(kind=attachment.get("type", "?"), note="не удалось загрузить")
            if item is not None:
                result.append(item)
        return result

    async def _one(
        self, peer_id: int, message_id: int, index: int, attachment: dict[str, Any]
    ) -> Attachment | None:
        kind = attachment.get("type", "")
        body = attachment.get(kind) or {}

        if kind == "photo":
            url = _largest_photo_url(body)
            if not url:
                return Attachment(kind="photo", note="не удалось определить размер")
            path = await self._download(peer_id, message_id, index, url, default_ext="jpg")
            return Attachment(kind="photo", path=path)

        if kind == "doc":
            ext = (body.get("ext") or "").lower()
            title = body.get("title") or "документ"
            if ext not in READABLE_EXTENSIONS:
                return Attachment(kind="doc", note=f"файл {title} (.{ext}) — формат не читается")
            path = await self._download(
                peer_id, message_id, index, body["url"], default_ext=ext, stem=title
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

        if kind in {"video", "wall", "wall_reply", "market", "poll", "sticker", "graffiti", "audio"}:
            return Attachment(kind=kind, note=f"вложение типа {kind} — содержимое недоступно боту")

        return Attachment(kind=kind or "неизвестно", note="вложение неизвестного типа")

    # --- скачивание ------------------------------------------------------

    async def _download(
        self,
        peer_id: int,
        message_id: int,
        index: int,
        url: str,
        *,
        default_ext: str,
        stem: str | None = None,
    ) -> Path:
        target_dir = self._dir / str(peer_id)
        target_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if stem:
            base = _UNSAFE.sub("_", Path(stem).stem)[:40] or "file"
        else:
            base = default_ext
        path = target_dir / f"{stamp}-{message_id}-{index}-{base}.{default_ext}"

        # Любой сбой посреди загрузки не должен оставлять обрезанный файл: Claude
        # прочитал бы его как настоящий и молча ответил по половине данных.
        try:
            async with self._session.get(url) as response:
                response.raise_for_status()

                declared = response.content_length
                if declared is not None and declared > self._max_bytes:
                    raise ValueError(f"вложение больше лимита ({declared} байт)")

                written = 0
                with path.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        written += len(chunk)
                        if written > self._max_bytes:
                            raise ValueError("вложение больше лимита")
                        handle.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

        log.info("Скачано вложение %s (%s байт)", path.name, written)
        return path

    # --- уборка ----------------------------------------------------------

    def _sweep(self, older_than: float | None) -> int:
        """Удаляет файлы старше отметки (None — вообще все) и пустые папки."""
        removed = 0
        for path in list(self._dir.rglob("*")):
            if not path.is_file():
                continue
            if older_than is not None and path.stat().st_mtime >= older_than:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        for directory in sorted(self._dir.rglob("*"), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
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
            except Exception as exc:  # noqa: BLE001
                log.warning("Уборка media не удалась: %s", exc)
            await asyncio.sleep(interval_seconds)


def _largest_photo_url(photo: dict[str, Any]) -> str | None:
    sizes = photo.get("sizes") or []
    if not sizes:
        return None
    best = max(sizes, key=lambda size: (size.get("width", 0), size.get("height", 0)))
    return best.get("url")
