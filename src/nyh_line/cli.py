"""命令入口。未知线路、未知角色，以及菲岛查单，都以非 0 退出。"""

from __future__ import annotations

import sys

USAGE = """用法:
  python -m nyh_line <line> <role>
  python -m nyh_line --help

线路: fd-globe, fd-smart, vtsi-dito, vtsi-globe, vtsi-smart, xiaola
角色: submit 或 check。菲岛只有 submit。

无参数或帮助只打印说明并退出，不访问网络。
"""


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    if len(args) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    line, role = args
    from nyh_line.lines.registry import role_ok, run_line

    if not role_ok(line, role):
        print("未知线路或角色", file=sys.stderr)
        return 2
    return run_line(line, role)
