"""菲岛下单。auth 是字段值按固定顺序拼接再加 key 的 md5，与设备签名不是同一个算法。"""

from __future__ import annotations

import hashlib
import logging

from nyh_line.errors import RequestNotSent
from nyh_line.log_setup import log_safe

logger = logging.getLogger(__name__)

MAINTENANCE_MESSAGE = "充值套餐维护中"
ORDER_PATH = "api/fd/rechargeOrder/create"
REQUIRED_TASK_FIELDS = ("task_id", "phone_number", "fd_content_pcode", "fd_content_money")


def fd_auth(fields: dict, key: str) -> str:
    """按 uid、pcode、phone、money、orderId 的顺序拼接，再加上游 key。"""
    ordered = ("uid", "pcode", "phone", "money", "orderId")
    raw = "".join(str(fields[name]) for name in ordered) + str(key)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _as_error(message: str, error_type: str = "") -> dict:
    """本地哨兵。error_type 记下传输异常类型，只供日志和暂停判断，不回写。"""
    return {"code": "ERROR", "message": message, "success": False, "error_type": error_type}


def money_as_int(value):
    """金额必须是整数文本。"50.00" 这类值不截断，返回 None 交给缺字段分支。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def unknown_reason(result: dict) -> str:
    """结果未知的原因：传输异常类型，其次本地哨兵说明，都没有就是缺 code。"""
    if str(result.get("code")) == "ERROR":
        return result.get("error_type") or result.get("message") or "ERROR"
    return "缺少 code"


class FdClient:
    """下单 URL 只请求一次。写出前失败抛 RequestNotSent，不伪装成业务码。"""

    def __init__(self, *, base_url: str, uid: str, key: str, transport):
        self.base_url = str(base_url).rstrip("/") + "/"
        self.uid = uid
        self.key = key
        self.transport = transport

    def create_order(self, *, phone: str, pcode, money, order_id) -> dict:
        body = {
            "uid": self.uid,
            "pcode": pcode,
            "phone": phone,
            "money": int(money),
            "orderId": order_id,
        }
        body["auth"] = fd_auth(body, self.key)
        url = self.base_url + ORDER_PATH
        try:
            response = self.transport.post(url, body)
        except RequestNotSent:
            raise
        except Exception as exc:
            return _as_error("网络请求失败", type(exc).__name__)
        if response is None:
            return _as_error("空响应")
        text = getattr(response, "text", None)
        if text is not None and str(text).strip() == "":
            return _as_error("空响应")
        try:
            payload = response.json()
        except Exception:
            return _as_error("非 JSON")
        if not isinstance(payload, dict):
            return _as_error("非 JSON")
        return payload


def _notify(center, task_id, code) -> None:
    center.send_template(task_id, code)


def submit_fd_task(task: dict, center, client: FdClient, *, notify: bool, on_unknown=None) -> str:
    """按回写表处理一笔菲岛提交。会回写的失败不附 api_result。

    on_unknown 收到结果未知的原因，供提交循环判断是否暂停拉单。
    """
    missing = [name for name in REQUIRED_TASK_FIELDS if task.get(name) in (None, "")]
    task_id = task.get("task_id")
    money = money_as_int(task.get("fd_content_money"))
    if missing or money is None:
        logger.warning("菲岛任务字段不全或金额不是整数，回写 0 task_id=%s missing=%s", task_id, ",".join(missing))
        center.feedback(task_id, 0)
        if notify:
            _notify(center, task_id, "MISSING_FIELD")
        return "missing-field"

    phone = "0" + str(task["phone_number"])
    try:
        result = client.create_order(
            phone=phone,
            pcode=task["fd_content_pcode"],
            money=money,
            order_id=task_id,
        )
    except RequestNotSent as exc:
        logger.warning("菲岛下单没发出，回写 0 task_id=%s reason=%s", task_id, exc)
        center.feedback(task_id, 0)
        if notify:
            _notify(center, task_id, "NOT_SENT")
        return "not-sent"

    code = result.get("code")
    if code is None or str(code) == "ERROR":
        reason = unknown_reason(result)
        logger.error("菲岛下单结果未知，不回写 task_id=%s reason=%s", task_id, reason)
        if on_unknown is not None:
            on_unknown(reason)
        return "unknown"
    logger.info("菲岛下单 task_id=%s code=%s resp=%s", task_id, code, log_safe(result))
    if str(code) == "0000":
        maintaining = result.get("success") is False and result.get("message") == MAINTENANCE_MESSAGE
        if not maintaining:
            return "accepted"
        center.feedback(task_id, 0)
        if notify:
            _notify(center, task_id, code)
        return "maintenance"
    center.feedback(task_id, 0)
    if notify:
        _notify(center, task_id, code)
    return "business-fail"
