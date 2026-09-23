"""一份 Center 客户端。签名不含 api_result，每个动作只提交一次。"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from nyh_line.center.clean import clean_api_result
from nyh_line.center.profiles import PullProfile

logger = logging.getLogger(__name__)

DEFAULT_VERSION = "2019_11_11__12_04_05"


class CenterClient:
    """向 Center 拉单、回写和同步余额。不写订单同步状态，也不回调代理端。"""

    def __init__(
        self,
        *,
        base_url: str,
        device_id,
        device_key: str,
        sim_id,
        profile: PullProfile,
        transport,
        version: str = DEFAULT_VERSION,
        country_code: str = "",
        fee_charging_line: str = "",
        alert_user_ids: tuple[str, ...] = (),
        clock=None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.device_id = device_id
        self.device_key = str(device_key)
        self.sim_id = sim_id
        self.profile = profile
        self.transport = transport
        self.version = version or DEFAULT_VERSION
        self.country_code = country_code or ""
        self.fee_charging_line = fee_charging_line or ""
        self.alert_user_ids = tuple(alert_user_ids)
        self.clock = clock or time.time

    def signature(self, action: str, when) -> str:
        """签名只拼接 action、设备号、设备密钥和时间。"""
        raw = f"{action}{self.device_id}{self.device_key}{when}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _now(self) -> int:
        return int(self.clock())

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _base_fields(self, action: str, sim_balance) -> dict:
        when = self._now()
        body = {
            "device_id": self.device_id,
            "sim_id": self.sim_id,
            "time": when,
            "action": action,
            "ver": self.version,
        }
        if sim_balance is not None:
            body["sim_balance"] = sim_balance
        return body

    def _sign_and_post(self, url: str, body: dict):
        """签名后提交一次。响应丢失也不再发第二遍。"""
        body["sign"] = self.signature(body["action"], body["time"])
        try:
            response = self.transport.post(url, body)
        except Exception as exc:
            logger.error("Center 请求失败 action=%s error=%s", body.get("action"), type(exc).__name__)
            return {"code": -1, "msg": "网络请求失败"}
        return self._read_json(response)

    def _read_json(self, response) -> dict:
        if response is None:
            return {"code": -1, "msg": "空响应"}
        try:
            payload = response.json()
        except Exception:
            return {"code": -1, "msg": "JSON解析失败"}
        if not isinstance(payload, dict):
            return {"code": -1, "msg": "JSON解析失败"}
        return payload

    def get_task(self) -> dict:
        """拉一笔任务。code 非 0 原样返回，不回写，也不当成异常抛出。"""
        body = self._base_fields("GetTasks", self.profile.sim_balance)
        if self.profile.include_empty_key:
            body[""] = 0
        if self.profile.include_line_country:
            body["fee_charging_line"] = self.fee_charging_line
            body["country_code"] = self.country_code
        return self._sign_and_post(self._url(self.profile.get_task_path), body)

    def feedback(self, task_id, status, api_result=None) -> dict:
        """回写一次。序列化失败就省略 api_result，状态仍然发送。"""
        body = self._base_fields("Feedback", None)
        body["task_id"] = task_id
        body["status"] = status
        body["line"] = 0
        if api_result is not None:
            try:
                body["api_result"] = json.dumps(
                    clean_api_result(api_result), ensure_ascii=False
                )
            except Exception:
                logger.error("运营商响应序列化失败，跳过 api_result task_id=%s", task_id)
        result = self._sign_and_post(self._url(self.profile.feedback_path), body)
        code = _code_of(result)
        level = logging.INFO if code == "0" else logging.WARNING
        logger.log(level, "Feedback task_id=%s status=%s code=%s", task_id, status, code)
        return result

    def in_executing_tasks(self) -> dict:
        """取执行中的发送行。VTSI 和赢啦都走 VTSI 口。"""
        body = self._base_fields("inExecutingTasks", None)
        return self._sign_and_post(self._url(self.profile.executing_path), body)

    def synchronize_balance(self, balance) -> dict:
        """把已经解析出的余额交给本 profile 的余额口。调用方负责不要传入占位 0。"""
        body = self._base_fields("queryAmountCallback", None)
        body["balance"] = balance
        return self._sign_and_post(self._url(self.profile.balance_path), body)

    def sms_receiving(self, body_text: str, reception_number: str, task_id) -> dict:
        """写一条短信接收。这不是 Feedback。"""
        body = self._base_fields("SMSContentReceiving", 0)
        body["body"] = body_text
        body["reception_number"] = reception_number
        body["task_id"] = task_id
        result = self._sign_and_post(self._url(self.profile.feedback_path), body)
        if _code_of(result) != "0":
            logger.warning("短信接收写入失败 task_id=%s code=%s", task_id, _code_of(result))
        return result

    def send_template(self, task_id, code) -> dict:
        """按本线路配置的收件人各发一条模板。收件人为空则不发。"""
        last = {"code": 0, "msg": "无收件人"}
        for user_id in self.alert_user_ids:
            body = self._base_fields("Template_sending", 0)
            body["user_id"] = user_id
            body["first"] = "线路充值出现错误"
            body["keyword1"] = ""
            body["keyword2"] = f"报错订单:{task_id}"
            body["keyword3"] = ""
            body["remark"] = f"错误编号:{code}"
            last = self._sign_and_post(self._url(self.profile.feedback_path), body)
            if _code_of(last) != "0":
                logger.warning("模板发送失败 task_id=%s user_id=%s code=%s", task_id, user_id, _code_of(last))
        return last


def _code_of(result) -> str:
    """Center 返回的 code 收成文本，便于比较和记日志。"""
    return str(result.get("code")) if isinstance(result, dict) else "-1"
