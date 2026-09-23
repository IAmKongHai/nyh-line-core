"""VTSI 三条线路的提交、查单、短信和 TOPUP 次数。"""

import hashlib
import logging
import threading
from pathlib import Path

import pytest
import requests

from nyh_line.lines import dito_vtsi, globe_vtsi, registry, smart_vtsi
from nyh_line.upstream import vtsi as vtsi_module

VTSI_LINES = (dito_vtsi, globe_vtsi, smart_vtsi)
from nyh_line.upstream.vtsi import (
    CA_FILES,
    SoapSession,
    VtsiGateway,
    configure_http_session,
    parse_wallet_balance,
    repo_cert_dir,
    result_code_of,
)
from tests.support import FakeTransport, actions, make_center

SUBMIT = {
    "dito": dito_vtsi,
    "smart": smart_vtsi,
    "globe": globe_vtsi,
}


def soap(result_code=None, status_code=None, include_result=True):
    execute = {}
    if include_result:
        execute["resultCode"] = result_code
    if status_code is not None:
        execute["return"] = {"statusCode": status_code}
    return {"Body": {"ExecuteResponse": execute}}


class Session:
    def __init__(self, result=None, error=None, sink=None, session_id="SID"):
        self.result = result
        self.error = error
        self.sink = sink if sink is not None else []
        self.session_id = session_id

    def create_session(self, username):
        return self.session_id

    def execute(self, request_map):
        self.sink.append(request_map)
        if self.error is not None:
            raise self.error
        return self.result


def _gateway(session, timeout=10):
    return VtsiGateway(
        lambda: session,
        username="nyh-user",
        password="pw",
        account="ACC100",
        timeout=timeout,
    )


def _submit(line, result=None, error=None):
    sink = []
    session = Session(result=result, error=error, sink=sink)
    gateway = _gateway(session)
    transport = FakeTransport()
    center = make_center(transport)
    outcome = SUBMIT[line].submit_task(
        {"task_id": 9, "phone_number": "9123456789", "vtsi_sku": "SKU1"},
        center,
        gateway,
    )
    return outcome, transport, sink


def _check(result):
    sink = []
    session = Session(result=result, sink=sink)
    gateway = _gateway(session)
    transport = FakeTransport()
    center = make_center(transport)
    outcome = dito_vtsi.check_task({"id": 8, "user_number": "9123456789"}, center, gateway)
    return outcome, transport, sink


@pytest.mark.parametrize(
    "line,code,status",
    [
        ("dito", "25", 3),
        ("dito", "1", 3),
        ("dito", "3", 0),
        ("dito", 3, 0),
        ("smart", "25", 3),
        ("smart", 1, 3),
        ("globe", "25", 3),
        ("globe", "1", 0),
        ("globe", 1, 0),
    ],
)
def test_submit_explicit_code_feedbacks_once(line, code, status):
    _outcome, transport, sink = _submit(line, soap(code))
    feedbacks = actions(transport, "Feedback")
    assert [body["status"] for body in feedbacks] == [status]
    assert "api_result" in feedbacks[0]
    assert len(actions(transport, "Template_sending")) == 1
    assert len(actions(transport, "SMSContentReceiving")) == 2
    assert [item["command"] for item in sink] == ["TOPUP"]
    assert "<merchantTransactionId>v9</merchantTransactionId>" in sink[0]["data"]
    assert "<mobileNo>09123456789</mobileNo>" in sink[0]["data"]


@pytest.mark.parametrize("line", ["dito", "smart", "globe"])
@pytest.mark.parametrize("code", ["2", 2])
def test_submit_result_two_does_not_feedback_and_writes_two_sms(line, code):
    _outcome, transport, sink = _submit(line, soap(code))
    assert actions(transport, "Feedback") == []
    assert actions(transport, "Template_sending") == []
    assert len(actions(transport, "SMSContentReceiving")) == 2
    assert len(sink) == 1


