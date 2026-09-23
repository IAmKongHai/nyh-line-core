"""查单进程的余额节流。解析失败不写 0，60 秒内不再查询。"""

from __future__ import annotations


class BalanceGate:
    """记录上一次查询时刻。失败和成功都占用这一次名额。"""

    def __init__(self, interval_seconds: float = 60):
        self.interval_seconds = interval_seconds
        self.last_attempt_at = None

    def tick(self, now: float, query, sync) -> str:
        if self.last_attempt_at is not None and now - self.last_attempt_at < self.interval_seconds:
            return "skipped"
        self.last_attempt_at = now
        try:
            amount = query()
        except Exception:
            return "parse_failed"
        if amount is None or str(amount).strip() == "":
            return "parse_failed"
        sync(amount)
        return "synced"
