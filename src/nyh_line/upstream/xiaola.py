"""赢啦下单和查单。下单发出后不重试。本地 error_code 不是业务码。"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

MISSING_PRODUCT = {
    "code": "MISSING_PRODUCT_CODE",
    "message": "任务数据中缺少产品代码",
}
_LOCAL_ERRORS = {"-1", "-2"}


def as_code(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def parse_xiaola_balance(payload):
    """余额字段缺失或业务码不对时抛错，调用方不得改写成 0。"""
    if not isinstance(payload, dict) or as_code(payload.get("code")) != "10000":
        raise ValueError("赢啦余额查询失败")
    result = payload.get("result")
    if not isinstance(result, dict) or "balance" not in result:
        raise ValueError("赢啦余额字段缺失")
    balance = result.get("balance")
    if balance is None or str(balance).strip() == "":
        raise ValueError("赢啦余额字段为空")
    return balance


class XiaolaClient:
    """赢啦 HTTP。超时和调用异常原样抛出，解析失败返回只有 error_code 的字典。"""

    def __init__(self, *, base_url: str, username: str, secret_key: str, transport):
        self.base_url = str(base_url).rstrip("/")
        self.username = username
        self.secret_key = secret_key
        self.transport = transport

    def _post_once(self, path: str, params: dict) -> dict:
        url = self.base_url + path
        response = self.transport.post(url, params)
        if response is None:
            return {"error_code": -2, "error_msg": "空响应"}
        try:
            payload = response.json()
        except Exception as exc:
            return {"error_code": -2, "error_msg": f"响应JSON解析失败: {exc}"}
        if not isinstance(payload, dict):
            return {"error_code": -2, "error_msg": "响应不是对象"}
        return payload

    def create_order(self, *, user_order_no: str, product_code, recharge_no: str) -> dict:
        return self._post_once(
            "/globe_api/public/topup",
            {
                "user_order_no": user_order_no,
                "product_code": product_code,
                "recharge_no": recharge_no,
                "uid": self.username,
            },
        )

    def query_order(self, user_order_no: str) -> dict:
        return self._post_once(
            "/globe_api/public/order",
            {"uid": self.username, "user_order_no": user_order_no},
        )

    def read_balance(self) -> dict:
        return self._post_once("/globe_api/public/dl/balance", {"uid": self.username})


def _is_local_sentinel(result: dict) -> bool:
    if "code" in result:
        return False
    return as_code(result.get("error_code")) in _LOCAL_ERRORS


def submit_xiaola_task(task: dict, center, client: XiaolaClient, blocklist) -> str:
    """拦截和缺产品代码都不打上游。明确业务失败才回写并带响应。"""
    task_id = task["task_id"]
    phone = str(task.get("phone_number", ""))
    if blocklist is not None and blocklist.is_blocked(phone):
        center.feedback(task_id, 3)
        return "blocked"
    product_code = task.get("content")
    if product_code in (None, ""):
        center.feedback(task_id, 3, dict(MISSING_PRODUCT))
        center.send_template(task_id, "MISSING_PRODUCT_CODE")
        return "missing-product"
    user_order_no = "nyh" + str(task_id)
    recharge_no = "0" + phone
    try:
        result = client.create_order(
            user_order_no=user_order_no,
            product_code=product_code,
            recharge_no=recharge_no,
        )
    except Exception:
        return "transport-failed"
    if not isinstance(result, dict) or _is_local_sentinel(result) or as_code(result.get("code")) is None:
        return "unknown"
    if as_code(result.get("code")) == "10000":
        return "accepted"
    center.feedback(task_id, 0, result)
    center.send_template(task_id, result.get("code"))
    return "business-fail"


def check_xiaola_task(record: dict, center, client: XiaolaClient) -> str:
    """查单看 result.state，不看外层 message，不写短信接收。"""
    task_id = record["id"]
    user_order_no = "nyh" + str(task_id)
    try:
        result = client.query_order(user_order_no)
    except Exception:
        return "transport-failed"
    if not isinstance(result, dict):
        return "unknown"
    code = as_code(result.get("code"))
    if code in {"10005", "10006"}:
        center.feedback(task_id, 0, result)
        return "rollback"
    if code != "10000":
        return "ignore"
    state_node = result.get("result")
    state_value = state_node.get("state") if isinstance(state_node, dict) else None
    state = as_code(state_value)
    if state == "1":
        return "pending"
    if state in {"2", "3"}:
        center.feedback(task_id, int(state), result)
        return f"feedback-{state}"
    logger.info("赢啦查单未知状态 task_id=%s state=%s", task_id, state_value)
    return "logged"
