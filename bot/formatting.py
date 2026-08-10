"""Приведение ответов Claude к тому, что ВК умеет показывать.

ВК рендерит только плоский текст: markdown не поддерживается вообще, а длина
одного сообщения ограничена 4096 символами.
"""

from __future__ import annotations

import re

from .config import MESSAGE_LIMIT

_FENCE = re.compile(r"^\s*```[\w+-]*\s*$", re.MULTILINE)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_ALT = re.compile(r"__(.+?)__", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BULLET = re.compile(r"^(\s*)[*+]\s+", re.MULTILINE)
_BLANK_RUN = re.compile(r"\n{3,}")
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\(\s*(<?)(https?://[^\s)>]+)\2\s*\)")
_AUTOLINK = re.compile(r"<(https?://[^\s>]+)>")


def _unlink(match: re.Match[str]) -> str:
    """[текст](адрес) -> «текст — адрес»; ВК кликабельные ссылки делает сам."""
    label, url = match.group(1).strip(), match.group(3)
    if not label or label == url:
        return url
    return f"{label} — {url}"


def to_plain_text(text: str) -> str:
    """Убирает markdown-разметку, которую ВК показал бы как мусорные символы."""
    text = _FENCE.sub("", text)
    text = _HEADING.sub("", text)
    text = _MD_LINK.sub(_unlink, text)
    text = _AUTOLINK.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _BOLD_ALT.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BULLET.sub(r"\1— ", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Режет текст на части, влезающие в одно сообщение ВК.

    Границы выбираются по убыванию предпочтения: пустая строка, перевод строки,
    пробел, и только в крайнем случае — посреди слова.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            cut = window.rfind(separator)
            if cut > limit // 3:
                break
            cut = -1
        if cut == -1:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return [part for part in parts if part]


def prepare(text: str) -> list[str]:
    """to_plain_text + split_message — то, что нужно на выходе почти всегда."""
    return split_message(to_plain_text(text))
