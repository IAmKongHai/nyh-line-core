"""赢啦拦截、缺码、提交和查单回写。"""

import hashlib
import logging

import pytest

from nyh_line.lines import yingla
from nyh_line.runner import XiaolaSubmitLoop, apply_batch_config
from nyh_line.settings import country_for_thread, threads_to_start
from nyh_line.upstream.xiaola import XiaolaClient
from nyh_line.xiaola.batch import BatchController
from nyh_line.xiaola.blocklist import Blocklist
from tests.support import FakeTransport, actions, make_xiaola_center
import threading


def _clients(response=None):
    upstream = FakeTransport(response=response if response is not None else {"code": 10000})
    center_transport = FakeTransport()
    client = XiaolaClient(
        base_url="http://upstream.invalid",
        username="user",
        secret_key="secret",
        transport=upstream,
    )
    center = make_xiaola_center(center_transport)
    return upstream, center_transport, client, center


def test_blocklist_exact_match_does_not_call_upstream(tmp_path):
    path = tmp_path / "numbers.txt"
    path.write_text("9123456789\n")
    upstream, center_transport, client, center = _clients()
    yingla.submit_task(
        {"task_id": 7, "phone_number": "9123456789", "content": "P"},
        center,
        client,
        Blocklist(str(path)),
    )
    assert upstream.calls == []
    feedbacks = actions(center_transport, "Feedback")
    assert [body["status"] for body in feedbacks] == [3]
    assert "api_result" not in feedbacks[0]
    assert actions(center_transport, "Template_sending") == []


def test_leading_zero_does_not_hit_blocklist(tmp_path):
    path = tmp_path / "numbers.txt"
    path.write_text("9123456789\n")
    upstream, center_transport, client, center = _clients({"code": 10000})
    yingla.submit_task(
        {"task_id": 7, "phone_number": "09123456789", "content": "P"},
        center,
        client,
        Blocklist(str(path)),
    )
    assert len(upstream.calls) == 1
    assert actions(center_transport, "Feedback") == []


def test_missing_blocklist_file_still_orders(tmp_path):
    upstream, _center_transport, client, center = _clients({"code": 10000})
    yingla.submit_task(
        {"task_id": 7, "phone_number": "9123456789", "content": "P"},
        center,
        client,
        Blocklist(str(tmp_path / "missing.txt")),
    )
    assert len(upstream.calls) == 1
    assert upstream.calls[0]["data"]["user_order_no"] == "nyh7"
    assert upstream.calls[0]["data"]["recharge_no"] == "09123456789"


def test_unreadable_blocklist_still_orders(tmp_path):
    upstream, _center_transport, client, center = _clients({"code": 10000})
    yingla.submit_task(
        {"task_id": 7, "phone_number": "9123456789", "content": "P"},
        center,
        client,
        Blocklist(str(tmp_path)),
    )
    assert len(upstream.calls) == 1


def test_missing_product_code_feedbacks_without_upstream():
    upstream, center_transport, client, center = _clients()
    yingla.submit_task(
        {"task_id": 7, "phone_number": "9123456789"},
        center,
        client,
        Blocklist("/no/such/list"),
    )
    assert upstream.calls == []
    feedbacks = actions(center_transport, "Feedback")
    assert [body["status"] for body in feedbacks] == [3]
    assert "MISSING_PRODUCT_CODE" in feedbacks[0]["api_result"]
    assert len(actions(center_transport, "Template_sending")) == 1


def _expected_sign(body: dict, secret_key: str) -> str:
    fields = {key: value for key, value in body.items() if key != "sign" and value not in (None, "")}
    joined = "&".join(f"{key}={value}" for key, value in sorted(fields.items()))
    return hashlib.md5((joined + secret_key).encode("utf-8")).hexdigest()


def test_create_query_and_balance_are_signed():
    upstream, center_transport, client, center = _clients({"code": 10000, "result": {"state": 1, "balance": "8"}})
    yingla.submit_task(
        {"task_id": 7, "phone_number": "9123456789", "content": "P"},
        center,
        client,
        None,
    )
    yingla.check_task({"id": 7, "user_number": "9123456789"}, center, client)
    client.read_balance()
    assert [call["url"].rsplit("/", 1)[-1] for call in upstream.calls] == ["topup", "order", "balance"]
    for call in upstream.calls:
        body = call["data"]
        assert body["sign"] == _expected_sign(body, "secret")
        assert "secret" not in body
    assert actions(center_transport, "Feedback") == []


def test_upstream_10000_does_not_feedback():
    upstream, center_transport, client, center = _clients({"code": 10000, "message": "ok"})
    yingla.submit_task(
        {"task_id": 7, "phone_number": "9123456789", "content": "P"},
        center,
        client,
        None,
    )
    assert actions(center_transport, "Feedback") == []
    assert len(upstream.calls) == 1


def test_other_business_code_feedbacks_with_response():
    upstream, center_transport, client, center = _clients({"code": 10001, "message": "余额不足", "price": 4})
    yingla.submit_task(
        {"task_id": 7, "phone_number": "9123456789", "content": "P"},
        center,
        client,
        None,
    )
    feedbacks = actions(center_transport, "Feedback")
    assert [body["status"] for body in feedbacks] == [0]
    assert "10001" in feedbacks[0]["api_result"]
    assert "price" not in feedbacks[0]["api_result"]
    assert len(actions(center_transport, "Template_sending")) == 1
    assert len(upstream.calls) == 1


