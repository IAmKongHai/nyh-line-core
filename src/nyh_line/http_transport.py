"""真实 HTTP 传输。每个 post 只发出一次。"""

from __future__ import annotations

from nyh_line.errors import RequestNotSent


def failed_before_send(exc) -> bool:
    """按 urllib3 的失败阶段判断请求是否确定没发出。

    只认连接阶段失败：ConnectTimeout，以及原因是 ConnectTimeoutError（含
    NewConnectionError、NameResolutionError）的 MaxRetryError。SSLError 整类算未知：
    握手失败和读响应时的 TLS EOF 包成同一结构，分不开。
    """
    import requests
    from urllib3.exceptions import ConnectTimeoutError, MaxRetryError

    if isinstance(exc, requests.exceptions.SSLError):
        return False
    if isinstance(exc, requests.ConnectTimeout):
        return True
    if not isinstance(exc, requests.ConnectionError) or not exc.args:
        return False
    first = exc.args[0]
    return isinstance(first, MaxRetryError) and isinstance(first.reason, ConnectTimeoutError)


def requests_transport(timeout: float = 30):
    import requests

    class Transport:
        def post(self, url, data):
            try:
                return requests.post(url, data=data, timeout=timeout)
            except requests.RequestException as exc:
                if failed_before_send(exc):
                    raise RequestNotSent(type(exc).__name__) from exc
                raise

    return Transport()
