"""测试里的假传输。不打开网络。"""

from nyh_line.center.client import CenterClient
from nyh_line.center.profiles import device_new_profile, xiaola_profile
from nyh_line.errors import RequestNotSent


class FakeResponse:
    def __init__(self, payload=None, text=None, broken=False):
        self._payload = payload
        self.text = "" if text == "" else (text if text is not None else "{}")
        self.broken = broken

    def json(self):
        if self.broken:
            raise ValueError("bad json")
        return self._payload


class FakeTransport:
    def __init__(self, response=None):
        self.calls = []
        self.response = {"code": 0} if response is None else response
        self.fail_before_write = False
        self.error_after_write = None

    def post(self, url, data):
        if self.fail_before_write:
            raise RequestNotSent("connection refused")
        self.calls.append({"url": url, "data": {key: value for key, value in data.items()}})
        if self.error_after_write is not None:
            raise self.error_after_write
        if self.response is None:
            return None
        if callable(self.response):
            return self.response(url, data)
        if hasattr(self.response, "json"):
            return self.response
        return FakeResponse(self.response)


def make_center(transport, *, profile=None, country_code="", fee_charging_line="", alert=("1",)):
    return CenterClient(
        base_url="http://center.invalid",
        device_id=7,
        device_key="device-key",
        sim_id=3,
        profile=profile or device_new_profile(),
        transport=transport,
        country_code=country_code,
        fee_charging_line=fee_charging_line,
        alert_user_ids=tuple(alert),
        clock=lambda: 1700000000,
    )


def make_xiaola_center(transport, **kwargs):
    return make_center(transport, profile=xiaola_profile(), **kwargs)


def actions(transport, name):
    return [call["data"] for call in transport.calls if call["data"].get("action") == name]
