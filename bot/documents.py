"""Документы: текст и картинки из .docx, страницы PDF картинкой.

PDF рисует pdftoppm из poppler-utils (системный пакет в образе). Тот же
pdftoppm нужен и самому Read: без него Read с параметром pages отвечает
«pdftoppm is not installed», и бот говорил, что «отрисовка PDF не работает».

Read у Claude Code понимает картинки, PDF и текст, а .docx для него — архив
с непонятным содержимым. Конвертер вроде LibreOffice решил бы это целиком,
вместе с вёрсткой, но это полгигабайта в образе и сотни мегабайт памяти на
сервере, где 955 МБ на всё и рядом VPN. А .docx — это zip с XML внутри:
текст достаётся стандартной библиотекой, картинки лежат в нём готовыми файлами.

Вёрстку так не увидеть, только содержимое. Для «в чём суть работы» этого
хватает; снимок страницы документа — только если человек сохранит его в PDF.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import textwrap
import zipfile
from pathlib import Path
from xml.etree import ElementTree

log = logging.getLogger(__name__)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Защита от zip-бомбы: сколько XML документа готовы распаковать.
MAX_XML_BYTES = 40 * 1024 * 1024
# Read читает текст кусками по 2000 строк, длинные строки обрезает. Абзацы
# режем на строки, а весь текст ограничиваем: дипломная работа влезает с
# запасом, а мегабайтный дамп таблиц не раздует контекст.
MAX_TEXT_CHARS = 300_000
LINE_WIDTH = 500
# Картинки достаём, чтобы агент мог на них посмотреть. Больше дюжины в одном
# документе — это уже не иллюстрации к тексту, а альбом, и диск общий.
MAX_IMAGES = 12
# И общий вес картинок: размер внутри архива объявляет сам файл, так что
# потолок на каждую по отдельности документ-бомбу не остановит.
MAX_IMAGES_TOTAL = 30 * 1024 * 1024
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}


# Страница PDF картинкой: длинная сторона в точках. Больше модель всё равно
# не увидит — картинку она получает ужатой до ~1568 точек.
PDF_PAGE_SIZE = 1600
PDF_TIMEOUT = 60


class DocumentError(ValueError):
    """Файл не разобрался: битый, не тот формат или слишком велик."""


async def render_pdf_page(source: Path, page: int, target: Path) -> Path:
    """Рисует одну страницу PDF в PNG. Возвращает путь к картинке."""
    prefix = target.with_suffix("")
    code, err = await _run(
        "pdftoppm", "-png", "-singlefile", "-f", str(page), "-l", str(page),
        "-scale-to", str(PDF_PAGE_SIZE), str(source), str(prefix),
    )
    result = prefix.with_suffix(".png")
    if code == 0 and result.is_file():
        return result
    result.unlink(missing_ok=True)
    pages = await pdf_page_count(source)
    if pages is not None and page > pages:
        raise DocumentError(f"в документе {pages} стр., страницы {page} нет")
    raise DocumentError(f"страница не отрисовалась: {err or f'код {code}'}")


async def pdf_page_count(source: Path) -> int | None:
    code, out = await _run("pdfinfo", str(source))
    match = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE) if code == 0 else None
    return int(match.group(1)) if match else None


async def _run(*command: str) -> tuple[int, str]:
    """Запускает утилиту poppler с таймаутом. Возвращает код и вывод."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=_raise_oom_priority if os.name == "posix" else None,  # noqa: PLW1509
        )
    except FileNotFoundError as exc:
        raise DocumentError(f"{command[0]} не установлен") from exc
    try:
        out, _ = await asyncio.wait_for(process.communicate(), PDF_TIMEOUT)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise DocumentError("документ рисуется слишком долго") from exc
    return process.returncode or 0, out.decode("utf-8", "replace").strip()[-300:]


def _raise_oom_priority() -> None:
    """Если памяти не хватит, ядро должно взять отрисовку, а не бота и не VPN."""
    with contextlib.suppress(OSError):
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as handle:
            handle.write("1000")


def extract_docx(source: Path, max_image_bytes: int) -> tuple[Path, list[Path]]:
    """Пишет рядом с файлом .txt с текстом и достаёт картинки.

    Возвращает путь к тексту и список картинок. Всё ложится в ту же папку,
    что и сам документ, — то есть в папку вложений с её уборкой и потолком.
    """
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise DocumentError("файл не похож на документ Word (.docx)") from exc

    with archive:
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError as exc:
            raise DocumentError("в архиве нет текста документа") from exc
        if info.file_size > MAX_XML_BYTES:
            raise DocumentError("текст документа слишком велик")
        root = ElementTree.fromstring(archive.read(info))
        text = _body_text(root)

        images: list[Path] = []
        media = [
            one for one in archive.infolist()
            if one.filename.startswith("word/media/")
            and one.filename.rsplit(".", 1)[-1].lower() in IMAGE_EXTENSIONS
            and 0 < one.file_size <= max_image_bytes
        ]
        # image2 раньше image10: в архиве они по алфавиту, а в документе по номеру.
        media.sort(key=lambda one: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", one.filename)])
        total = 0
        for number, one in enumerate(media[:MAX_IMAGES], start=1):
            total += one.file_size
            if total > MAX_IMAGES_TOTAL:
                break
            ext = one.filename.rsplit(".", 1)[-1].lower()
            target = source.with_name(f"{source.stem}-img{number}.{ext}")
            target.write_bytes(archive.read(one))
            images.append(target)

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n\n[дальше текст обрезан: документ слишком длинный]"
    header = [
        f"Текст документа {source.name}, извлечён из .docx (без вёрстки).",
        f"Исходный файл, если его надо переслать: {source}",
    ]
    if images:
        header.append(
            f"Картинок в документе: {len(media)}, сохранены первые {len(images)}. "
            "Посмотреть картинку — Read по пути:"
        )
        header.extend(f"  {path}" for path in images)
    if not text.strip():
        text = "[текста в документе нет — возможно, он целиком из картинок]"

    target = source.with_suffix(".txt")
    target.write_text("\n".join(header) + "\n\n" + text + "\n", encoding="utf-8")
    log.info("Разобран %s: %s символов, картинок %s", source.name, len(text), len(images))
    return target, images


def _body_text(root: ElementTree.Element) -> str:
    """Абзацы и таблицы по порядку, как они идут в документе."""
    body = root.find(f"{_W}body")
    if body is None:
        return ""
    lines: list[str] = []
    for block in body:
        if block.tag == f"{_W}p":
            _add(lines, _paragraph(block))
        elif block.tag == f"{_W}tbl":
            lines.append("")
            for row in block.iter(f"{_W}tr"):
                cells = [
                    " ".join(_paragraph(p) for p in cell.iter(f"{_W}p")).strip()
                    for cell in row.findall(f"{_W}tc")
                ]
                _add(lines, " | ".join(cells))
            lines.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _paragraph(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts)


def _add(lines: list[str], text: str) -> None:
    # Пустой абзац — это отбивка в документе, её сохраняем одной пустой строкой.
    if not text.strip():
        lines.append("")
        return
    for piece in text.split("\n"):
        lines.extend(textwrap.wrap(piece, LINE_WIDTH, break_long_words=True) or [""])
