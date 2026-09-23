"""线路进程里需要区分的失败类型。"""


class RequestNotSent(Exception):
    """连接在请求正文写出之前就失败了。此时上游还没有收到这笔充值。"""