@pytest.mark.parametrize("line", ["dito", "smart", "globe"])
def test_missing_result_code_does_not_feedback(line):
    _outcome, transport, _sink = _submit(line, soap(include_result=False))
    assert actions(transport, "Feedback") == []
    assert len(actions(transport, "SMSContentReceiving")) == 2


@pytest.mark.parametrize("line", ["dito", "smart", "globe"])
def test_submit_timeout_writes_one_sms_and_does_not_feedback(line):
    _outcome, transport, sink = _submit(line, error=TimeoutError("read timeout"))
    assert actions(transport, "Feedback") == []
    assert actions(transport, "Template_sending") == []
    assert len(actions(transport, "SMSContentReceiving")) == 1
    assert [item["command"] for item in sink] == ["TOPUP"]


def test_session_create_can_retry_but_topup_runs_once():
    sink = []
    attempts = {"n": 0}

    def factory():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("session down")
        return Session(result=soap("2"), sink=sink)

    gateway = VtsiGateway(
        factory,
        username="nyh-user",
        password="pw",
        account="ACC100",
        timeout=50,
    )
    transport = FakeTransport()
    dito_vtsi.submit_task(
        {"task_id": 3, "phone_number": "900", "vtsi_sku": "S"},
        make_center(transport),
        gateway,
    )
    assert attempts["n"] == 3
    assert [item["command"] for item in sink] == ["TOPUP"]


def test_each_command_opens_a_new_session():
    """工厂内部可以复用 client，但每条命令前都要 CreateSession 一次。"""
    opened = {"n": 0}

    class Counted(Session):
        def create_session(self, username):
            opened["n"] += 1
            return super().create_session(username)

    shared = Counted(result=soap("2"))

    def factory():
        return shared

    gateway = VtsiGateway(
        factory,
        username="nyh-user",
        password="pw",
        account="ACC100",
        timeout=50,
    )
    gateway.topup(merchant_transaction_id="v1", phone="0900", sku="S")
    gateway.query("v1")
    assert opened["n"] == 2


@pytest.mark.parametrize(
    "result_code,status_code,feedback_status,sms_count",
    [
        ("25", "2", 0, 0),
        (25, 2, 0, 0),
        ("3", "2", None, 0),
        ("", "2", None, 0),
        ("2", "1", None, 1),
        ("2", "4", None, 1),
        ("2", "9", None, 1),
        ("2", "2", 2, 1),
        ("2", "3", 3, 1),
        ("2", "25", 0, 1),
        (2, 2, 2, 1),
        (2, 3, 3, 1),
        (2, 25, 0, 1),
    ],
)
def test_check_matches_one_row(result_code, status_code, feedback_status, sms_count):
    _outcome, transport, sink = _check(soap(result_code, status_code))
    feedbacks = actions(transport, "Feedback")
    if feedback_status is None:
        assert feedbacks == []
    else:
        assert [body["status"] for body in feedbacks] == [feedback_status]
        assert "api_result" in feedbacks[0]
    assert len(actions(transport, "SMSContentReceiving")) == sms_count
    assert "<merchantTransactionId>v8</merchantTransactionId>" in sink[0]["data"]
    assert sink[0]["command"] == "GETTRANSDETAILSBYMERCHANTID"
    assert len(sink) == 1


def test_check_timeout_does_not_feedback_or_sms():
    sink = []
    gateway = _gateway(Session(error=TimeoutError("read"), sink=sink))
    transport = FakeTransport()
    dito_vtsi.check_task({"id": 8, "user_number": "9123456789"}, make_center(transport), gateway)
    assert actions(transport, "Feedback") == []
    assert actions(transport, "SMSContentReceiving") == []
    assert len(sink) == 1


def _soap_with_return_xml(result_code: str, inner_xml: str) -> str:
    escaped = inner_xml.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<Envelope><Body><ExecuteResponse>"
        f"<resultCode>{result_code}</resultCode><return>{escaped}</return>"
        "</ExecuteResponse></Body></Envelope>"
    )


