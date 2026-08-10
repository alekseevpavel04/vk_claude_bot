"""Обёртка над Claude Agent SDK: один ход диалога = один вызов run_turn.

Используется `query()` с `resume`, а не долгоживущий ClaudeSDKClient: процесс
поднимается на время запроса и гаснет, поэтому падение агента не уносит бота,
а контекст живёт в сессии на диске.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    delete_session,
    list_sessions,
    query,
)

log = logging.getLogger(__name__)

# Набор инструментов задаётся явно: всего, чего здесь нет, у агента просто не
# существует. Bash, Write и Edit не входят — бот не должен ничего менять на VPS.
TOOLS = ["Read", "Glob", "WebSearch", "WebFetch", "TodoWrite"]

# Read спрашивает разрешение у нашего кода на каждый вызов (см. _permission_check):
# без этого он читает любой файл в контейнере, включая /proc/1/environ с токенами.
AUTO_ALLOWED_TOOLS = ["Glob", "WebSearch", "WebFetch", "TodoWrite"]

SYSTEM_PROMPT = """\
Ты — личный ассистент одного человека. Вы переписываетесь ВКонтакте, он читает
тебя с телефона.

Как писать:
- По-русски, если собеседник не пишет на другом языке.
- Живым связным текстом, как пишут человеку в мессенджере, а не отчётом. Без
  заголовков и без дробления каждой мысли в отдельный пункт.
- Сразу ответом. Без вступлений вроде «Отличный вопрос», без концовок вроде
  «уточни, если нужно», без напоминаний перепроверить информацию.
- Коротко: обычно хватает пары предложений или небольшого абзаца. Разворачивайся,
  только если человек просит разобраться подробно.
- Список — когда перечисляешь действительно разные вещи, и тогда простыми
  строками с «— » в начале. Связный рассказ в список не превращай.
- Не описывай, как добывал ответ: какие сайты открывал, где не пустил доступ, чем
  один источник отличается от другого. Это твоя кухня, читателю она не нужна.
  Если чего-то выяснить не удалось — скажи это одной фразой.
- Не приводи список источников в конце. Если ссылка важна сама по себе, вставь
  голый адрес прямо в предложение.

ВКонтакте не понимает markdown: **звёздочки**, ##заголовки, ```блоки кода```,
таблицы и ссылки вида [текст](адрес) видны как мусорные символы. Пиши обычным
текстом, код — просто отдельными строками.

Инструменты:
- WebSearch и WebFetch — когда вопрос про свежие события, цены, курсы, версии,
  документацию, и вообще когда важна точность. Наугад отвечать не надо.
- Read — читает присланные файлы и фотографии, пути к ним указаны в сообщении.
  ВАЖНО про его формат: текстовые файлы Read показывает с номерами строк слева,
  в виде «     5→содержимое строки». Номер и стрелку дорисовывает сам инструмент,
  в файле их нет. Никогда не принимай их за содержимое: если в файле числа, то
  номера строк к этим числам отношения не имеют и в подсчёты не идут.
- Файлы ты не редактируешь и команды не выполняешь — таких инструментов у тебя
  нет. Если просят сделать что-то на сервере, честно скажи, что не можешь.
