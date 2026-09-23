"""每条线路的进程策略：线程数、空闲间隔、有没有查单。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LinePolicy:
    line: str
    role: str
    threads: int
    idle_low: int
    idle_high: int
    has_check: bool
    timeout: float | None = None
    busy_sleep: float = 0.2
    balance_interval: float = 60
    # 菲岛：连续这么多笔结果未知就暂停拉单。0 表示不暂停。
    unknown_pause_after: int = 0
    unknown_pause_seconds: float = 0


# 无任务时的等待按现有进程保留，不收成一个数。
_POLICIES = {
    ("fd-globe", "submit"): LinePolicy(
        "fd-globe", "submit", 1, 7, 15, False, unknown_pause_after=3, unknown_pause_seconds=60
    ),
    ("fd-smart", "submit"): LinePolicy(
        "fd-smart", "submit", 1, 7, 15, False, unknown_pause_after=3, unknown_pause_seconds=60
    ),
    ("vtsi-dito", "submit"): LinePolicy("vtsi-dito", "submit", 1, 7, 15, True, timeout=50),
    ("vtsi-dito", "check"): LinePolicy("vtsi-dito", "check", 1, 7, 15, True, timeout=50, busy_sleep=0.2),
    ("vtsi-globe", "submit"): LinePolicy("vtsi-globe", "submit", 4, 30, 60, True, timeout=50),
    ("vtsi-globe", "check"): LinePolicy("vtsi-globe", "check", 1, 15, 30, True, timeout=50),
    ("vtsi-smart", "submit"): LinePolicy("vtsi-smart", "submit", 2, 7, 15, True, timeout=10),
    ("vtsi-smart", "check"): LinePolicy("vtsi-smart", "check", 1, 7, 15, True, timeout=10),
    ("xiaola", "submit"): LinePolicy("xiaola", "submit", 1, 7, 15, True, busy_sleep=0.2),
    ("xiaola", "check"): LinePolicy("xiaola", "check", 1, 7, 15, True, busy_sleep=0.5),
}

PROGRAMS = (
    ("line-fd-globe-submit", "fd-globe", "submit"),
    ("line-fd-smart-submit", "fd-smart", "submit"),
    ("line-vtsi-dito-submit", "vtsi-dito", "submit"),
    ("line-vtsi-dito-check", "vtsi-dito", "check"),
    ("line-vtsi-globe-submit", "vtsi-globe", "submit"),
    ("line-vtsi-globe-check", "vtsi-globe", "check"),
    ("line-vtsi-smart-submit", "vtsi-smart", "submit"),
    ("line-vtsi-smart-check", "vtsi-smart", "check"),
    ("line-xiaola-submit", "xiaola", "submit"),
    ("line-xiaola-check", "xiaola", "check"),
)


def policy_for(line: str, role: str) -> LinePolicy:
    try:
        return _POLICIES[(line, role)]
    except KeyError as exc:
        raise KeyError(f"未知线路或角色: {line} {role}") from exc


def known_role(line: str, role: str) -> bool:
    return (line, role) in _POLICIES
