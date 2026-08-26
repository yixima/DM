"""送信ペースの制御。

大量配信で最も高くつく失敗は「速く送りすぎて迷惑メール判定されること」と
「相手サイトに負荷をかけること」。ここで一括して間隔を管理する。
"""
from __future__ import annotations

import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo


class Pacer:
    """1件ごとに最低 interval 秒 + ゆらぎ を空ける。"""

    def __init__(self, min_seconds: float, jitter_seconds: float = 0.0, max_per_hour: int | None = None) -> None:
        self.min_seconds = max(0.0, min_seconds)
        self.jitter_seconds = max(0.0, jitter_seconds)
        self.max_per_hour = max_per_hour
        self._last: float | None = None
        self._window: list[float] = []

    def wait(self) -> None:
        now = time.monotonic()
        if self._last is not None:
            delay = self.min_seconds + random.uniform(0, self.jitter_seconds)
            remaining = delay - (now - self._last)
            if remaining > 0:
                time.sleep(remaining)
        if self.max_per_hour:
            self._respect_hourly_cap()
        self._last = time.monotonic()
        self._window.append(self._last)

    def _respect_hourly_cap(self) -> None:
        assert self.max_per_hour
        cutoff = time.monotonic() - 3600
        self._window = [t for t in self._window if t >= cutoff]
        if len(self._window) >= self.max_per_hour:
            sleep_for = 3600 - (time.monotonic() - self._window[0]) + 1
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._window = [t for t in self._window if t >= time.monotonic() - 3600]


def in_quiet_hours(quiet: tuple[int, int], tz_name: str = "Asia/Tokyo", now: datetime | None = None) -> bool:
    """深夜・早朝の送信を避ける。start > end の場合は日をまたぐ区間として扱う。"""
    start, end = quiet
    if start == end:
        return False
    current = (now or datetime.now(ZoneInfo(tz_name))).astimezone(ZoneInfo(tz_name)).hour
    if start < end:
        return start <= current < end
    return current >= start or current < end
