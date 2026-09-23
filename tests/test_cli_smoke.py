"""命令入口、环境模板和 Supervisor 定义。"""

import re
import socket
import subprocess
from pathlib import Path

from nyh_line.cli import main
from nyh_line.lines.registry import run_line
from nyh_line.policy import PROGRAMS
from nyh_line.settings import LEGACY_KEYS, LINE_PREFIX, required_keys

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


# ---- 部署材料（U7） ----


def _ini(program):
    return (ROOT / "deploy" / "supervisor" / f"{program}.ini").read_text(encoding="utf-8")


def test_supervisor_command_uses_venv_python_for_its_own_line():
    for program, line, role in PROGRAMS:
        command = re.search(r"^command=(.*)$", _ini(program), re.MULTILINE).group(1).strip()
        assert command.endswith(f".venv/bin/python -m nyh_line {line} {role}")
        assert command.startswith("/")
        assert program == f"line-{line}-{role}"


def test_supervisor_logs_are_capped_and_stop_waits():
    for program, _line, _role in PROGRAMS:
        text = _ini(program)
        assert "stdout_logfile_maxbytes=" in text
        assert "stdout_logfile_backups=" in text
        assert int(re.search(r"^stopwaitsecs=(\d+)", text, re.MULTILINE).group(1)) >= 60
        assert 'environment=PYTHONUNBUFFERED="1"' in text
        assert "autostart=false" in text
        assert f"/logs/{program}.log" in text


def test_readme_covers_deploy_steps():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for program, _line, _role in PROGRAMS:
        assert program in text
    for phrase in ("NYH_LINE_ENV_FILE", "切换另行授权", "菲岛回调留在 Center", "pip install -e .", "chmod 600"):
        assert phrase in text
    for prefix in ("FD_GLOBE_", "FD_SMART_", "VTSI_DITO_", "VTSI_GLOBE_", "VTSI_SMART_", "XIAOLA_"):
        assert prefix in text


def test_logs_gitkeep_is_tracked():
    assert (ROOT / "logs" / ".gitkeep").exists()
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "logs/.gitkeep"], cwd=ROOT, capture_output=True, text=True
    )
    assert tracked.returncode == 0, tracked.stderr


def _env_example_keys():
    text = (ROOT / "env.example").read_text(encoding="utf-8")
    return {line.split("=", 1)[0].strip() for line in text.splitlines() if "=" in line and not line.startswith("#")}


def test_env_example_matches_keys_read_by_code():
    keys = _env_example_keys()
    for line in LINE_PREFIX:
        assert set(required_keys(line)) <= keys, line
    optional = {"CENTER_VERSION", "BLOCKLIST_FILE_PATH", "MAX_THREADS", "RECHARGE_INTERVAL_TIME",
                "RECHARGE_MAX_TASKS_PER_BATCH", "RECHARGE_BATCH_INTERVAL", "THREAD1_COUNTRY_CODE"}
    assert optional <= keys
    assert not keys & set(LEGACY_KEYS)
