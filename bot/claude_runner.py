"""Обёртка над Claude Agent SDK: один ход диалога = один вызов run_turn.

Используется `query()` с `resume`, а не долгоживущий ClaudeSDKClient: процесс
поднимается на время запроса и гаснет, поэтому падение агента не уносит бота,
а контекст живёт в сессии на диске.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    HookMatcher,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    delete_session,
    list_sessions,
    query,
)

from . import agent_tools
from .netcheck import url_problem

log = logging.getLogger(__name__)

# Набор инструментов задаётся явно: всего, чего здесь нет, у агента просто не
# существует. Bash, Write и Edit не входят — бот не должен ничего менять на VPS.
TOOLS = ["Read", "Glob", "WebSearch", "WebFetch", "TodoWrite"]

# Свои инструменты бота (bot/agent_tools.py): браузер, отправка файлов, Vivino.
# Это не «немного больше возможностей вообще», а конкретные функции в коде бота:
# выполнить произвольную команду ими по-прежнему нельзя.
OWN_TOOLS = list(agent_tools.FULL_TOOL_NAMES)
ALLOWED_TOOLS = TOOLS + OWN_TOOLS

# Инструменты, которым в аргументах приезжает адрес. Его проверяет и хук, и сам
# инструмент: цена промаха — метаданные облака и соседи по хосту.
_URL_ARGUMENTS = {
    "WebFetch": "url",
    "mcp__bot__page_read": "url",
    "mcp__bot__page_screenshot": "url",
    "mcp__bot__save_image": "url",
}

# Единственная папка, которую агенту разрешено читать: сюда MediaStore кладёт
# присланные вложения. Имя должно совпадать с config.media_dir.
MEDIA_SUBDIR = "media"

# Переменные окружения бота, которым нечего делать в процессе Claude Code.
# SDK отдаёт подпроцессу всё окружение целиком, а там лежит ключ ВК: чтение
# /proc и так закрыто хуком, но ключа в чужом процессе быть просто не должно.
_SECRET_ENV_KEYS = ("VK_TOKEN", "ALLOWED_USER_IDS", "VK_GROUP_ID")

# Потолок JSON-сообщения между SDK и CLI. Файл едет в base64 (+33% к размеру),
# поэтому берём с запасом над MAX_ATTACHMENT_MB (20 МБ по умолчанию).
MAX_BUFFER_BYTES = 48 * 1024 * 1024

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
- page_screenshot — снимок страницы настоящим браузером. Он сохраняется
  картинкой, ты открываешь её инструментом Read и смотришь на сайт своими
  глазами. Бери, когда важно, как оно выглядит: товар, украшение, фотография,
  график, таблица, карта, — и когда страница текстом не даётся.
- page_read — та же страница браузером, но текстом: содержимое, адреса картинок
  на ней и ссылки. Бери, когда WebFetch не открыл страницу или вернул огрызок,
  а ещё когда нужны адреса картинок.
- Часть сайтов браузер не пустит: покажет «проверяем браузер», капчу или откажет
  по адресу сервера. Инструмент об этом честно пишет. В таком случае не
  пересказывай заглушку и не выдумывай, что там было: скажи одной фразой, что
  сайт не открылся, и предложи, что можешь вместо этого — поискать в другом
  месте или дать ссылку. Выдачу поисковиков браузером не открывай, для поиска
  есть WebSearch.
- send_photo и send_file — прислать человеку картинку или файл прямо в
  переписку. «Пришли фото» — это про них: находишь картинку (адреса покажет
  page_read), отправляешь send_photo, человек сразу её видит. Отправленное не
  отправляй второй раз и не пересказывай словами, что на нём.
- save_image — скачать картинку к себе, чтобы разглядеть её через Read.
- wine_search — рейтинги вин на Vivino, про него отдельно ниже.
- В сообщении могут быть пересланные сообщения, ответы на чужие реплики и
  репосты записей — они идут отдельными блоками с пометкой «Пересланное
  сообщение», «Ответ на сообщение» или «Запись со стены». Это часть вопроса:
  смотри их, не переспрашивай. Текст записи со стены приходит целиком, вместе
  с картинками к ней, так что «не вижу содержимое поста» отвечать не надо.
- Голосовые ты не слышишь. Иногда ВК прикладывает к ним свою расшифровку
  («расшифровка: …») — тогда работай с ней. Если её нет, скажи, что голосовое
  недоступно, и попроси написать текстом.
- Read — читает присланные файлы и фотографии, пути к ним указаны в сообщении.
  Читай их ВСЕ, каждый отдельным вызовом, и только потом отвечай. Сколько файлов
  приложено, написано в сообщении числом — сверься с ним. Это твоя самая частая
  ошибка: человек прикладывает четыре фотографии, ты смотришь первую и отвечаешь
  по ней, а про остальные три говоришь так, будто их видел. Прикинуть по первому
  файлу, что на других, нельзя — фотографии разные.
  ВАЖНО про его формат: текстовые файлы Read показывает с номерами строк слева,
  в виде «     5→содержимое строки». Номер и стрелку дорисовывает сам инструмент,
  в файле их нет. Никогда не принимай их за содержимое: если в файле числа, то
  номера строк к этим числам отношения не имеют и в подсчёты не идут.
- Файлы ты не редактируешь и команды не выполняешь — таких инструментов у тебя
  нет. Если просят сделать что-то на сервере, честно скажи, что не можешь.

Про вино. Рейтинг не бери из памяти — только wine_search: память тут врёт
уверенно и мимо. С фотографии читай этикетку целиком: производитель, название,
сорт, год. Если на фото несколько бутылок или фотографий пришло несколько —
собери все названия и передай их ОДНИМ вызовом списком. Просят проверить все —
значит все, без «самых интересных». Названия пиши латиницей, как на этикетке.
На каждое название Vivino отдаёт несколько кандидатов: бери тот, чьё имя
действительно совпадает с этикеткой, а если ничего не совпало — так и скажи,
вместо того чтобы выдать похожее за нужное. В ответе на каждое вино хватает
строки: название, оценка и сколько людей её ставило, — и общий вывод, что из
этого брать.
"""

