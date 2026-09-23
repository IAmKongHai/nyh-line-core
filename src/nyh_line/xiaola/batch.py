"""赢啦提交的批次计数。热更新限额时保留当前批已完成的单数。"""

from __future__ import annotations


class BatchController:
    def __init__(self, interval_time: float, max_tasks_per_batch: int, batch_interval: float):
        self.interval_time = interval_time
        self.max_tasks_per_batch = max_tasks_per_batch
        self.batch_interval = batch_interval
        self.tasks_in_current_batch = 0

    def replace_limits(self, interval_time: float, max_tasks_per_batch: int, batch_interval: float) -> None:
        """只换限额，不清空当前批计数。"""
        self.interval_time = interval_time
        self.max_tasks_per_batch = max_tasks_per_batch
        self.batch_interval = batch_interval

    def on_task_finished(self) -> tuple[float, bool]:
        self.tasks_in_current_batch += 1
        if self.tasks_in_current_batch >= self.max_tasks_per_batch:
            return self.batch_interval, True
        return self.interval_time, False

    def on_batch_interval_completed(self) -> None:
        self.tasks_in_current_batch = 0
