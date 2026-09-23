"""VTSI 三条线路的提交、查单、短信和 TOPUP 次数。"""

import hashlib
from pathlib import Path

import pytest

from nyh_line.lines import dito_vtsi, globe_vtsi, smart_vtsi
from nyh_line.upstream.vtsi import (
    CA_FILES,
    VtsiGateway,
    configure_http_session,
    parse_wallet_balance,
    repo_cert_dir,
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
    opened = {"n": 0}

    def factory():
        opened["n"] += 1
        return Session(result=soap("2"))

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
