"""Браузер: открыть страницу по-настоящему, посмотреть на неё и снять экран.

Зачем он вообще. WebFetch у Claude Code скачивает HTML и пересказывает его
текстом. Половина сайтов так не работает: содержимое рисует JavaScript, магазин
отдаёт заглушку без «настоящего» браузера, а картинки, вёрстку и графики текстом
не передать никак. Здесь запускается настоящий Chromium, страница рендерится,
и агент получает и текст, и список картинок, и снимок экрана, который читает
инструментом Read — то есть смотрит глазами.

Почему свой клиент CDP, а не Playwright. Playwright тянет за собой node-драйвер
(ещё один процесс на ~70 МБ) и свою сборку браузера на пол-гигабайта. На сервере
с 955 МБ памяти рядом с VPN это непозволительно, а нужно нам от протокола пять
команд. Chromium ставится системным пакетом в образ, общаемся с ним по
DevTools Protocol через обычный веб-сокет.

Память. Браузер поднимается на время запроса и гасится сразу после, вхолостую
не висит. Одновременно работает не больше одного (`_LOCK`): два Chromium рядом с
процессом Claude — это гарантированный OOM. Дочернему процессу поднимается
oom_score_adj: если памяти всё-таки не хватит, ядро должно выбрать браузер, а не
бота и не VPN.

Похожесть на настоящий браузер. Магазины и доски объявлений закрываются от
автоматики, и headless-Chromium из коробки виден насквозь: `navigator.webdriver`,
«HeadlessChrome» в User-Agent, ноль плагинов, SwiftShader вместо видеокарты,
одно ядро процессора. Мы не притворяемся человеком — мы убираем расхождения,
из-за которых страница отказывается рисоваться вообще. Самое грубое из них —
разъезд версий: подставленный User-Agent говорил «Chrome 131», а заголовки
клиентских подсказок, которые браузер шлёт сам, — «Chromium 151». Поэтому
User-Agent берётся у самого браузера (Browser.getVersion) и правится ровно в
одном месте: HeadlessChrome -> Chrome.

Часть сайтов вместо страницы показывает «Проверяем браузер» и через несколько
секунд пускает дальше. Раньше снимок доставался ровно от этой заглушки —
теперь она распознаётся, и браузер ждёт настоящую страницу.

Чего это не лечит: блокировку по адресу. Авито с IP чужого дата-центра отвечает
«Доступ ограничен: проблема с IP» независимо от того, как выглядит браузер. Тут
помогает только выход через другой адрес — на этот случай есть BROWSER_PROXY.

Сеть. Каждый запрос страницы проходит через Fetch.requestPaused и проверяется:
во внутреннюю сеть (метаданные облака, соседи по хосту) не пускаем никого —
ни саму страницу, ни её картинки, ни редиректы. Проверять только адрес, который
попросил агент, недостаточно: перенаправление увело бы браузер куда угодно.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .netcheck import resolves_to_private

log = logging.getLogger(__name__)


class BrowserUnavailable(RuntimeError):
    """Chromium не установлен — бот работает, просто без «глаз»."""


class BrowserError(RuntimeError):
    """Страница не открылась."""


# Где искать Chromium. CHROMIUM_PATH перекрывает всё — им же удобно проверять
# локально на Windows, где стоит Chrome или Edge.
_CANDIDATES = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
)

# Снимок делается кусками такой высоты. Причина в модели: картинку она видит
# после сжатия до ~1568 точек по длинной стороне, и лента высотой в четыре
# экрана превращается в нечитаемую полоску. Куски по высоте экрана остаются
# читаемыми.
SHOT_HEIGHT = 1600
VIEWPORT = (1280, 900)
# Сколько кусков снимать за один вызов. Больше — дороже по токенам и по
# времени; меньше — на длинной странице приходится звать инструмент лишний раз,
# а каждый раз это новый запуск браузера.
MAX_SHOTS = 4
# Сколько текста забирать со страницы. Больше — только раздувает контекст.
MAX_TEXT = 30_000
MAX_IMAGES = 30
MAX_LINKS = 40

# Чего браузеру грузить не нужно: видео и звук съедают память и трафик, а на
# снимке всё равно остаются чёрным прямоугольником.
_BLOCKED_TYPES = {"Media", "CSPViolationReport", "Ping"}

# Запасной User-Agent: настоящий спрашивается у браузера при подключении, а
# это значение идёт в скачивание файлов, где браузера нет.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"

# Сколько ждать, пока «Проверяем браузер» превратится в страницу.
CHALLENGE_WAIT = 20.0

# По этим словам видно, что вместо страницы показали проверку или отказ.
# Проверяется начало текста: в подвале нормальной страницы «captcha» может
# встретиться просто так.
_BLOCK_MARKERS = (
    ("проверяем браузер", "сайт проверяет браузер"),
    ("проверка браузера", "сайт проверяет браузер"),
    ("just a moment", "сайт проверяет браузер (Cloudflare)"),
    ("checking your browser", "сайт проверяет браузер"),
    ("enable javascript and cookies", "сайт требует включить JavaScript и куки"),
    ("attention required", "сайт заблокировал запрос (Cloudflare)"),
    ("доступ ограничен", "сайт закрыл доступ с этого адреса"),
    ("проблема с ip", "сайт закрыл доступ с этого адреса"),
    ("подтвердите, что вы не робот", "сайт просит пройти капчу"),
    ("вы не робот", "сайт просит пройти капчу"),
    ("i am not a robot", "сайт просит пройти капчу"),
    ("request could not be satisfied", "сайт отказал (защита CloudFront)"),
    ("access denied", "сайт отказал в доступе"),
    ("403 forbidden", "сайт отказал в доступе"),
    ("are you a robot", "сайт просит пройти капчу"),
    ("выключите vpn", "сайт закрыл доступ с этого адреса"),
    ("отключить vpn", "сайт закрыл доступ с этого адреса"),
    ("problem with your ip", "сайт закрыл доступ с этого адреса"),
    ("unusual traffic", "сайт счёл запросы подозрительными"),
    ("необычный трафик", "сайт счёл запросы подозрительными"),
)

# Правки, которые страница видит до собственных скриптов. Ни одна из них не
# делает бота человеком — они убирают следы автоматики, по которым страница
# отказывается открываться.
_STEALTH_JS = """
(() => {
  const hide = (obj, name, value) => {
    try { Object.defineProperty(obj, name, {get: () => value, configurable: true}); } catch (e) {}
  };
  // Свойство лежит на прототипе; подменённый геттер оставляет само имя на
  // месте, а часть проверок смотрит именно на наличие. Сначала удаляем.
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
  if ('webdriver' in navigator) { hide(navigator, 'webdriver', undefined); }
  hide(navigator, 'languages', ['ru-RU', 'ru', 'en-US', 'en']);
  hide(navigator, 'hardwareConcurrency', 8);
  hide(navigator, 'deviceMemory', 8);
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      hide(navigator, 'plugins', [1, 2, 3, 4, 5]);
    }
  } catch (e) {}
  try { window.chrome = window.chrome || {runtime: {}}; } catch (e) {}
  try {
    const original = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (parameter) {
      if (parameter === 37445) return 'Intel Inc.';
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';
      return original.call(this, parameter);
    };
  } catch (e) {}
  try {
    const query = navigator.permissions && navigator.permissions.query;
    if (query) {
      navigator.permissions.query = (parameters) =>
        parameters && parameters.name === 'notifications'
          ? Promise.resolve({state: Notification.permission})
          : query.call(navigator.permissions, parameters);
    }
  } catch (e) {}
})();
"""

# Один браузер за раз на весь процесс.
_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class PageImage:
    url: str
    alt: str
    width: int
    height: int


@dataclass
class Rendered:
    url: str
    title: str
    text: str
    status: int | None = None
    images: list[PageImage] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    shots: list[Path] = field(default_factory=list)
    text_truncated: bool = False
    page_height: int = 0
    # Что вместо страницы: проверка браузера, капча, отказ по адресу.
    blocked: str | None = None
    # С какой высоты начат снимок и сколько снято — для длинных страниц.
    shot_from: int = 0
    shot_to: int = 0


def find_chromium() -> str | None:
    explicit = os.environ.get("CHROMIUM_PATH", "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None
    for candidate in _CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("chromium") or shutil.which("chrome")


def available() -> bool:
    return find_chromium() is not None


# --- разговор по DevTools Protocol ----------------------------------------


class _Cdp:
    """Минимальный клиент CDP: послать команду, дождаться события."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._next_id = 0
        self._replies: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        self._handlers: dict[str, Any] = {}
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        with contextlib.suppress(Exception):
            await self._ws.close()

    def on(self, event: str, handler: Any) -> None:
        self._handlers[event] = handler

    async def _read_loop(self) -> None:
        async for message in self._ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(message.data)
            except ValueError:
                continue
            reply_id = data.get("id")
            if reply_id is not None:
                future = self._replies.pop(reply_id, None)
                if future is not None and not future.done():
                    future.set_result(data)
                continue
            event = data.get("method")
            if not event:
                continue
            for future in self._waiters.pop(event, []):
                if not future.done():
                    future.set_result(data.get("params") or {})
            handler = self._handlers.get(event)
            if handler is not None:
                # Обработчик может ходить в сеть (проверка адреса резолвит имя),
                # а цикл чтения останавливать нельзя: в нём же приходят ответы
                # на команды, которых обработчик и ждёт.
                asyncio.create_task(handler(data.get("params") or {}))

    async def send(
        self, method: str, params: dict[str, Any] | None = None,
        session_id: str | None = None, timeout: float = 30.0,
    ) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._replies[self._next_id] = future
        await self._ws.send_str(json.dumps(message))
        data = await asyncio.wait_for(future, timeout=timeout)
        if "error" in data:
            raise BrowserError(f"{method}: {data['error'].get('message', data['error'])}")
        return data.get("result") or {}

    def expect(self, event: str) -> asyncio.Future[dict[str, Any]]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(event, []).append(future)
        return future


