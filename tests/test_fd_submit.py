"""菲岛 Globe / Smart 提交回写。"""

import hashlib
import logging
import threading

import pytest

from nyh_line.errors import RequestNotSent
from nyh_line.lines import globe_fd, smart_fd
from nyh_line.lines.registry import build_fd_loop
from nyh_line.upstream.fd import FdClient
from tests.support import FakeResponse, FakeTransport, actions, make_center

LINES = {"globe": globe_fd, "smart": smart_fd}


def _task(**overrides):
    task = {
        "task_id": 42,
        "phone_number": "9123456789",
        "fd_content_pcode": "P1",
        "fd_content_money": "50",
    }
    task.update(overrides)
    return task


def _client(transport):
    return FdClient(base_url="http://fd.invalid", uid="uid-1", key="fd-key", transport=transport)


def _run(line, transport, center_transport, response=None, **task_overrides):
    if response is not None:
        transport.response = response
    center = make_center(center_transport)
    result = LINES[line].submit_task(_task(**task_overrides), center, _client(transport))
    return result


@pytest.mark.parametrize("line", ["globe", "smart"])
def test_success_does_not_feedback_and_order_id_is_task_id(line):
    upstream = FakeTransport(response={"code": "0000", "success": True, "message": "ok"})
    center = FakeTransport()
    _run(line, upstream, center)
    assert actions(center, "Feedback") == []
    assert actions(center, "Template_sending") == []
    assert len(upstream.calls) == 1
    body = upstream.calls[0]["data"]
    assert body["orderId"] == 42
    assert body["phone"] == "09123456789"
    raw = "uid-1" + "P1" + "09123456789" + "50" + "42" + "fd-key"
    assert body["auth"] == hashlib.md5(raw.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("line,templates", [("globe", 0), ("smart", 1)])
def test_maintenance_feedback_status_zero_without_api_result(line, templates):
    upstream = FakeTransport(
        response={"code": "0000", "success": False, "message": "充值套餐维护中"}
    )
    center = FakeTransport()
    _run(line, upstream, center)
    bodies = actions(center, "Feedback")
    assert len(bodies) == 1
    assert bodies[0]["status"] == 0
    assert "api_result" not in bodies[0]
    assert len(actions(center, "Template_sending")) == templates


def test_other_false_success_on_0000_does_not_feedback():
    upstream = FakeTransport(response={"code": "0000", "success": False, "message": "其他"})
    center = FakeTransport()
    _run("globe", upstream, center)
    assert actions(center, "Feedback") == []


@pytest.mark.parametrize("line,templates", [("globe", 0), ("smart", 1)])
def test_business_code_feedback_zero_and_template_only_on_smart(line, templates):
    upstream = FakeTransport(response={"code": "1001", "message": "拒绝"})
    center = FakeTransport()
    _run(line, upstream, center)
    bodies = actions(center, "Feedback")
    assert len(bodies) == 1
    assert bodies[0]["status"] == 0
    assert "api_result" not in bodies[0]
    assert len(actions(center, "Template_sending")) == templates


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(text="", payload={"code": "0000"}),
        FakeResponse(text="not-json", broken=True),
        {"code": "ERROR", "message": "本地哨兵", "success": False},
    ],
)
def test_sent_but_unreadable_does_not_feedback_and_posts_once(response):
    upstream = FakeTransport(response=response)
    center = FakeTransport()
    _run("smart", upstream, center)
    assert actions(center, "Feedback") == []
    assert actions(center, "Template_sending") == []
    assert len(upstream.calls) == 1


def test_timeout_after_send_does_not_feedback_and_posts_once():
    upstream = FakeTransport()
    upstream.error_after_write = TimeoutError("read timeout")
    center = FakeTransport()
    _run("globe", upstream, center)
    assert actions(center, "Feedback") == []
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("line,templates", [("globe", 0), ("smart", 1)])
def test_connection_refused_before_write_feedbacks_once(line, templates):
    upstream = FakeTransport()
    upstream.fail_before_write = True
    center = FakeTransport()
    _run(line, upstream, center)
    bodies = actions(center, "Feedback")
    assert len(bodies) == 1
    assert bodies[0]["status"] == 0
    assert "api_result" not in bodies[0]
    assert upstream.calls == []
    assert len(actions(center, "Template_sending")) == templates


