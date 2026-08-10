"""Точка входа: слушает Long Poll ВК и гоняет сообщения через Claude."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import signal
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from . import claude_runner, formatting
from .config import Config, ConfigError, load_config
from .http import make_session
from .media import MediaStore, MessagePart
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
/stop — прервать ответ; если я свободен, выключить меня (спрошу подтверждение)
/cancel — только прервать ответ, никогда не выключает
/new — забыть контекст этого разговора и начать заново
/limits — сколько потрачено лимитов подписки и когда обнулятся
/status — чем занят прямо сейчас
/clear — полная очистка: все разговоры и все файлы
/help — это сообщение

Предупрежу сам, когда расход подойдёт к лимиту. Раньше этого Claude процент не
сообщает, так что до 90% в /limits будет только время до обновления.\
"""

# Сколько секунд действует подтверждение /stop.
STOP_CONFIRM_WINDOW = 120

# peer_id беседы начинается с этого числа; всё, что меньше — личная переписка.
CHAT_PEER_BASE = 2_000_000_000

# Сколько id обработанных сообщений помнить ради защиты от повторов.
SEEN_MESSAGES_LIMIT = 500

# Сколько вопросов может ждать своей очереди у одного собеседника.
QUEUE_LIMIT = 5

# Потолок на один ход. Без него зависший агент держал бы собеседника вечно:
# бот молчит, очередь стоит, и понять это можно только по /status.
TURN_TIMEOUT = 900

# Потолок на скачивание всех вложений одного сообщения.
MEDIA_TIMEOUT = 180

# Потолок ожидания продолжения (само окно — MESSAGE_MERGE_SECONDS в .env):
# поток сообщений не должен откладывать ответ бесконечно.
COALESCE_MAX = 15.0

# Задержка между частями длинного ответа — против флуд-контроля ВК.
CHUNK_PAUSE = 0.35

STOP_CONFIRM_REPLY = """\
Точно выключить? Я остановлюсь целиком и сам обратно не поднимусь — ни после
перезапуска контейнера, ни после перезагрузки сервера.

Отправь /stop ещё раз в течение двух минут, если да.\
"""

STOP_REPLY = """\
Выключаюсь. Включить обратно: ./deploy/deploy.sh start\
"""

CLEAR_INTRO = "Чищу всё: разговоры и файлы…"

# Команда — это слэш и одно слово. Всё остальное («/usr/bin/env — что это?»)
# отправляется Claude как обычный вопрос.
_COMMAND = re.compile(r"^/([^\W\d_][\w-]*)$", re.UNICODE)