# Что забрать со страницы. Возвращает всё одним объектом, чтобы не гонять
# несколько Runtime.evaluate подряд.
_EXTRACT_JS = """
(() => {
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return null; } };
  const seen = new Set();
  const images = [];
  const push = (src, alt, w, h) => {
    const url = abs(src);
    if (!url || seen.has(url) || url.startsWith('data:')) return;
    seen.add(url);
    images.push({url: url, alt: (alt || '').slice(0, 100), w: w || 0, h: h || 0});
  };
  const og = document.querySelector('meta[property="og:image"], meta[name="og:image"]');
  if (og && og.content) push(og.content, 'og:image', 0, 0);
  for (const img of Array.from(document.images)) {
    if (img.naturalWidth < 150 || img.naturalHeight < 150) continue;
    push(img.currentSrc || img.src, img.alt, img.naturalWidth, img.naturalHeight);
  }
  const links = [];
  const seenHref = new Set();
  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
    const href = abs(a.getAttribute('href'));
    if (!href || !/^https?:/.test(href) || seenHref.has(href)) continue;
    const text = (a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    if (!text) continue;
    seenHref.add(href);
    links.push([text, href]);
  }
  const body = document.body ? (document.body.innerText || '') : '';
  return {
    title: document.title || '',
    url: location.href,
    text: body.replace(/\\n{3,}/g, '\\n\\n'),
    images: images,
    links: links,
    height: Math.max(document.body ? document.body.scrollHeight : 0,
                     document.documentElement ? document.documentElement.scrollHeight : 0),
  };
})()
"""


