"""运行参数：一份环境文件加进程环境。配置有错时进程在拉单前退出。"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# 每条线路的键名前缀。设备键、VTSI 账户和告警收件人都由它派生。
LINE_PREFIX = {
    "fd-globe": "FD_GLOBE",
    "fd-smart": "FD_SMART",
    "vtsi-dito": "VTSI_DITO",
    "vtsi-globe": "VTSI_GLOBE",
    "vtsi-smart": "VTSI_SMART",
    "xiaola": "XIAOLA",
}

# 六条线路共用的 Center 地址。
CENTER_KEYS = ("CENTER_PROTOCOL", "CENTER_HOST")

# 已删除的无前缀键。环境里还有任何一个就拒绝启动，避免半迁移状态。
LEGACY_KEYS = ("CENTER_DEVICE_ID", "CENTER_DEVICE_KEY", "CENTER_SIM_ID", "VTSI_ACCOUNT")

_VTSI_SHARED = ("VTSI_USERNAME", "VTSI_PASSWORD", "VTSI_WSDL")

# 各线路要解析成 URL 的键。
_URL_KEYS = {
    "fd-globe": ("FD_GLOBE_URL",),
    "fd-smart": ("FD_SMART_URL",),
    "vtsi-dito": ("VTSI_WSDL",),
    "vtsi-globe": ("VTSI_WSDL",),
    "vtsi-smart": ("VTSI_WSDL",),
    "xiaola": ("XIAOLA_API_BASE_URL",),
}

_ALERT_KEY = {line: f"{prefix}_ALERT_USER_IDS" for line, prefix in LINE_PREFIX.items()}

ENV_FILE_VARIABLE = "NYH_LINE_ENV_FILE"


def repo_root() -> Path:
    """按源码位置推算仓库根。要求可编辑安装，见 README 的部署一节。"""
    return Path(__file__).resolve().parents[2]


def device_id_key(line: str) -> str:
    return f"{LINE_PREFIX[line]}_CENTER_DEVICE_ID"


def device_key_key(line: str) -> str:
    return f"{LINE_PREFIX[line]}_CENTER_DEVICE_KEY"


def sim_id_key(line: str) -> str:
    return f"{LINE_PREFIX[line]}_CENTER_SIM_ID"


def vtsi_account_key(line: str) -> str:
    return f"{LINE_PREFIX[line]}_ACCOUNT"


def required_keys(line: str) -> tuple[str, ...]:
    """本线路必填的键：Center 地址、本线路三个设备键，以及上游凭据。"""
    keys = CENTER_KEYS + (device_id_key(line), device_key_key(line), sim_id_key(line))
    if line.startswith("fd-"):
        prefix = LINE_PREFIX[line]
        return keys + (f"{prefix}_UID", f"{prefix}_KEY", f"{prefix}_URL")
    if line.startswith("vtsi-"):
        return keys + _VTSI_SHARED + (vtsi_account_key(line),)
    return keys + ("XIAOLA_API_BASE_URL", "XIAOLA_API_USERNAME", "XIAOLA_API_SECRET_KEY", "DEVICE_TYPE_XIAOLA")


# ---- 环境文件 ----


def default_env_path(process_env: Mapping[str, str] | None = None) -> Path:
    process_env = os.environ if process_env is None else process_env
    configured = str(process_env.get(ENV_FILE_VARIABLE, "") or "").strip()
    return Path(configured) if configured else repo_root() / ".env"


def parse_env_text(text: str) -> dict[str, str]:
    """解析 KEY=VALUE。忽略空行和 # 注释，去掉值两端成对的引号。"""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_env_file(path: Path) -> dict[str, str]:
    """文件不存在时返回空。缺键由启动校验报出。"""
    try:
        return parse_env_text(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def environ_map(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """注入的 env 原样使用；否则读环境文件，进程环境里的同名键优先。只在启动时读一次。"""
    if env is not None:
        return env
    merged = load_env_file(default_env_path())
    merged.update(os.environ)
    return merged


# ---- 数值与 URL ----


@dataclass(frozen=True)
class XiaolaSubmitSettings:
    """赢啦提交的批次数值。启动时解析一次，改配置要重启进程。"""

    max_threads: int
    interval_time: float
    max_tasks_per_batch: int
    batch_interval: float


# 键名、未填时的默认值、是否必须是整数。
_XIAOLA_NUMBERS = (
    ("MAX_THREADS", 1, True),
    ("RECHARGE_INTERVAL_TIME", 3, False),
    ("RECHARGE_MAX_TASKS_PER_BATCH", 10, True),
    ("RECHARGE_BATCH_INTERVAL", 15, False),
)


def _positive_number(raw, default, integer: bool):
    """空值取默认值。其余必须是有限的正数，整数项不接受小数。解析失败抛 ValueError。"""
    text = str(raw if raw is not None else "").strip()
    if text == "":
        return default
    value = int(text) if integer else float(text)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(text)
    return value


def xiaola_submit_settings(env: Mapping[str, str]) -> XiaolaSubmitSettings:
    parsed = [_positive_number(env.get(key), default, integer) for key, default, integer in _XIAOLA_NUMBERS]
    return XiaolaSubmitSettings(*parsed)


def _url_ok(url) -> bool:
    parsed = urlparse(str(url or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def center_base_url(env: Mapping[str, str]) -> str:
    protocol = str(env["CENTER_PROTOCOL"]).strip()
    host = str(env["CENTER_HOST"]).strip()
    return f"{protocol}://{host}"


# ---- 启动校验 ----


def _present(env: Mapping[str, str], key: str) -> bool:
    return bool(str(env.get(key, "") or "").strip())


def _duplicate_device_keys(line: str, env: Mapping[str, str]) -> list[str]:
    """本线路 device_id 与环境里其它任一线路相同时，返回所有相同的键名。"""
    own_key = device_id_key(line)
    own = str(env.get(own_key, "") or "").strip()
    if not own:
        return []
    same = [
        device_id_key(other)
        for other in LINE_PREFIX
        if other != line and str(env.get(device_id_key(other), "") or "").strip() == own
    ]
    return [own_key] + same if same else []


def startup_problems(line: str, role: str, env: Mapping[str, str]) -> list[str]:
    """一次返回全部配置问题。每条只写键名，不写键值。"""
    problems = []
    missing = [key for key in required_keys(line) if not _present(env, key)]
    if missing:
        problems.append("缺少环境变量: " + ",".join(missing))
    legacy = [key for key in LEGACY_KEYS if key in env]
    if legacy:
        problems.append("旧的无前缀键仍在环境里，请改用线路前缀键并删除: " + ",".join(legacy))
    duplicated = _duplicate_device_keys(line, env)
    if duplicated:
        problems.append("设备号与其它线路重复: " + ",".join(duplicated))
    bad_urls = [key for key in _URL_KEYS[line] if _present(env, key) and not _url_ok(env.get(key))]
    if all(_present(env, key) for key in CENTER_KEYS) and not _url_ok(center_base_url(env)):
        bad_urls = ["CENTER_PROTOCOL/CENTER_HOST"] + bad_urls
    if bad_urls:
        problems.append("URL 需要 http(s) scheme 和主机名: " + ",".join(bad_urls))
    if line == "xiaola" and role == "submit":
        bad_numbers = []
        for key, default, integer in _XIAOLA_NUMBERS:
            try:
                _positive_number(env.get(key), default, integer)
            except ValueError:
                bad_numbers.append(key)
        if bad_numbers:
            problems.append("数值必须是有限的正数: " + ",".join(bad_numbers))
        elif not threads_to_start(env):
            problems.append("赢啦提交没有任何国家码: THREADn_COUNTRY_CODE")
    return problems


def alert_user_ids(line: str, env: Mapping[str, str]) -> tuple[str, ...]:
    raw = str(env.get(_ALERT_KEY[line], "")).strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def country_for_thread(thread_id: int, env: Mapping[str, str]) -> str:
    """线程国家码缺了就是空。调用方不得改充别的国家。"""
    return str(env.get(f"THREAD{thread_id}_COUNTRY_CODE", "") or "").strip()


def threads_to_start(env: Mapping[str, str]) -> list[tuple[int, str]]:
    """只启动写了国家码的赢啦线程。MAX_THREADS 已在启动校验里解析过。"""
    count = _positive_number(env.get("MAX_THREADS"), 1, True)
    started = []
    for thread_id in range(1, count + 1):
        country = country_for_thread(thread_id, env)
        if country:
            started.append((thread_id, country))
    return started
