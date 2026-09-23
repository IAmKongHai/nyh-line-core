"""VTSI 提交和查单。会话可以重建，TOPUP 和查询命令发出后不重试。"""

from __future__ import annotations

import datetime
import hashlib
import json
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import xmltodict

TOPUP_COMMAND = "TOPUP"
RESULT_COMMAND = "GETTRANSDETAILSBYMERCHANTID"
BALANCE_COMMAND = "GETWALLETBALANCE"
CA_FILES = (
    "gd_tls_issuing_dv-r1v1.pem",
    "gd_tls_root-r1.pem",
)


def repo_cert_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "certs"


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def meta_to_xml(meta: dict) -> str:
    """把 meta 收成一段 XML，交给 Execute 的 data 字段。"""
    return xmltodict.unparse({"meta": meta}, pretty=False)


def build_authenticated_execute(command: str, meta: dict, *, session_id: str, username: str, password: str) -> dict:
    """组一笔已认证的 Execute。密码是 sha1(用户名 + 密码 + sessionId)。"""
    return {
        "sessionId": session_id,
        "username": username,
        "password": sha1_hex(f"{username}{password}{session_id}"),
        "command": command,
        "data": meta_to_xml(meta),
    }


def parse_soap_element(element):
    """把 SOAP 元素收成 dict。叶子节点留下文本，命名空间前缀丢掉。"""
    children = list(element)
    text = (element.text or "").strip()
    if not children:
        return text
    parsed = {}
    for child in children:
        tag = child.tag.split("}")[-1]
        parsed[tag] = parse_soap_element(child)
    return parsed


def parse_soap_response(raw):
    """原始 XML、带 content 的响应，或已经是 dict 的结果，都收成决策用的对象。"""
    if isinstance(raw, dict):
        return raw
    if hasattr(raw, "content"):
        raw = raw.content
    if isinstance(raw, bytes):
        raw = raw.decode("ISO-8859-1")
    if not isinstance(raw, str):
        raise ValueError("无法读取 VTSI 响应")
    parsed = parse_soap_element(ET.fromstring(raw))
    if not isinstance(parsed, dict):
        raise ValueError("VTSI 响应不是对象")
    return parsed


def normalize_execute_result(parsed: dict) -> dict:
    """resultCode 为 2 且 return 仍是 XML 时，拆成标签字典，供 statusCode 读取。"""
    execute = parsed.get("Body", {}).get("ExecuteResponse", {})
    if not isinstance(execute, dict):
        return parsed
    returned = execute.get("return")
    if as_code(execute.get("resultCode")) == "2" and isinstance(returned, str) and returned.strip().startswith("<"):
        root = ET.fromstring(returned)
        execute["return"] = {child.tag.split("}")[-1]: (child.text or "") for child in list(root)}
    return parsed


def combined_ca_bundle() -> str:
    """certifi 加上仓库里的两张 GoDaddy 公共 CA。校验不能只开系统默认。"""
    import certifi

    sources = [Path(certifi.where())]
    sources.extend(repo_cert_dir() / name for name in CA_FILES)
    handle = tempfile.NamedTemporaryFile("w", prefix="nyh-line-ca-", suffix=".pem", delete=False)
    with handle:
        for path in sources:
            data = path.read_text(encoding="utf-8")
            if data and not data.endswith("\n"):
                data += "\n"
            handle.write(data)
    return handle.name


def configure_http_session(http):
    """把仓库 CA 挂到 requests 会话上。"""
    http.verify = combined_ca_bundle()
    return http


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
    """每次命令前新建 VTSI session。会话工厂失败可以再试，命令本身只执行一次。"""

    def __init__(
        self,
        session_factory,
        *,
        username: str,
        password: str,
        account: str,
        timeout: float,
        session_attempts: int = 3,
        clock=None,
    ):
        self.session_factory = session_factory
        self.username = username
        self.password = password
        self.account = account
        self.timeout = timeout
        self.session_attempts = session_attempts
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))

    def _open_authenticated_session(self):
        last_error = None
        for _ in range(self.session_attempts):
            try:
                session = self.session_factory()
                session_id = session.create_session(self.username)
                return session, session_id
            except Exception as exc:
                last_error = exc
        if last_error is None:
            raise RuntimeError("无法创建 VTSI 会话")
        raise last_error

    def execute(self, command: str, meta: dict, *, normalize: bool = True):
        session, session_id = self._open_authenticated_session()
        body = build_authenticated_execute(
            command,
            meta,
            session_id=session_id,
            username=self.username,
            password=self.password,
        )
        raw = session.execute(body)
        parsed = parse_soap_response(raw)
        if normalize:
            return normalize_execute_result(parsed)
        return parsed

    def topup(self, *, merchant_transaction_id: str, phone: str, sku: str):
        moment = self.clock()
        merchant_date = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.execute(
            TOPUP_COMMAND,
            {
                "merchantTransactionId": merchant_transaction_id,
                "merchantDate": merchant_date,
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
        return self.execute(
            BALANCE_COMMAND,
            {"accountNo": self.account},
            normalize=False,
        )


def zeep_session_factory(wsdl: str, timeout: float):
    """真实 SOAP 会话。测试不调用这里，避免打开 WSDL。Execute 只发一次。"""
    import requests
    import zeep
    from zeep.transports import Transport

    http = configure_http_session(requests.Session())
    client = zeep.Client(wsdl, transport=Transport(session=http, timeout=timeout))

    class ZeepSession:
        def create_session(self, username: str) -> str:
            with client.settings(raw_response=True):
                response = client.service.CreateSession(username=username)
            parsed = parse_soap_response(response)
            created = parsed.get("Body", {}).get("CreateSessionResponse", {})
            session_id = created.get("sessionId") if isinstance(created, dict) else None
            if as_code(created.get("resultCode") if isinstance(created, dict) else None) != "2" or not session_id:
                raise RuntimeError("获取 session 失败")
            return str(session_id)

        def execute(self, request_map: dict):
            with client.settings(raw_response=True):
                return client.service.Execute(**request_map)

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