async def _wait_port(user_data_dir: Path, process: asyncio.subprocess.Process, timeout: float) -> tuple[int, str]:
    """Ждёт, пока Chromium напишет порт отладки в DevToolsActivePort."""
    marker = user_data_dir / "DevToolsActivePort"
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise BrowserError(f"Chromium сразу завершился (код {process.returncode})")
        if marker.exists():
            try:
                lines = marker.read_text(encoding="utf-8").splitlines()
                if len(lines) >= 2 and lines[0].strip():
                    return int(lines[0].strip()), lines[1].strip()
            except (OSError, ValueError):
                pass
        await asyncio.sleep(0.1)
    raise BrowserError("Chromium не запустился за отведённое время")


def _launch_args(binary: str, user_data_dir: Path) -> list[str]:
    args = [
        binary,
        "--headless=new",
        "--remote-debugging-port=0",
        f"--user-data-dir={user_data_dir}",
        # В контейнере нет ни CAP_SYS_ADMIN, ни пользовательских namespace,
        # поэтому собственная песочница Chromium не поднимается. Границей
        # служит сам контейнер: чужого кода мы не запускаем, а страницу
        # смотрим и гасим.
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        # Без этого navigator.webdriver кричит о себе на каждой странице.
        "--disable-blink-features=AutomationControlled",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--metrics-recording-only",
        "--mute-audio",
        "--hide-scrollbars",
        "--force-color-profile=srgb",
        # Изоляция сайтов держит по процессу на домен — на нашей памяти это
        # непозволительная роскошь. Страницу мы открываем одну и сразу гасим.
        "--disable-features=Translate,BackForwardCache,IsolateOrigins,site-per-process,OptimizationHints,MediaRouter",
        "--disable-site-isolation-trials",
        "--js-flags=--max-old-space-size=128",
        f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
        "--lang=ru-RU,ru",
        f"--accept-lang={ACCEPT_LANGUAGE}",
        "about:blank",
    ]
    # BROWSER_PROXY — если браузеру нужен свой отдельный выход; иначе берём тот
    # же HTTPS_PROXY, которым ходят остальные запросы бота. Одна переменная на
    # всё — чтобы не получилось, что снимок идёт через прокси, а карточка
    # товара с того же сайта — напрямую с адреса сервера.
    proxy = (
        os.environ.get("BROWSER_PROXY", "").strip()
        or os.environ.get("HTTPS_PROXY", "").strip()
        or os.environ.get("https_proxy", "").strip()
    )
    if proxy:
        # Единственный способ открыть сайт, который закрылся от адреса сервера.
        # Задаёт его хозяин бота в .env, агент на это влиять не может.
        args.insert(-1, f"--proxy-server={proxy}")
    return args


