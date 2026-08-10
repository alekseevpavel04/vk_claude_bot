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
    return aiohttp.ClientSession(timeout=timeout, connector=connector)
