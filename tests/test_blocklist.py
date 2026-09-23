"""赢啦拦截名单：精确匹配、缺文件放行、按 mtime 热加载。"""

from nyh_line.xiaola.blocklist import Blocklist


def test_default_reload_interval_and_comments(tmp_path):
    path = tmp_path / "numbers.txt"
    path.write_text("# note\n\n9123456789\n;skip\n")
    blocklist = Blocklist(str(path))
    assert blocklist.reload_interval_seconds == 900
    assert blocklist.is_blocked("9123456789") is True
    assert blocklist.is_blocked("09123456789") is False


def test_missing_file_is_empty(tmp_path):
    blocklist = Blocklist(str(tmp_path / "missing.txt"))
    assert blocklist.is_blocked("9123456789") is False


def test_reload_respects_interval(tmp_path):
    path = tmp_path / "numbers.txt"
    path.write_text("9123456789\n")
    clock = {"now": 1000.0}
    blocklist = Blocklist(str(path), clock=lambda: clock["now"])
    path.write_text("9123456789\n9000000000\n")
    clock["now"] = 1500
    assert blocklist.is_blocked("9000000000") is False
    clock["now"] = 1900
    assert blocklist.is_blocked("9000000000") is True
