"""Context passed to every source: settings, HTTP client, local dataset DB."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field

from ..config import Settings
from ..http import Http


@dataclass
class Ctx:
    settings: Settings
    http: Http
    _db: sqlite3.Connection | None = field(default=None, repr=False)
    _db_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def db(self) -> sqlite3.Connection:
        """Local SQLite with bulk datasets (FAA registry, etc)."""
        with self._db_lock:
            if self._db is None:
                self._db = sqlite3.connect(self.settings.db_path, check_same_thread=False)
                self._db.row_factory = sqlite3.Row
            return self._db


def cite(name: str, url: str) -> dict[str, str]:
    return {"name": name, "url": url}
