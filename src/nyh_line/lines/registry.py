"""线路名到提交、查单函数的对应。菲岛没有查单。"""

from __future__ import annotations

import logging
import random
import sys
import threading
import time

from nyh_line.balance import BalanceGate
from nyh_line.center.client import CenterClient
from nyh_line.center.profiles import device_new_profile, xiaola_profile
from nyh_line.http_transport import requests_transport
from nyh_line.log_setup import configure_logging
from nyh_line.lines import dito_vtsi, globe_fd, globe_vtsi, smart_fd, smart_vtsi, yingla
from nyh_line.policy import known_role, policy_for
from nyh_line.runner import (
    CountrySlot,
    PollLoop,
    UnknownStreak,
    XiaolaSubmitLoop,
    install_signals,
    maintain,
    wait_or_stop,
)
from nyh_line.settings import (
    alert_user_ids,
    center_base_url,
    device_id_key,
    device_key_key,
    environ_map,
    sim_id_key,
    startup_problems,
    threads_to_start,
    vtsi_account_key,
    xiaola_submit_settings,
)
from nyh_line.upstream.fd import FdClient
from nyh_line.upstream.vtsi import VtsiGateway, parse_wallet_balance, publish_ca_bundle, zeep_session_factory
from nyh_line.upstream.xiaola import XiaolaClient, parse_xiaola_balance
from nyh_line.xiaola.batch import BatchController
from nyh_line.xiaola.blocklist import Blocklist

logger = logging.getLogger(__name__)

_FD_SUBMIT = {
    "fd-globe": globe_fd.submit_task,
    "fd-smart": smart_fd.submit_task,
}
_VTSI_SUBMIT = {
    "vtsi-dito": dito_vtsi.submit_task,
    "vtsi-globe": globe_vtsi.submit_task,
    "vtsi-smart": smart_vtsi.submit_task,
}
_VTSI_CHECK = {
    "vtsi-dito": dito_vtsi.check_task,
    "vtsi-globe": globe_vtsi.check_task,
    "vtsi-smart": smart_vtsi.check_task,
}


def role_ok(line: str, role: str) -> bool:
    return known_role(line, role)


class CheckBalance:
    """四条查单共用的余额节流。解析失败不会把余额写成 0。"""

    def __init__(self, line: str, query, sync):
        interval = policy_for(line, "check").balance_interval
        self.gate = BalanceGate(interval)
        self.query = query
        self.sync = sync

    def sync_balance(self, now: float) -> str:
        return self.gate.tick(now, self.query, self.sync)


def _make_center(env, line: str, profile, country_code: str = ""):
    return CenterClient(
        base_url=center_base_url(env),
        device_id=str(env[device_id_key(line)]).strip(),
        device_key=str(env[device_key_key(line)]).strip(),
        sim_id=str(env[sim_id_key(line)]).strip(),
        profile=profile,
        transport=requests_transport(),
        version=str(env.get("CENTER_VERSION", "")).strip() or "2019_11_11__12_04_05",
        country_code=country_code,
        fee_charging_line=str(env.get("DEVICE_TYPE_XIAOLA", "")).strip(),
        alert_user_ids=alert_user_ids(line, env),
    )


def _records(payload) -> list:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def build_fd_loop(line: str, center, client, stop: threading.Event, *, wait=None, sleep=None, randint=None) -> PollLoop:
    """菲岛提交循环。连续结果未知达到策略阈值时，在这一笔之后暂停拉单。"""
    policy = policy_for(line, "submit")
    pause = wait or (lambda seconds: wait_or_stop(stop, seconds))
    streak = UnknownStreak(policy.unknown_pause_after, policy.unknown_pause_seconds, pause)
    submit = _FD_SUBMIT[line]

    def handle(task):
        outcome = submit(task, center, client, on_unknown=streak.note_reason)
        streak.observe(outcome)
        return outcome

    return PollLoop(policy, center.get_task, handle, sleep=sleep, randint=randint, stop=stop, wait=pause)


def _serve_fd(line: str, env, stop: threading.Event) -> None:
    center = _make_center(env, line, device_new_profile())
    prefix = "FD_GLOBE" if line == "fd-globe" else "FD_SMART"
    client = FdClient(
        base_url=env[f"{prefix}_URL"],
        uid=env[f"{prefix}_UID"],
        key=env[f"{prefix}_KEY"],
        transport=requests_transport(),
    )
    loop = build_fd_loop(line, center, client, stop)
    install_signals(loop)
    loop.run_forever()


def build_check_loop(
    line: str, center, check_one, balance_query, stop: threading.Event, *, wait=None, sleep=None, randint=None, clock=None
) -> PollLoop:
    """查单循环：每轮先同步余额，再取执行中的发送行逐条查询。一条坏记录不挡住后面的记录。"""
    policy = policy_for(line, "check")
    balance = CheckBalance(line, balance_query, center.synchronize_balance)
    now = clock or time.time

    def pull():
        payload = center.in_executing_tasks()
        records = _records(payload)
        if not records:
            if isinstance(payload, dict) and str(payload.get("code")) != "0":
                return payload
            return {"code": 120, "msg": "empty"}
        return {"code": 0, "data": records}

    def handle(records):
        for record in records:
            loop.guard(check_one, record)

    loop = PollLoop(
        policy,
        pull,
        handle,
        sleep=sleep,
        randint=randint,
        stop=stop,
        wait=wait,
        before_round=lambda: balance.sync_balance(now()),
    )
    return loop


