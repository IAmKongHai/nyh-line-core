"""VTSI Dito。resultCode 25 和 1 都回写状态 3。"""

from nyh_line.upstream.vtsi import check_vtsi_task, submit_vtsi_task

TERMINAL_FAIL = {"25", "1"}


def submit_task(task, center, gateway) -> str:
    return submit_vtsi_task(task, center, gateway, terminal_fail=TERMINAL_FAIL)


def check_task(record, center, gateway) -> str:
    return check_vtsi_task(record, center, gateway)
