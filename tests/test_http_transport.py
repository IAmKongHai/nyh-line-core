"""真实 requests 传输的发出判定。只有连接阶段失败才算没发出。"""

import http.client
import logging
import socket
import threading

import pytest
import requests
import urllib3.exceptions as u3

from nyh_line.errors import RequestNotSent
from nyh_line.http_transport import requests_transport
from nyh_line.lines import globe_fd
from nyh_line.upstream.fd import FdClient
from tests.support import FakeTransport, actions, make_center


def _task():
    return {"task_id": 42, "phone_number": "9123456789", "fd_content_pcode": "P1", "fd_content_money": "50"}


class _CloseAfterBody:
    """本地 socket 服务器：读完请求正文后不回响应，直接关连接。"""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.bodies = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _addr = self.server.accept()
        with conn:
            raw = b""
            while b"\r\n\r\n" not in raw:
                raw += conn.recv(4096)
            head, _sep, body = raw.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            while len(body) < length:
                body += conn.recv(4096)
            self.bodies.append(body)
        self.server.close()


def test_upstream_closes_after_reading_body_is_unknown(caplog):
    """AE1：上游读完正文后断开，Feedback 0 次，ERROR 带 task_id。"""
    caplog.set_level(logging.ERROR)
    server = _CloseAfterBody()
    client = FdClient(
        base_url=f"http://127.0.0.1:{server.port}",
        uid="uid-1",
        key="fd-key",
        transport=requests_transport(timeout=5),
    )
    center_transport = FakeTransport()
    outcome = globe_fd.submit_task(_task(), make_center(center_transport), client)
    server.thread.join(timeout=5)
    assert b"orderId=42" in server.bodies[0]
    assert outcome == "unknown"
    assert actions(center_transport, "Feedback") == []
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "task_id=42" in errors[0].getMessage()


def _max_retry(reason):
    return u3.MaxRetryError(None, "/api", reason=reason)


def _not_sent_errors():
    return [
        requests.ConnectionError(_max_retry(u3.NameResolutionError("fd.invalid", None, socket.gaierror()))),
        requests.ConnectionError(_max_retry(u3.NewConnectionError(None, "refused"))),
        requests.ConnectTimeout(_max_retry(u3.ConnectTimeoutError(None, "timeout"))),
    ]


def _maybe_sent_errors():
    return [
        requests.ConnectionError(
            u3.ProtocolError("Connection aborted.", http.client.RemoteDisconnected("closed"))
        ),
        requests.exceptions.SSLError(_max_retry(u3.SSLError("EOF occurred in violation of protocol"))),
        requests.ReadTimeout("read timed out"),
    ]


@pytest.mark.parametrize("error", _not_sent_errors(), ids=["dns", "refused", "connect-timeout"])
def test_connect_phase_failures_raise_request_not_sent(monkeypatch, error):
    def post(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(RequestNotSent):
        requests_transport().post("http://fd.invalid/api", {"a": 1})


@pytest.mark.parametrize("error", _maybe_sent_errors(), ids=["disconnected", "ssl", "read-timeout"])
def test_maybe_sent_failures_pass_through(monkeypatch, error):
    def post(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(type(error)):
        requests_transport().post("http://fd.invalid/api", {"a": 1})


def _submit_with_error(monkeypatch, error):
    def post(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(requests, "post", post)
    client = FdClient(base_url="http://fd.invalid", uid="uid-1", key="fd-key", transport=requests_transport())
    center_transport = FakeTransport()
    outcome = globe_fd.submit_task(_task(), make_center(center_transport), client)
    return outcome, center_transport


def test_dns_failure_feedbacks_zero_once(monkeypatch):
    """AE2：DNS 解析失败，Feedback 状态 0 恰好一次。"""
    outcome, center_transport = _submit_with_error(monkeypatch, _not_sent_errors()[0])
    assert outcome == "not-sent"
    assert [body["status"] for body in actions(center_transport, "Feedback")] == [0]


@pytest.mark.parametrize("error", _maybe_sent_errors(), ids=["disconnected", "ssl", "read-timeout"])
def test_maybe_sent_failures_do_not_feedback(monkeypatch, error):
    outcome, center_transport = _submit_with_error(monkeypatch, error)
    assert outcome == "unknown"
    assert actions(center_transport, "Feedback") == []
