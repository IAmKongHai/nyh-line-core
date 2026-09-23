"""命令入口、环境模板和 Supervisor 定义。"""

import socket
from pathlib import Path

from nyh_line.cli import main
from nyh_line.lines.registry import run_line
from nyh_line.policy import PROGRAMS

ROOT = Path(__file__).resolve().parents[1]


def test_help_and_no_args_exit_without_network(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("不应访问网络")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    assert main([]) == 0
    assert main(["--help"]) == 0


def test_unknown_line_exits_nonzero_and_stable():
    first = main(["no-such-line", "submit"])
    second = main(["no-such-line", "submit"])
    assert first != 0
    assert first == second
    assert main(["fd-globe", "check"]) == first
    assert main(["fd-smart", "check"]) == first
    assert main(["xiaola", "inspect"]) == first


def test_globe_fd_missing_env_exits_before_pull():
    started = []
    first = run_line("fd-globe", "submit", env={}, serve=lambda *_args: started.append(1))
    second = run_line("fd-globe", "submit", env={}, serve=lambda *_args: started.append(1))
    assert first != 0
    assert first == second
    assert started == []


def test_env_example_has_empty_values_only():
    for line in (ROOT / "env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        _key, separator, value = stripped.partition("=")
        assert separator == "="
        assert value.strip() == ""


def test_readme_lists_programs_and_cutover_notes():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for program, line, role in PROGRAMS:
        assert program in text
        assert f"python -m nyh_line {line} {role}" in text
    assert "切换另授" in text
    assert "菲岛回调留在 nyhCenter" in text
    assert "PYTHONPATH=src python -m nyh_line" in text


def test_supervisor_definitions_match_the_ten_programs():
    directory = ROOT / "deploy" / "supervisor"
    files = sorted(path.stem for path in directory.glob("*.ini"))
    assert files == sorted(program for program, _line, _role in PROGRAMS)
    assert len(files) == 10
    for program, line, role in PROGRAMS:
        path = directory / f"{program}.ini"
        text = path.read_text(encoding="utf-8")
        assert f"[program:{program}]" in text
        assert f"python -m nyh_line {line} {role}" in text
        assert "stopasgroup=true" in text
        assert "killasgroup=true" in text
        seconds = int(text.split("stopwaitsecs=", 1)[1].split()[0])
        assert seconds >= 60
        assert "wwwroot" not in text


def test_tree_has_no_filled_config_or_dependency_on_old_tree():
    assert not (ROOT / "config.json").exists()
    assert not (ROOT / ".env").exists()
    assert not (ROOT / "blocked_numbers.txt").exists()
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "legacy-cgi" in project
    assert "requests>=2.31" in project
    assert "pandas" not in project
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "supervisorctl-server" not in text
        assert "sync_" + "status" not in text
        assert "wwwroot" not in text
