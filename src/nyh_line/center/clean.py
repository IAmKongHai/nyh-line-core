"""清洗上游响应，去掉不该随订单明细出圈的字段。"""

from __future__ import annotations

# 这两项只按原名丢掉，是本地排障挂上去的上下文。
_DROP_EXACT = {"_request_info", "_response_info"}

# 任意嵌套层都丢掉。比较前去掉下划线并转小写，nonceStr 与 nonce_str 都命中。
_DROP_NESTED = {
    "price",
    "sign",
    "signature",
    "uid",
    "noncestr",
    "secret",
    "token",
}


def _nested_name(key) -> str:
    return str(key).lower().replace("_", "")


def clean_api_result(value, seen=None):
    """先清洗再交给序列化。循环引用换成 None，保证清洗结果可以序列化。"""
    if seen is None:
        seen = set()
    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            return None
        seen.add(marker)
        cleaned = {}
        for key, item in value.items():
            if key in _DROP_EXACT:
                continue
            if _nested_name(key) in _DROP_NESTED:
                continue
            cleaned[key] = clean_api_result(item, seen)
        seen.discard(marker)
        return cleaned
    if isinstance(value, list):
        marker = id(value)
        if marker in seen:
            return None
        seen.add(marker)
        cleaned_list = [clean_api_result(item, seen) for item in value]
        seen.discard(marker)
        return cleaned_list
    if isinstance(value, tuple):
        return [clean_api_result(item, seen) for item in value]
    return value
