# nyh-line-core

六条充值线路的独立工人。改一条线路的拉单、提交或查单规则时只改这一处。

生产切换另授。本仓库只交付代码和 Supervisor 定义，不停止、不启动任何生产进程。

菲岛回调留在 nyhCenter。菲岛成功、失败和退款终态仍由 Center 回调写入，这里不提供菲岛查单。

## 命令入口

在本仓库根目录执行。下面几条都是入口：无参数、帮助，以及「线路 + 角色」。

```bash
PYTHONPATH=src python -m nyh_line
PYTHONPATH=src python -m nyh_line --help
PYTHONPATH=src python -m nyh_line fd-globe submit
```

线路名：`fd-globe`、`fd-smart`、`vtsi-dito`、`vtsi-globe`、`vtsi-smart`、`xiaola`。

角色：`submit` 或 `check`。菲岛只有 `submit`。未知线路、未知角色，或给菲岛加上 `check`，进程以非 0 退出。

缺少必填环境变量时，进程在进入拉单前退出。键名见 `env.example`，值放在部署机的环境里，不要写进仓库。

安装后也可以直接执行 `python -m nyh_line <line> <role>`。

## 十个程序

| 程序名 | 命令 |
| --- | --- |
| line-fd-globe-submit | `python -m nyh_line fd-globe submit` |
| line-fd-smart-submit | `python -m nyh_line fd-smart submit` |
| line-vtsi-dito-submit | `python -m nyh_line vtsi-dito submit` |
| line-vtsi-dito-check | `python -m nyh_line vtsi-dito check` |
| line-vtsi-globe-submit | `python -m nyh_line vtsi-globe submit` |
| line-vtsi-globe-check | `python -m nyh_line vtsi-globe check` |
| line-vtsi-smart-submit | `python -m nyh_line vtsi-smart submit` |
| line-vtsi-smart-check | `python -m nyh_line vtsi-smart check` |
| line-xiaola-submit | `python -m nyh_line xiaola submit` |
| line-xiaola-check | `python -m nyh_line xiaola check` |

定义在 `deploy/supervisor/`。每份都有 `stopasgroup`、`killasgroup`，`stopwaitsecs` 至少 60 秒。`directory` 在部署时改成这台机器上的仓库路径。

## 生产切换（另行授权，本次不执行）

按线路切换，不要让同一 `device_id` 的新旧查单同时 Feedback。

1. 先停该线路正在运行的旧提交和旧查单，确认进程已经退出。
2. 再起新查单，用原来的商户单号消化已经处于执行中的发送行。
3. 最后起新提交。
4. 不要停 nyhCenter 上的菲岛回调。停在执行中且始终没有回调的菲岛单，由人工在 Center 回滚。

## 测试

```bash
python -m pytest
```

测试注入假传输和假 SOAP，不请求外网，也不读取仓库外的配置。