def _agent_metadata(agent: str, product: str) -> dict[str, Any] | None:
    """Клиентские подсказки, согласованные с подставленным User-Agent."""
    match = re.search(r"Chrome/(\d+)", agent)
    if match is None:
        return None
    major = match.group(1)
    full = product.split("/", 1)[1] if "/" in product else f"{major}.0.0.0"
    brands = [
        {"brand": "Not_A Brand", "version": "24"},
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
    ]
    return {
        "brands": brands,
        "fullVersionList": [dict(item, version=full) for item in brands],
        "fullVersion": full,
        "platform": "Linux",
        "platformVersion": "6.8.0",
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }


def _raise_oom_priority() -> None:
    """Если памяти не хватит, ядро должно взять браузер, а не бота."""
    with contextlib.suppress(OSError):
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as handle:
            handle.write("1000")


async def render(
    url: str,
    *,
    out_dir: Path | None = None,
    prefix: str = "shot",
    shots: int = 0,
    full_page: bool = False,
    start_at: int = 0,
    want_details: bool = True,
    load_timeout: float = 30.0,
    settle: float = 1.5,
) -> Rendered:
    """Открывает страницу и возвращает всё, что с неё удалось снять.

    shots: сколько снимков экрана сохранить (0 — не снимать).
    full_page: снимать не только первый экран, но и то, что ниже.
    start_at: с какой высоты начинать снимок — чтобы досмотреть длинную страницу.
    """
    binary = find_chromium()
    if binary is None:
        raise BrowserUnavailable(
            "Chromium в этом окружении не установлен — смотреть страницы глазами не получится."
        )

    async with _LOCK:
        return await _render(
            binary, url, out_dir=out_dir, prefix=prefix, shots=shots,
            full_page=full_page, start_at=start_at, want_details=want_details,
            load_timeout=load_timeout, settle=settle,
        )


