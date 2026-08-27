"""Проверка адресов перед тем, как куда-то пойти.

Контейнер видит внутреннюю сеть сервера: метаданные облака (169.254.169.254),
соседние сервисы на хосте (172.17.0.1), собственный loopback. Всё, что ходит в
сеть по адресу, пришедшему от агента или со страницы, обязано пройти здесь.

Живёт отдельным модулем, потому что проверяют одно и то же трое: хук в
claude_runner, скачивание файлов в media и браузер.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


def resolves_to_private(host: str) -> bool:
    """Ведёт ли адрес во внутреннюю сеть (loopback, LAN, метаданные облака).

    Проверяется и то, во что имя резолвится: иначе достаточно любого домена,
    указывающего на 169.254.169.254 или 172.17.0.1.
    """
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    # Старые формы записи адреса: 2130706433 и 127.1 — это тот же 127.0.0.1.
    # Их разбирает inet_aton, а вот ip_address выше на них спотыкается; на том,
    # что их поймёт системный резолвер, полагаться нельзя — в glibc понимает, в
    # других реализациях нет.
    try:
        packed = socket.inet_aton(host)
    except OSError:
        pass
    else:
        return not ipaddress.IPv4Address(packed).is_global
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False  # не резолвится — пусть вызывающий сам получит свою ошибку
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return True
        except ValueError:
            continue
    return False


async def url_problem(url: str, *, allow_http: bool = True) -> str | None:
    """Почему по этому адресу ходить нельзя. None — можно.

    Текст возвращается готовым к показу агенту: он попадёт и в отказ хука, и в
    ошибку инструмента.
    """
    if not isinstance(url, str) or not url.strip():
        return "Не указан адрес."
    parsed = urlparse(url.strip())
    schemes = ("http", "https") if allow_http else ("https",)
    if parsed.scheme not in schemes:
        # file:// прочитал бы что угодно на диске в обход остальных проверок.
        return "Открывать можно только адреса " + " и ".join(schemes) + "."
    if not parsed.hostname:
        return "В адресе нет имени сервера."
    if await asyncio.to_thread(resolves_to_private, parsed.hostname):
        return "Во внутреннюю сеть сервера ходить нельзя, только в интернет."
    return None