def build_vtsi_gateway(line: str, role: str, env, ca_path: str) -> VtsiGateway:
    """按线路策略组 VTSI 网关：超时同时管 WSDL 和 SOAP 调用，提交侧带发送截止。"""
    policy = policy_for(line, role)
    timeout = policy.timeout or 50
    return VtsiGateway(
        zeep_session_factory(env["VTSI_WSDL"], timeout, ca_path),
        username=env["VTSI_USERNAME"],
        password=env["VTSI_PASSWORD"],
        account=env[vtsi_account_key(line)],
        timeout=timeout,
        send_deadline=policy.send_deadline,
    )


def _serve_vtsi(line: str, role: str, env, stop: threading.Event) -> None:
    policy = policy_for(line, role)
    center = _make_center(env, line, device_new_profile())
    # 主线程在起工作线程前生成一次 CA 包，之后所有会话都用这个固定路径。
    gateway = build_vtsi_gateway(line, role, env, publish_ca_bundle())
    if role == "submit":
        submit = _VTSI_SUBMIT[line]

        def worker():
            loop = PollLoop(policy, center.get_task, lambda task: submit(task, center, gateway), stop=stop)
            return loop.run_forever

        keeper = maintain(line, role, worker, stop)
        install_signals(PollLoop(policy, lambda: {"code": 120}, lambda task: None, stop=stop))
        while not stop.is_set():
            keeper.reconcile()
            stop.wait(0.2)
        return

    check = _VTSI_CHECK[line]
    loop = build_check_loop(
        line,
        center,
        lambda record: check(record, center, gateway),
        lambda: parse_wallet_balance(gateway.balance()),
        stop,
    )
    install_signals(loop)
    loop.run_forever()


def _xiaola_client(env) -> XiaolaClient:
    return XiaolaClient(
        base_url=env["XIAOLA_API_BASE_URL"],
        username=env["XIAOLA_API_USERNAME"],
        secret_key=env["XIAOLA_API_SECRET_KEY"],
        transport=requests_transport(),
    )


def xiaola_slot_work(env, blocklist, stop: threading.Event, *, make_center=None, make_client=None, wait=None, randint=None):
    """一个赢啦国家槽位的工作函数。每次（重新）开工都新建客户端，批次控制器用槽位上的那一个。"""
    policy = policy_for("xiaola", "submit")
    make_center = make_center or (lambda country: _make_center(env, "xiaola", xiaola_profile(), country))
    make_client = make_client or (lambda: _xiaola_client(env))
    randint = randint or random.randint

    def work(slot: CountrySlot) -> str:
        center = make_center(slot.country)
        client = make_client()
        cycle = XiaolaSubmitLoop(
            country=slot.country,
            batch=slot.batch,
            pull=center.get_task,
            handle=lambda task: yingla.submit_task(task, center, client, blocklist),
            stop=stop,
            wait=wait,
        )
        while not stop.is_set():
            outcome = cycle.advance()
            if outcome in {"interrupted", "stopped", "no-country"}:
                return outcome
            if outcome == "idle" and not cycle.wait(randint(policy.idle_low, policy.idle_high)):
                return "interrupted"
        return "stopped"

    return work


def _serve_xiaola(role: str, env, stop: threading.Event) -> None:
    policy = policy_for("xiaola", role)
    if role == "check":
        center = _make_center(env, "xiaola", xiaola_profile())
        client = _xiaola_client(env)
        loop = build_check_loop(
            "xiaola",
            center,
            lambda record: yingla.check_task(record, center, client),
            lambda: parse_xiaola_balance(client.read_balance()),
            stop,
        )
        install_signals(loop)
        loop.run_forever()
        return

    blocklist = Blocklist(str(env.get("BLOCKLIST_FILE_PATH") or ""))
    numbers = xiaola_submit_settings(env)
    work = xiaola_slot_work(env, blocklist, stop)
    slots = [
        CountrySlot(
            thread_id,
            country,
            BatchController(numbers.interval_time, numbers.max_tasks_per_batch, numbers.batch_interval),
            work,
            stop,
        )
        for thread_id, country in threads_to_start(env)
    ]
    threads = [slot.start() for slot in slots]
    install_signals(PollLoop(policy, lambda: {"code": 120}, lambda task: None, stop=stop))
    while not stop.is_set():
        stop.wait(1)
    for thread in threads:
        thread.join(timeout=1)


def serve_forever(line: str, role: str, env) -> None:
    stop = threading.Event()
    if line in _FD_SUBMIT:
        _serve_fd(line, env, stop)
        return
    if line in _VTSI_SUBMIT:
        _serve_vtsi(line, role, env, stop)
        return
    _serve_xiaola(role, env, stop)


def run_line(line: str, role: str, env=None, serve=None) -> int:
    """配置有任何问题就在拉单前退出。stderr 只列键名，不打印键值。"""
    current = environ_map(env)
    problems = startup_problems(line, role, current)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2
    configure_logging()
    logger.info("启动 %s %s", line, role)
    runner = serve or serve_forever
    runner(line, role, current)
    return 0
