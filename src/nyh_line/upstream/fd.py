"""菲岛下单。auth 是字段值按固定顺序拼接再加 key 的 md5，与设备签名不是同一个算法。"""

from __future__ import annotations

import hashlib

from nyh_line.errors import RequestNotSent

MAINTENANCE_MESSAGE = "充值套餐维护中"
ORDER_PATH = "api/fd/rechargeOrder/create"
REQUIRED_TASK_FIELDS = ("task_id", "phone_number", "fd_content_pcode", "fd_content_money")


def fd_auth(fields: dict, key: str) -> str:
    """按 uid、pcode、phone、money、orderId 的顺序拼接，再加上游 key。"""
    ordered = ("uid", "pcode", "phone", "money", "orderId")
    raw = "".join(str(fields[name]) for name in ordered) + str(key)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _as_error(message: str) -> dict:
    return {"code": "ERROR", "message": message, "success": False}


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
        except Exception:
            return _as_error("网络请求失败")
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


def submit_fd_task(task: dict, center, client: FdClient, *, notify: bool) -> str:
    """按回写表处理一笔菲岛提交。会回写的失败不附 api_result。"""
    missing = [name for name in REQUIRED_TASK_FIELDS if task.get(name) in (None, "")]
    task_id = task.get("task_id")
    if missing:
        center.feedback(task_id, 0)
        if notify:
            _notify(center, task_id, "MISSING_FIELD")
        return "missing-field"

    phone = "0" + str(task["phone_number"])
    try:
        result = client.create_order(
            phone=phone,
            pcode=task["fd_content_pcode"],
            money=task["fd_content_money"],
            order_id=task_id,
        )
    except RequestNotSent:
        center.feedback(task_id, 0)
        if notify:
            _notify(center, task_id, "NOT_SENT")
        return "not-sent"

    code = result.get("code")
    if code is None or str(code) == "ERROR":
        return "unknown"
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
