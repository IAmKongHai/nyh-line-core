"""菲岛 Smart 提交。会回写的失败发一条模板。"""

from nyh_line.upstream.fd import submit_fd_task


def submit_task(task, center, client) -> str:
    return submit_fd_task(task, center, client, notify=True)
