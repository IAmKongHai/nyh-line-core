"""进程循环：空闲等待、停止信号、提交线程死掉后再拉起。"""

from __future__ import annotations

import logging
import random
import signal
import threading
import time

from nyh_line.policy import LinePolicy, policy_for
from nyh_line.xiaola.batch import BatchController

logger = logging.getLogger(__name__)

QUIET_CODES = {"6003", "120", "5001"}


class PollLoop:
    """拉一次、处理一次。停止之后不再拉新单。"""

    def __init__(self, policy: LinePolicy, pull, handle, sleep=None, randint=None, stop=None, log=None):
        self.policy = policy
        self.pull = pull
        self.handle = handle
        self.sleep = sleep or time.sleep
        self.randint = randint or random.randint
        self.stop = stop or threading.Event()
        self.log = log or logger

    def handle_sigterm(self, signum, frame) -> None:
        if signum != signal.SIGTERM:
            return
        self.stop.set()

    def _note_idle(self, code) -> None:
        if str(code) not in QUIET_CODES:
            self.log.error("拉单失败 code=%s", code)

    def run_round(self) -> str:
        if self.stop.is_set():
            return "stopped"
        pulled = self.pull()
        if not isinstance(pulled, dict) or str(pulled.get("code")) != "0":
            code = pulled.get("code") if isinstance(pulled, dict) else -1
            self._note_idle(code)
            self.sleep(self.randint(self.policy.idle_low, self.policy.idle_high))
            return "idle"
        self.handle(pulled.get("data") or {})
        if self.policy.busy_sleep:
            self.sleep(self.policy.busy_sleep)
        return "worked"


def install_signals(loop: PollLoop) -> None:
    signal.signal(signal.SIGTERM, loop.handle_sigterm)


class ThreadKeeper:
    """把活着的工作线程补到配置的数量。"""

    def __init__(self, count: int, worker_factory, stop: threading.Event):
        self.count = count
        self.worker_factory = worker_factory
        self.stop = stop
        self.workers: list[threading.Thread] = []

    def reconcile(self) -> int:
        self.workers = [worker for worker in self.workers if worker.is_alive()]
        while len(self.workers) < self.count and not self.stop.is_set():
            thread = threading.Thread(target=self.worker_factory(), daemon=True)
            thread.start()
            self.workers.append(thread)
        return sum(1 for worker in self.workers if worker.is_alive())


def maintain(line: str, role: str, worker_factory, stop: threading.Event) -> ThreadKeeper:
    policy = policy_for(line, role)
    return ThreadKeeper(policy.threads, worker_factory, stop)


def wait_or_stop(stop: threading.Event, seconds: float, sleeper=None) -> bool:
    """等待批间隔。返回 False 表示停止信号已经打断这次等待。"""
    if sleeper is not None:
        return bool(sleeper(seconds))
    if stop.is_set():
        return False
    interrupted = stop.wait(timeout=seconds)
    return not interrupted


class XiaolaSubmitLoop:
    """赢啦提交循环。批次没走完之前不再拉下一单。"""

    def __init__(self, *, country: str, batch: BatchController, pull, handle, stop, wait=None):
        self.country = country or ""
        self.batch = batch
        self.pull = pull
        self.handle = handle
        self.stop = stop
        self.wait = wait or (lambda seconds: wait_or_stop(stop, seconds))
        self.waiting_batch = False
        self.pending_delay = 0

    def advance(self) -> str:
        if self.stop.is_set():
            return "stopped"
        if self.waiting_batch:
            finished = self.wait(self.pending_delay)
            if not finished or self.stop.is_set():
                return "interrupted"
            self.batch.on_batch_interval_completed()
            self.waiting_batch = False
            return "batch-gap-done"
        if not self.country.strip():
            return "no-country"
        pulled = self.pull()
        if not isinstance(pulled, dict) or str(pulled.get("code")) != "0":
            return "idle"
        self.handle(pulled.get("data") or {})
        delay, batch_full = self.batch.on_task_finished()
        self.pending_delay = delay
        if batch_full:
            self.waiting_batch = True
        return "pulled"


def apply_batch_config(batch: BatchController, interval_time, max_tasks_per_batch, batch_interval) -> None:
    """热更新只换限额。"""
    batch.replace_limits(interval_time, max_tasks_per_batch, batch_interval)
