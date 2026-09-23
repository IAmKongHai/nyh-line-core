"""VTSI 提交和查单。会话可以重建，TOPUP 和查询命令发出后不重试。"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

TOPUP_COMMAND = "TOPUP"
RESULT_COMMAND = "GETTRANSDETAILSBYMERCHANTID"


def as_code(value):
    """比较前把数字和同值字符串收成同一段文本。空值视为没有结果码。"""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if text == "":
        return None
    return text


def result_code_of(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    execute = payload.get("Body", {})
    if not isinstance(execute, dict):
        return None
    execute = execute.get("ExecuteResponse", {})
    if not isinstance(execute, dict):
        return None
    return as_code(execute.get("resultCode"))


def status_code_of(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    execute = payload.get("Body", {}).get("ExecuteResponse", {})
    if not isinstance(execute, dict):
        return None
    returned = execute.get("return")
    if not isinstance(returned, dict):
        return None
    return as_code(returned.get("statusCode"))


def parse_wallet_balance(payload) -> str:
    """从余额 XML 取出最后一个钱包余额。解析不了就抛错，调用方不得改写成 0。"""
    if not isinstance(payload, dict):
        raise ValueError("余额响应不是对象")
    execute = payload.get("Body", {}).get("ExecuteResponse", {})
    if not isinstance(execute, dict):
        raise ValueError("余额响应缺少 ExecuteResponse")
    xml_text = execute.get("return")
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise ValueError("余额响应缺少 XML")
    root = ET.fromstring(xml_text)
    wallets = root.findall("wallet")
    if not wallets:
        raise ValueError("余额响应没有 wallet")
    balance = wallets[-1].findtext("balance")
    if balance is None or str(balance).strip() == "":
        raise ValueError("余额字段为空")
    return str(balance).strip()


class VtsiGateway:
    """每次命令前新建会话。会话工厂失败可以再试，命令本身只执行一次。"""

    def __init__(self, session_factory, *, timeout: float, session_attempts: int = 3):
        self.session_factory = session_factory
        self.timeout = timeout
        self.session_attempts = session_attempts

    def _open_session(self):
        last_error = None
        for _ in range(self.session_attempts):
            try:
                return self.session_factory()
            except Exception as exc:
                last_error = exc
        if last_error is None:
            raise RuntimeError("无法创建 VTSI 会话")
        raise last_error

    def execute(self, command: str, payload: dict):
        session = self._open_session()
        return session.execute(command, payload)

    def topup(self, *, merchant_transaction_id: str, phone: str, sku: str):
        return self.execute(
            TOPUP_COMMAND,
            {
                "merchantTransactionId": merchant_transaction_id,
                "mobileNo": phone,
                "sku": sku,
            },
        )

    def query(self, merchant_transaction_id: str):
        return self.execute(
            RESULT_COMMAND,
            {"merchantTransactionId": merchant_transaction_id},
        )

    def balance(self):
        return self.execute("BALANCE", {})


def zeep_session_factory(wsdl: str, timeout: float, verify):
    """真实 SOAP 会话。测试不调用这里，避免打开 WSDL。"""
    import requests
    import zeep
    from zeep.transports import Transport

    session = requests.Session()
    session.verify = verify
    client = zeep.Client(wsdl, transport=Transport(session=session, timeout=timeout))

    class ZeepSession:
        def execute(self, command, payload):
            return client.service.Execute({"command": command, **payload})

    return ZeepSession()


def _sms(center, content, phone: str, task_id) -> None:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    center.sms_receiving(text, phone, task_id)


def submit_vtsi_task(task: dict, center, gateway: VtsiGateway, *, terminal_fail: set[str]) -> str:
    """提交前写一条短信，拿到响应再写一条。两条都不看 resultCode。"""
    task_id = task["task_id"]
    phone = "0" + str(task["phone_number"])
    merchant_id = "v" + str(task_id)
    request_body = {
        "phone": phone,
        "SKU": task.get("vtsi_sku"),
        "merchantTransactionId": merchant_id,
    }
    _sms(center, request_body, phone, task_id)
    try:
        result = gateway.topup(
            merchant_transaction_id=merchant_id,
            phone=phone,
            sku=task.get("vtsi_sku"),
        )
    except Exception:
        return "transport-failed"
    _sms(center, result, phone, task_id)
    code = result_code_of(result)
    if code is None or code == "2":
        return "accepted" if code == "2" else "missing-code"
    status = 3 if code in terminal_fail else 0
    center.feedback(task_id, status, result)
    center.send_template(task_id, code)
    return f"feedback-{status}"


_CHECK_TERMINAL = {"2": 2, "3": 3, "25": 0}


def check_vtsi_task(record: dict, center, gateway: VtsiGateway) -> str:
    """查单从上到下只命中一行。短信只在表里写明的时候写。"""
    task_id = record["id"]
    merchant_id = "v" + str(task_id)
    try:
        result = gateway.query(merchant_id)
    except Exception:
        return "transport-failed"
    code = result_code_of(result)
    if code == "25":
        center.feedback(task_id, 0, result)
        return "rollback"
    if code != "2":
        return "ignore"
    phone = "0" + str(record.get("user_number", ""))
    _sms(center, result, phone, task_id)
    status_code = status_code_of(result)
    mapped = _CHECK_TERMINAL.get(status_code)
    if mapped is None:
        return "pending"
    center.feedback(task_id, mapped, result)
    return f"feedback-{mapped}"
