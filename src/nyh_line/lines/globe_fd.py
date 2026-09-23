"""菲岛 Globe 提交。失败回写不发模板。"""

from nyh_line.upstream.fd import submit_fd_task


def submit_task(task, center, client, on_unknown=None) -> str:
    return submit_fd_task(task, center, client, notify=False, on_unknown=on_unknown)
