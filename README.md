# SNMP 交换机监控面板

轮询交换机 SNMP，Web 展示端口实时速率、健康指标与历史曲线。

## 功能

- 端口状态与实时 RX/TX 速率（默认每 2s 轮询）
- CPU / 内存 / 温度（按 Huawei/H3C/Cisco OID 自动识别）
- 历史曲线（默认每 60s 采样一次，保留 7 天），平滑曲线
- 鼠标悬停显示时间与速率；可切换 RX / TX 显示；自动选中第一个端口

## 环境要求

- Python 3.9+
- 仅依赖 `pysnmp==7.1.21`（见 `requirements.txt`；最后一个支持 Python 3.9 的版本）
- SNMP v2c，默认 host `192.168.10.1`、community `public`、port `161`

## 本地运行

Windows：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe snmp_web.py 192.168.10.1 -c public -w 8000 --bind 127.0.0.1
```

Linux：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python snmp_web.py 192.168.10.1 -c public -w 8000 --bind 127.0.0.1
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `host` | `192.168.10.1` | 交换机地址 |
| `-c, --community` | `public` | SNMP community |
| `-p, --port` | `161` | SNMP 端口 |
| `-w, --web-port` | `8000` | HTTP 端口 |
| `--bind` | `127.0.0.1` | HTTP 监听地址（局域网访问用 `0.0.0.0`） |
| `-i, --interval` | `2` | 轮询间隔（秒） |
| `--record-interval` | `60` | 历史采样间隔（秒） |
| `--retain-days` | `7` | 历史保留天数 |
| `--db` | `db/history.db` | SQLite 数据库路径 |

## Debian 部署（systemd 开机自启）

以下命令均以 root 执行，部署目录为 `/opt/snmp-dashboard`。

### 1. 安装依赖与代码

```bash
apt update && apt install -y python3 python3-venv git ufw

git clone https://github.com/sz2013/snmp-dashboard.git /opt/snmp-dashboard
cd /opt/snmp-dashboard

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 配置

```bash
cp deploy/.env.example /opt/snmp-dashboard/.env
nano /opt/snmp-dashboard/.env        # 填 SNMP_HOST / SNMP_COMMUNITY
chmod 600 /opt/snmp-dashboard/.env
```

### 3. 安装 systemd 服务

```bash
cp deploy/snmp-dashboard.service /etc/systemd/system/snmp-dashboard.service
systemctl daemon-reload
systemctl enable --now snmp-dashboard
systemctl status snmp-dashboard --no-pager
```

`deploy/snmp-dashboard.service` 关键内容：

```ini
WorkingDirectory=/opt/snmp-dashboard
EnvironmentFile=/opt/snmp-dashboard/.env
ExecStart=/opt/snmp-dashboard/.venv/bin/python snmp_web.py ${SNMP_HOST} -c ${SNMP_COMMUNITY} -w 8000 --bind 0.0.0.0 -i 2 --record-interval 60 --retain-days 7 --db /opt/snmp-dashboard/db/history.db
Restart=always
```

### 4. 开放端口并访问

```bash
ufw allow 8000/tcp
```

浏览器访问 `http://<服务器IP>:8000/`。

### 运维

```bash
journalctl -u snmp-dashboard -f      # 查看日志
systemctl restart snmp-dashboard     # 重启
```

数据库位于 `/opt/snmp-dashboard/db/history.db`，由 `history.init()` 自动创建；`systemctl stop/restart` 会优雅退出并落盘。

## 目录结构

```
snmp_web.py     # HTTP 服务 + 后台轮询线程，提供 /api/data、/api/history
history.py      # SQLite 历史持久化（WAL，按线程连接）
web/            # 前端静态资源（index.html / app.js / history.js / style.css）
deploy/         # systemd 单元与 .env 示例
requirements.txt
```

## 安全说明

- 服务无鉴权、明文 HTTP，请勿直接暴露到公网；建议限制来源网段，或前置 nginx 反向代理并启用 HTTPS + 认证。
- SNMP community 通过 `/opt/snmp-dashboard/.env` 提供（`chmod 600`），不会写入仓库。
