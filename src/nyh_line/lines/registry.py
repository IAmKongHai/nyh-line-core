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
    PollLoop,
    UnknownStreak,
    XiaolaSubmitLoop,
    apply_batch_config,
    install_signals,
    maintain,
    wait_or_stop,
)
from nyh_line.settings import (
    alert_user_ids,
    center_base_url,
    country_for_thread,
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

    return PollLoop(policy, center.get_task, handle, sleep=sleep, randint=randint, stop=stop)


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
    while not stop.is_set():
        loop.run_round()


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
            def run():
                loop = PollLoop(policy, center.get_task, lambda task: submit(task, center, gateway), stop=stop)
                while not stop.is_set():
                    try:
                        loop.run_round()
                    except Exception:
                        logger.exception("VTSI 提交线程退出，准备按配置数量再拉起")
                        return

            return run

        keeper = maintain(line, role, worker, stop)
        install_signals(PollLoop(policy, lambda: {"code": 120}, lambda task: None, stop=stop))
        while not stop.is_set():
            keeper.reconcile()
            stop.wait(0.2)
        return

    check = _VTSI_CHECK[line]
    balance = CheckBalance(line, lambda: parse_wallet_balance(gateway.balance()), center.synchronize_balance)

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
            check(record, center, gateway)

    loop = PollLoop(policy, pull, handle, stop=stop)
    install_signals(loop)
    while not stop.is_set():
        balance.sync_balance(time.time())
        loop.run_round()


def _serve_xiaola(role: str, env, stop: threading.Event) -> None:
    policy = policy_for("xiaola", role)
    if role == "check":
        center = _make_center(env, "xiaola", xiaola_profile())
        client = XiaolaClient(
            base_url=env["XIAOLA_API_BASE_URL"],
            username=env["XIAOLA_API_USERNAME"],
            secret_key=env["XIAOLA_API_SECRET_KEY"],
            transport=requests_transport(),
        )
        balance = CheckBalance(
            "xiaola",
            lambda: parse_xiaola_balance(client.read_balance()),
            center.synchronize_balance,
        )

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
                yingla.check_task(record, center, client)

        loop = PollLoop(policy, pull, handle, stop=stop)
        install_signals(loop)
        while not stop.is_set():
            balance.sync_balance(time.time())
            loop.run_round()
        return

    blocklist = Blocklist(str(env.get("BLOCKLIST_FILE_PATH") or ""))
    numbers = xiaola_submit_settings(env)

    def start_one(country: str):
        def run():
            batch = BatchController(numbers.interval_time, numbers.max_tasks_per_batch, numbers.batch_interval)
            center = _make_center(env, "xiaola", xiaola_profile(), country)
            client = XiaolaClient(
                base_url=env["XIAOLA_API_BASE_URL"],
                username=env["XIAOLA_API_USERNAME"],
                secret_key=env["XIAOLA_API_SECRET_KEY"],
                transport=requests_transport(),
            )
            cycle = XiaolaSubmitLoop(
                country=country,
                batch=batch,
                pull=center.get_task,
                handle=lambda task: yingla.submit_task(task, center, client, blocklist),
                stop=stop,
            )
            while not stop.is_set():
                apply_batch_config(
                    batch,
                    float(env.get("RECHARGE_INTERVAL_TIME") or batch.interval_time),
                    int(env.get("RECHARGE_MAX_TASKS_PER_BATCH") or batch.max_tasks_per_batch),
                    float(env.get("RECHARGE_BATCH_INTERVAL") or batch.batch_interval),
                )
                outcome = cycle.advance()
                if outcome in {"interrupted", "stopped", "no-country"}:
                    return
                if outcome == "idle":
                    delay = random.randint(policy.idle_low, policy.idle_high)
                    if not cycle.wait(delay):
                        return

        return run

    threads = []
    for _thread_id, country in threads_to_start(env):
        if not country_for_thread(_thread_id, env):
            continue
        thread = threading.Thread(target=start_one(country), daemon=True)
        thread.start()
        threads.append(thread)
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