def test_timeout_does_not_feedback_and_orders_once():
    upstream, center_transport, client, center = _clients()
    upstream.error_after_write = TimeoutError("read timeout")
    yingla.submit_task(
        {"task_id": 7, "phone_number": "9123456789", "content": "P"},
        center,
        client,
        None,
    )
    assert actions(center_transport, "Feedback") == []
    assert actions(center_transport, "Template_sending") == []
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("error_code", [-1, -2, "-1", "-2"])
def test_local_sentinel_without_business_code_does_not_feedback(error_code):
    upstream, center_transport, client, center = _clients({"error_code": error_code, "error_msg": "local"})
    yingla.submit_task(
        {"task_id": 7, "phone_number": "9123456789", "content": "P"},
        center,
        client,
        None,
    )
    assert actions(center_transport, "Feedback") == []
    assert actions(center_transport, "Template_sending") == []
    assert len(upstream.calls) == 1


def _check(response):
    upstream, center_transport, client, center = _clients(response)
    yingla.check_task({"id": 15, "user_number": "9123456789"}, center, client)
    return upstream, center_transport


@pytest.mark.parametrize(
    "state,status",
    [(1, None), (2, 2), (3, 3), (9, None), ("2", 2)],
)
def test_check_state_mapping(state, status, caplog):
    caplog.set_level(logging.INFO)
    response = {"code": 10000, "message": "成功", "result": {"state": state, "price": 6}}
    _upstream, center_transport = _check(response)
    feedbacks = actions(center_transport, "Feedback")
    assert actions(center_transport, "SMSContentReceiving") == []
    if status is None:
        assert feedbacks == []
    else:
        assert [body["status"] for body in feedbacks] == [status]
        assert "成功" in feedbacks[0]["api_result"]
        assert f'"state": {state}' in feedbacks[0]["api_result"] or f'"state": "{state}"' in feedbacks[0]["api_result"]
        assert "price" not in feedbacks[0]["api_result"]
    if state == 9:
        assert "未知状态" in caplog.text


@pytest.mark.parametrize("code", [10005, 10006, "10005", "10006"])
def test_check_missing_order_ignores_state(code):
    _upstream, center_transport = _check({"code": code, "result": {"state": 2}, "message": "成功"})
    feedbacks = actions(center_transport, "Feedback")
    assert [body["status"] for body in feedbacks] == [0]
    assert "api_result" in feedbacks[0]
    assert actions(center_transport, "SMSContentReceiving") == []


def test_check_other_code_does_not_feedback_or_sms():
    _upstream, center_transport = _check({"code": 10001, "message": "成功", "result": {"state": 2}})
    assert actions(center_transport, "Feedback") == []
    assert actions(center_transport, "SMSContentReceiving") == []


def test_empty_country_does_not_pull_or_fall_back():
    assert country_for_thread(1, {}) == ""
    assert threads_to_start({"MAX_THREADS": "2", "THREAD2_COUNTRY_CODE": "PH"}) == [(2, "PH")]
    pulls = []
    loop = XiaolaSubmitLoop(
        country=country_for_thread(1, {}),
        batch=BatchController(1, 1, 5),
        pull=lambda: pulls.append(1),
        handle=lambda task: None,
        stop=threading.Event(),
    )
    assert loop.advance() == "no-country"
    assert pulls == []


def test_batch_waits_before_next_pull_and_stop_interrupts():
    pulls = {"n": 0}

    def pull():
        pulls["n"] += 1
        return {"code": 0, "data": {"task_id": pulls["n"]}}

    seen = {}

    def wait(seconds):
        seen["during"] = pulls["n"]
        seen["seconds"] = seconds
        return True

    loop = XiaolaSubmitLoop(
        country="PH",
        batch=BatchController(1, 1, 15),
        pull=pull,
        handle=lambda task: None,
        stop=threading.Event(),
        wait=wait,
    )
    assert loop.advance() == "pulled"
    assert loop.advance() == "batch-gap-done"
    assert seen == {"during": 1, "seconds": 15}
    assert pulls["n"] == 1
    assert loop.advance() == "pulled"
    assert pulls["n"] == 2

    stop = threading.Event()
    pulls["n"] = 0

    def stopping_wait(seconds):
        stop.set()
        return False

    stopped = XiaolaSubmitLoop(
        country="PH",
        batch=BatchController(1, 1, 15),
        pull=pull,
        handle=lambda task: None,
        stop=stop,
        wait=stopping_wait,
    )
    assert stopped.advance() == "pulled"
    assert stopped.advance() == "interrupted"
    assert pulls["n"] == 1
    assert stopped.advance() == "stopped"
    assert pulls["n"] == 1


def test_batch_reload_keeps_current_count():
    batch = BatchController(1, 10, 15)
    batch.on_task_finished()
    apply_batch_config(batch, 2, 3, 9)
    assert batch.tasks_in_current_batch == 1
    assert batch.max_tasks_per_batch == 3
    assert batch.batch_interval == 9
