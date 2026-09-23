"""业务日志：按 task_id 能查到拉单、上游结果和 Feedback，且不泄露密钥。"""

import io
import logging
import threading

from nyh_line.lines import dito_vtsi, smart_fd, yingla
from nyh_line.log_setup import LOG_FORMAT, configure_logging, log_safe
from nyh_line.policy import policy_for
from nyh_line.runner import PollLoop
from nyh_line.upstream.fd import FdClient
from nyh_line.upstream.vtsi import VtsiGateway
from nyh_line.upstream.xiaola import XiaolaClient
from tests.support import FakeResponse, FakeTransport, make_center, make_xiaola_center
from tests.test_vtsi_status import Session, soap


def _messages(caplog):
    return [record.getMessage() for record in caplog.records]


def test_vtsi_submit_logs_pull_result_and_feedback_in_order(caplog):
    caplog.set_level(logging.INFO)
    center_transport = FakeTransport()
    center = make_center(center_transport)
    gateway = VtsiGateway(lambda: Session(result=soap("25")), username="u", password="p", account="A", timeout=10)
    loop = PollLoop(
        policy_for("vtsi-dito", "submit"),
        lambda: {"code": 0, "data": {"task_id": 9, "phone_number": "9123456789", "vtsi_sku": "S"}},
        lambda task: dito_vtsi.submit_task(task, center, gateway),
        sleep=lambda _seconds: None,
    )
    loop.run_round()
    lines = [message for message in _messages(caplog) if "task_id=9" in message]
    pulled = next(index for index, text in enumerate(lines) if "拉到任务" in text)
    result = next(index for index, text in enumerate(lines) if "resultCode=25" in text)
    feedback = next(index for index, text in enumerate(lines) if "Feedback" in text and "status=3" in text)
    assert pulled < result < feedback
    assert "phone=9123456789" in lines[pulled]


def test_xiaola_check_logs_state_and_feedback(caplog):
    caplog.set_level(logging.INFO)
    upstream = FakeTransport(response={"code": 10000, "result": {"state": 2}})
    client = XiaolaClient(base_url="http://x.invalid", username="u", secret_key="s", transport=upstream)
    yingla.check_task({"id": 15, "user_number": "9123456789"}, make_xiaola_center(FakeTransport()), client)
    text = "\n".join(_messages(caplog))
    assert "task_id=15" in text and "state=2" in text
    assert "Feedback task_id=15 status=2 code=0" in text


_SECRETS = {
    "password": "PW-LEAK-1",
    "auth": "AUTH-LEAK-2",
    "sign": "SIGN-LEAK-3",
    "price": "PRICE-LEAK-4",
    "secret": "SECRET-LEAK-5",
    "device_key": "DKEY-LEAK-6",
    "sessionId": "SESSION-LEAK-7",
    "key": "KEY-LEAK-8",
    "secret_key": "SKEY-LEAK-9",
}


def test_full_submit_logs_contain_no_secrets(caplog):
    caplog.set_level(logging.DEBUG)
    leaky = {"code": "1001", "message": "拒绝", **_SECRETS, "data": dict(_SECRETS)}

    # 菲岛 Smart：业务失败，回写 0 并发模板，日志附清洗后的响应。
    upstream = FakeTransport(response=leaky)
    center_transport = FakeTransport(response={"code": 0, "msg": "ok", "sign": "SIGN-LEAK-3"})
    client = FdClient(base_url="http://fd.invalid", uid="uid", key="FDKEY-LEAK-10", transport=upstream)
    smart_fd.submit_task(
        {"task_id": 42, "phone_number": "9123456789", "fd_content_pcode": "P", "fd_content_money": "50"},
        make_center(center_transport),
        client,
    )

    # VTSI：Execute 带密码和 sessionId，结果 25 回写。
    session = Session(result={"Body": {"ExecuteResponse": {"resultCode": "25", **_SECRETS}}}, session_id="SESSION-LEAK-7")
    gateway = VtsiGateway(lambda: session, username="u", password="VTSIPW-LEAK-11", account="A", timeout=10)
    dito_vtsi.submit_task({"task_id": 9, "phone_number": "9123456789", "vtsi_sku": "S"}, make_center(FakeTransport()), gateway)

    # 赢啦：业务失败带响应。
    xiaola_upstream = FakeTransport(response={"code": 10001, **_SECRETS})
    xiaola = XiaolaClient(base_url="http://x.invalid", username="u", secret_key="XSECRET-LEAK-12", transport=xiaola_upstream)
    yingla.submit_task({"task_id": 7, "phone_number": "9123456789", "content": "P"}, make_xiaola_center(FakeTransport()), xiaola, None)

    text = caplog.text
    for value in list(_SECRETS.values()) + ["FDKEY-LEAK-10", "VTSIPW-LEAK-11", "XSECRET-LEAK-12", "device-key"]:
        assert value not in text
    assert "task_id=42" in text and "task_id=9" in text and "task_id=7" in text


def test_log_safe_drops_log_only_keys():
    cleaned = log_safe({"code": 1, "Password": "x", "session_id": "y", "nested": [{"secretKey": "z", "ok": 1}]})
    assert cleaned == '{"code": 1, "nested": [{"ok": 1}]}'


def test_third_party_loggers_are_warning_after_configure():
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    stream = io.StringIO()
    try:
        configure_logging(stream=stream)
        configure_logging(stream=stream)
        assert logging.getLogger("zeep").level == logging.WARNING
        assert logging.getLogger("urllib3").level == logging.WARNING
        assert logging.getLogger("requests").level == logging.WARNING
        added = [handler for handler in root.handlers if handler not in before]
        assert len(added) == 1
        worker = threading.Thread(target=lambda: logging.getLogger("nyh_line.test").info("hello"), name="worker-1")
        worker.start()
        worker.join()
        line = stream.getvalue().strip()
        assert " INFO [worker-1] nyh_line.test: hello" in line
        assert line[:4].isdigit()
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                root.removeHandler(handler)
        root.setLevel(level)
    assert "%(threadName)s" in LOG_FORMAT


def test_non_json_center_reply_logs_feedback_warning(caplog):
    caplog.set_level(logging.INFO)
    center = make_center(FakeTransport(response=FakeResponse(text="<html>", broken=True)))
    center.feedback(5, 2)
    records = [record for record in caplog.records if "Feedback task_id=5" in record.getMessage()]
    assert len(records) == 1
    assert "code=-1" in records[0].getMessage()
    assert records[0].levelno == logging.WARNING
