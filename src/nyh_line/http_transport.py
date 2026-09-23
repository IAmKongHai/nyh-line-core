"""真实 HTTP 传输。每个 post 只发出一次。"""

from __future__ import annotations

from nyh_line.errors import RequestNotSent


def requests_transport(timeout: float = 30):
    import requests

    class Transport:
        def post(self, url, data):
            try:
                return requests.post(url, data=data, timeout=timeout)
            except requests.ConnectionError as exc:
                raise RequestNotSent(str(exc)) from exc

    return Transport()
