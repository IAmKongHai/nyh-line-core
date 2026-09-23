"""两条拉单 profile。赢啦和设备口的 URL、字段不能合成一套。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PullProfile:
    """一条线路访问 Center 时用的路径和拉单字段。"""

    name: str
    get_task_path: str
    feedback_path: str
    executing_path: str
    balance_path: str
    sim_balance: int
    include_empty_key: bool
    include_line_country: bool


def device_new_profile() -> PullProfile:
    """菲岛和 VTSI 拉单走 device_new，余额走 VTSI 口。"""
    return PullProfile(
        name="device_new",
        get_task_path="/notify/device_new/index.html",
        feedback_path="/notify/device_new/index.html",
        executing_path="/notify/vtsi/index.html",
        balance_path="/notify/vtsi/index.html",
        sim_balance=0,
        include_empty_key=True,
        include_line_country=False,
    )


def xiaola_profile() -> PullProfile:
    """赢啦拉单走 task_client，余额仍走 device_new，不写 VTSI 余额表。"""
    return PullProfile(
        name="xiaola",
        get_task_path="/notify/task_client/getTask",
        feedback_path="/notify/device_new/index.html",
        executing_path="/notify/vtsi/index.html",
        balance_path="/notify/device_new/index.html",
        sim_balance=1000000,
        include_empty_key=False,
        include_line_country=True,
    )
