"""环境文件、线路前缀键和启动校验。所有失败都发生在拉单之前。"""

from pathlib import Path

import pytest

from nyh_line.lines import registry
from nyh_line.lines.registry import run_line
from nyh_line.settings import (
    LINE_PREFIX,
    environ_map,
    parse_env_text,
    xiaola_submit_settings,
)

ROOT = Path(__file__).resolve().parents[1]


def _line_env(line, device_id="100"):
    prefix = LINE_PREFIX[line]
    env = {
        f"{prefix}_CENTER_DEVICE_ID": device_id,
        f"{prefix}_CENTER_DEVICE_KEY": f"{prefix.lower()}-device-secret",
        f"{prefix}_CENTER_SIM_ID": "3",
    }
    if line.startswith("fd-"):
        env.update({f"{prefix}_UID": "uid", f"{prefix}_KEY": "fd-secret", f"{prefix}_URL": "https://fd.example.com/"})
    elif line.startswith("vtsi-"):
        env.update(
            {
                "VTSI_USERNAME": "user",
                "VTSI_PASSWORD": "vtsi-secret",
                "VTSI_WSDL": "https://vtsi.example.com/ws?wsdl",
                f"{prefix}_ACCOUNT": f"ACC-{prefix}",
            }
        )
    else:
        env.update(
            {
                "XIAOLA_API_BASE_URL": "https://xiaola.example.com",
                "XIAOLA_API_USERNAME": "user",
                "XIAOLA_API_SECRET_KEY": "xiaola-secret",
                "DEVICE_TYPE_XIAOLA": "1",
                "THREAD1_COUNTRY_CODE": "PH",
            }
        )
    return env


def _env(*lines):
    env = {"CENTER_PROTOCOL": "https", "CENTER_HOST": "center.example.com"}
    for index, line in enumerate(lines):
        env.update(_line_env(line, device_id=str(100 + index)))
    return env


def _run(line, role, env, capsys):
    served = []
    code = run_line(line, role, env=env, serve=lambda *args: served.append(args))
    return code, served, capsys.readouterr().err


def test_only_globe_keys_pass_globe_and_fail_dito(capsys):
    env = _env("vtsi-globe")
    code, served, _err = _run("vtsi-globe", "check", env, capsys)
    assert code == 0 and len(served) == 1
    code, served, err = _run("vtsi-dito", "check", env, capsys)
    assert code != 0 and served == []
    assert "VTSI_DITO_CENTER_DEVICE_ID" in err


def test_duplicate_device_id_lists_both_keys_without_value(capsys):
    """AE4：两条线路设备号相同，拉单前退出，stderr 列两个键名，不含键值。"""
    env = _env("vtsi-dito", "vtsi-globe")
    env["VTSI_DITO_CENTER_DEVICE_ID"] = "4242"
    env["VTSI_GLOBE_CENTER_DEVICE_ID"] = "4242"
    code, served, err = _run("vtsi-dito", "check", env, capsys)
    assert code != 0 and served == []
    assert "VTSI_DITO_CENTER_DEVICE_ID" in err
    assert "VTSI_GLOBE_CENTER_DEVICE_ID" in err
    assert "4242" not in err


@pytest.mark.parametrize("legacy", ["CENTER_DEVICE_ID", "CENTER_DEVICE_KEY", "CENTER_SIM_ID", "VTSI_ACCOUNT"])
def test_legacy_unprefixed_key_blocks_startup(legacy, capsys):
    env = _env("vtsi-smart")
    env[legacy] = "old-value"
    code, served, err = _run("vtsi-smart", "submit", env, capsys)
    assert code != 0 and served == []
    assert legacy in err
    assert "前缀" in err
    assert "old-value" not in err


@pytest.mark.parametrize(
    "key,value",
    [
        ("RECHARGE_INTERVAL_TIME", "abc"),
        ("MAX_THREADS", "two"),
        ("RECHARGE_INTERVAL_TIME", "nan"),
        ("RECHARGE_INTERVAL_TIME", "0"),
        ("RECHARGE_INTERVAL_TIME", "-3"),
        ("MAX_THREADS", "0"),
        ("RECHARGE_MAX_TASKS_PER_BATCH", "2.5"),
        ("RECHARGE_BATCH_INTERVAL", "inf"),
    ],
)
def test_bad_xiaola_numbers_fail_submit(key, value, capsys):
    env = _env("xiaola")
    env[key] = value
    code, served, err = _run("xiaola", "submit", env, capsys)
    assert code != 0 and served == []
    assert key in err


