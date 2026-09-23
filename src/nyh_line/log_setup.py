"""进程日志：写标准输出，带时间、级别、线程名。轮转交给 supervisor。

业务日志只记白名单字段。需要附上游响应时先过 log_safe，请求体一律不记。
"""

from __future__ import annotations

import json
import logging
import sys

from nyh_line.center.clean import clean_api_result

LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"

# 第三方库的调试日志不进生产输出。
QUIET_LIBRARIES = ("zeep", "urllib3", "requests")

# 在 clean_api_result 之外，日志里再去掉这些键。比较前去掉下划线并转小写。
_LOG_DROP = {"password", "key", "devicekey", "secretkey", "auth", "sessionid"}

_HANDLER_MARK = "_nyh_line_stdout"


def configure_logging(stream=None, level: int = logging.INFO) -> None:
    """给根 logger 挂一个 stdout handler。重复调用不会重复挂。"""
    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(handler, _HANDLER_MARK, False) for handler in root.handlers):
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


def _drop_log_keys(value):
    if isinstance(value, dict):
        return {
            key: _drop_log_keys(item)
            for key, item in value.items()
            if str(key).lower().replace("_", "") not in _LOG_DROP
        }
    if isinstance(value, list):
        return [_drop_log_keys(item) for item in value]
    return value


def log_safe(value) -> str:
    """把上游响应收成一段可以写进日志的文本：先 clean_api_result，再去掉日志专用的敏感键。"""
    try:
        return json.dumps(_drop_log_keys(clean_api_result(value)), ensure_ascii=False, default=str)
    except Exception:
        return "<无法序列化>"