"""

# Как показывать вызовы инструментов в чате.
_TOOL_LABELS: dict[str, tuple[str, str | None]] = {
    "WebSearch": ("🔎 Ищу", "query"),
    "WebFetch": ("🌐 Читаю", "url"),
    "Read": ("📄 Смотрю", "file_path"),
    "Glob": ("📂 Ищу файлы", "pattern"),
    "TodoWrite": ("📝 Планирую", None),
}


@dataclass
class TurnResult:
    text: str
    session_id: str | None
    is_error: bool
    cost_usd: float | None
    # Состояние лимитов подписки, если API его прислало по ходу запроса.
    rate_limits: dict[str, RateLimitInfo] = field(default_factory=dict)


def describe_tool(block: ToolUseBlock) -> str | None:
    """Короткая строка про вызов инструмента, или None если показывать нечего."""
    label, argument = _TOOL_LABELS.get(block.name, (f"🔧 {block.name}", None))
    if argument is None:
        return label
    value = block.input.get(argument)
    if not isinstance(value, str) or not value:
        return label
    if argument == "file_path":
        value = Path(value).name
    if len(value) > 120:
        value = value[:117] + "..."
    return f"{label}: {value}"


def _log_stderr(line: str) -> None:
    """Без этого падение CLI видно только как «exit code 1» без причины."""
    line = line.strip()
    if line:
        log.warning("claude stderr: %s", line)


def _make_permission_check(workspace: Path):
    """Разрешает Read только внутри рабочей папки.

    Без этой проверки агент может прочитать любой файл контейнера — например
    /proc/1/environ, где лежат токены ВК и Claude. Достаточно попросить его об
    этом в переписке или подсунуть такую инструкцию в веб-странице.
    """
    root = workspace.resolve()

    async def check(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name != "Read":
            return PermissionResultDeny(message=f"Инструмент {tool_name} боту недоступен.")

        raw = tool_input.get("file_path")
        if not isinstance(raw, str) or not raw:
            return PermissionResultDeny(message="Не указан путь к файлу.")
        try:
            target = Path(raw).resolve()
        except (OSError, ValueError):
            return PermissionResultDeny(message="Некорректный путь.")

        if target != root and root not in target.parents:
            log.warning("Отклонено чтение вне рабочей папки: %s", target)
            return PermissionResultDeny(
                message="Читать можно только файлы, присланные в этой переписке."
            )
        return PermissionResultAllow()

    return check


def _build_options(
    *,
    cwd: Path,
    resume: str | None,
    model: str | None,
    max_turns: int,
) -> ClaudeAgentOptions:
    env: dict[str, str] = {}
    # Транскрипты сессий нужны — на них держится resume, — но пусть SDK не
    # подмешивает пользовательские настройки и CLAUDE.md с хост-машины.
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token

    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        allowed_tools=AUTO_ALLOWED_TOOLS,
        disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "Task"],
        # Read не в allowed_tools, поэтому решение по нему принимает can_use_tool.
        # Спрашивать человека в переписке нельзя — отвечаем за него из кода.
        permission_mode="default",
        can_use_tool=_make_permission_check(cwd),
        setting_sources=[],
        cwd=str(cwd),
        resume=resume,
        max_turns=max_turns,
        model=model,
        env=env,
        stderr=_log_stderr,
    )


async def _as_stream(text: str) -> AsyncIterator[dict[str, Any]]:
    """Один вопрос, поданный потоком.

    can_use_tool работает только в streaming-режиме: со строковым промптом SDK
    отказывается запускаться. Отдаём ровно одно сообщение и закрываем поток —
    для обычного вопроса этого достаточно.
    """
    yield {"type": "user", "message": {"role": "user", "content": text}}


async def run_turn(
    *,
    prompt: str,
    cwd: Path,
    resume: str | None,
    model: str | None,
    max_turns: int,
    on_tool: Callable[[str], Awaitable[None]] | None = None,
) -> TurnResult:
    """Прогоняет один вопрос через агента и возвращает готовый ответ."""
    options = _build_options(cwd=cwd, resume=resume, model=model, max_turns=max_turns)

    collected: list[str] = []
    session_id: str | None = None
    is_error = False
    cost: float | None = None
    result_text: str | None = None
    fatal: str | None = None
    limits: dict[str, RateLimitInfo] = {}

    try:
        async for message in query(prompt=_as_stream(prompt), options=options):
            if isinstance(message, RateLimitEvent):
                info = message.rate_limit_info
                limits[info.rate_limit_type or "unknown"] = info
            elif isinstance(message, AssistantMessage):
                if message.error:
                    fatal = _explain_error(message.error)
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        collected.append(block.text.strip())
                    elif isinstance(block, ToolUseBlock) and on_tool is not None:
                        note = describe_tool(block)
                        if note:
                            await on_tool(note)
            elif isinstance(message, ResultMessage):
                session_id = message.session_id
                cost = message.total_cost_usd
                result_text = message.result
                is_error = message.is_error or message.subtype != "success"
                if is_error:
                    log.warning(
                        "Ход завершился с ошибкой: subtype=%s terminal_reason=%s errors=%s",
                        message.subtype,
                        message.terminal_reason,
                        message.errors,
                    )
    except CLINotFoundError as exc:
        raise RuntimeError(
            "Не найден Claude Code CLI. Установи его: npm install -g @anthropic-ai/claude-code"
        ) from exc

    if fatal:
        return TurnResult(
            text=fatal, session_id=session_id, is_error=True, cost_usd=cost, rate_limits=limits
        )

    text = (result_text or "").strip() or "\n\n".join(collected).strip()
    if not text:
        text = (
            "Ответ не получился — агент завершил ход без текста. "
            "Попробуй переформулировать вопрос или сбросить контекст командой /new."
        )
        is_error = True

    return TurnResult(
        text=text, session_id=session_id, is_error=is_error, cost_usd=cost, rate_limits=limits
    )


# --- лимиты подписки ------------------------------------------------------

_LIMIT_NAMES = {
    "five_hour": "текущее пятичасовое окно",
    "seven_day": "неделя",
    "seven_day_opus": "неделя, Opus",
    "seven_day_sonnet": "неделя, Sonnet",
    "overage": "перерасход",
}


def _human_delay(seconds: float) -> str:
    if seconds < 60:
        return "меньше минуты"
    # Округляем, а не обрезаем: «3 ч 19 мин» вместо ожидаемых 3 ч 20 мин выглядит
    # как ошибка, хотя разница в доли секунды.
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч" if hours else f"{days} дн"


# Пороги расхода в процентах. Работают, только если API пришлёт utilization —
# сейчас он приходит пустым, и живых сигналов ровно два: status allowed_warning
# (подходим к лимиту) и rejected (исчерпан). Им соответствуют отметки 90 и 100.
LIMIT_THRESHOLDS = (25, 50, 75, 90, 100)
WARNING_MARK = 90
EXHAUSTED_MARK = 100


def utilization_percent(info: RateLimitInfo) -> float | None:
    """API отдаёт долю 0..1 либо сразу проценты — приводим к процентам."""
    if info.utilization is None:
        return None
    return info.utilization * 100 if info.utilization <= 1 else info.utilization


def _reset_note(info: RateLimitInfo) -> str:
    if not info.resets_at:
        return ""
    left = info.resets_at - time.time()
    return f" Обнулится через {_human_delay(left)}." if left > 0 else " Уже обнулился."


def threshold_alerts(
    marks: dict[str, int], limits: dict[str, RateLimitInfo]
) -> tuple[list[str], dict[str, int]]:
    """Сообщения о пересечённых порогах расхода.

    `marks` — самый высокий порог, о котором уже предупреждали, по каждому виду
    лимита. Возвращает новые сообщения и обновлённые отметки: предупреждаем один
    раз на порог, а когда окно обнуляется и расход падает — отметка снимается.
    """
    alerts: list[str] = []
    updated = dict(marks)

    for kind, info in sorted(limits.items()):
        name = _LIMIT_NAMES.get(kind, kind)
        percent = utilization_percent(info)

        by_percent = (
            max((step for step in LIMIT_THRESHOLDS if percent >= step), default=0)
            if percent is not None
            else 0
        )
        by_status = {"rejected": EXHAUSTED_MARK, "allowed_warning": WARNING_MARK}.get(
            info.status, 0
        )
        reached = max(by_percent, by_status)

        previous = updated.get(kind, 0)
        if reached == previous:
            continue
        updated[kind] = reached
        if reached < previous:
            continue  # окно обнулилось — молча снимаем отметку

        if reached >= EXHAUSTED_MARK:
            alerts.append(f"Лимит подписки Claude исчерпан ({name}).{_reset_note(info)}")
        elif percent is not None:
            alerts.append(
                f"Израсходовано {reached}% лимита подписки Claude ({name}).{_reset_note(info)}"
            )
        else:
            alerts.append(f"Подписка Claude подходит к лимиту ({name}).{_reset_note(info)}")

    return alerts, updated


def describe_limits(limits: dict[str, RateLimitInfo]) -> str:
    if not limits:
        return (
            "Claude не прислал данные о лимитах — так бывает, когда до потолка ещё далеко. "
            "Значит, беспокоиться не о чем."
        )

    now = time.time()
    lines: list[str] = []
    for kind, info in sorted(limits.items()):
        name = _LIMIT_NAMES.get(kind, kind)
        parts: list[str] = []
        percent = utilization_percent(info)
        if percent is not None:
            parts.append(f"потрачено {percent:.0f}%")
        if info.status == "rejected":
            parts.append("ИСЧЕРПАН")
        elif info.status == "allowed_warning":
            parts.append("близко к пределу")
        elif percent is None:
            parts.append("расход в норме")
        if info.resets_at:
            left = info.resets_at - now
            parts.append(f"обнулится через {_human_delay(left)}" if left > 0 else "уже обнулился")
        lines.append(f"— {name}: {', '.join(parts)}" if parts else f"— {name}: данных нет")

    text = "Лимиты подписки Claude:\n" + "\n".join(lines)
    if all(info.utilization is None for info in limits.values()):
        # Точный процент приходит не всегда — у API для этого канала его просто нет.
        text += (
            "\nТочный процент Claude здесь не сообщает — только состояние. "
            "Предупрежу сам, когда подписка подойдёт к лимиту."
        )
    return text


async def probe_rate_limits(*, cwd: Path, model: str | None) -> dict[str, RateLimitInfo]:
    """Самый дешёвый запрос, ради которого API сообщит текущие лимиты."""
    result = await run_turn(
        prompt="Ответь одним словом: ок",
        cwd=cwd,
        resume=None,
        model=model,
        max_turns=1,
    )
    return result.rate_limits


# --- удаление разговоров --------------------------------------------------


def _forget_blocking(cwd: Path, session_ids: list[str] | None) -> int:
    directory = str(cwd)
    if session_ids is None:
        try:
            session_ids = [item.session_id for item in list_sessions(directory=directory)]
        except Exception as exc:  # noqa: BLE001 — отсутствие транскриптов не ошибка
            log.warning("Не удалось перечислить сессии Claude: %s", exc)
            return 0

    removed = 0
    for session_id in session_ids:
        try:
            delete_session(session_id, directory=directory)
            removed += 1
        except Exception as exc:  # noqa: BLE001 — часть могла исчезнуть сама
            log.debug("Сессия %s не удалена: %s", session_id, exc)
    return removed


async def forget_sessions(cwd: Path, session_ids: list[str] | None = None) -> int:
    """Стирает транскрипты разговоров с диска. None — стереть все в этой папке."""
    return await asyncio.to_thread(_forget_blocking, cwd, session_ids)


def _explain_error(kind: str) -> str:
    return {
        "authentication_failed": (
            "Claude не авторизован. Проверь CLAUDE_CODE_OAUTH_TOKEN в .env "
            "(получить: `claude setup-token`)."
        ),
        "billing_error": "Проблема с оплатой или лимитом подписки Claude.",
        "rate_limit": "Достигнут лимит запросов Claude. Попробуй чуть позже.",
        "invalid_request": "Claude отклонил запрос как некорректный.",
        "server_error": "Ошибка на стороне Claude. Попробуй ещё раз.",
    }.get(kind, f"Ошибка Claude: {kind}")