def test_fake_session_signs_topup_and_parses_result_code():
    sink = []
    session = Session(
        result="<Envelope><Body><ExecuteResponse><resultCode>25</resultCode></ExecuteResponse></Body></Envelope>",
        sink=sink,
        session_id="SID42",
    )
    gateway = VtsiGateway(
        lambda: session,
        username="nyh-user",
        password="pw",
        account="ACC100",
        timeout=10,
    )
    transport = FakeTransport()
    dito_vtsi.submit_task(
        {"task_id": 9, "phone_number": "9123456789", "vtsi_sku": "SKU1"},
        make_center(transport),
        gateway,
    )
    body = sink[0]
    assert body["sessionId"] == "SID42"
    assert body["username"] == "nyh-user"
    assert body["password"] == hashlib.sha1(b"nyh-userpwSID42").hexdigest()
    assert body["command"] == "TOPUP"
    assert "<sku>SKU1</sku>" in body["data"]
    assert len(sink) == 1
    feedbacks = actions(transport, "Feedback")
    assert [item["status"] for item in feedbacks] == [3]


@pytest.mark.parametrize("line", VTSI_LINES)
def test_line_wrappers_send_authenticated_execute(line):
    sink = []
    session = Session(
        result="<Envelope><Body><ExecuteResponse><resultCode>2</resultCode></ExecuteResponse></Body></Envelope>",
        sink=sink,
        session_id="SID42",
    )
    gateway = VtsiGateway(
        lambda: session,
        username="nyh-user",
        password="pw",
        account="ACC100",
        timeout=10,
    )
    center = make_center(FakeTransport())
    line.submit_task(
        {"task_id": 9, "phone_number": "9123456789", "vtsi_sku": "SKU1"},
        center,
        gateway,
    )
    line.check_task({"id": 8, "user_number": "9123456789"}, center, gateway)
    assert [body["command"] for body in sink] == ["TOPUP", "GETTRANSDETAILSBYMERCHANTID"]
    for body in sink:
        assert body["sessionId"] == "SID42"
        assert body["username"] == "nyh-user"
        assert body["password"] == hashlib.sha1(b"nyh-userpwSID42").hexdigest()
        assert body["data"].startswith("<?xml") or "<meta>" in body["data"]


def test_fake_session_parses_check_status_from_raw_xml():
    raw = _soap_with_return_xml("2", "<transaction><statusCode>2</statusCode></transaction>")
    session = Session(result=raw, session_id="SID")
    gateway = _gateway(session)
    transport = FakeTransport()
    dito_vtsi.check_task({"id": 8, "user_number": "9123456789"}, make_center(transport), gateway)
    feedbacks = actions(transport, "Feedback")
    assert [item["status"] for item in feedbacks] == [2]
    assert len(actions(transport, "SMSContentReceiving")) == 1


def test_balance_queries_wallet_with_account_and_parses_xml():
    sink = []
    raw = _soap_with_return_xml(
        "2",
        "<wallets><wallet><accountNo>ACC100</accountNo><balance>12</balance></wallet></wallets>",
    )
    session = Session(result=raw, sink=sink)
    gateway = _gateway(session)
    assert parse_wallet_balance(gateway.balance()) == "12"
    assert sink[0]["command"] == "GETWALLETBALANCE"
    assert "<accountNo>ACC100</accountNo>" in sink[0]["data"]


class _RawResponse:
    def __init__(self, content: bytes):
        self.content = content