async def _render(
    binary: str,
    url: str,
    *,
    out_dir: Path | None,
    prefix: str,
    shots: int,
    full_page: bool,
    start_at: int,
    want_details: bool,
    load_timeout: float,
    settle: float,
) -> Rendered:
    user_data_dir = Path(tempfile.mkdtemp(prefix="vkbot-chrome-"))
    process = await asyncio.create_subprocess_exec(
        *_launch_args(binary, user_data_dir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=_raise_oom_priority if os.name == "posix" else None,  # noqa: PLW1509
    )

    session: aiohttp.ClientSession | None = None
    cdp: _Cdp | None = None
    try:
        port, path = await _wait_port(user_data_dir, process, timeout=25.0)
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(
            f"http://127.0.0.1:{port}{path}",
            max_msg_size=64 * 1024 * 1024,  # снимок экрана едет в base64 одним сообщением
            heartbeat=None,
            timeout=aiohttp.ClientWSTimeout(ws_close=10),
        )
        cdp = _Cdp(ws)

        target = await cdp.send("Target.createTarget", {"url": "about:blank"})
        attached = await cdp.send(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )
        sid = attached["sessionId"]

        # Кэш уже проверенных хостов. Имя отдельное, и это важно: обработчик
        # ниже держит его в замыкании и живёт до самого закрытия браузера, а
        # `blocked` дальше по функции получает причину отказа сайта. Пока имя
        # было общим, перепривязка затирала кэш строкой (или None), проверка
        # падала, и запрос оставался висеть непродолженным — картинки на снимке
        # молча оставались пустыми.
        checked: dict[str, bool] = {}
        status: dict[str, int] = {}

        async def on_paused(params: dict[str, Any]) -> None:
            request_id = params.get("requestId")
            target_url = (params.get("request") or {}).get("url") or ""
            try:
                if params.get("resourceType") in _BLOCKED_TYPES or await _is_forbidden(target_url, checked):
                    await cdp.send(
                        "Fetch.failRequest",
                        {"requestId": request_id, "errorReason": "BlockedByClient"},
                        session_id=sid, timeout=10,
                    )
                    return
                await cdp.send(
                    "Fetch.continueRequest", {"requestId": request_id}, session_id=sid, timeout=10
                )
            except Exception as exc:  # noqa: BLE001 — запрос мог отмениться сам
                log.debug("Fetch.requestPaused (%s): %s", target_url[:80], exc)

        async def on_response(params: dict[str, Any]) -> None:
            if params.get("type") == "Document" and "code" not in status:
                status["code"] = int((params.get("response") or {}).get("status") or 0)

        cdp.on("Fetch.requestPaused", on_paused)
        cdp.on("Network.responseReceived", on_response)

        await cdp.send("Page.enable", session_id=sid)
        await cdp.send("Network.enable", session_id=sid)
        await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session_id=sid)
        await cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": VIEWPORT[0], "height": VIEWPORT[1],
                "deviceScaleFactor": 1, "mobile": False,
                # Без этого screen остаётся 800x600 — окно больше экрана, чего
                # на настоящей машине не бывает.
                "screenWidth": 1920, "screenHeight": 1080,
                "positionX": 0, "positionY": 0,
            },
            session_id=sid,
        )
        # User-Agent берём у самого браузера и правим ровно одно слово. Своя
        # выдуманная строка разъезжается с заголовками клиентских подсказок,
        # которые Chromium шлёт сам, — и это расхождение видно любому защитному
        # скрипту лучше, чем честное «HeadlessChrome».
        agent = USER_AGENT
        # Пустой словарь заранее: если команда не ответит, ниже всё равно
        # спрашивают product, и без этого отказ getVersion ронял бы весь снимок
        # вместо того, чтобы откатиться на строку по умолчанию.
        version: dict[str, Any] = {}
        try:
            version = await cdp.send("Browser.getVersion", timeout=10)
            real = str(version.get("userAgent") or "")
            if real:
                agent = real.replace("HeadlessChrome", "Chrome")
        except Exception as exc:  # noqa: BLE001
            log.debug("Browser.getVersion не ответил: %s", exc)
        override: dict[str, Any] = {"userAgent": agent, "acceptLanguage": ACCEPT_LANGUAGE}
        metadata = _agent_metadata(agent, str(version.get("product") or ""))
        if metadata:
            # Заголовки Sec-CH-UA браузер формирует сам, и в headless-режиме в
            # них остаётся «HeadlessChrome» — при подменённом User-Agent это
            # прямое противоречие, заметное любому защитному скрипту.
            override["userAgentMetadata"] = metadata
        try:
            await cdp.send("Emulation.setUserAgentOverride", override, session_id=sid)
        except BrowserError:
            override.pop("userAgentMetadata", None)
            await cdp.send("Emulation.setUserAgentOverride", override, session_id=sid)
        with contextlib.suppress(Exception):
            await cdp.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _STEALTH_JS},
                session_id=sid,
            )

        load = cdp.expect("Page.loadEventFired")
        navigation = await cdp.send("Page.navigate", {"url": url}, session_id=sid, timeout=load_timeout)
        if navigation.get("errorText"):
            raise BrowserError(_explain(navigation["errorText"]))

        try:
            await asyncio.wait_for(load, timeout=load_timeout)
        except asyncio.TimeoutError:
            # Страница может «грузиться» бесконечно из-за счётчиков и рекламы,
            # но нарисована к этому моменту уже вся. Снимаем что есть.
            log.info("Страница %s не досказала о загрузке — снимаю как есть", url[:80])
        await asyncio.sleep(settle)

        if full_page:
            await _scroll_through(cdp, sid)

        # Сайт мог показать не страницу, а «Проверяем браузер». Такие заглушки
        # сами уходят через несколько секунд — ждём настоящее содержимое, иначе
        # снимок достанется ровно от заглушки.
        blocked = await _wait_out_challenge(cdp, sid)

        result = Rendered(url=url, title="", text="", status=status.get("code"), blocked=blocked)
        if want_details:
            evaluated = await cdp.send(
                "Runtime.evaluate",
                {"expression": _EXTRACT_JS, "returnByValue": True, "awaitPromise": False},
                session_id=sid,
            )
            payload = (evaluated.get("result") or {}).get("value") or {}
            text = str(payload.get("text") or "").strip()
            result.title = str(payload.get("title") or "").strip()
            result.url = str(payload.get("url") or url)
            result.text = text[:MAX_TEXT]
            result.text_truncated = len(text) > MAX_TEXT
            result.page_height = int(payload.get("height") or 0)
            result.images = [
                PageImage(
                    url=str(item.get("url")),
                    alt=str(item.get("alt") or ""),
                    width=int(item.get("w") or 0),
                    height=int(item.get("h") or 0),
                )
                for item in (payload.get("images") or [])[:MAX_IMAGES]
                if item.get("url")
            ]
            result.links = [
                (str(pair[0]), str(pair[1]))
                for pair in (payload.get("links") or [])[:MAX_LINKS]
                if isinstance(pair, list) and len(pair) == 2
            ]

        # Отказ не всегда выглядит как страница с текстом: Wildberries отдаёт
        # код 498 и пустое тело, Ozon — 403 с двумя строчками про VPN. Молчащая
        # пустая страница выглядела бы для агента как «сайт просто пустой».
        if result.blocked is None and result.status and result.status >= 400:
            result.blocked = _explain_status(result.status)

        if shots > 0 and out_dir is not None:
            result.shots, result.shot_from, result.shot_to = await _capture(
                cdp, sid, out_dir, prefix,
                shots=shots, full_page=full_page, start_at=start_at,
                page_height=result.page_height,
            )
        return result
    finally:
        if cdp is not None:
            with contextlib.suppress(Exception):
                await cdp.send("Browser.close", timeout=5)
            await cdp.close()
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
        await asyncio.to_thread(shutil.rmtree, user_data_dir, True)


