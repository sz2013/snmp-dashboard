# AGENTS.md

SNMP switch web dashboard. Pure-stdlib web server + vanilla JS frontend (no build step, no framework, no tests/lint/typecheck config).

## Setup / interpreter

- Use the bundled venv: `.venv\Scripts\python.exe` (Python 3.10.6). Dependencies are pinned in `requirements.txt` (`pysnmp==7.1.29`); install with `.venv\Scripts\python.exe -m pip install -r requirements.txt`.
- pysnmp 7.x async API is used everywhere: `pysnmp.hlapi.v3arch.asyncio`. `UdpTransportTarget.create(...)` is awaitable and `bulk_walk_cmd` is an async iterator — do not port snippets from older pysnmp tutorials.
- SNMP v2c only (`mpModel=1`); defaults: host `192.168.10.1`, community `public`, port `161`.

## Commands

```powershell
.venv\Scripts\python.exe snmp_web.py [host] -c public -w 8000 --bind 127.0.0.1 -i 2 --record-interval 60 --retain-days 7 --db db/history.db
```

No test suite and no lint/typecheck commands exist; verify changes by running the server against a device and loading the page.

## Architecture

- `snmp_web.py` — `http.server.ThreadingHTTPServer`; starts a daemon `poll_loop` thread that calls `build_payload` and caches the result in `CACHE`. Routes: `/api/data`, `/api/history`, `/`, `/static/*`. There is no async framework; `build_payload` bridges via `asyncio.run`.
- `history.py` — SQLite persistence. One connection per thread (`threading.local`), WAL mode. `history.init(path)` must run before any query; `snmp_web.py` calls it with an absolute path.
- `web/` — static frontend served as-is at `/static/`; `history.js` draws an inline SVG chart, `app.js` polls `/api/data`.
- `deploy/` — systemd unit + env example for Debian. `snmp_web.py` handles SIGINT/SIGTERM (sets `STOP`, joins `poll_loop`, flushes `ACC`, closes DB) so `systemctl stop/restart` is clean.

## Gotchas

- The SQLite DB lives in `db/` (default `db/history.db`, parent dir auto-created by `history.init`); `db/` and `__pycache__/` are ignored via `.gitignore`.
- Port `idx` is the SNMP ifIndex used as dict/DB key; keep it a string. `history.record` only stores ports whose `oper == "up"`.
- `EXCLUDE_PORTS` regex in `snmp_web.py` filters virtual/loopback interfaces.
- Health metrics (`HEALTH` CPU/mem/temp) are vendor-specific OID lists (Huawei/H3C/Cisco); `resolve_health` probes each candidate and picks the numerically max entry, then caches the resolved OID in `HEALTH_INDEX`.
- UI strings are Chinese (`lang="zh-CN"`); keep new user-facing text consistent.
- Code style: no comments, snake_case, functions grouped by concern.