# Как показывать вызовы инструментов в чате.
_TOOL_LABELS: dict[str, tuple[str, str | None]] = {
    "WebSearch": ("🔎 Ищу", "query"),
    "WebFetch": ("🌐 Читаю", "url"),
    "Read": ("📄 Смотрю", "file_path"),
    "Glob": ("📂 Ищу файлы", "pattern"),
    "TodoWrite": ("📝 Планирую", None),
    "mcp__bot__wine_search": ("🍷 Смотрю рейтинги", None),
    "mcp__bot__page_read": ("🌐 Открываю браузером", "url"),
    "mcp__bot__page_screenshot": ("👀 Смотрю на страницу", "url"),
    "mcp__bot__save_image": ("🖼 Забираю картинку", "url"),
    "mcp__bot__send_photo": ("📤 Отправляю фото", "source"),
    "mcp__bot__send_file": ("📎 Отправляю файл", "source"),
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
        # В чате полезно имя файла, а не путь вида /app/workspace/media/145.../…
        value = Path(value).name
    if len(value) > 120:
        value = value[:117] + "..."
    return f"{label}: {value}"


def _log_stderr(line: str) -> None:
    """Без этого падение CLI видно только как «exit code 1» без причины."""
    line = line.strip()
    if line:
        log.warning("claude stderr: %s", line)


def _inside(target: Path, root: Path) -> bool:
    return target == root or root in target.parents


def _make_tool_guard(workspace: Path):
    """Хук, ограничивающий инструменты агента.

    Именно хук, а не can_use_tool: тот вызывается лишь для инструментов, по
    которым CLI сам не принял решение, а Read он авторазрешает как безопасный —
    callback до нас просто не доходит (проверено на живом сервере).

    У Claude Code есть и своё ограничение рабочей директорией, но полагаться
    только на него нельзя: это чужая настройка по умолчанию, которая может
    измениться с версией CLI, а цена промаха — токены ВК и Claude из
    /proc/1/environ, выданные в переписку по первой просьбе.

    Границы разные для чтения и для поиска. Read пускается только в
    workspace/media — туда и только туда бот кладёт присланные файлы, и больше
    агенту читать нечего. Рабочая папка целиком для Read не годится: рядом
    лежат state.json и всё, что когда-нибудь окажется смонтировано внутрь неё.
    Glob ограничен рабочей папкой: он показывает только имена, а запрет на
    шаблон без пути ломал бы обычный поиск.
    """
    cwd = workspace.resolve()
    read_root = (cwd / MEDIA_SUBDIR).resolve()

    def resolve(raw: str) -> Path:
        # Относительный путь агент задаёт от своей рабочей папки. Path.resolve()
        # взял бы рабочую папку процесса бота (/app) — это другая директория, и
        # проверка получалась бы не про тот путь.
        path = Path(raw)
        return path.resolve() if path.is_absolute() else (cwd / path).resolve()

    async def guard(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name")
        tool_input = input_data.get("tool_input") or {}
        verdict: str | None = None

        if tool_name == "Read":
            raw = tool_input.get("file_path")
            if not isinstance(raw, str) or not raw:
                verdict = "Не указан путь к файлу."
            else:
                try:
                    target = resolve(raw)
                except (OSError, ValueError):
                    verdict = "Некорректный путь."
                else:
                    if not _inside(target, read_root):
                        log.warning("Отклонено чтение вне папки вложений: %s", target)
                        verdict = "Читать можно только файлы, присланные в этой переписке."

        elif tool_name == "Glob":
            # Сам по себе Glob ничего не читает, но шаблоном можно осмотреть
            # файловую систему сервера — этого агенту тоже не нужно. Проверяем
            # и относительные пути: «../..» уводит наружу не хуже абсолютного.
            for key in ("path", "pattern"):
                value = tool_input.get(key)
                if not isinstance(value, str) or not value:
                    continue
                # До первого спецсимвола шаблона — дальше начинается маска, и
                # каталогом это уже не является.
                base = re.split(r"[*?\[]", value, maxsplit=1)[0]
                try:
                    target = resolve(base)
                except (OSError, ValueError):
                    verdict = "Некорректный путь."
                    break
                if not _inside(target, cwd):
                    log.warning("Отклонён поиск файлов вне рабочей папки: %s", value)
                    verdict = "Искать можно только в рабочей папке."
                    break

        elif tool_name in _URL_ARGUMENTS:
            # Контейнер видит внутреннюю сеть сервера: метаданные облака,
            # соседние сервисы на хосте. Инструменты с адресом — единственный
            # способ туда попасть, в том числе по ссылке со страницы.
            url = tool_input.get(_URL_ARGUMENTS[tool_name])
            verdict = await url_problem(url if isinstance(url, str) else "")
            if verdict:
                log.warning("Отклонён %s: %s (%r)", tool_name, verdict, url)

        elif tool_name in ("mcp__bot__send_photo", "mcp__bot__send_file"):
            # Отправлять можно и по адресу, и лежащим файлом: адрес проверяем
            # здесь, а путь — в самом инструменте, он знает папку вложений.
            source = tool_input.get("source")
            if isinstance(source, str) and source.lower().startswith(("http://", "https://")):
                verdict = await url_problem(source)
                if verdict:
                    log.warning("Отклонена отправка с адреса: %s (%r)", verdict, source)

        elif tool_name not in ALLOWED_TOOLS:
            # Набор инструментов уже задан в options, но пусть последнее слово
            # остаётся за хуком: он в нашем коде и не зависит от того, как
            # очередная версия CLI поймёт allowed_tools.
            log.warning("Отклонён инструмент вне набора: %s", tool_name)
            verdict = "Такого инструмента у тебя нет."

        if verdict is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": verdict,
            }
        }

    return guard


