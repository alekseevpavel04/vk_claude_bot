"""Конфигурация бота: читается из .env один раз при старте."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

VK_API_VERSION = "5.199"
# Long Poll держит соединение до wait секунд. Больше 25 брать не стоит:
# часть промежуточных прокси рвёт соединение на 30 секундах.
LONGPOLL_WAIT = 25
# Лимит ВК на одно сообщение — 4096 символов; берём с запасом.
MESSAGE_LIMIT = 4000


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    vk_token: str
    vk_group_id: int
    allowed_user_ids: frozenset[int]
    claude_model: str | None
    workspace: Path
    media_dir: Path
    state_file: Path
    media_ttl_days: int
    max_attachment_bytes: int
    max_turns: int
    show_tool_progress: bool


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"В .env не задан {name}. Скопируй .env.example в .env и заполни."
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом, получено: {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"ALLOWED_USER_IDS содержит не число: {chunk!r}. "
                "Нужны числовые id пользователей через запятую."
            ) from exc
    if not ids:
        raise ConfigError(
            "ALLOWED_USER_IDS пуст — тогда бот не сможет ответить никому. "
            "Укажи хотя бы свой числовой id ВК."
        )
    return frozenset(ids)


def load_config() -> Config:
    load_dotenv(ROOT / ".env")

    workspace = ROOT / "workspace"
    media_dir = workspace / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    group_id_raw = _require("VK_GROUP_ID").lstrip("-")
    try:
        group_id = int(group_id_raw)
    except ValueError as exc:
        raise ConfigError(f"VK_GROUP_ID должен быть числом, получено: {group_id_raw!r}") from exc

    return Config(
        vk_token=_require("VK_TOKEN"),
        vk_group_id=group_id,
        allowed_user_ids=_user_ids(_require("ALLOWED_USER_IDS")),
        claude_model=os.environ.get("CLAUDE_MODEL", "").strip() or None,
        workspace=workspace,
        media_dir=media_dir,
        state_file=ROOT / "state.json",
        media_ttl_days=_int("MEDIA_TTL_DAYS", 7),
        max_attachment_bytes=_int("MAX_ATTACHMENT_MB", 20) * 1024 * 1024,
        max_turns=_int("MAX_TURNS", 30),
        show_tool_progress=_bool("SHOW_TOOL_PROGRESS", True),
    )
