"""菲岛 Globe / Smart 提交回写。"""

import hashlib

import pytest

from nyh_line.lines import globe_fd, smart_fd
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