@pytest.mark.parametrize("field", ["task_id", "phone_number", "fd_content_pcode", "fd_content_money"])
@pytest.mark.parametrize("line,templates", [("globe", 0), ("smart", 1)])
def test_missing_field_feedbacks_without_calling_upstream(field, line, templates):
    upstream = FakeTransport()
    center = FakeTransport()
    _run(line, upstream, center, **{field: ""})
    bodies = actions(center, "Feedback")
    assert len(bodies) == 1
    assert bodies[0]["status"] == 0
    assert "api_result" not in bodies[0]
    assert upstream.calls == []
    assert len(actions(center, "Template_sending")) == templates


@pytest.mark.parametrize("line,templates", [("globe", 0), ("smart", 1)])
def test_decimal_money_feedbacks_zero_without_upstream(line, templates):
    """AE3：金额 50.00 按缺字段处理，回写 0 一次，不打上游。"""
    upstream = FakeTransport(response={"code": "0000", "success": True})
    center = FakeTransport()
    outcome = _run(line, upstream, center, fd_content_money="50.00")
    assert outcome == "missing-field"
    assert [body["status"] for body in actions(center, "Feedback")] == [0]
    assert upstream.calls == []
    assert len(actions(center, "Template_sending")) == templates


def test_integer_money_text_is_sent_as_int():
    upstream = FakeTransport(response={"code": "0000", "success": True})
    _run("globe", upstream, FakeTransport(), fd_content_money="50")
    assert upstream.calls[0]["data"]["money"] == 50


# ---- 连续结果未知时暂停拉单（R17） ----


class _Script:
    """按顺序给出每一笔的上游结果：unknown 抛读超时，accepted 受理，not-sent 连接前失败。"""

    def __init__(self, kinds):
        self.kinds = list(kinds)

    def __call__(self, _url, _data):
        kind = self.kinds.pop(0)
        if kind == "unknown":
            raise TimeoutError("read timeout")
        if kind == "not-sent":
            raise RequestNotSent("refused")
        return FakeResponse({"code": "0000", "success": True, "message": "ok"})


def _fd_loop(kinds, stop=None, wait=None):
    stop = stop or threading.Event()
    pulls = {"n": 0}

    def center_response(_url, data):
        if data.get("action") == "GetTasks":
            pulls["n"] += 1
            return FakeResponse({"code": 0, "data": _task(task_id=pulls["n"])})
        return FakeResponse({"code": 0})

    center_transport = FakeTransport(response=center_response)
    upstream = FakeTransport(response=_Script(kinds))
    waits = []

    def record_wait(seconds):
        waits.append((seconds, pulls["n"]))
        return True

    loop = build_fd_loop(
        "fd-globe",
        make_center(center_transport),
        _client(upstream),
        stop,
        wait=wait or record_wait,
        sleep=lambda _seconds: None,
        randint=lambda start, _end: start,
    )
    return loop, pulls, waits, center_transport


def test_three_unknown_in_a_row_pause_sixty_seconds(caplog):
    caplog.set_level(logging.ERROR)
    loop, pulls, waits, center_transport = _fd_loop(["unknown", "unknown", "unknown", "accepted"])
    for _ in range(3):
        loop.run_round()
    assert waits == [(60, 3)]
    critical = [record for record in caplog.records if record.levelno == logging.CRITICAL]
    assert len(critical) == 1
    assert "TimeoutError" in critical[0].getMessage()
    assert actions(center_transport, "Feedback") == []
    loop.run_round()
    assert pulls["n"] == 4


def test_accepted_resets_unknown_streak():
    loop, _pulls, waits, _center = _fd_loop(["unknown", "unknown", "accepted", "unknown", "unknown"])
    for _ in range(5):
        loop.run_round()
    assert waits == []


def test_not_sent_resets_unknown_streak():
    loop, _pulls, waits, center_transport = _fd_loop(["unknown", "unknown", "not-sent", "unknown", "unknown"])
    for _ in range(5):
        loop.run_round()
    assert waits == []
    assert [body["status"] for body in actions(center_transport, "Feedback")] == [0]


def test_stop_during_pause_ends_without_pulling():
    stop = threading.Event()

    def stopping_wait(_seconds):
        stop.set()
        return False

    loop, pulls, _waits, _center = _fd_loop(["unknown"] * 4, stop=stop, wait=stopping_wait)
    for _ in range(3):
        loop.run_round()
    assert stop.is_set()
    assert loop.run_round() == "stopped"
    assert pulls["n"] == 3
