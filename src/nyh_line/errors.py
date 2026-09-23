"""线路进程里需要区分的失败类型。"""


class RequestNotSent(Exception):
    """连接在请求正文写出之前就失败了。此时上游还没有收到这笔充值。"""


class SendDeadlineMissed(Exception):
    """VTSI TOPUP 没能在发送截止时间前发出。这一笔没有发出，交给查单收口。"""