def _build_options(
    *,
    cwd: Path,
    resume: str | None,
    model: str | None,
    max_turns: int,
    tools_context: agent_tools.ToolContext | None = None,
) -> ClaudeAgentOptions:
    # SDK передаёт подпроцессу всё окружение бота и накрывает его этим словарём.
    # Убрать ключ насовсем нельзя — только перекрыть, поэтому затираем пустым.
    env: dict[str, str] = {key: "" for key in _SECRET_ENV_KEYS}
    # Транскрипты сессий нужны — на них держится resume, — но пусть SDK не
    # подмешивает пользовательские настройки и CLAUDE.md с хост-машины.
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token

    # Свои инструменты живут в этом же процессе и знают, с кем идёт разговор.
    # Без контекста (проверка лимитов, служебные запуски) их просто нет.
    servers: dict[str, Any] = {}
    if tools_context is not None:
        servers[agent_tools.SERVER_NAME] = agent_tools.build_server(tools_context)

    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        allowed_tools=ALLOWED_TOOLS if servers else TOOLS,
        mcp_servers=servers,
        # Никаких чужих MCP-серверов из настроек машины — только наш.
        strict_mcp_config=True,
        disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "Task"],
        # Всё, чего нет в tools, отклоняется молча: спрашивать человека в
        # переписке всё равно некого. Пути для Read проверяет хук ниже.
        permission_mode="dontAsk",
        hooks={"PreToolUse": [HookMatcher(hooks=[_make_tool_guard(cwd)])]},
        setting_sources=[],
        cwd=str(cwd),
        resume=resume,
        max_turns=max_turns,
        model=model,
        env=env,
        stderr=_log_stderr,
        # SDK и CLI общаются одним JSON-сообщением на событие, а содержимое
        # прочитанного файла едет внутри него. Буфер по умолчанию — 1 МБ, то
        # есть любое фото с телефона роняет ход с «JSON message exceeded
        # maximum buffer size». Держим запас над лимитом на вложение.
        max_buffer_size=MAX_BUFFER_BYTES,
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
    tools_context: agent_tools.ToolContext | None = None,
) -> TurnResult:
    """Прогоняет один вопрос через агента и возвращает готовый ответ."""
    options = _build_options(
        cwd=cwd, resume=resume, model=model, max_turns=max_turns, tools_context=tools_context
    )

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
                        "Ход завершился с ошибкой: subtype=%s terminal_reason=%s "
                        "errors=%s api_status=%s result=%r",
                        message.subtype,
                        message.terminal_reason,
                        message.errors,
                        message.api_error_status,
                        (message.result or "")[:200],
                    )
    except CLINotFoundError as exc:
        raise RuntimeError(
            "Не найден Claude Code CLI. Установи его: npm install -g @anthropic-ai/claude-code"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # Получив результат с is_error, CLI намеренно выходит ненулевым кодом, а
        # SDK превращает это в голое исключение — с текстом вроде «returned an
        # error result: success», где success это просто subtype. Сам ответ к
        # этому моменту уже получен, и ронять из-за такого ход незачем.
        log.warning("Агент завершился с ошибкой: %s", exc)
        limit_note = _exhausted_limit_note(limits)
        if limit_note:
            return TurnResult(
                text=limit_note, session_id=session_id, is_error=True,
                cost_usd=cost, rate_limits=limits,
            )
        if not (result_text or collected):
            return TurnResult(
                text=f"Claude прервал работу: {exc}", session_id=session_id, is_error=True,
                cost_usd=cost, rate_limits=limits,
            )
        is_error = True

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
        # Процент приходит не всегда: API начинает его слать только ближе к
        # потолку (на практике — около 90%). Пока молчит — расход невелик.
        text += (
            "\nПроцент Claude пока не сообщает — он появляется ближе к лимиту. "
            "Предупрежу сам, когда это случится."
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
    # Убираем за собой: иначе каждый /limits оставлял бы на диске транскрипт
    # разговора из одной реплики.
    if result.session_id:
        await forget_sessions(cwd, [result.session_id])
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


def _forget_stale_blocking(cwd: Path, max_age_hours: int, keep: frozenset[str]) -> int:
    directory = str(cwd)
    try:
        sessions = list_sessions(directory=directory)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось перечислить сессии Claude: %s", exc)
        return 0

    cutoff = time.time() - max_age_hours * 3600
    stale: list[str] = []
    for info in sessions:
        # Разговоры, которые бот ещё помнит, не трогаем ни при каком возрасте:
        # удалить транскрипт под активной сессией — значит уронить следующий
        # resume, а это как раз та поломка, которую мы уже однажды ловили.
        if info.session_id in keep:
            continue
        stamp = info.last_modified
        if stamp is None:
            continue
        # SDK отдаёт миллисекунды; страхуемся, если однажды станут секундами.
        seconds = stamp / 1000 if stamp > 1e12 else stamp
        if seconds < cutoff:
            stale.append(info.session_id)

    return _forget_blocking(cwd, stale) if stale else 0


async def forget_stale_sessions(
    cwd: Path, max_age_hours: int, keep: set[str] | frozenset[str] | None = None
) -> int:
    """Убирает транскрипты, к которым давно не обращались.

    Нужна отдельно от prune по state.json: тот знает только про разговоры,
    которые бот ещё помнит. Транскрипты от /new, от проверок и от прежних
    запусков не значатся нигде и иначе остаются на диске навсегда.
    """
    if max_age_hours <= 0:
        return 0
    removed = await asyncio.to_thread(
        _forget_stale_blocking, cwd, max_age_hours, frozenset(keep or ())
    )
    if removed:
        log.info("Удалено забытых транскриптов: %s", removed)
    return removed


def _exhausted_limit_note(limits: dict[str, RateLimitInfo]) -> str | None:
    """Понятное объяснение вместо технической ошибки, если упёрлись в лимит."""
    for kind, info in sorted(limits.items()):
        if info.status != "rejected":
            continue
        name = _LIMIT_NAMES.get(kind, kind)
        return (
            f"Лимит подписки Claude исчерпан ({name}).{_reset_note(info)} "
            "Раньше этого я отвечать не смогу."
        )
    return None


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