@dataclass
class Job:
    # Всё сообщение целиком: внутри могут быть пересланные и ответ, а их нужно
    # разбирать наравне с самим текстом.
    message: dict[str, Any]
    message_id: int


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

    def submit(self, job: Job) -> bool:
        """Ставит вопрос в очередь. False — очередь переполнена."""
        if self._queue.qsize() >= QUEUE_LIMIT:
            return False
        self._queue.put_nowait(job)
        return True

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
            jobs = [await self._queue.get()]
            # «Печатает» включаем сразу, ещё на паузе: иначе после отправки
            # сообщения несколько секунд не происходит вообще ничего.
            typing = asyncio.create_task(self._bot.keep_typing(self.peer_id))
            try:
                await self._gather(jobs)
                self._turn_task = asyncio.create_task(self._handle(jobs))
                self.busy_since = time.monotonic()
                try:
                    await self._turn_task
                except asyncio.CancelledError:
                    # Отменён именно ход (командой /stop), а не весь воркер.
                    if self._loop_task is not None and self._loop_task.cancelling():
                        raise
                    await self._say(self.peer_id, "Остановил. Что дальше?")
                except Exception as exc:  # noqa: BLE001 — воркер переживает любой сбой
                    log.exception("Ошибка при обработке сообщения от %s", self.peer_id)
                    await self._say(self.peer_id, f"Что-то сломалось: {exc}")
            finally:
                typing.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await typing
                self._turn_task = None
                self.busy_since = None
                for _ in jobs:
                    self._queue.task_done()

    async def _gather(self, jobs: list[Job]) -> None:
        """Добирает сообщения, пришедшие сразу следом, чтобы ответить разом.

        Переслав что-то с телефона, человек тут же дописывает комментарий — ВК
        шлёт это двумя сообщениями подряд, и без паузы бот отвечал бы на репост
        и на комментарий порознь, не связав их. Отсчёт идёт от последнего
        сообщения, поэтому вопрос, набранный в три захода, тоже соберётся в один.
        """
        window = self._bot.config.merge_window_seconds
        if window <= 0:
            return
        deadline = time.monotonic() + COALESCE_MAX
        while True:
            left = min(window, deadline - time.monotonic())
            if left <= 0:
                break
            try:
                jobs.append(await asyncio.wait_for(self._queue.get(), timeout=left))
            except asyncio.TimeoutError:
                break
        if len(jobs) > 1:
            log.info("Сообщений собрано в один вопрос: %s", len(jobs))

    async def _say(self, peer_id: int, text: str) -> None:
        """Отправка, которая не может убить воркер.

        Сообщения об ошибке шлются из блока except: если сама отправка упадёт
        (сеть, флуд-контроль, запрет писать этому человеку), исключение вылетит
        из цикла и воркер молча умрёт — дальше этот собеседник не дождётся
        вообще ничего.
        """
        try:
            await self._bot.reply(peer_id, text)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить сообщение в %s", peer_id)

    async def _handle(self, jobs: list[Job]) -> None:
        bot = self._bot
        progress = _ProgressReporter(bot, self.peer_id)

        # Скачивание — тоже под таймаутом, и отдельно от хода: оно идёт до него,
        # а сервер, отдающий файл по байту в минуту, держал бы соединение живым
        # сколько угодно (sock_read так и не сработает).
        try:
            messages = await asyncio.wait_for(self._collect(jobs), timeout=MEDIA_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("Вложения от %s не скачались за %s с", self.peer_id, MEDIA_TIMEOUT)
            await self._say(self.peer_id, "Не смог скачать вложения — ВК не отдаёт файлы.")
            return
        prompt = _build_prompt(messages)

        stored = bot.sessions.get(self.peer_id)
        if stored is not None and stored.age_hours() > bot.config.session_ttl_hours:
            log.info(
                "Разговор с %s не обновлялся %.0f ч — начинаю с чистого листа",
                self.peer_id,
                stored.age_hours(),
            )
            await claude_runner.forget_sessions(bot.config.workspace, [stored.session_id])
            bot.sessions.reset(self.peer_id)
            stored = None

        try:
            result = await asyncio.wait_for(
                claude_runner.run_turn(
                    prompt=prompt,
                    cwd=bot.config.workspace,
                    resume=stored.session_id if stored else None,
                    model=bot.config.claude_model,
                    max_turns=bot.config.max_turns,
                    on_tool=progress.report if bot.config.show_tool_progress else None,
                ),
                timeout=TURN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("Ход для %s не уложился в %s с — прерываю", self.peer_id, TURN_TIMEOUT)
            await self._say(
                self.peer_id,
                f"Не уложился в {TURN_TIMEOUT // 60} минут и прервался. "
                "Попробуй сузить вопрос или начать заново: /new",
            )
            return

        if result.session_id:
            bot.sessions.remember(self.peer_id, result.session_id)
        alerts = bot.remember_limits(result.rate_limits)

        await bot.reply(self.peer_id, result.text)
        for alert in alerts:
            await bot.reply(self.peer_id, alert)
        if result.cost_usd:
            log.info("Ход для %s стоил $%.4f", self.peer_id, result.cost_usd)

    async def _collect(self, jobs: list[Job]) -> list[list[MessagePart]]:
        """Разбирает все собранные сообщения, деля один бюджет на вложения."""
        budget = self._bot.media.new_budget()
        return [
            await self._bot.media.collect_parts(
                self.peer_id, job.message_id, job.message, budget=budget
            )
            for job in jobs
        ]


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
        shutdown: asyncio.Event | None = None,
    ) -> None:
        self.config = config
        self.vk = vk
        self.media = media
        self.sessions = sessions
        self.shutdown = shutdown or asyncio.Event()
        # True, если выключение запрошено командой /stop, а не сигналом ОС.
        self.killed = False
        self._workers: dict[int, PeerWorker] = {}
        # Когда был первый /stop, по собеседникам: второй в течение окна
        # подтверждает выключение. Именно по собеседникам — иначе «да» одного
        # человека засчиталось бы как ответ на вопрос, заданный другому.
        self._stop_asked_at: dict[int, float] = {}
        # Команды, которые уже выполняются: второй такой же запускать нельзя.
        self._running: set[str] = set()
        self._limits: dict[str, Any] = {}
        self._limits_at = 0.0
        # Самый высокий порог расхода, о котором уже предупреждали, по видам лимита.
        self._limit_marks: dict[str, int] = {}
        # Долгие команды не должны блокировать приём событий Long Poll.
        self._tasks: set[asyncio.Task[None]] = set()
        # id уже обработанных сообщений: Long Poll при переподключении может
        # повторить события, а дважды отвеченный вопрос — двойной расход подписки.
        self._seen: OrderedDict[int, None] = OrderedDict()

    def _is_duplicate(self, message_id: int) -> bool:
        if message_id <= 0:
            return False
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        while len(self._seen) > SEEN_MESSAGES_LIMIT:
            self._seen.popitem(last=False)
        return False

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _spawn_once(self, key: str, coro: Any) -> bool:
        """Как _spawn, но одновременно выполняется только одна такая команда.

        /limits поднимает рядом ещё один процесс Claude; два сразу — это пик
        под гигабайт на сервере, где столько всего памяти и рядом живёт чужой
        сервис. Второй запрос лучше отклонить, чем устроить общий OOM.
        """
        if key in self._running:
            coro.close()  # иначе Python отругается на невыполненную корутину
            return False
        self._running.add(key)

        async def guarded() -> None:
            try:
                await coro
            finally:
                self._running.discard(key)

        self._spawn(guarded())
        return True

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        """Иначе исключение фоновой задачи пропадает совсем: Python сообщит о
        нём только при сборке мусора, и то в поток ошибок без контекста."""
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error("Фоновая задача упала", exc_info=error)

    def remember_limits(self, limits: dict[str, Any]) -> list[str]:
        """Запоминает состояние лимитов и отдаёт предупреждения о новых порогах."""
        if not limits:
            return []
        self._limits = limits
        self._limits_at = time.monotonic()
        alerts, self._limit_marks = claude_runner.threshold_alerts(self._limit_marks, limits)
        return alerts

    # --- отправка --------------------------------------------------------

    async def reply(self, peer_id: int, text: str) -> None:
        chunks = formatting.prepare(text)
        for index, chunk in enumerate(chunks):
            # Пауза между частями: ВК срабатывает флуд-контролем на очередь
            # сообщений подряд и может отбить длинный ответ целиком.
            if index:
                await asyncio.sleep(CHUNK_PAUSE)
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
        if peer_id >= CHAT_PEER_BASE:
            # Беседа. Бот личный: в общем чате он отвечал бы на каждую реплику
            # любого, кто попал в белый список, и жёг бы подписку.
            log.info("Игнорирую сообщение из беседы %s", peer_id)
            return
        if from_id not in self.config.allowed_user_ids:
            log.info("Игнорирую сообщение от %s (нет в ALLOWED_USER_IDS)", from_id)
            return

        message_id = int(message.get("id", 0))
        if self._is_duplicate(message_id):
            log.info("Повтор сообщения %s — пропускаю", message_id)
            return

        text = (message.get("text") or "").strip()
        if text.startswith("/"):
            handled = await self._command(peer_id, text)
            if handled:
                return

        has_content = bool(
            text
            or message.get("attachments")
            or message.get("fwd_messages")
            or message.get("reply_message")
        )
        if not has_content:
            return
        # Обычный вопрос между двумя /stop снимает запрос подтверждения: иначе
        # «стоп» спустя минуту разговора неожиданно выключил бы бота.
        self._stop_asked_at.pop(peer_id, None)
        job = Job(message=message, message_id=message_id)

        worker = self._worker(peer_id)
        if not worker.submit(job):
            await self.reply(
                peer_id,
                f"Больше {QUEUE_LIMIT} вопросов в очередь не приму — сначала разберусь "
                "с этими. Прервать текущий: /cancel",
            )
            return
        # Сообщаем об ожидании только на первом отложенном вопросе: иначе на
        # каждое следующее сообщение прилетал бы одинаковый ответ.
        if worker.busy and worker.queued == 1:
            await self.reply(peer_id, "Занят предыдущим вопросом — отвечу на этот следом.")

    def _worker(self, peer_id: int) -> PeerWorker:
        worker = self._workers.get(peer_id)
        if worker is None:
            worker = PeerWorker(peer_id, self)
            worker.start()
            self._workers[peer_id] = worker
        return worker

    async def _command(self, peer_id: int, text: str) -> bool:
        match = _COMMAND.match(text.split(maxsplit=1)[0])
        if match is None:
            # Сообщение начинается со слэша, но командой не является: путь
            # /usr/bin/env, дробь, адрес. Такое должно уйти Claude как вопрос,
            # а не получить в ответ «не знаю команду».
            return False
        command = match.group(1).lower()

        # Любая команда, кроме повторного /stop, снимает запрос подтверждения.
        if command != "stop":
            self._stop_asked_at.pop(peer_id, None)

        if command in {"help", "start", "помощь"}:
            await self.reply(peer_id, HELP_TEXT)
            return True

        if command in {"new", "reset", "новый"}:
            worker = self._workers.get(peer_id)
            if worker is not None:
                worker.cancel_current()
            stored = self.sessions.get(peer_id)
            had = self.sessions.reset(peer_id)
            if stored is not None:
                # Забыть разговор и оставить его транскрипт на диске — верный
                # способ незаметно засорить сервер.
                self._spawn(
                    claude_runner.forget_sessions(self.config.workspace, [stored.session_id])
                )
            await self.reply(
                peer_id,
                "Контекст сброшен, начинаем заново." if had else "Контекста и так не было. Спрашивай.",
            )
            return True

        if command in {"cancel", "отмена"}:
            worker = self._workers.get(peer_id)
            if worker is not None and worker.cancel_current():
                return True  # сообщение отправит сам воркер, поймав CancelledError
            await self.reply(peer_id, "Сейчас нечего прерывать.")
            return True

        if command in {"stop", "стоп"}:
            # Пока идёт ответ, «стоп» логичнее читать как «прекрати это»;
            # выключение имеет смысл только когда бот и так свободен.
            worker = self._workers.get(peer_id)
            if worker is not None and worker.cancel_current():
                return True  # сообщение отправит сам воркер, поймав CancelledError
            await self._stop(peer_id)
            return True

        if command in {"clear", "очистить"}:
            if not self._spawn_once("clear", self._clear(peer_id)):
                await self.reply(peer_id, "Уже чищу, подожди немного.")
            return True

        if command in {"limits", "лимиты"}:
            if not self._spawn_once("limits", self._limits_report(peer_id)):
                await self.reply(peer_id, "Уже узнаю лимиты, ответ будет через минуту.")
            return True

        if command in {"status", "статус"}:
            await self.reply(peer_id, self._status(peer_id))
            return True

        await self.reply(peer_id, f"Не знаю команду /{command}. Что умею — /help")
        return True

    async def _stop(self, peer_id: int) -> None:
        now = time.monotonic()
        asked_at = self._stop_asked_at.get(peer_id)
        confirmed = asked_at is not None and now - asked_at <= STOP_CONFIRM_WINDOW
        if not confirmed:
            self._stop_asked_at[peer_id] = now
            await self.reply(peer_id, STOP_CONFIRM_REPLY)
            return

        log.warning("Получена подтверждённая команда /stop — выключаюсь")
        with contextlib.suppress(Exception):
            await self.reply(peer_id, STOP_REPLY)
        self.killed = True
        self.shutdown.set()

    async def _clear(self, peer_id: int) -> None:
        await self.reply(peer_id, CLEAR_INTRO)

        for worker in self._workers.values():
            worker.cancel_current()

        forgotten = len(self.sessions.clear_all())
        # None — снести вообще все транскрипты в рабочей папке, включая те,
        # про которые state.json уже не помнит.
        transcripts = await claude_runner.forget_sessions(self.config.workspace, None)
        files = await asyncio.to_thread(self.media.purge_all)

        await self.reply(
            peer_id,
            "Готово, начинаем с нуля.\n"
            f"— разговоров забыто: {forgotten}\n"
            f"— транскриптов удалено: {transcripts}\n"
            f"— файлов удалено: {files}",
        )

    async def _limits_report(self, peer_id: int) -> None:
        fresh = self._limits and time.monotonic() - self._limits_at < 120

        if not fresh and any(worker.busy for worker in self._workers.values()):
            # Узнать лимиты можно только запросом к Claude, а это второй процесс
            # рядом с работающим. На сервере с гигабайтом памяти это верный OOM.
            if self._limits:
                await self.reply(
                    peer_id,
                    claude_runner.describe_limits(self._limits)
                    + "\n\nЭто данные с прошлого ответа — сейчас я занят вопросом.",
                )
            else:
                await self.reply(peer_id, "Сейчас занят вопросом, спроси лимиты после ответа.")
            return

        if not fresh:
            with contextlib.suppress(Exception):
                await self.vk.set_activity(peer_id)
            try:
                limits = await claude_runner.probe_rate_limits(
                    cwd=self.config.workspace, model=self.config.claude_model
                )
            except Exception as exc:  # noqa: BLE001
                await self.reply(peer_id, f"Не смог узнать лимиты: {exc}")
                return
            self.remember_limits(limits)

        await self.reply(peer_id, claude_runner.describe_limits(self._limits))

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
            age = stored.age_hours()
            line = f"Контекст: {stored.turns} ход(ов), последний {age:.0f} ч назад."
            if self.config.session_ttl_hours > 0:
                left = max(0.0, self.config.session_ttl_hours - age)
                line += f" Сам обнулится через {left / 24:.0f} дн."
            else:
                line += " Сам не обнуляется."
            lines.append(line)
        else:
            lines.append("Контекст пустой.")
        return "\n".join(lines)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for worker in self._workers.values():
            await worker.close()


def _build_prompt(messages: list[list[MessagePart]]) -> str:
    """Один вопрос из всех собранных сообщений.

    Сообщений может быть несколько: переслав что-то с телефона, человек тут же
    дописывает комментарий, и ВК шлёт это отдельными сообщениями. Для Claude это
    один вопрос, поэтому свой текст всех сообщений идёт подряд, а пересланное и
    записи со стены — блоками под ним.
    """
    own: list[str] = []
    blocks: list[str] = []
    files: list[str] = []
    notes: list[str] = []

    for parts in messages:
        for index, part in enumerate(parts):
            if index == 0:
                if part.text:
                    own.append(part.text)
            elif part.text or part.attachments:
                header = part.label or "Вложенное сообщение"
                blocks.append(f"{header}:\n{part.text}" if part.text else f"{header}: без текста")

            source = f" (из: {part.label.lower()})" if part.label else ""
            for item in part.attachments:
                if item.path is not None:
                    files.append(f"- {item.path}{source}")
                elif item.note:
                    notes.append(f"- {item.kind}: {item.note}{source}")

    out = ["\n".join(own) if own else "(сообщение без текста)"]
    out.extend(blocks)
    if files:
        out.append("Приложенные файлы (прочитай их инструментом Read):\n" + "\n".join(files))
    if notes:
        out.append("Прочие вложения:\n" + "\n".join(notes))

    return "\n\n".join(out)


async def run() -> None:
    config = load_config()

    if config.kill_flag.exists():
        log.warning(
            "Бот выключен стоп-фразой (%s). Чтобы включить: ./deploy/deploy.sh start",
            config.kill_flag,
        )
        return

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
            http,
            config.media_dir,
            config.max_attachment_bytes,
            config.media_ttl_days,
            config.max_media_total_bytes,
        )
        sessions = SessionStore(config.state_file)

        stop = asyncio.Event()
        _install_signal_handlers(stop)
        bot = Bot(config, vk, media, sessions, shutdown=stop)

        media.purge_once()
        await _prune_sessions(bot)
        purge_task = asyncio.create_task(media.purge_loop(), name="media-purge")
        prune_task = asyncio.create_task(_prune_loop(bot), name="session-prune")

        poller = asyncio.create_task(_poll(bot, vk), name="longpoll")
        await stop.wait()

        log.info("Останавливаюсь...")
        for task in (poller, purge_task, prune_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await bot.close()

        if bot.killed:
            config.kill_flag.write_text(
                f"Выключен стоп-фразой {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n",
                encoding="utf-8",
            )
            log.warning("Записан %s — при следующем запуске бот сразу выйдет", config.kill_flag)


async def _prune_sessions(bot: Bot) -> None:
    """Выбрасывает разговоры, в которых давно молчат, вместе с транскриптами."""
    stale = bot.sessions.prune(bot.config.session_ttl_hours)
    if stale:
        await claude_runner.forget_sessions(bot.config.workspace, stale)
    # Плюс всё, что осталось на диске само по себе: транскрипты от /new,
    # от проверок настройки и от прежних запусков бота. Активные разговоры
    # передаём отдельно — их не трогаем, каким бы ни был возраст файла.
    await claude_runner.forget_stale_sessions(
        bot.config.workspace,
        bot.config.session_ttl_hours,
        keep=set(bot.sessions.session_ids()),
    )


async def _prune_loop(bot: Bot, interval_seconds: int = 3600) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await _prune_sessions(bot)
        except Exception as exc:  # noqa: BLE001
            log.warning("Уборка разговоров не удалась: %s", exc)


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
