"""空闲间隔、停止信号、线程再拉起、余额节流。"""

import logging
import signal
import threading
import time
import xml.etree.ElementTree as ET

import pytest

from nyh_line.lines import dito_vtsi, registry
from nyh_line.lines.registry import CheckBalance
from nyh_line.policy import policy_for
from nyh_line.runner import CountrySlot, PollLoop, install_signals, maintain, respawn_delay, run_one
from nyh_line.upstream.fd import FdClient
from nyh_line.upstream.vtsi import VtsiGateway, parse_wallet_balance
from nyh_line.upstream.xiaola import parse_xiaola_balance
from nyh_line.xiaola.batch import BatchController
from tests.support import FakeResponse, FakeTransport, actions, make_center
from tests.test_vtsi_status import Session, soap


class ListLog:
    def __init__(self):
        self.errors = []
        self.infos = []

    def error(self, message, *args):
        self.errors.append(message % args if args else message)

    def exception(self, message, *args):
        self.errors.append(message % args if args else message)

    def info(self, message, *args):
        self.infos.append(message % args if args else message)


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


# ---- 每笔防护与线程看护（U5） ----


def test_fd_handle_error_is_contained_and_backs_off(monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    pulls = {"n": 0}

    def center_response(_url, data):
        if data.get("action") == "GetTasks":
            pulls["n"] += 1
            return FakeResponse({"code": 0, "data": {"task_id": 70 + pulls["n"], "phone_number": "9"}})
        return FakeResponse({"code": 0})

    def boom(task, center, client, on_unknown=None):
        raise ValueError("bad task")

    monkeypatch.setitem(registry._FD_SUBMIT, "fd-globe", boom)
    waits = []
    loop = registry.build_fd_loop(
        "fd-globe",
        make_center(FakeTransport(response=center_response)),
        FdClient(base_url="http://fd.invalid", uid="u", key="k", transport=FakeTransport()),
        threading.Event(),
        wait=lambda seconds: waits.append(seconds) or True,
        sleep=lambda _seconds: None,
        randint=lambda start, _end: start,
    )
    assert loop.run_round() == "worked"
    assert waits == [policy_for("fd-globe", "submit").error_backoff] == [3]
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1 and "task_id=71" in errors[0]
    assert loop.run_round() == "worked"
    assert pulls["n"] == 2


def _check_loop(records, query_results, balance_query=None, center_extra=None, sync=None):
    """查单一轮：inExecutingTasks 给出 records，gateway.query 按顺序返回 query_results。"""
    def center_response(_url, data):
        if data.get("action") == "inExecutingTasks":
            return FakeResponse({"code": 0, "data": records})
        if center_extra is not None:
            return center_extra(data)
        return FakeResponse({"code": 0})

    transport = FakeTransport(response=center_response)
    center = make_center(transport)
    if sync is not None:
        center.synchronize_balance = sync
    results = list(query_results)
    queried = []

    class Gateway:
        def query(self, merchant_id):
            queried.append(merchant_id)
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        def balance(self):
            raise AssertionError("不应查余额")

    gateway = Gateway()
    waits = []
    loop = registry.build_check_loop(
        "vtsi-dito",
        center,
        lambda record: dito_vtsi.check_task(record, center, gateway),
        balance_query or (lambda: "12"),
        threading.Event(),
        wait=lambda seconds: waits.append(seconds) or True,
        sleep=lambda _seconds: None,
        randint=lambda start, _end: start,
        clock=lambda: 0,
    )
    return loop, transport, queried, waits


def test_bad_first_record_does_not_block_the_rest(caplog):
    """AE5：第一条缺 id，第二、三条照常查询并按表回写，ERROR 一条。"""
    caplog.set_level(logging.ERROR)
    records = [{"user_number": "1"}, {"id": 2, "user_number": "2"}, {"id": 3, "user_number": "3"}]
    loop, transport, queried, _waits = _check_loop(records, [soap("2", "2"), soap("2", "3")])
    loop.run_cycle()
    assert queried == ["v2", "v3"]
    assert [(body["task_id"], body["status"]) for body in actions(transport, "Feedback")] == [(2, 2), (3, 3)]
    assert len([record for record in caplog.records if record.levelno == logging.ERROR]) == 1


def test_unparseable_query_result_skips_only_that_record():
    records = [{"id": 1, "user_number": "1"}, {"id": 2, "user_number": "2"}]
    loop, transport, queried, _waits = _check_loop(records, [ET.ParseError("bad xml"), soap("2", "2")])
    loop.run_cycle()
    assert queried == ["v1", "v2"]
    assert [(body["task_id"], body["status"]) for body in actions(transport, "Feedback")] == [(2, 2)]


def test_balance_sync_error_does_not_stop_the_round(caplog):
    caplog.set_level(logging.ERROR)
    records = [{"id": 5, "user_number": "5"}]

    synced = []

    def sync(amount):
        synced.append(amount)
        raise RuntimeError("center down")

    loop, transport, queried, _waits = _check_loop(records, [soap("2", "2")], sync=sync)
    loop.run_cycle()
    assert queried == ["v5"]
    assert [body["status"] for body in actions(transport, "Feedback")] == [2]
    assert synced == ["12"]
    assert len([record for record in caplog.records if record.levelno == logging.ERROR]) == 1


def test_run_one_does_not_swallow_keyboard_interrupt():
    def interrupt(_item):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_one(interrupt, {"task_id": 1}, task_id=1, pause=lambda _seconds: True)


def _slot(work, stop=None, waits=None):
    stop = stop or threading.Event()
    waits = waits if waits is not None else []
    batch = BatchController(3, 10, 15)
    slot = CountrySlot(1, "PH", batch, work, stop, wait=lambda seconds: waits.append(seconds) or True)
    return slot, batch, waits


def test_crashed_slot_restarts_same_country_with_same_batch():
    seen = []

    def work(slot):
        seen.append((slot.country, slot.batch, slot.batch.tasks_in_current_batch))
        if len(seen) == 1:
            slot.batch.on_task_finished()
            raise RuntimeError("thread crashed")
        return "stopped"

    slot, batch, waits = _slot(work)
    assert slot.run() == "stopped"
    assert waits == [2]
    assert [item[0] for item in seen] == ["PH", "PH"]
    assert seen[1][1] is batch
    assert seen[1][2] == 1


@pytest.mark.parametrize("outcome", ["no-country", "stopped", "interrupted"])
def test_slot_normal_return_is_not_restarted(outcome):
    calls = []

    def work(_slot):
        calls.append(1)
        return outcome

    slot, _batch, waits = _slot(work)
    assert slot.run() == outcome
    assert calls == [1] and waits == []


def test_slot_backoff_grows_and_caps():
    def work(slot):
        if slot.crashes < 3:
            raise RuntimeError("again")
        return "stopped"

    slot, _batch, waits = _slot(work)
    slot.run()
    assert waits == [2, 4, 6]
    assert respawn_delay(15) == 30 and respawn_delay(40) == 30


def test_stop_during_slot_backoff_ends_without_restart():
    stop = threading.Event()
    calls = []

    def work(_slot):
        calls.append(1)
        raise RuntimeError("crash")

    slot = CountrySlot(1, "PH", BatchController(3, 10, 15), work, stop, wait=lambda _seconds: stop.set() or False)
    assert slot.run() == "stopped"
    assert calls == [1]


def test_slot_thread_is_named_by_country():
    done = threading.Event()
    slot, _batch, _waits = _slot(lambda _slot: done.set() or "stopped")
    thread = slot.start()
    thread.join(timeout=2)
    assert thread.name == "xiaola-PH" and done.is_set()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_vtsi_keeper_backs_off_then_restores_four_threads():
    stop = threading.Event()
    waits = []
    born = []
    lock = threading.Lock()

    def factory():
        def run():
            with lock:
                born.append(1)
                index = len(born)
            if index == 2:
                raise RuntimeError("worker crashed")
            stop.wait(5)

        return run

    keeper = maintain("vtsi-globe", "submit", factory, stop, wait=lambda seconds: waits.append(seconds) or True)
    assert keeper.reconcile() == 4
    for _ in range(200):
        if any(not thread.is_alive() for thread in keeper.workers):
            break
        time.sleep(0.01)
    assert keeper.reconcile() == 4
    for _ in range(200):
        if len(born) == 5:
            break
        time.sleep(0.01)
    assert waits == [2]
    assert len(born) == 5
    assert sum(1 for thread in keeper.workers if thread.is_alive()) == 4
    stop.set()
    for thread in keeper.workers:
        thread.join(timeout=1)


def test_stop_during_keeper_backoff_does_not_start_worker():
    stop = threading.Event()
    born = []

    def factory():
        return lambda: born.append(1)

    keeper = maintain("vtsi-dito", "submit", factory, stop, wait=lambda _seconds: stop.set() or False)
    keeper.reconcile()
    keeper.workers[0].join(timeout=1)
    keeper.reconcile()
    keeper.workers[0].join(timeout=1)
    assert born == [1]
