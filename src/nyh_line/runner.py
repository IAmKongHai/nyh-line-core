"""进程循环：空闲等待、停止信号、每笔防护、线程死掉后退避再拉起。"""

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


def task_id_of(item):
    """提交任务带 task_id，查单记录带 id。取不到时返回 None。"""
    if isinstance(item, dict):
        return item.get("task_id", item.get("id"))
    return None


def run_one(handle, item, *, task_id, pause=None, backoff: float = 3, log=None):
    """处理一笔。意外异常只影响这一笔：记 ERROR（带 task_id），可打断地退避后返回 "crashed"。

    只捕获 Exception，不吞 SystemExit 和 KeyboardInterrupt。这里从不 Feedback：
    分不清异常发生在请求发出之前还是之后。
    """
    try:
        return handle(item)
    except Exception:
        shown = "unknown" if task_id in (None, "") else task_id
        (log or logger).exception("处理这一笔时出错，跳过 task_id=%s", shown)
        if pause is not None:
            pause(backoff)
        return "crashed"


def respawn_delay(crashes: int) -> float:
    """第 n 次重新拉起前的退避秒数，与旧赢啦 main.py 一致。"""
    return min(30, 2 * crashes)


class PollLoop:
    """拉一次、处理一次。停止之后不再拉新单。每笔 handle 都经过 run_one。"""

    def __init__(
        self, policy: LinePolicy, pull, handle, sleep=None, randint=None, stop=None, log=None, wait=None, before_round=None
    ):
        self.policy = policy
        self.pull = pull
        self.handle = handle
        self.sleep = sleep or time.sleep
        self.randint = randint or random.randint
        self.stop = stop or threading.Event()
        self.log = log or logger
        self.wait = wait or (lambda seconds: wait_or_stop(self.stop, seconds))
        # 每轮拉单前做的事（查单的余额同步）。失败只记日志，不退避。
        self.before_round = before_round

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
        data = pulled.get("data") or {}
        _note_pulled(self.log, data)
        self.guard(self.handle, data)
        if self.policy.busy_sleep:
            self.sleep(self.policy.busy_sleep)
        return "worked"

    def guard(self, handle, item):
        """按本线路的退避时长包住一笔。查单的 handle 用它逐条包住记录。"""
        return run_one(
            handle,
            item,
            task_id=task_id_of(item),
            pause=self.wait,
            backoff=self.policy.error_backoff,
            log=self.log,
        )

    def run_cycle(self):
        """跑一轮。拉单本身抛错也只让这一轮退避，不让线程或进程退出。"""
        if self.before_round is not None:
            run_one(lambda _item: self.before_round(), None, task_id=None, log=self.log)
        return run_one(
            lambda _item: self.run_round(),
            None,
            task_id=None,
            pause=self.wait,
            backoff=self.policy.error_backoff,
            log=self.log,
        )

    def run_forever(self) -> None:
        while not self.stop.is_set():
            self.run_cycle()


def _note_pulled(log, data) -> None:
    """提交拉到一笔时记 task_id 和手机号；查单拉到一批时记条数。"""
    if isinstance(data, dict):
        log.info("拉到任务 task_id=%s phone=%s", data.get("task_id"), data.get("phone_number"))
    elif isinstance(data, list):
        log.info("取到执行中记录 %s 条", len(data))


def install_signals(loop: PollLoop) -> None:
    signal.signal(signal.SIGTERM, loop.handle_sigterm)