@pytest.mark.parametrize("url", ["example.com/", "https://", "ftp://fd.example.com"])
def test_bad_fd_url_fails_submit(url, capsys):
    env = _env("fd-globe")
    env["FD_GLOBE_URL"] = url
    code, served, err = _run("fd-globe", "submit", env, capsys)
    assert code != 0 and served == []
    assert "FD_GLOBE_URL" in err


def test_bad_center_host_fails(capsys):
    env = _env("fd-smart")
    env["CENTER_PROTOCOL"] = "center.example.com"
    code, served, _err = _run("fd-smart", "submit", env, capsys)
    assert code != 0 and served == []


def test_xiaola_submit_needs_a_country_but_check_does_not(capsys):
    env = _env("xiaola")
    del env["THREAD1_COUNTRY_CODE"]
    code, served, err = _run("xiaola", "submit", env, capsys)
    assert code != 0 and served == []
    assert "THREADn_COUNTRY_CODE" in err
    code, served, _err = _run("xiaola", "check", env, capsys)
    assert code == 0 and len(served) == 1


def test_xiaola_numbers_parsed_once_with_defaults():
    parsed = xiaola_submit_settings({"RECHARGE_INTERVAL_TIME": "2.5", "MAX_THREADS": "4"})
    assert parsed.interval_time == 2.5
    assert parsed.max_threads == 4
    assert parsed.max_tasks_per_batch == 10
    assert parsed.batch_interval == 15


def test_env_file_comments_blank_lines_and_empty_values():
    text = "# 注释\n\nCENTER_HOST=center.example.com\nEMPTY=\n  SPACED = value \nQUOTED=\"a b\"\nNOEQUALS\n"
    assert parse_env_text(text) == {
        "CENTER_HOST": "center.example.com",
        "EMPTY": "",
        "SPACED": "value",
        "QUOTED": "a b",
    }


def test_process_env_overrides_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "line.env"
    env_file.write_text("CENTER_HOST=from-file\nCENTER_PROTOCOL=https\n", encoding="utf-8")
    monkeypatch.setenv("NYH_LINE_ENV_FILE", str(env_file))
    monkeypatch.setenv("CENTER_HOST", "from-process")
    merged = environ_map()
    assert merged["CENTER_HOST"] == "from-process"
    assert merged["CENTER_PROTOCOL"] == "https"


def test_missing_env_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("NYH_LINE_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("CENTER_HOST", raising=False)
    assert "CENTER_HOST" not in environ_map()


def test_injected_env_is_used_as_is():
    env = {"CENTER_HOST": "x"}
    assert environ_map(env) is env


@pytest.mark.parametrize("line", list(LINE_PREFIX))
def test_make_center_reads_prefixed_device_keys(line):
    env = _env(*LINE_PREFIX)
    prefix = LINE_PREFIX[line]
    center = registry._make_center(env, line, registry.device_new_profile())
    assert center.device_id == env[f"{prefix}_CENTER_DEVICE_ID"]
    assert center.device_key == env[f"{prefix}_CENTER_DEVICE_KEY"]
    assert center.sim_id == env[f"{prefix}_CENTER_SIM_ID"]


def test_vtsi_dito_balance_uses_dito_account(monkeypatch):
    env = _env("vtsi-dito", "vtsi-globe", "vtsi-smart")
    gateways = []

    class Captured(Exception):
        pass

    real_gateway = registry.VtsiGateway

    def capture(*args, **kwargs):
        gateway = real_gateway(*args, **kwargs)
        gateways.append(gateway)
        raise Captured

    monkeypatch.setattr(registry, "VtsiGateway", capture)
    with pytest.raises(Captured):
        registry._serve_vtsi("vtsi-dito", "check", env, stop=None)
    assert gateways[0].account == "ACC-VTSI_DITO"


def test_env_example_lists_every_line_device_keys():
    text = (ROOT / "env.example").read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0].strip() for line in text.splitlines() if "=" in line and not line.startswith("#")}
    for prefix in LINE_PREFIX.values():
        for suffix in ("CENTER_DEVICE_ID", "CENTER_DEVICE_KEY", "CENTER_SIM_ID", "ALERT_USER_IDS"):
            assert f"{prefix}_{suffix}" in keys
    for vtsi in ("VTSI_DITO", "VTSI_GLOBE", "VTSI_SMART"):
        assert f"{vtsi}_ACCOUNT" in keys
    for legacy in ("CENTER_DEVICE_ID", "CENTER_DEVICE_KEY", "CENTER_SIM_ID", "VTSI_ACCOUNT"):
        assert legacy not in keys
