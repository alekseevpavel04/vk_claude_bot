"""Хранилище id сессий Claude — по одной на собеседника.

Сам транскрипт диалога пишет SDK (в ~/.claude/projects/...); здесь лежит только
привязка «peer_id ВК -> session_id Claude», чтобы контекст переживал рестарт
сервиса.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    session_id: str
    updated_at: str
    turns: int = 0

    def age_hours(self) -> float:
        """Сколько часов прошло с последнего хода. inf, если время не разобрать."""
        try:
            stamp = datetime.fromisoformat(self.updated_at)
        except ValueError:
            return float("inf")
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds() / 3600


class SessionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, SessionInfo] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Не удалось прочитать %s (%s), начинаю с чистого состояния", self._path, exc)
            return
        for peer, item in raw.items():
            if isinstance(item, dict) and item.get("session_id"):
                self._data[peer] = SessionInfo(
                    session_id=item["session_id"],
                    updated_at=item.get("updated_at", ""),
                    turns=int(item.get("turns", 0)),
                )

    def _save(self) -> None:
        payload = {
            peer: {
                "session_id": info.session_id,
                "updated_at": info.updated_at,
                "turns": info.turns,
            }
            for peer, info in self._data.items()
        }
        # Пишем через временный файл, чтобы падение посреди записи не оставило
        # обрезанный JSON.
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, peer_id: int) -> SessionInfo | None:
        return self._data.get(str(peer_id))

    def remember(self, peer_id: int, session_id: str) -> None:
        key = str(peer_id)
        previous = self._data.get(key)
        turns = previous.turns + 1 if previous and previous.session_id == session_id else 1
        self._data[key] = SessionInfo(
            session_id=session_id,
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            turns=turns,
        )
        self._save()

    def reset(self, peer_id: int) -> bool:
        removed = self._data.pop(str(peer_id), None) is not None
        if removed:
            self._save()
        return removed

    def session_ids(self) -> list[str]:
        return [info.session_id for info in self._data.values()]

    def clear_all(self) -> list[str]:
        """Забывает все разговоры. Возвращает id забытых сессий."""
        removed = self.session_ids()
        self._data.clear()
        self._save()
        return removed

    def prune(self, ttl_hours: int) -> list[str]:
        """Выбрасывает разговоры, в которых давно не писали."""
        if ttl_hours <= 0:
            return []
        stale = {
            peer: info for peer, info in self._data.items() if info.age_hours() > ttl_hours
        }
        for peer in stale:
            del self._data[peer]
        if stale:
            self._save()
            log.info("Просрочено разговоров: %s", len(stale))
        return [info.session_id for info in stale.values()]
