"""Точка входа: слушает Long Poll ВК и гоняет сообщения через Claude."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from . import claude_runner, formatting
from .config import Config, ConfigError, load_config
from .http import make_session
from .media import Attachment, MediaStore
from .sessions import SessionStore
from .vk import VkClient, iter_events

log = logging.getLogger("vk_claude_bot")

# Как часто обновлять индикатор «печатает» (ВК гасит его примерно через 10 с).
TYPING_INTERVAL = 5.0
# Не чаще одного сообщения о прогрессе в эти секунды и не больше лимита за ход.
PROGRESS_MIN_INTERVAL = 3.0
PROGRESS_MAX_PER_TURN = 12

HELP_TEXT = """\
Просто напиши вопрос — отвечу. Можно кидать фото и документы, я их посмотрю.
Умею искать в интернете, если вопрос про свежие данные.

Команды:
/new — начать разговор с чистого листа (забыть контекст)
/stop — прервать текущий ответ
/status — что сейчас происходит
/help — это сообщение\
"""


@dataclass
class Job:
    text: str
    message_id: int
    attachments: list[dict[str, Any]]


class PeerWorker:
    """Обрабатывает сообщения одного собеседника строго по одному за раз."""

    def __init__(self, peer_id: int, bot: "Bot") -> None:
        self.peer_id = peer_id
        self._bot = bot
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._loop_task: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self.busy_since: float | None = None

    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run(), name=f"peer-{self.peer_id}")

    def submit(self, job: Job) -> None:
        self._queue.put_nowait(job)

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def busy(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    def cancel_current(self) -> bool:
        if self.busy and self._turn_task is not None:
            self._turn_task.cancel()
            return True
        return False

    async def close(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            self._turn_task = asyncio.create_task(self._handle(job))
            self.busy_since = time.monotonic()
            try:
                await self._turn_task
            except asyncio.CancelledError:
                # Отменён именно ход (командой /stop), а не весь воркер.
                if self._loop_task is not None and self._loop_task.cancelling():
                    raise
                await self._bot.reply(self.peer_id, "Остановил. Что дальше?")
            except Exception as exc:  # noqa: BLE001 — воркер должен пережить любой сбой
                log.exception("Ошибка при обработке сообщения от %s", self.peer_id)
                await self._bot.reply(self.peer_id, f"Что-то сломалось: {exc}")
            finally:
                self._turn_task = None
                self.busy_since = None
                self._queue.task_done()

    async def _handle(self, job: Job) -> None:
        bot = self._bot
        typing = asyncio.create_task(bot.keep_typing(self.peer_id))
        progress = _ProgressReporter(bot, self.peer_id)
        try:
            attachments = await bot.media.collect(self.peer_id, job.message_id, job.attachments)
            prompt = _build_prompt(job.text, attachments)

            stored = bot.sessions.get(self.peer_id)
            result = await claude_runner.run_turn(
                prompt=prompt,
                cwd=bot.config.workspace,
                resume=stored.session_id if stored else None,
                model=bot.config.claude_model,
                max_turns=bot.config.max_turns,
                on_tool=progress.report if bot.config.show_tool_progress else None,
            )

            if result.session_id:
                bot.sessions.remember(self.peer_id, result.session_id)

            await bot.reply(self.peer_id, result.text)
            if result.cost_usd:
                log.info("Ход для %s стоил $%.4f", self.peer_id, result.cost_usd)
        finally:
            typing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing


class _ProgressReporter:
    """Шлёт в чат короткие заметки о вызовах инструментов, не заспамливая его."""

    def __init__(self, bot: "Bot", peer_id: int) -> None:
        self._bot = bot
        self._peer_id = peer_id
        self._last_at = 0.0
        self._sent = 0

    async def report(self, note: str) -> None:
        now = time.monotonic()
        if self._sent >= PROGRESS_MAX_PER_TURN:
            return
        if now - self._last_at < PROGRESS_MIN_INTERVAL:
            return
        self._last_at = now
        self._sent += 1
        with contextlib.suppress(Exception):
            await self._bot.vk.send_message(self._peer_id, note)


class Bot:
    def __init__(
        self,
        config: Config,
        vk: VkClient,
        media: MediaStore,
        sessions: SessionStore,
    ) -> None:
        self.config = config
        self.vk = vk
        self.media = media
        self.sessions = sessions
        self._workers: dict[int, PeerWorker] = {}

    # --- отправка --------------------------------------------------------

    async def reply(self, peer_id: int, text: str) -> None:
        chunks = formatting.prepare(text)
        for chunk in chunks:
            await self.vk.send_message(peer_id, chunk)

    async def keep_typing(self, peer_id: int) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self.vk.set_activity(peer_id)
            await asyncio.sleep(TYPING_INTERVAL)

    # --- приём -----------------------------------------------------------

    async def dispatch(self, update: dict[str, Any]) -> None:
        if update.get("type") != "message_new":
            return

        message = (update.get("object") or {}).get("message") or {}
        from_id = message.get("from_id")
        peer_id = message.get("peer_id")
        if not isinstance(from_id, int) or not isinstance(peer_id, int):
            return
        if from_id <= 0:
            # Сообщение от самого сообщества — эхо собственных ответов.
            return
        if from_id not in self.config.allowed_user_ids:
            log.info("Игнорирую сообщение от %s (нет в ALLOWED_USER_IDS)", from_id)
            return

        text = (message.get("text") or "").strip()
        if text.startswith("/"):
            handled = await self._command(peer_id, text)
            if handled:
                return

        job = Job(
            text=text,
            message_id=int(message.get("id", 0)),
            attachments=message.get("attachments") or [],
        )
        if not job.text and not job.attachments:
            return

        worker = self._worker(peer_id)
        if worker.busy:
            await self.reply(peer_id, "Занят предыдущим вопросом — отвечу на этот следом.")
        worker.submit(job)

    def _worker(self, peer_id: int) -> PeerWorker:
        worker = self._workers.get(peer_id)
        if worker is None:
            worker = PeerWorker(peer_id, self)
            worker.start()
            self._workers[peer_id] = worker
        return worker

    async def _command(self, peer_id: int, text: str) -> bool:
        command = text.split(maxsplit=1)[0].lower().lstrip("/")

        if command in {"help", "start", "помощь"}:
            await self.reply(peer_id, HELP_TEXT)
            return True

        if command in {"new", "reset", "новый"}:
            worker = self._workers.get(peer_id)
            if worker is not None:
                worker.cancel_current()
            had = self.sessions.reset(peer_id)
            await self.reply(
                peer_id,
                "Контекст сброшен, начинаем заново." if had else "Контекста и так не было. Спрашивай.",
            )
            return True

        if command in {"stop", "стоп"}:
            worker = self._workers.get(peer_id)
            if worker is not None and worker.cancel_current():
                return True  # сообщение отправит сам воркер, поймав CancelledError
            await self.reply(peer_id, "Сейчас нечего останавливать.")
            return True

        if command in {"status", "статус"}:
            await self.reply(peer_id, self._status(peer_id))
            return True

        return False

    def _status(self, peer_id: int) -> str:
        worker = self._workers.get(peer_id)
        stored = self.sessions.get(peer_id)
        lines = []
        if worker is not None and worker.busy and worker.busy_since is not None:
            lines.append(f"Работаю над вопросом уже {int(time.monotonic() - worker.busy_since)} с.")
        else:
            lines.append("Свободен, жду вопрос.")
        if worker is not None and worker.queued:
            lines.append(f"В очереди ещё сообщений: {worker.queued}.")
        if stored:
            lines.append(f"Контекст: {stored.turns} ход(ов), последний {stored.updated_at}.")
        else:
            lines.append("Контекст пустой.")
        return "\n".join(lines)

    async def close(self) -> None:
        for worker in self._workers.values():
            await worker.close()


def _build_prompt(text: str, attachments: list[Attachment]) -> str:
    parts: list[str] = [text or "(сообщение без текста)"]

    files = [item for item in attachments if item.path is not None]
    notes = [item for item in attachments if item.path is None and item.note]

    if files:
        listing = "\n".join(f"- {item.path}" for item in files)
        parts.append("Приложенные файлы (прочитай их инструментом Read):\n" + listing)
    if notes:
        listing = "\n".join(f"- {item.kind}: {item.note}" for item in notes)
        parts.append("Прочие вложения:\n" + listing)

    return "\n\n".join(parts)


async def run() -> None:
    config = load_config()
    log.info(
        "Запуск: группа %s, разрешено пользователей: %s, рабочая папка %s",
        config.vk_group_id,
        len(config.allowed_user_ids),
        config.workspace,
    )

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
    async with make_session(timeout) as http:
        vk = VkClient(http, config.vk_token, config.vk_group_id)
        media = MediaStore(
            http, config.media_dir, config.max_attachment_bytes, config.media_ttl_days
        )
        sessions = SessionStore(config.state_file)
        bot = Bot(config, vk, media, sessions)

        media.purge_once()
        purge_task = asyncio.create_task(media.purge_loop(), name="media-purge")

        stop = asyncio.Event()
        _install_signal_handlers(stop)

        poller = asyncio.create_task(_poll(bot, vk), name="longpoll")
        await stop.wait()

        log.info("Останавливаюсь...")
        for task in (poller, purge_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await bot.close()


async def _poll(bot: Bot, vk: VkClient) -> None:
    async for update in iter_events(vk):
        try:
            await bot.dispatch(update)
        except Exception:  # noqa: BLE001 — одно кривое событие не должно ронять цикл
            log.exception("Не смог обработать событие %s", update.get("type"))


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: add_signal_handler не поддерживается, KeyboardInterrupt хватит.
            pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except ConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        log.info("Остановлено с клавиатуры")


if __name__ == "__main__":
    main()