class _FakeZeepClient:
    """不打开 WSDL。settings 和 service 的形状与 zeep 原始响应一致。"""

    def __init__(self, execute_xml: str, create_xml: str):
        self.execute_xml = execute_xml
        self.create_xml = create_xml
        self.calls = []
        self.service = self

    def settings(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def CreateSession(self, username):
        self.calls.append(("CreateSession", username))
        return _RawResponse(self.create_xml.encode("ISO-8859-1"))

    def Execute(self, **request_map):
        self.calls.append(("Execute", request_map))
        return _RawResponse(self.execute_xml.encode("ISO-8859-1"))


def test_soap_session_parses_execute_before_result_code():
    execute_xml = (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body><ExecuteResponse><resultCode>25</resultCode></ExecuteResponse></soap:Body>"
        "</soap:Envelope>"
    )
    create_xml = (
        "<Envelope><Body><CreateSessionResponse>"
        "<resultCode>2</resultCode><sessionId>SID9</sessionId>"
        "</CreateSessionResponse></Body></Envelope>"
    )
    fake = _FakeZeepClient(execute_xml, create_xml)
    gateway = VtsiGateway(
        lambda: SoapSession(fake),
        username="nyh-user",
        password="pw",
        account="ACC100",
        timeout=10,
    )
    result = gateway.topup(merchant_transaction_id="v9", phone="09123456789", sku="SKU1")
    assert result_code_of(result) == "25"
    execute_calls = [item for item in fake.calls if item[0] == "Execute"]
    assert len(execute_calls) == 1
    body = execute_calls[0][1]
    assert body["command"] == "TOPUP"
    assert body["sessionId"] == "SID9"
    assert body["username"] == "nyh-user"
    assert body["password"] == hashlib.sha1(b"nyh-userpwSID9").hexdigest()
    assert "<merchantTransactionId>v9</merchantTransactionId>" in body["data"]
    assert "<accountNo>" not in body["data"]


def test_soap_session_balance_uses_account_and_repo_parser():
    inner = "<wallets><wallet><accountNo>ACC100</accountNo><balance>12</balance></wallet></wallets>"
    execute_xml = _soap_with_return_xml("2", inner)
    create_xml = (
        "<Envelope><Body><CreateSessionResponse>"
        "<resultCode>2</resultCode><sessionId>SID9</sessionId>"
        "</CreateSessionResponse></Body></Envelope>"
    )
    fake = _FakeZeepClient(execute_xml, create_xml)
    gateway = VtsiGateway(
        lambda: SoapSession(fake),
        username="nyh-user",
        password="pw",
        account="ACC100",
        timeout=10,
    )
    assert parse_wallet_balance(gateway.balance()) == "12"
    body = [item[1] for item in fake.calls if item[0] == "Execute"][0]
    assert body["command"] == "GETWALLETBALANCE"
    assert "<accountNo>ACC100</accountNo>" in body["data"]
    assert "verify" not in body


def test_http_session_verify_includes_repo_cas():
    class Http:
        verify = None

    http = Http()
    configure_http_session(http)
    text = Path(http.verify).read_text(encoding="utf-8")
    for name in CA_FILES:
        certificate = (repo_cert_dir() / name).read_text(encoding="utf-8").strip()
        assert certificate in text


def test_wallet_balance_parser_rejects_missing_wallet():
    with pytest.raises(ValueError):
        parse_wallet_balance({"Body": {"ExecuteResponse": {"return": "<root></root>"}}})
    assert parse_wallet_balance(
        {"Body": {"ExecuteResponse": {"return": "<root><wallet><balance>12</balance></wallet></root>"}}}
    ) == "12"


# ---- 长时间运行：CA 固定路径、client 按线程复用、超时、发送截止（U3） ----


def test_ca_bundle_published_ten_times_leaves_one_file(tmp_path):
    target = tmp_path / "var" / "vtsi-ca-bundle.pem"
    paths = {vtsi_module.publish_ca_bundle(target) for _ in range(10)}
    assert paths == {str(target)}
    assert sorted(path.name for path in target.parent.iterdir()) == ["vtsi-ca-bundle.pem"]
    text = target.read_text(encoding="utf-8")
    for name in CA_FILES:
        assert (repo_cert_dir() / name).read_text(encoding="utf-8").strip() in text


class _CountingZeep:
    """替换 zeep.Client 和 Transport，记录构造次数和超时参数。"""

    def __init__(self, fail_first=False):
        self.clients = 0
        self.transports = []
        self.fail_first = fail_first
        create_xml = (
            "<Envelope><Body><CreateSessionResponse>"
            "<resultCode>2</resultCode><sessionId>SID9</sessionId>"
            "</CreateSessionResponse></Body></Envelope>"
        )
        execute_xml = "<Envelope><Body><ExecuteResponse><resultCode>2</resultCode></ExecuteResponse></Body></Envelope>"
        self.fakes = []
        self.xml = (execute_xml, create_xml)

    def client(self, wsdl, transport=None):
        self.clients += 1
        if self.fail_first and self.clients == 1:
            raise requests.ConnectionError("wsdl down")
        fake = _FakeZeepClient(*self.xml)
        self.fakes.append(fake)
        return fake

    def transport(self, **kwargs):
        self.transports.append(kwargs)
        return object()


def _patched_factory(monkeypatch, tmp_path, line="vtsi-dito", fail_first=False):
    import zeep
    import zeep.transports

    counting = _CountingZeep(fail_first=fail_first)
    monkeypatch.setattr(zeep, "Client", counting.client)
    monkeypatch.setattr(zeep.transports, "Transport", counting.transport)
    ca_path = vtsi_module.publish_ca_bundle(tmp_path / "ca.pem")
    gateway = registry.build_vtsi_gateway(line, "submit", _env_for(line), ca_path)
    return counting, gateway


def _create_session_calls(counting):
    return sum(1 for fake in counting.fakes for call in fake.calls if call[0] == "CreateSession")


def test_client_built_once_per_thread_and_session_per_command(monkeypatch, tmp_path):
    counting, gateway = _patched_factory(monkeypatch, tmp_path)
    for _ in range(3):
        gateway.execute("GETWALLETBALANCE", {"accountNo": "ACC100"}, normalize=False)
    assert counting.clients == 1
    assert _create_session_calls(counting) == 3


def test_two_threads_build_two_clients(monkeypatch, tmp_path):
    counting, gateway = _patched_factory(monkeypatch, tmp_path)
    threads = [threading.Thread(target=gateway.query, args=("v1",)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert counting.clients == 2


def test_failed_client_build_is_not_cached(monkeypatch, tmp_path):
    counting, gateway = _patched_factory(monkeypatch, tmp_path, fail_first=True)
    gateway.query("v1")
    gateway.query("v2")
    assert counting.clients == 2
    assert _create_session_calls(counting) == 2


@pytest.mark.parametrize("line,seconds", [("vtsi-smart", 10), ("vtsi-globe", 50), ("vtsi-dito", 50)])
def test_transport_sets_wsdl_and_operation_timeout(monkeypatch, tmp_path, line, seconds):
    counting, gateway = _patched_factory(monkeypatch, tmp_path, line=line)
    gateway.query("v1")
    assert counting.transports[0]["timeout"] == seconds
    assert counting.transports[0]["operation_timeout"] == seconds
    assert gateway.send_deadline == 15
    check_gateway = registry.build_vtsi_gateway(line, "check", _env_for(line), "ca.pem")
    assert check_gateway.send_deadline is None


def _env_for(line):
    return {
        "VTSI_WSDL": "https://vtsi.invalid/ws?wsdl",
        "VTSI_USERNAME": "nyh-user",
        "VTSI_PASSWORD": "pw",
        f"{line.upper().replace('-', '_')}_ACCOUNT": "ACC100",
    }


def test_topup_read_timeout_is_transport_failed_without_resend():
    outcome, transport, sink = _submit("dito", error=requests.ReadTimeout("read timed out"))
    assert outcome == "transport-failed"
    assert actions(transport, "Feedback") == []
    assert [item["command"] for item in sink] == ["TOPUP"]


def test_check_sms_uses_center_number_as_is():
    _outcome, transport, _sink = _check(soap("2", "2"))
    sms = actions(transport, "SMSContentReceiving")
    assert [body["reception_number"] for body in sms] == ["9123456789"]


def test_submit_still_sends_leading_zero():
    _outcome, transport, sink = _submit("dito", soap("2"))
    assert "<mobileNo>09123456789</mobileNo>" in sink[0]["data"]
    assert {body["reception_number"] for body in actions(transport, "SMSContentReceiving")} == {"09123456789"}


class _Clock:
    """单调时钟。每次 CreateSession 之后按脚本前进。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _ClockSession(Session):
    def __init__(self, clock, advance, fail_times=0, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock
        self.advance = advance
        self.fail_times = fail_times
        self.creates = 0

    def create_session(self, username):
        self.creates += 1
        self.clock.now += self.advance
        if self.creates <= self.fail_times:
            raise RuntimeError("session down")
        return self.session_id


def _deadline_submit(session, clock, caplog):
    caplog.set_level(logging.ERROR)
    gateway = VtsiGateway(
        lambda: session,
        username="nyh-user",
        password="pw",
        account="ACC100",
        timeout=50,
        send_deadline=15,
        monotonic=clock,
    )
    transport = FakeTransport()
    outcome = dito_vtsi.submit_task(
        {"task_id": 9, "phone_number": "9123456789", "vtsi_sku": "SKU1"}, make_center(transport), gateway
    )
    return outcome, transport


def test_deadline_passed_after_session_skips_topup(caplog):
    clock = _Clock()
    sink = []
    session = _ClockSession(clock, advance=16, result=soap("2"), sink=sink)
    outcome, transport = _deadline_submit(session, clock, caplog)
    assert outcome == "deadline-missed"
    assert sink == []
    assert actions(transport, "Feedback") == []
    assert len(actions(transport, "SMSContentReceiving")) == 1
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1 and "task_id=9" in errors[0].getMessage()


def test_deadline_stops_session_retries(caplog):
    clock = _Clock()
    sink = []
    session = _ClockSession(clock, advance=15, fail_times=1, result=soap("2"), sink=sink)
    outcome, _transport = _deadline_submit(session, clock, caplog)
    assert outcome == "deadline-missed"
    assert session.creates == 1
    assert sink == []


def test_topup_sent_before_deadline_waits_for_timeout(caplog):
    clock = _Clock()
    sink = []

    class SlowSession(_ClockSession):
        def execute(self, request_map):
            self.sink.append(request_map)
            self.clock.now += 30
            raise requests.ReadTimeout("read timed out")

    session = SlowSession(clock, advance=14, sink=sink)
    outcome, transport = _deadline_submit(session, clock, caplog)
    assert outcome == "transport-failed"
    assert [item["command"] for item in sink] == ["TOPUP"]
    assert actions(transport, "Feedback") == []


def test_query_and_balance_ignore_send_deadline():
    clock = _Clock()
    sink = []
    session = _ClockSession(clock, advance=100, result=soap("2", "2"), sink=sink)
    gateway = VtsiGateway(
        lambda: session, username="u", password="p", account="ACC100", timeout=50, send_deadline=15, monotonic=clock
    )
    gateway.query("v1")
    gateway.balance()
    assert [item["command"] for item in sink] == ["GETTRANSDETAILSBYMERCHANTID", "GETWALLETBALANCE"]


def test_configuring_sessions_repeatedly_does_not_accumulate_files(tmp_path, monkeypatch):
    """本地复现：每配置一次会话就多一个 CA 临时文件。修复后只有固定路径一个文件。"""
    import tempfile

    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setattr(vtsi_module, "ca_bundle_path", lambda: tmp_path / "var" / "vtsi-ca-bundle.pem", raising=False)

    class Http:
        verify = None

    for _ in range(10):
        configure_http_session(Http())
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(files) == 1