async def _scroll_through(cdp: _Cdp, sid: str) -> None:
    """Проматывает страницу сверху донизу и возвращается наверх.

    Ленивые картинки грузятся, только когда до них домотали, а один прыжок в
    самый низ пропускает всё, что между: на длинной статье половина иллюстраций
    оставалась пустыми рамками. Поэтому идём шагами, но не больше десяти —
    бесконечная лента иначе будет мотаться вечно.
    """
    try:
        metrics = await cdp.send("Page.getLayoutMetrics", session_id=sid)
    except Exception:  # noqa: BLE001
        return
    content = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
    height = int(content.get("height") or 0)
    if height <= VIEWPORT[1]:
        return

    step = max(VIEWPORT[1], height // 10)
    position = 0
    while position < height and position < step * 10:
        position += step
        with contextlib.suppress(Exception):
            await cdp.send(
                "Runtime.evaluate",
                {"expression": f"window.scrollTo(0, {position}); 0"},
                session_id=sid,
                timeout=10,
            )
        await asyncio.sleep(0.25)
    with contextlib.suppress(Exception):
        await cdp.send(
            "Runtime.evaluate", {"expression": "window.scrollTo(0, 0); 0"}, session_id=sid, timeout=10
        )
    await asyncio.sleep(0.5)


async def _page_start(cdp: _Cdp, sid: str) -> str:
    """Заголовок и начало текста — по ним видно заглушку вместо страницы."""
    try:
        evaluated = await cdp.send(
            "Runtime.evaluate",
            {
                "expression": "document.title + \"\\n\" + "
                              "((document.body && document.body.innerText) || '').slice(0, 600)",
                "returnByValue": True,
            },
            session_id=sid,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        return ""
    return str((evaluated.get("result") or {}).get("value") or "").lower()


def _block_reason(head: str) -> str | None:
    for marker, reason in _BLOCK_MARKERS:
        if marker in head:
            return reason
    return None


async def _wait_out_challenge(cdp: _Cdp, sid: str) -> str | None:
    """Ждёт, пока проверка браузера уступит место странице.

    Возвращает причину, если так и не уступила: агент должен сказать человеку
    «сайт не пустил», а не пересказывать содержимое заглушки.
    """
    head = await _page_start(cdp, sid)
    reason = _block_reason(head)
    if reason is None:
        return None

    log.info("Страница показывает проверку: %s", reason)
    deadline = asyncio.get_running_loop().time() + CHALLENGE_WAIT
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(2.0)
        head = await _page_start(cdp, sid)
        again = _block_reason(head)
        if again is None:
            log.info("Проверка пройдена, страница открылась")
            return None
        reason = again
    return reason


async def _capture(
    cdp: _Cdp, sid: str, out_dir: Path, prefix: str, *,
    shots: int, full_page: bool, start_at: int, page_height: int,
) -> tuple[list[Path], int, int]:
    metrics = await cdp.send("Page.getLayoutMetrics", session_id=sid)
    content = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
    width = int(content.get("width") or VIEWPORT[0])
    width = max(320, min(width, VIEWPORT[0]))
    height = max(int(content.get("height") or 0), page_height)

    total = max(height, VIEWPORT[1])
    start = max(0, min(int(start_at or 0), max(0, total - 100)))
    if not full_page:
        pieces = [(start, VIEWPORT[1])]
    else:
        pieces = []
        offset = start
        while offset < total and len(pieces) < min(shots, MAX_SHOTS):
            pieces.append((offset, min(SHOT_HEIGHT, total - offset)))
            offset += SHOT_HEIGHT

    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for index, (offset, piece_height) in enumerate(pieces, 1):
        shot = await cdp.send(
            "Page.captureScreenshot",
            {
                # jpeg, а не png: снимок едет к модели картинкой, и лишние
                # мегабайты png — это только буфер SDK и токены.
                "format": "jpeg",
                "quality": 72,
                "captureBeyondViewport": True,
                "clip": {
                    "x": 0, "y": offset, "width": width,
                    "height": max(1, piece_height), "scale": 1,
                },
            },
            session_id=sid,
            timeout=60,
        )
        data = base64.b64decode(shot["data"])
        suffix = "" if len(pieces) == 1 else f"-{index}"
        path = out_dir / f"{prefix}{suffix}.jpg"
        await asyncio.to_thread(path.write_bytes, data)
        saved.append(path)
    last = pieces[-1] if pieces else (start, 0)
    return saved, start, last[0] + last[1]


async def _is_forbidden(url: str, cache: dict[str, bool]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in ("data", "blob", "about"):
        return False
    if parsed.scheme not in ("http", "https"):
        return True
    host = parsed.hostname or ""
    if not host:
        return True
    if host not in cache:
        cache[host] = await asyncio.to_thread(resolves_to_private, host)
        if cache[host]:
            log.warning("Браузеру отказано во внутреннюю сеть: %s", url[:100])
    return cache[host]


def _explain_status(status: int) -> str:
    """Код ответа словами. Авито отдаёт 439, Wildberries — 498, и оба при этом
    рисуют что-то похожее на страницу: без этой проверки агент принимал бы
    заглушку за содержимое."""
    if status in (401, 403, 429, 439, 498, 503):
        return (
            f"сайт закрыл доступ (код {status}) — обычно это блокировка по адресу "
            "сервера, а не поломка страницы"
        )
    if status in (404, 410):
        return f"страницы по этому адресу нет (код {status})"
    return f"сайт ответил кодом {status}, а не страницей"


def _explain(error_text: str) -> str:
    """Ошибка Chromium в понятных словах — она уедет прямо агенту."""
    known = {
        "net::ERR_NAME_NOT_RESOLVED": "такого домена не существует",
        "net::ERR_CONNECTION_REFUSED": "сервер отказал в соединении",
        "net::ERR_CONNECTION_TIMED_OUT": "сервер не отвечает",
        "net::ERR_CONNECTION_RESET": "сервер оборвал соединение",
        "net::ERR_CERT_AUTHORITY_INVALID": "у сайта недействительный сертификат",
        "net::ERR_CERT_COMMON_NAME_INVALID": "сертификат сайта выписан на другое имя",
        "net::ERR_BLOCKED_BY_CLIENT": "адрес заблокирован (внутренняя сеть)",
        "net::ERR_ABORTED": "загрузка прервалась",
    }
    for code, human in known.items():
        if code in error_text:
            return f"Страница не открылась: {human}."
    return f"Страница не открылась: {error_text}."
