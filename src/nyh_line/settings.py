"""运行参数只从环境读取。缺必填项时进程在拉单前退出。"""

from __future__ import annotations

import os
from collections.abc import Mapping

CENTER_KEYS = (
    "CENTER_PROTOCOL",
    "CENTER_HOST",
    "CENTER_DEVICE_ID",
    "CENTER_DEVICE_KEY",
    "CENTER_SIM_ID",
)

_LINE_KEYS = {
    "fd-globe": ("FD_GLOBE_UID", "FD_GLOBE_KEY", "FD_GLOBE_URL"),
    "fd-smart": ("FD_SMART_UID", "FD_SMART_KEY", "FD_SMART_URL"),
    "vtsi-dito": ("VTSI_USERNAME", "VTSI_PASSWORD", "VTSI_ACCOUNT", "VTSI_WSDL"),
    "vtsi-globe": ("VTSI_USERNAME", "VTSI_PASSWORD", "VTSI_ACCOUNT", "VTSI_WSDL"),
    "vtsi-smart": ("VTSI_USERNAME", "VTSI_PASSWORD", "VTSI_ACCOUNT", "VTSI_WSDL"),
    "xiaola": ("XIAOLA_API_BASE_URL", "XIAOLA_API_USERNAME", "XIAOLA_API_SECRET_KEY", "DEVICE_TYPE_XIAOLA"),
}

_ALERT_KEY = {
    "fd-globe": "FD_GLOBE_ALERT_USER_IDS",
    "fd-smart": "FD_SMART_ALERT_USER_IDS",
    "vtsi-dito": "VTSI_DITO_ALERT_USER_IDS",
    "vtsi-globe": "VTSI_GLOBE_ALERT_USER_IDS",
    "vtsi-smart": "VTSI_SMART_ALERT_USER_IDS",
    "xiaola": "XIAOLA_ALERT_USER_IDS",
}


def environ_map(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return os.environ if env is None else env


def missing_keys(line: str, env: Mapping[str, str]) -> list[str]:
    required = CENTER_KEYS + _LINE_KEYS[line]
    return [key for key in required if not str(env.get(key, "")).strip()]


def alert_user_ids(line: str, env: Mapping[str, str]) -> tuple[str, ...]:
    raw = str(env.get(_ALERT_KEY[line], "")).strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def country_for_thread(thread_id: int, env: Mapping[str, str]) -> str:
    """线程国家码缺了就是空。调用方不得改充别的国家。"""
    return str(env.get(f"THREAD{thread_id}_COUNTRY_CODE", "") or "").strip()


def threads_to_start(env: Mapping[str, str]) -> list[tuple[int, str]]:
    """只启动写了国家码的赢啦线程。"""
    raw_count = str(env.get("MAX_THREADS", "")).strip()
    count = int(raw_count) if raw_count else 1
    started = []
    for thread_id in range(1, count + 1):
        country = country_for_thread(thread_id, env)
        if country:
            started.append((thread_id, country))
    return started


def center_base_url(env: Mapping[str, str]) -> str:
    protocol = str(env["CENTER_PROTOCOL"]).strip()
    host = str(env["CENTER_HOST"]).strip()
    return f"{protocol}://{host}"
