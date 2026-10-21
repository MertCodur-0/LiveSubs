"""Translation cache: an in-memory LRU in front of SQLite.

Key: (model, profile, normalized source text). The dialogue context is deliberately left out of
the key: a repeated line ("Get in the car!") then comes back instantly, at the cost of ignoring
that its context differs.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from livesubs.engine.text.normalize import key as text_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    model TEXT NOT NULL,
    profile TEXT NOT NULL,
    key TEXT NOT NULL,
    source TEXT NOT NULL,
    translation TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (model, profile, key)
)
"""

CacheKey = tuple[str, str, str]


class TranslationCache:
    def __init__(self, path: Path | None = None, memory_size: int = 2000) -> None:
        """``path=None`` keeps everything in memory (tests, or a disabled disk cache)."""
        self._lock = threading.Lock()
        self._memory: OrderedDict[CacheKey, str] = OrderedDict()
        self._memory_size = memory_size
        self._db: sqlite3.Connection | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(_SCHEMA)

    @staticmethod
    def make_key(model: str, profile: str, text: str) -> CacheKey:
        return model, profile, text_key(text)

    def get(self, model: str, profile: str, text: str) -> str | None:
        cache_key = self.make_key(model, profile, text)
        with self._lock:
            if (hit := self._memory.get(cache_key)) is not None:
                self._memory.move_to_end(cache_key)
                return hit
            if self._db is None:
                return None
            row = self._db.execute(
                "SELECT translation FROM translations WHERE model=? AND profile=? AND key=?",
                cache_key,
            ).fetchone()
            if row is None:
                return None
            self._remember(cache_key, row[0])
            return row[0]

    def put(self, model: str, profile: str, source: str, translation: str) -> None:
        cache_key = self.make_key(model, profile, source)
        with self._lock:
            self._remember(cache_key, translation)
            if self._db is not None:
                self._db.execute(
                    "INSERT OR REPLACE INTO translations VALUES (?, ?, ?, ?, ?, ?)",
                    (*cache_key, source, translation, time.time()),
                )

    def clear(self, model: str | None = None) -> None:
        """Forget everything, or one model's translations (after changing its prompt/settings)."""
        with self._lock:
            if model is None:
                self._memory.clear()
            else:
                for cache_key in [k for k in self._memory if k[0] == model]:
                    del self._memory[cache_key]
            if self._db is not None:
                if model is None:
                    self._db.execute("DELETE FROM translations")
                else:
                    self._db.execute("DELETE FROM translations WHERE model=?", (model,))

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _remember(self, cache_key: CacheKey, translation: str) -> None:
        self._memory[cache_key] = translation
        self._memory.move_to_end(cache_key)
        while len(self._memory) > self._memory_size:
            self._memory.popitem(last=False)
