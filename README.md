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

配置有错时，进程在进入拉单前以非 0 退出，stderr 只列键名，不打印键值。会拦下的情况：本线路缺必填键、环境里还留着旧的无前缀设备键、本线路 `device_id` 与其它线路重复、URL 缺 scheme 或主机名、赢啦数值不是有限的正数、赢啦提交一个国家码都没有。

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

定义在 `deploy/supervisor/`。每份都用 `/opt/nyh-line-core/.venv/bin/python` 启动，带 `stopasgroup`、`killasgroup`，`stopwaitsecs` 为 60 秒，`autostart=false`。日志写到仓库里的 `logs/<程序名>.log`，单个文件 50MB，保留 10 份。

## 部署

以下步骤从一台只有 Python 3.13 和 Supervisor 的机器开始。部署目录以 `/opt/nyh-line-core` 为例；换目录时，把 10 份 ini 里的 `command`、`directory`、`stdout_logfile` 一起改掉。

1. 取代码并建虚拟环境。

   ```bash
   git clone git@github.com:IAmKongHai/nyh-line-core.git /opt/nyh-line-core
   cd /opt/nyh-line-core
   python3.13 -m venv .venv
   .venv/bin/pip install -e .
   ```

   必须用 `pip install -e .` 可编辑安装。CA 证书目录 `certs/`、VTSI 合并 CA 包的输出目录 `var/`、默认环境文件 `.env`，都按源码位置推算仓库根。装成普通包后代码跑在 `site-packages` 下，这三处路径全部失效，而且 `certs/` 不会被打包进去。

2. 放环境文件。

   ```bash
   cp env.example /opt/nyh-line-core/.env
   chmod 600 /opt/nyh-line-core/.env
   ```

   进程启动时读取 `NYH_LINE_ENV_FILE` 指向的文件，没设这个变量就读仓库根的 `.env`。进程环境里的同名键优先于文件。文件只在启动时读一次，改完要重启对应程序。文件的属主应是 Supervisor 运行这些程序的用户，权限 600。

   `CENTER_DEVICE_ID`、`CENTER_DEVICE_KEY`、`CENTER_SIM_ID`、`VTSI_ACCOUNT` 这几个旧的无前缀键已删除。环境里只要还有其中一个，所有程序都拒绝启动。

3. 按线路核对设备号。每条线路的三个设备键必须来自这条线路自己的旧配置，六条线路的 `device_id` 互不相同。

   | 线路 | 键名前缀 | 旧配置来源 |
   | --- | --- | --- |
   | 菲岛 Globe | `FD_GLOBE_` | Center `FD_config` 当前返回的菲岛 Globe 设备 |
   | 菲岛 Smart | `FD_SMART_` | Center `FD_config` 当前返回的菲岛 Smart 设备 |
   | VTSI Dito | `VTSI_DITO_` | 旧 `vtsi-dito` 目录的 `data/config.json` |
   | VTSI Globe | `VTSI_GLOBE_` | 旧 `vtsi-globe` 目录的 `data/config.json` |
   | VTSI Smart | `VTSI_SMART_` | 旧 `vtsi-smart` 目录的 `data/config.json` |
   | 赢啦 | `XIAOLA_` | 旧赢啦目录的 `.env` |

   VTSI 三条各填自己的 `VTSI_<X>_ACCOUNT`（与 Center `vtisConfig` 按运营商给的 account 一致）；用户名、密码和 WSDL 三条共用。之后 Center 侧轮换设备密钥时，要同步改这份文件。

4. 告警收件人。`<前缀>_ALERT_USER_IDS` 填逗号分隔的用户 ID。留空时这条线路不发模板，回写照常。

5. 装 Supervisor 定义并核对。

   ```bash
   cp deploy/supervisor/*.ini /etc/supervisor/conf.d/
   supervisorctl reread
   supervisorctl update
   ```

   ini 都是 `autostart=false`，`update` 只登记不启动。环境文件填好之前执行 `supervisorctl start <程序名>`，程序会在拉单前退出并进入 FATAL，`logs/<程序名>.log` 里写着缺的键名。这可以用来确认目录、解释器和日志路径都对。

6. 日志。每行带时间、级别、线程名。按 `task_id` 可以查到一单的拉单、上游结果和每次 Feedback。日志里记完整手机号，不记设备密钥、上游密钥、VTSI 密码、签名、`auth` 和 `price`。

### 结果未知的单

以下情况不回写，发送行保持执行中，日志有一条带 `task_id` 的 ERROR：

- 菲岛：请求可能已到达上游（连接被对端断开或重置、读超时、SSL 错误，或响应无法解析）。只有连接阶段失败（连接超时、DNS 解析失败、连接被拒）才回写 0。同一条菲岛线路连续 3 笔结果未知时，暂停拉单 60 秒，只记一条 CRITICAL。
- VTSI：TOPUP 发出后超时（Smart 10 秒，Dito、Globe 50 秒），或拉单后 15 秒内没能发出 TOPUP。由查单按 `v<task_id>` 对账收口。
- 赢啦：下单超时或响应无法解析。由查单按 `nyh<task_id>` 对账收口。

菲岛没有查单。停在执行中且始终没有回调的菲岛单，按 `task_id` 查日志确认后，由人工在 Center 回滚。

## 生产切换（另行授权，本次不执行）

切换另行授权。按线路切换，不要让同一 `device_id` 的新旧查单同时 Feedback。

1. 核对这条线路在新环境文件里的 `<前缀>_CENTER_DEVICE_ID`，与正在运行的旧进程用的设备号一致（来源见「部署」第 3 步）。
2. 停该线路正在运行的旧提交和旧查单，确认进程已经退出。
3. 起新查单，用原来的商户单号消化已经处于执行中的发送行。
4. 最后起新提交。
5. 不要停 Center 上的菲岛回调，菲岛回调留在 Center。停在执行中且始终没有回调的菲岛单，由人工在 Center 回滚。

## 测试

```bash
.venv/bin/python -m pytest
```

测试注入假传输和假 SOAP，不请求外网，也不读取仓库外的配置。
