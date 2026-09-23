"""Center 客户端：签名、清洗、两条拉单 URL、余额口。"""

import hashlib
import json

import pytest

from nyh_line.center.clean import clean_api_result
from nyh_line.center.profiles import device_new_profile, xiaola_profile
from tests.support import FakeResponse, FakeTransport, actions, make_center, make_xiaola_center


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def test_signature_ignores_api_result():
    transport = FakeTransport()
    client = make_center(transport)
    client.feedback(11, 3)
    client.feedback(11, 3, {"price": 9, "result": {"state": 2}})
    bodies = actions(transport, "Feedback")
    assert len(bodies) == 2
    expected = _md5("Feedback7device-key1700000000")
    assert bodies[0]["sign"] == expected
    assert bodies[1]["sign"] == expected
    assert bodies[0]["sign"] == client.signature("Feedback", bodies[0]["time"])


def test_clean_drops_nested_secrets_and_keeps_business_fields():
    raw = {"code": 10000, "result": {"state": 1, "price": 3, "statusCode": "2"}}
    raw["_response_info"] = {"json_data": raw, "uid": "account"}
    raw["sign"] = "abc"
    raw["nested"] = {"nonceStr": "n", "token": "t", "secret": "s", "keep": 1}
    cleaned = clean_api_result(raw)
    encoded = json.dumps(cleaned)
    assert json.loads(encoded)["result"]["state"] == 1
    assert cleaned["result"]["statusCode"] == "2"
    assert "price" not in cleaned["result"]
    assert "_response_info" not in cleaned
    assert "_request_info" not in cleaned
    assert "sign" not in cleaned
    assert "uid" not in encoded
    assert cleaned["nested"] == {"keep": 1}


def test_feedback_omits_api_result_when_serialization_fails_but_keeps_status():
    class Boom:
        pass

    transport = FakeTransport()
    client = make_center(transport)
    client.feedback(4, 0, {"keep": Boom()})
    body = actions(transport, "Feedback")[0]
    assert "api_result" not in body
    assert body["status"] == 0
    assert body["task_id"] == 4


def test_circular_yingla_response_is_serializable_on_feedback():
    raw = {"code": 10000, "result": {"state": 2, "price": 8}}
    raw["_response_info"] = {"json_data": raw}
    transport = FakeTransport()
    client = make_center(transport)
    client.feedback(5, 2, raw)
    body = actions(transport, "Feedback")[0]
    parsed = json.loads(body["api_result"])
    assert parsed["result"]["state"] == 2
    assert "price" not in parsed["result"]


def test_pull_urls_are_not_swapped():
    device_transport = FakeTransport(response={"code": 1, "msg": "none"})
    device = make_center(device_transport)
    device_result = device.get_task()
    device_call = device_transport.calls[0]
    assert device_result["code"] == 1
    assert "device_new" in device_call["url"]
    assert "task_client/getTask" not in device_call["url"]
    assert device_call["data"]["sim_balance"] == 0
    assert device_call["data"][""] == 0
    assert "fee_charging_line" not in device_call["data"]
    assert "country_code" not in device_call["data"]
    assert device_call["data"]["sign"] == _md5("GetTasks7device-key1700000000")

    xiaola_transport = FakeTransport(response={"code": 0, "data": {}})
    xiaola = make_xiaola_center(
        xiaola_transport, country_code="PH", fee_charging_line="xiaola"
    )
    xiaola.get_task()
    xiaola_call = xiaola_transport.calls[0]
    assert "task_client/getTask" in xiaola_call["url"]
    assert "device_new" not in xiaola_call["url"]
    assert xiaola_call["data"]["country_code"] == "PH"
    assert xiaola_call["data"]["fee_charging_line"] == "xiaola"
    assert xiaola_call["data"]["sim_balance"] == 1000000
    assert "" not in xiaola_call["data"]


def test_nonzero_get_task_does_not_feedback_or_raise():
    transport = FakeTransport(response={"code": 1, "msg": "empty"})
    client = make_center(transport)
    assert client.get_task()["code"] == 1
    assert actions(transport, "Feedback") == []
    assert len(transport.calls) == 1


def test_get_task_and_feedback_are_not_retried():
    transport = FakeTransport()
    transport.error_after_write = TimeoutError("lost")
    client = make_center(transport)
    assert client.get_task()["code"] == -1
    assert len(transport.calls) == 1
    transport.error_after_write = None
    transport.response = None
    client.feedback(1, 0, {"ok": True})
    assert len(transport.calls) == 2
    assert actions(transport, "Feedback")[0]["status"] == 0


def test_balance_endpoints_follow_profile():
    device_transport = FakeTransport()
    make_center(device_transport).synchronize_balance("12.5")
    device_call = device_transport.calls[0]
    assert "/notify/vtsi/index.html" in device_call["url"]
    assert device_call["data"]["action"] == "queryAmountCallback"
    assert device_call["data"]["balance"] == "12.5"

    xiaola_transport = FakeTransport()
    make_xiaola_center(xiaola_transport).synchronize_balance("9")
    xiaola_call = xiaola_transport.calls[0]
    assert "/notify/device_new/index.html" in xiaola_call["url"]
    assert "vtsi" not in xiaola_call["url"]
    assert xiaola_call["data"]["action"] == "queryAmountCallback"


def test_client_does_not_write_sync_status_or_callback_agent():
    assert not hasattr(make_center(FakeTransport()), "sync_status")
    transport = FakeTransport()
    client = make_center(transport)
    client.get_task()
    client.feedback(1, 2, {"result": {"state": 1}})
    client.in_executing_tasks()
    client.synchronize_balance("1")
    client.sms_receiving("hi", "0912", 1)
    joined = " ".join(call["url"] for call in transport.calls)
    assert "sync_status" not in joined
    assert "callback/order" not in joined
    assert device_new_profile().balance_path != xiaola_profile().balance_path


def test_executing_tasks_use_vtsi_path_for_both_profiles():
    for factory in (make_center, make_xiaola_center):
        transport = FakeTransport(response={"code": 0, "data": []})
        factory(transport).in_executing_tasks()
        assert "/notify/vtsi/index.html" in transport.calls[0]["url"]
        assert transport.calls[0]["data"]["action"] == "inExecutingTasks"


@pytest.mark.parametrize("broken", [FakeResponse(text="", payload={"code": "0000"}), FakeResponse(text="nope", broken=True)])
def test_unreadable_response_is_a_single_post(broken):
    transport = FakeTransport(response=broken)
    client = make_center(transport)
    client.get_task()
    assert len(transport.calls) == 1
