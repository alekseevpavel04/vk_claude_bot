"""Приведение картинки к тому, что ВК примет фотографией.

Половина современного веба отдаёт webp (Wildberries — только его), а ВКонтакте
берёт в сообщение jpg, png и gif. Без пересчёта «пришли фото вот отсюда»
превращалось бы в «держи непонятный файл документом».

Заодно тут же ужимается всё слишком большое: ВК не берёт стороны больше
нескольких тысяч точек, а телефону такую картинку всё равно не показать.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Что ВК берёт как фотографию в сообщении.
VK_PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}

# Потолок стороны. Больше ВК не примет, а на телефоне разницы не видно.
MAX_SIDE = 3000
# Выше этого веса пережимаем даже нормальный jpg.
MAX_BYTES = 8 * 1024 * 1024

JPEG_QUALITY = 88


def prepare_for_vk(path: Path) -> tuple[Path, str | None]:
    """Готовит файл к отправке фотографией.

    Возвращает путь (может быть новым файлом рядом) и пояснение, если что-то
    поменялось. Если картинку не открыть — возвращает исходный путь и None:
    решать, что делать дальше, будет вызывающий, а не исключение отсюда.
    """
    extension = path.suffix.lower().lstrip(".")
    try:
        size = path.stat().st_size
    except OSError:
        return path, None

    fits = extension in VK_PHOTO_EXTENSIONS and size <= MAX_BYTES
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover — в образе Pillow есть
        log.warning("Pillow не установлен, картинка идёт как есть")
        return path, None

    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            if fits and max(width, height) <= MAX_SIDE:
                return path, None

            note_parts: list[str] = []
            if max(width, height) > MAX_SIDE:
                image.thumbnail((MAX_SIDE, MAX_SIDE))
                note_parts.append(f"уменьшено до {image.size[0]}x{image.size[1]}")
            if extension not in VK_PHOTO_EXTENSIONS:
                note_parts.append(f"переведено из {extension} в jpg")

            # Прозрачность в jpg не переносится — подкладываем белый фон, иначе
            # прозрачные места станут чёрными.
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.split()[-1])
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            target = path.with_suffix(".jpg")
            if target == path:
                target = path.with_name(path.stem + "-vk.jpg")
            image.save(target, "JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception as exc:  # noqa: BLE001 — не картинка или битый файл
        log.info("Не удалось пересчитать %s: %s", path.name, exc)
        return path, None

    log.info("Картинка подготовлена для ВК: %s -> %s", path.name, target.name)
    return target, ", ".join(note_parts) or None
