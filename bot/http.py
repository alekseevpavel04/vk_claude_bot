"""Единая настройка HTTP-клиента.

Корневые сертификаты берём из certifi, а не из системного хранилища: на Windows
у Python его попросту нет, и любой HTTPS-запрос падает с
CERTIFICATE_VERIFY_FAILED. На Linux разницы нет, зато код одинаковый везде.
"""

from __future__ import annotations

import ssl

import aiohttp
import certifi


def make_session(timeout: aiohttp.ClientTimeout | None = None) -> aiohttp.ClientSession:
    context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=context)
    # trust_env — чтобы HTTPS_PROXY из .env доходил и до собственных запросов
    # бота: Vivino, карточек Wildberries, скачивания картинок для отправки.
    # Без него прокси задавался только браузеру (BROWSER_PROXY), и «выйти с
    # другого адреса» работало ровно наполовину: снимок страницы шёл через
    # прокси, а рейтинг вина и карточка товара — по-прежнему с адреса сервера.
    return aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=True)
