"""Обёртка над Claude Agent SDK: один ход диалога = один вызов run_turn.

Используется `query()` с `resume`, а не долгоживущий ClaudeSDKClient: процесс
поднимается на время запроса и гаснет, поэтому падение агента не уносит бота,
а контекст живёт в сессии на диске.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

log = logging.getLogger(__name__)

# Набор инструментов задаётся явно: всего, чего здесь нет, у агента просто не
# существует. Bash, Write и Edit не входят — бот не должен ничего менять на VPS.
TOOLS = ["Read", "Glob", "WebSearch", "WebFetch", "TodoWrite"]

SYSTEM_PROMPT = """\
Ты — личный ассистент, который общается с одним человеком в переписке ВКонтакте.
Отвечай на русском, если собеседник не пишет на другом языке.

Формат ответа:
- ВКонтакте не умеет markdown. Пиши обычным текстом.
- Не используй **жирный**, ##заголовки, ```блоки кода``` и таблицы — они
  отобразятся как мусорные символы. Код давай отдельными строками как есть.
- Списки — обычными строками, можно начинать с «— » или «• ».
- Это переписка с телефона: отвечай по делу и без длинных вступлений. Развёрнутый
  ответ уместен, только если вопрос действительно этого требует.

Инструменты:
- Есть веб-поиск (WebSearch) и чтение страниц (WebFetch). Пользуйся ими, когда
  вопрос про свежие события, цены, курсы, версии, документацию или когда точность
  важнее скорости. Ссылайся на источник, если факт неочевиден.
- Файлов на сервере ты не редактируешь и команд не выполняешь — таких инструментов
  у тебя нет. Если просят что-то сделать на сервере, честно скажи, что не можешь.
- Если в сообщении указан путь к вложению, прочитай его инструментом Read: так ты
  увидишь присланное фото или документ.
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
        allowed_tools=TOOLS,
        disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "Task"],
        # Всё, чего нет в allowed_tools, отклоняется молча вместо запроса
        # разрешения — спросить пользователя в переписке всё равно некого.
        permission_mode="dontAsk",
        setting_sources=[],
        cwd=str(cwd),
        resume=resume,
        max_turns=max_turns,
        model=model,
        env=env,
    )


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

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
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
        return TurnResult(text=fatal, session_id=session_id, is_error=True, cost_usd=cost)

    text = (result_text or "").strip() or "\n\n".join(collected).strip()
    if not text:
        text = (
            "Ответ не получился — агент завершил ход без текста. "
            "Попробуй переформулировать вопрос или сбросить контекст командой /new."
        )
        is_error = True

    return TurnResult(text=text, session_id=session_id, is_error=is_error, cost_usd=cost)


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
