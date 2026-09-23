"""空闲间隔、停止信号、线程再拉起、余额节流。"""

import signal
import threading
import time

import pytest

from nyh_line.lines import dito_vtsi
from nyh_line.lines.registry import CheckBalance
from nyh_line.policy import policy_for
from nyh_line.runner import PollLoop, install_signals, maintain
from nyh_line.upstream.vtsi import VtsiGateway, parse_wallet_balance
from nyh_line.upstream.xiaola import parse_xiaola_balance
from tests.support import FakeTransport, actions, make_center
from tests.test_vtsi_status import Session


class ListLog:
    def __init__(self):
        self.errors = []

    def error(self, message, *args):
        self.errors.append(message % args if args else message)


@pytest.mark.parametrize(
    "line,role,low,high",
    [
        ("fd-globe", "submit", 7, 15),
        ("fd-smart", "submit", 7, 15),
        ("vtsi-dito", "submit", 7, 15),
        ("vtsi-smart", "submit", 7, 15),
        ("vtsi-dito", "check", 7, 15),
        ("vtsi-smart", "check", 7, 15),
        ("xiaola", "check", 7, 15),
        ("vtsi-globe", "check", 15, 30),
        ("vtsi-globe", "submit", 30, 60),
    ],
)
def test_idle_wait_uses_line_range(line, role, low, high):
    seen = {}

    def randint(start, end):
        seen["bounds"] = (start, end)
        return start

    def sleep(seconds):
        seen["slept"] = seconds

    def handle(_task):
        raise AssertionError("无任务不应进入处理")

    loop = PollLoop(
        policy_for(line, role),
        lambda: {"code": 1, "msg": "none"},
        handle,
        sleep=sleep,
        randint=randint,
    )
    assert loop.run_round() == "idle"
    assert seen["bounds"] == (low, high)
    assert low <= seen["slept"] <= high


@pytest.mark.parametrize("code", [120, 6003, 5001, "120"])
def test_quiet_code_does_not_log_or_feedback(code):
    log = ListLog()
    transport = FakeTransport(response={"code": code, "msg": "wait"})
    center = make_center(transport)
    loop = PollLoop(
        policy_for("fd-globe", "submit"),
        center.get_task,
        lambda _task: (_ for _ in ()).throw(AssertionError("handle")),
        sleep=lambda _seconds: None,
        randint=lambda start, end: start,
        log=log,
    )
    assert loop.run_round() == "idle"
    assert loop.run_round() == "idle"
    assert log.errors == []
    assert actions(transport, "Feedback") == []

    log = ListLog()
    transport = FakeTransport(response={"code": -1, "msg": "down"})
    center = make_center(transport)
    loop = PollLoop(
        policy_for("fd-globe", "submit"),
        center.get_task,
        lambda _task: None,
        sleep=lambda _seconds: None,
        randint=lambda start, end: start,
        log=log,
    )
    assert loop.run_round() == "idle"
    assert loop.run_round() == "idle"
    assert len(log.errors) == 2
    assert actions(transport, "Feedback") == []


def test_sigterm_stops_new_pull_and_unknown_result_has_no_feedback():
    transport = FakeTransport()
    center = make_center(transport)
    sink = []
    gateway = VtsiGateway(
        lambda: Session(error=TimeoutError("read timeout"), sink=sink),
        username="nyh-user",
        password="pw",
        account="ACC100",
        timeout=50,
    )
    pulls = {"n": 0}

    def pull():
        pulls["n"] += 1
        return {"code": 0, "data": {"task_id": 9, "phone_number": "9123456789", "vtsi_sku": "SKU"}}

    loop = PollLoop(
        policy_for("vtsi-dito", "submit"),
        pull,
        lambda task: dito_vtsi.submit_task(task, center, gateway),
        sleep=lambda _seconds: None,
        randint=lambda start, end: start,
    )
    assert loop.run_round() == "worked"
    assert pulls["n"] == 1
    assert [item["command"] for item in sink] == ["TOPUP"]
    assert actions(transport, "Feedback") == []
    assert len(actions(transport, "SMSContentReceiving")) == 1
    loop.handle_sigterm(signal.SIGTERM, None)
    assert loop.run_round() == "stopped"
    assert pulls["n"] == 1
    assert actions(transport, "Feedback") == []


def test_install_signals_registers_sigterm(monkeypatch):
    registered = {}
    monkeypatch.setattr(signal, "signal", lambda signum, handler: registered.setdefault(signum, handler))
    loop = PollLoop(policy_for("fd-globe", "submit"), lambda: {"code": 1}, lambda task: None)
    install_signals(loop)
    assert registered[signal.SIGTERM] == loop.handle_sigterm


def test_globe_submit_respawns_until_four_threads():
    stop = threading.Event()
    born = []
    lock = threading.Lock()

    def factory():
        def run():
            with lock:
                born.append(1)
                index = len(born)
            if index == 1:
                return
            stop.wait(2)

        return run

    keeper = maintain("vtsi-globe", "submit", factory, stop)
    assert keeper.count == 4
    assert policy_for("vtsi-dito", "submit").threads == 1
    assert policy_for("vtsi-smart", "submit").threads == 2
    keeper.reconcile()
    for _ in range(50):
        if any(not thread.is_alive() for thread in list(keeper.workers)):
            break
        time.sleep(0.01)
    keeper.reconcile()
    alive = [thread for thread in keeper.workers if thread.is_alive()]
    assert len(alive) == 4
    stop.set()
    for thread in keeper.workers:
        thread.join(timeout=1)


@pytest.mark.parametrize("line", ["vtsi-dito", "vtsi-globe", "vtsi-smart", "xiaola"])
def test_balance_parse_failure_does_not_sync_or_retry_within_60_seconds(line):
    assert policy_for(line, "check").balance_interval == 60
    queries = {"n": 0}
    synced = []

    def query():
        queries["n"] += 1
        if line == "xiaola":
            return parse_xiaola_balance({"code": 10000, "result": {}})
        return parse_wallet_balance({"Body": {"ExecuteResponse": {"return": "<root></root>"}}})

    worker = CheckBalance(line, query, synced.append)
    assert worker.sync_balance(0) == "parse_failed"
    assert worker.sync_balance(59) == "skipped"
    assert queries["n"] == 1
    assert synced == []


def test_parsed_balance_is_forwarded_as_is():
    synced = []

    def query():
        xml = "<root><wallet><balance>12</balance></wallet></root>"
        return parse_wallet_balance({"Body": {"ExecuteResponse": {"return": xml}}})

    worker = CheckBalance("vtsi-dito", query, synced.append)
    assert worker.sync_balance(0) == "synced"
    assert synced == ["12"]
