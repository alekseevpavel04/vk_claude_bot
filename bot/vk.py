"""Асинхронный клиент VK API и цикл Bots Long Poll.

Long Poll выбран вместо Callback API намеренно: он работает исходящими
запросами, поэтому VPS не нужен белый IP, домен и HTTPS-сертификат.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp

from .config import LONGPOLL_WAIT, VK_API_VERSION

log = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"


# Сколько раз пробовать загрузить файл. Сервер загрузки ВК периодически
# отвечает 502, и одна неудачная попытка не повод отказывать человеку.
UPLOAD_ATTEMPTS = 3


class UploadRetry(RuntimeError):
    """Загрузка не задалась так, что имеет смысл повторить."""


class VkError(RuntimeError):
    """Ошибка, вернувшаяся в поле `error` ответа VK API."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method} -> VK error {code}: {message}")
        self.method = method
        self.code = code
        self.message = message


class VkClient:
    def __init__(self, session: aiohttp.ClientSession, token: str, group_id: int) -> None:
        self._session = session
        self._token = token
        self.group_id = group_id

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    async def call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        payload["access_token"] = self._token
        payload["v"] = VK_API_VERSION

        # VK отдаёт 5xx и таймауты заметно чаще, чем хотелось бы; ретраим сеть,
        # но не логические ошибки — те возвращаем наверх сразу.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with self._session.post(API_URL + method, data=payload) as response:
                    data = await response.json(content_type=None)
                break
            # ValueError — ответ не разобрался как JSON: так выглядит заглушка
            # прокси или страница ошибки вместо ответа API.
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last_exc = exc
                delay = 2**attempt
                log.warning("Сеть при вызове %s (%s), повтор через %ss", method, exc, delay)
                await asyncio.sleep(delay)
        else:
            raise RuntimeError(f"{method}: сеть недоступна") from last_exc

        if "error" in data:
            error = data["error"]
            raise VkError(method, error.get("error_code", -1), error.get("error_msg", "?"))
        return data["response"]

    async def send_message(
        self, peer_id: int, text: str, attachment: str | None = None
    ) -> None:
        await self.call(
            "messages.send",
            peer_id=peer_id,
            message=text,
            attachment=attachment,
            random_id=random.getrandbits(31),
        )

    # --- отправка файлов -------------------------------------------------
    #
    # Схема у ВК одинаковая для всего: спросить адрес сервера загрузки,
    # положить туда файл обычной multipart-формой, отдать полученную квитанцию
    # обратно в API и получить строку вложения вида photo-123_456. Её потом
    # достаточно передать в messages.send.

    async def _post_file(self, upload_url: str, field: str, path: Path) -> dict[str, Any]:
        """Кладёт файл на сервер загрузки и разбирает квитанцию.

        Сервер загрузки — не API: он отвечает то JSON, то страницей «временно
        недоступно» с кодом 502. Из десяти попыток замера сорвалась одна, и
        выглядело это в переписке как «не смог отправить фото» на ровном месте.
        Поэтому здесь всё, что не разобралось, — повод повторить, а не ошибка
        для человека.
        """
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form = aiohttp.FormData()
        with path.open("rb") as handle:
            form.add_field(field, handle, filename=path.name, content_type=content_type)
            async with self._session.post(upload_url, data=form) as response:
                status = response.status
                body = await response.text()
        if status >= 400:
            raise UploadRetry(f"сервер загрузки ответил {status}")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise UploadRetry("сервер загрузки ответил не JSON") from exc

    async def _upload(
        self, get_server: str, field: str, path: Path, receipt: str, **params: Any
    ) -> dict[str, Any]:
        """Загрузка с повторами: адрес сервера одноразовый, берём каждый раз новый."""
        last: Exception | None = None
        for attempt in range(UPLOAD_ATTEMPTS):
            if attempt:
                await asyncio.sleep(1 + attempt)
            try:
                server = await self.call(get_server, **params)
                uploaded = await self._post_file(server["upload_url"], field, path)
                value = uploaded.get(receipt)
                if not value or value == "[]":
                    # Так ВК отвечает и когда файл не подошёл, и когда у него
                    # самого не задалось. Отличить нельзя, поэтому пробуем ещё.
                    raise UploadRetry("ВК не принял файл")
                return uploaded
            except (UploadRetry, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = exc
                log.warning("Загрузка %s не удалась (%s), попытка %s", path.name, exc, attempt + 1)
        raise RuntimeError(f"не удалось загрузить файл в ВК: {last}")

    async def upload_photo(self, peer_id: int, path: Path) -> str:
        """Кладёт картинку в диалог и возвращает строку вложения."""
        uploaded = await self._upload(
            "photos.getMessagesUploadServer", "photo", path, "photo", peer_id=peer_id
        )
        saved = await self.call(
            "photos.saveMessagesPhoto",
            photo=uploaded["photo"],
            server=uploaded["server"],
            hash=uploaded["hash"],
        )
        item = saved[0]
        return f"photo{item['owner_id']}_{item['id']}"

    async def upload_doc(self, peer_id: int, path: Path, title: str | None = None) -> str:
        """Кладёт файл в диалог документом и возвращает строку вложения."""
        uploaded = await self._upload(
            "docs.getMessagesUploadServer", "file", path, "file", peer_id=peer_id, type="doc"
        )
        saved = await self.call("docs.save", file=uploaded["file"], title=title or path.name)
        doc = saved.get("doc") or saved
        return f"doc{doc['owner_id']}_{doc['id']}"

    async def set_activity(self, peer_id: int) -> None:
        """Индикатор «печатает». Держится примерно 10 секунд, потом гаснет."""
        await self.call(
            "messages.setActivity",
            peer_id=peer_id,
            type="typing",
            group_id=self.group_id,
        )

    async def get_long_poll_server(self) -> dict[str, str]:
        return await self.call("groups.getLongPollServer", group_id=self.group_id)


async def iter_events(client: VkClient) -> AsyncIterator[dict[str, Any]]:
    """Бесконечно отдаёт события сообщества из Long Poll.

    Коды `failed` из ответа сервера:
      1 — история устарела, взять новый ts из ответа;
      2 — истёк key, перезапросить сервер;
      3 — потеряна информация, перезапросить сервер и ts.
    """
    server = key = None
    ts: str | None = None
    backoff = 1

    while True:
        try:
            if server is None:
                data = await client.get_long_poll_server()
                server, key, ts = data["server"], data["key"], data["ts"]
                log.info("Long Poll сервер получен, ts=%s", ts)

            params = {"act": "a_check", "key": key, "ts": ts, "wait": LONGPOLL_WAIT}
            async with client.session.get(server, params=params) as response:
                payload = await response.json(content_type=None)

            backoff = 1

            failed = payload.get("failed")
            if failed == 1:
                ts = payload["ts"]
                continue
            if failed in (2, 3):
                log.info("Long Poll failed=%s, перезапрашиваю сервер", failed)
                server = key = None
                if failed == 3:
                    ts = None
                continue
            if failed is not None:
                log.error("Неизвестный failed=%s, перезапрашиваю сервер", failed)
                server = key = None
                continue

            ts = payload["ts"]
            for update in payload.get("updates", []):
                yield update

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл не должен умирать никогда
            # Ошибка 15 здесь — не сбой сети, а нехватка прав у ключа, и сама
            # она не пройдёт. Повторять всё равно будем (вдруг ключ поменяют на
            # ходу), но в логе должно быть написано, что чинить.
            if isinstance(exc, VkError) and exc.code == 15:
                log.error(
                    "Long Poll недоступен: у ключа сообщества нет права «Управление "
                    "сообществом». Перевыпусти ключ, отметив «Управление сообществом», "
                    "«Сообщения сообщества», «Фотографии» и «Документы»."
                )
            log.warning("Сбой Long Poll (%s), повтор через %ss", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            server = key = None
