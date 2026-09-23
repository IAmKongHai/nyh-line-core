"""赢啦拦截名单。用 Center 下发的原始号码做精确匹配。"""

from __future__ import annotations

import os
import threading
import time


class Blocklist:
    """文件缺失或读失败时保持空集合，调用方继续下单。默认 900 秒看一次 mtime。"""

    def __init__(self, file_path: str, reload_interval_seconds: int = 900, clock=None):
        self.file_path = file_path
        self.reload_interval_seconds = max(1, int(reload_interval_seconds or 900))
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._numbers: set[str] = set()
        self._last_loaded_at = 0.0
        self._last_mtime = 0.0
        self._load()

    def _load(self) -> None:
        numbers: set[str] = set()
        try:
            mtime = os.path.getmtime(self.file_path)
        except OSError:
            mtime = 0.0
        if mtime > 0:
            try:
                with open(self.file_path, "r", encoding="utf-8") as handle:
                    for raw_line in handle:
                        line = raw_line.strip()
                        if not line or line.startswith("#") or line.startswith(";"):
                            continue
                        numbers.add(line)
            except OSError:
                numbers = set()
                mtime = 0.0
        with self._lock:
            self._numbers = numbers
            self._last_mtime = mtime
            self._last_loaded_at = self.clock()

    def reload_if_needed(self) -> None:
        now = self.clock()
        if now - self._last_loaded_at < self.reload_interval_seconds:
            return
        try:
            current_mtime = os.path.getmtime(self.file_path)
        except OSError:
            current_mtime = 0.0
        if current_mtime == self._last_mtime:
            with self._lock:
                self._last_loaded_at = now
            return
        self._load()

    def is_blocked(self, phone_number: str | None) -> bool:
        if phone_number is None:
            return False
        self.reload_if_needed()
        candidate = str(phone_number).strip()
        with self._lock:
            return candidate in self._numbers
