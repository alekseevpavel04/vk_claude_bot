"""Проверка конфигурации перед запуском бота.

Ничего никому не пишет: только читающие вызовы VK API и один короткий вопрос
Claude. Секреты не печатает — только формат и длину, чтобы можно было безопасно
показать вывод.

Запуск:  python -m scripts.check_setup      (из корня проекта)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from bot.config import ConfigError, load_config  # noqa: E402
from bot.http import make_session  # noqa: E402
from bot.vk import VkClient, VkError  # noqa: E402

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[ ?? ]"

failures: list[str] = []


def report(status: str, title: str, detail: str = "") -> None:
    line = f"{status} {title}"
    if detail:
        line += f" — {detail}"
    print(line)
    if status == FAIL:
        failures.append(title)


def describe_secret(value: str) -> str:
    """Формат и длина — без самого секрета."""
    if not value:
        return "пусто"
    for prefix in ("vk1.a.", "sk-ant-oat01-", "sk-ant-api03-"):
        if value.startswith(prefix):
            return f"{prefix}… ({len(value)} символов)"
    return f"неизвестный формат ({len(value)} символов)"


async def check_vk(config) -> None:
    timeout = aiohttp.ClientTimeout(total=30)
    async with make_session(timeout) as http:
        vk = VkClient(http, config.vk_token, config.vk_group_id)

        try:
            groups = await vk.call("groups.getById", group_ids=config.vk_group_id)
            group = groups["groups"][0] if isinstance(groups, dict) else groups[0]
            report(OK, "Токен сообщества", f"{group['name']} (id {group['id']})")
        except VkError as exc:
            hint = {
                5: "токен недействителен или отозван",
                27: "это ключ приложения, а нужен ключ сообщества",
                100: "проверь VK_GROUP_ID — это номер из адреса vk.com/club…",
            }.get(exc.code, exc.message)
            report(FAIL, "Токен сообщества", f"ошибка {exc.code}: {hint}")
            return

        try:
            server = await vk.get_long_poll_server()
            report(OK, "Long Poll включён", f"ts={server['ts']}")
        except VkError as exc:
            if exc.code == 15:
                # Самая частая причина — ключ без права «Управление сообществом»:
                # сообщения им читаются и пишутся, а Long Poll не отдаётся.
                hint = (
                    "у ключа нет права «Управление сообществом» — без него ВК не отдаёт "
                    "Long Poll, и бот не увидит ни одного сообщения. Перевыпусти ключ "
                    "сразу со всеми четырьмя галочками: «Управление сообществом», "
                    "«Сообщения сообщества», «Фотографии», «Документы». Если право есть — "
                    "проверь, что Long Poll включён: Работа с API → Long Poll API"
                )
            elif exc.code == 100:
                hint = (
                    "Long Poll выключен: Управление → Работа с API → Long Poll API → «Включён», "
                    "и на вкладке «Типы событий» включи «Входящее сообщение»"
                )
            else:
                hint = exc.message
            report(FAIL, "Long Poll", f"ошибка {exc.code}: {hint}")

        try:
            users = await vk.call("users.get", user_ids=",".join(map(str, sorted(config.allowed_user_ids))))
            names = ", ".join(f"{u['first_name']} {u['last_name']} (id {u['id']})" for u in users)
            report(OK, "Белый список", names)
        except VkError as exc:
            report(WARN, "Белый список", f"не удалось проверить id: {exc.message}")

        # Права на отправку файлов. Проверяем именно вызовом, а не по описанию
        # ключа: ключ сообщества с одними «сообщениями» отвечает на эти методы
        # ошибкой 15, и обнаруживается это иначе только в момент, когда человек
        # просит прислать фото.
        peer = sorted(config.allowed_user_ids)[0]
        for method, what in (
            ("photos.getMessagesUploadServer", "фотографии"),
            ("docs.getMessagesUploadServer", "документы"),
        ):
            params = {"peer_id": peer}
            if method.startswith("docs"):
                params["type"] = "doc"
            try:
                await vk.call(method, **params)
                report(OK, f"Право слать {what}", "есть")
            except VkError as exc:
                if exc.code == 15:
                    hint = (
                        "у ключа нет прав. Перевыпусти ключ сообщества, отметив «Фотографии» "
                        "и «Документы»: Управление → Работа с API → Ключи доступа. "
                        "Без этого бот не сможет ничего присылать, всё остальное работает"
                    )
                elif exc.code == 901:
                    # Не про права ключа: ВК запрещает сообществу писать первым.
                    # Как только человек напишет сообществу сам, запрет снимается.
                    hint = (
                        "человек ещё ни разу не писал этому сообществу, и ВК не даёт "
                        "написать ему первым. Пройдёт само, как только он отправит боту "
                        "любое сообщение — например /help"
                    )
                else:
                    hint = exc.message
                report(WARN, f"Право слать {what}", hint)


async def check_browser() -> None:
    """Не «бинарник на месте», а «страница открылась»: разница принципиальная."""
    from bot import browser

    binary = browser.find_chromium()
    if binary is None:
        report(
            WARN,
            "Браузер",
            "Chromium не найден. Бот будет работать, но не сможет смотреть на страницы "
            "и снимать экран. В образе он ставится сам; локально задай CHROMIUM_PATH",
        )
        return
    try:
        page = await asyncio.wait_for(
            browser.render("https://example.com", want_details=True, settle=0.5), timeout=90
        )
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "Браузер", f"{binary} не открыл страницу: {exc}")
        return
    report(OK, "Браузер", f"{binary}, example.com открылся: «{page.title}»")


def check_claude_cli() -> bool:
    binary = shutil.which("claude")
    if binary is None:
        report(
            FAIL,
            "Claude Code CLI",
            "не найден в PATH. Установи: npm install -g @anthropic-ai/claude-code",
        )
        return False
    try:
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=60, check=True
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        report(FAIL, "Claude Code CLI", f"не запускается: {exc}")
        return False
    report(OK, "Claude Code CLI", version)
    return True


async def check_claude_turn(config) -> None:
    from bot import claude_runner

    print("       (пробный вопрос Claude, это займёт несколько секунд…)")
    try:
        result = await claude_runner.run_turn(
            prompt="Ответь ровно одним словом: готов",
            cwd=config.workspace,
            resume=None,
            model=config.claude_model,
            max_turns=1,
        )
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "Ответ Claude", str(exc))
        return

    # Проверка не должна оставлять после себя транскрипт разговора из одной фразы.
    if result.session_id:
        with contextlib.suppress(Exception):
            await claude_runner.forget_sessions(config.workspace, [result.session_id])

    answer = result.text.replace("\n", " ")[:80]
    if result.is_error:
        report(FAIL, "Ответ Claude", answer)
    else:
        report(OK, "Ответ Claude", f"«{answer}»")


async def main() -> int:
    print("Проверка конфигурации vk_claude_bot\n")

    try:
        config = load_config()
    except ConfigError as exc:
        report(FAIL, "Файл .env", str(exc))
        return 1

    report(OK, "VK_TOKEN", describe_secret(config.vk_token))
    report(OK, "VK_GROUP_ID", str(config.vk_group_id))

    claude_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if claude_token:
        report(OK, "CLAUDE_CODE_OAUTH_TOKEN", describe_secret(claude_token))
    elif config.claude_token_is_placeholder:
        report(
            WARN,
            "CLAUDE_CODE_OAUTH_TOKEN",
            "в .env остались иксы из шаблона — игнорирую. Настоящий: `claude setup-token`",
        )
    else:
        report(WARN, "CLAUDE_CODE_OAUTH_TOKEN", "не задан — авторизация возьмётся из ~/.claude")

    report(OK, "Рабочая папка", str(config.workspace))
    print()

    await check_vk(config)
    print()

    await check_browser()
    print()

    if check_claude_cli():
        await check_claude_turn(config)

    print()
    if failures:
        print(f"Не пройдено: {len(failures)} — {', '.join(failures)}")
        return 1
    print("Всё готово. Запускай бота: python -m bot.main")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
