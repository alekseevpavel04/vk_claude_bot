"""Конфигурация бота: читается из .env один раз при старте."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

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
    # Файл-метка «выключен стоп-фразой»: пока он есть, бот при старте сразу
    # выходит. Лежит в workspace, поэтому переживает пересоздание контейнера.
    kill_flag: Path
    media_ttl_days: int
    # Через сколько часов простоя начинать разговор с чистого листа. Контекст
    # копится с каждым ходом и стоит токенов, а недельная давность в личной
    # переписке всё равно не нужна.
    session_ttl_hours: int
    max_attachment_bytes: int
    # Потолок на всю папку вложений. Диск на VPS общий с соседним сервисом,
    # и «кончилось место» для него — отказ, а не неудобство.
    max_media_total_bytes: int
    max_turns: int
    # Сколько ждать продолжения, прежде чем браться за ответ: пересланное с
    # телефона и комментарий к нему приходят двумя сообщениями подряд.
    merge_window_seconds: float
    show_tool_progress: bool
    # Значение из .env осталось шаблонным (иксы из .env.example).
    claude_token_is_placeholder: bool


def is_placeholder(value: str) -> bool:
    """Значение выглядит как незаполненный шаблон из .env.example."""
    return "xxxx" in value.lower()


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"В .env не задан {name}. Скопируй .env.example в .env и заполни."
        )
    if is_placeholder(value):
        raise ConfigError(
            f"В .env значение {name} осталось шаблонным из .env.example — подставь настоящее."
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


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть числом, получено: {raw!r}") from exc


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


def _claude_token_is_placeholder() -> bool:
    """Шаблонный токен хуже отсутствующего: он перебьёт рабочую авторизацию
    из ~/.claude и даст невнятную ошибку аутентификации. Убираем его из
    окружения, чтобы SDK взял настоящие креды, если они есть."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token and is_placeholder(token):
        log.warning(
            "CLAUDE_CODE_OAUTH_TOKEN в .env остался шаблонным — игнорирую его. "
            "Получи настоящий командой `claude setup-token`."
        )
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        return True
    return False


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    token_is_placeholder = _claude_token_is_placeholder()

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
        # Состояние лежит внутри workspace: так весь изменяемый стейт бота — это
        # одна папка, которую достаточно смонтировать в контейнер одним томом.
        state_file=workspace / "state.json",
        kill_flag=workspace / ".killed",
        media_ttl_days=_int("MEDIA_TTL_DAYS", 7),
        session_ttl_hours=_int("SESSION_TTL_HOURS", 168),
        max_attachment_bytes=_int("MAX_ATTACHMENT_MB", 20) * 1024 * 1024,
        max_media_total_bytes=_int("MEDIA_MAX_TOTAL_MB", 512) * 1024 * 1024,
        max_turns=_int("MAX_TURNS", 30),
        merge_window_seconds=max(0.0, _float("MESSAGE_MERGE_SECONDS", 3.0)),
        show_tool_progress=_bool("SHOW_TOOL_PROGRESS", True),
        claude_token_is_placeholder=token_is_placeholder,
    )