class ThreadKeeper:
    """把活着的工作线程补到配置的数量。重新拉起的线程先按 min(30, 2n) 秒退避再开工。"""

    def __init__(self, count: int, worker_factory, stop: threading.Event, wait=None, name: str = "worker"):
        self.count = count
        self.worker_factory = worker_factory
        self.stop = stop
        self.wait = wait or (lambda seconds: wait_or_stop(stop, seconds))
        self.name = name
        self.workers: list[threading.Thread] = []
        self.started = 0
        self.respawns = 0

    def _delayed(self, target, delay: float):
        def run():
            if delay and not self.wait(delay):
                return
            target()

        return run

    def reconcile(self) -> int:
        self.workers = [worker for worker in self.workers if worker.is_alive()]
        while len(self.workers) < self.count and not self.stop.is_set():
            delay = 0
            if self.started >= self.count:
                self.respawns += 1
                delay = respawn_delay(self.respawns)
                logger.error("工作线程退出，%s 秒后重新拉起（第 %s 次）", delay, self.respawns)
            self.started += 1
            thread = threading.Thread(
                target=self._delayed(self.worker_factory(), delay),
                daemon=True,
                name=f"{self.name}-{self.started}",
            )
            thread.start()
            self.workers.append(thread)
        return sum(1 for worker in self.workers if worker.is_alive())


def maintain(line: str, role: str, worker_factory, stop: threading.Event, wait=None) -> ThreadKeeper:
    policy = policy_for(line, role)
    return ThreadKeeper(policy.threads, worker_factory, stop, wait=wait, name=f"{line}-{role}")


class CountrySlot:
    """赢啦一个 (thread_id, country) 槽位。

    work 因意外异常退出时，按 min(30, 2n) 秒退避后以同一国家重新开工，批次控制器挂在槽位上沿用，
    计数不归零。work 正常返回（stopped、interrupted、no-country）时槽位结束，不再拉起。
    """

    def __init__(self, thread_id: int, country: str, batch: BatchController, work, stop: threading.Event, wait=None):
        self.thread_id = thread_id
        self.country = country
        self.batch = batch
        self.work = work
        self.stop = stop
        self.wait = wait or (lambda seconds: wait_or_stop(stop, seconds))
        self.crashes = 0

    def run(self) -> str:
        while not self.stop.is_set():
            try:
                return self.work(self)
            except Exception:
                self.crashes += 1
                delay = respawn_delay(self.crashes)
                logger.exception("赢啦 %s 线程退出，%s 秒后重新拉起（第 %s 次）", self.country, delay, self.crashes)
                if not self.wait(delay):
                    return "stopped"
        return "stopped"

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, daemon=True, name=f"xiaola-{self.country}")
        thread.start()
        return thread


def wait_or_stop(stop: threading.Event, seconds: float, sleeper=None) -> bool:
    """等待批间隔。返回 False 表示停止信号已经打断这次等待。"""
    if sleeper is not None:
        return bool(sleeper(seconds))
    if stop.is_set():
        return False
    interrupted = stop.wait(timeout=seconds)
    return not interrupted


class UnknownStreak:
    """菲岛连续结果未知时暂停拉单。只看提交结果，handle 抛错不计入也不清零。"""

    # 这些结果说明上游给了明确答复或确认没发出，计数清零。
    _CLEARS = {"accepted", "business-fail", "maintenance", "not-sent"}

    def __init__(self, threshold: int, pause_seconds: float, wait, log=None):
        self.threshold = threshold
        self.pause_seconds = pause_seconds
        self.wait = wait
        self.log = log or logger
        self.count = 0
        self.last_reason = ""

    def note_reason(self, reason) -> None:
        self.last_reason = str(reason)

    def observe(self, outcome) -> None:
        if outcome in self._CLEARS:
            self.count = 0
            return
        if outcome != "unknown" or self.threshold <= 0:
            return
        self.count += 1
        if self.count < self.threshold:
            return
        self.log.critical(
            "连续 %s 笔结果未知，暂停拉单 %s 秒 last_error=%s",
            self.count,
            self.pause_seconds,
            self.last_reason,
        )
        self.count = 0
        self.wait(self.pause_seconds)


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
        data = pulled.get("data") or {}
        _note_pulled(logger, data)
        run_one(self.handle, data, task_id=task_id_of(data), pause=self.wait)
        delay, batch_full = self.batch.on_task_finished()
        self.pending_delay = delay
        if batch_full:
            self.waiting_batch = True
        return "pulled"


def apply_batch_config(batch: BatchController, interval_time, max_tasks_per_batch, batch_interval) -> None:
    """热更新只换限额。"""
    batch.replace_limits(interval_time, max_tasks_per_batch, batch_interval)
