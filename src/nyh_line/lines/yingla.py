"""赢啦提交和查单。生产循环用公共 Center 客户端，不另做一套下单脚本。"""

from nyh_line.upstream.xiaola import check_xiaola_task, submit_xiaola_task


def submit_task(task, center, client, blocklist) -> str:
    return submit_xiaola_task(task, center, client, blocklist)


def check_task(record, center, client) -> str:
    return check_xiaola_task(record, center, client)
