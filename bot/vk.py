"""Асинхронный клиент VK API и цикл Bots Long Poll.

Long Poll выбран вместо Callback API намеренно: он работает исходящими
запросами, поэтому VPS не нужен белый IP, домен и HTTPS-сертификат.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .config import LONGPOLL_WAIT, VK_API_VERSION

log = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"


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
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
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

    async def send_message(self, peer_id: int, text: str) -> None:
        await self.call(
            "messages.send",
            peer_id=peer_id,
            message=text,
            random_id=random.getrandbits(31),
        )

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
            log.warning("Сбой Long Poll (%s), повтор через %ss", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            server = key = None
