import argparse
import asyncio
import json
import os
import re
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import history

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    bulk_walk_cmd,
    get_cmd,
)

SYS = {
    "sysUpTime": "1.3.6.1.2.1.1.3.0",
}

EXCLUDE_PORTS = re.compile(r"^(InLoopBack|NULL|Console|LoopBack|Tunnel|Virtual|Vlanif)", re.I)

MAX_POINTS = 2000

COL = {
    "name": "1.3.6.1.2.1.31.1.1.1.1",
    "oper": "1.3.6.1.2.1.2.2.1.8",
    "in": "1.3.6.1.2.1.31.1.1.1.6",
    "out": "1.3.6.1.2.1.31.1.1.1.10",
}

OPER = {
    "1": "up",
    "2": "down",
    "3": "testing",
    "4": "unknown",
    "5": "dormant",
    "6": "notPresent",
    "7": "lowerLayerDown",
}

HEALTH = {
    "cpu": [
        "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5",
        "1.3.6.1.4.1.9.2.1.56.0",
        "1.3.6.1.4.1.25506.2.6.1.1.1.1.6",
    ],
    "mem": [
        "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.7",
        "1.3.6.1.4.1.9.9.48.1.1.1.6",
        "1.3.6.1.4.1.25506.2.6.1.1.1.1.8",
    ],
    "temp": [
        "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.11",
        "1.3.6.1.4.1.9.9.13.1.3.1.3",
        "1.3.6.1.4.1.25506.2.6.1.1.1.1.12",
    ],
}

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
LOCK = threading.Lock()
STATE = {"prev": {}, "ts": None}
CACHE = {"data": None}
CACHE_LOCK = threading.Lock()
HEALTH_INDEX = {}
HEALTH_RESOLVED = False
STOP = threading.Event()
ACC = {}
ACC_LOCK = threading.Lock()


async def get_many(engine, auth, target, context, oids):
    err, status, _, binds = await get_cmd(
        engine,
        auth,
        target,
        context,
        *[ObjectType(ObjectIdentity(oid)) for oid in oids],
    )
    if err or status:
        raise RuntimeError(str(err) if err else f"{status.prettyPrint()}")
    return {str(vb[0]): vb[1].prettyPrint() for vb in binds}


async def walk_column(engine, auth, target, context, oid):
    out = {}
    async for err, status, _index, varBinds in bulk_walk_cmd(
        engine, auth, target, context, 0, 25, ObjectType(ObjectIdentity(oid))
    ):
        if err or status:
            break
        stop = False
        for vb in varBinds:
            key = str(vb[0])
            if not key.startswith(oid):
                stop = True
                break
            out[key[len(oid) + 1 :]] = vb[1].prettyPrint()
        if stop:
            break
    return out


def to_number(value):
    if value is None:
        return None
    try:
        return float(str(value).split()[0])
    except (ValueError, IndexError):
        return None


def fmt_uptime(value):
    ticks = to_number(value)
    if ticks is None:
        return value
    secs = int(ticks) // 100
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{days}天 {clock}" if days else clock


async def collect(host, community, port):
    global HEALTH_RESOLVED

    engine = SnmpEngine()
    auth = CommunityData(community, mpModel=1)
    context = ContextData()
    target = await UdpTransportTarget.create((host, port), timeout=2, retries=1)

    resolving = not (HEALTH_RESOLVED and HEALTH_INDEX)
    health_task = resolve_health(engine, auth, target, context) if resolving else read_health(
        engine, auth, target, context
    )

    tasks = [get_many(engine, auth, target, context, list(SYS.values()))]
    tasks += [walk_column(engine, auth, target, context, oid) for oid in COL.values()]
    tasks.append(health_task)
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sys_raw = results[0]
    if isinstance(sys_raw, Exception):
        raise sys_raw
    system = {label: sys_raw.get(oid) for label, oid in SYS.items()}

    ports = {}
    for field, res in zip(COL.keys(), results[1 : 1 + len(COL)]):
        if isinstance(res, Exception):
            continue
        for idx, value in res.items():
            ports.setdefault(idx, {})[field] = value

    health_res = results[-1]
    health = {}
    if not isinstance(health_res, Exception):
        if resolving:
            resolved, health = health_res
            HEALTH_INDEX.update(resolved)
            HEALTH_RESOLVED = True
        else:
            health = health_res

    return system, ports, health


async def resolve_health(engine, auth, target, context):
    resolved, values = {}, {}
    depth = max(len(v) for v in HEALTH.values())
    for i in range(depth):
        pending = [m for m in HEALTH if m not in resolved and i < len(HEALTH[m])]
        if not pending:
            break
        cols = await asyncio.gather(
            *[walk_column(engine, auth, target, context, HEALTH[m][i]) for m in pending],
            return_exceptions=True,
        )
        for metric, col in zip(pending, cols):
            if isinstance(col, Exception):
                continue
            pairs = [(to_number(v), k) for k, v in col.items()]
            pairs = [(n, k) for n, k in pairs if n is not None]
            if pairs:
                num, key = max(pairs, key=lambda item: item[0])
                resolved[metric] = f"{HEALTH[metric][i]}.{key}" if key else HEALTH[metric][i]
                values[metric] = num
    return resolved, values


async def read_health(engine, auth, target, context):
    if not HEALTH_INDEX:
        return {}
    metrics = list(HEALTH_INDEX)
    oids = [HEALTH_INDEX[m] for m in metrics]
    raw = await get_many(engine, auth, target, context, oids)
    out = {}
    for metric, oid in zip(metrics, oids):
        num = to_number(raw.get(oid))
        if num is not None:
            out[metric] = num
    return out


def build_payload(host, community, port):
    try:
        system, ports_raw, health = asyncio.run(collect(host, community, port))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "ts": time.time()}

    now = time.monotonic()
    with LOCK:
        prev = STATE["prev"]
        prev_ts = STATE["ts"]
        dt = (now - prev_ts) if prev_ts else None
        result = []
        for idx, row in ports_raw.items():
            name = row.get("name", f"if{idx}")
            if EXCLUDE_PORTS.match(name):
                continue
            oper_code = str(row.get("oper", ""))
            rx = tx = 0.0
            old = prev.get(idx)
            if old and dt and dt > 0:
                try:
                    rx = max(0.0, (int(row.get("in", 0)) - int(old.get("in", 0))) * 8 / dt)
                    tx = max(0.0, (int(row.get("out", 0)) - int(old.get("out", 0))) * 8 / dt)
                except (TypeError, ValueError):
                    pass
            result.append(
                {
                    "idx": idx,
                    "name": name,
                    "oper": OPER.get(oper_code, oper_code or "unknown"),
                    "rx_bps": round(rx, 1),
                    "tx_bps": round(tx, 1),
                }
            )
        STATE["prev"] = ports_raw
        STATE["ts"] = now

    result.sort(key=lambda r: int(r["idx"]) if str(r["idx"]).isdigit() else 0)
    up = sum(1 for r in result if r["oper"] == "up")
    return {
        "ok": True,
        "ts": time.time(),
        "host": host,
        "system": {**system, "sysUpTime": fmt_uptime(system.get("sysUpTime"))},
        "health": health,
        "counts": {"up": up, "total": len(result)},
        "ports": result,
    }


class Handler(BaseHTTPRequestHandler):
    config = {}

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type):
        data = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def _serve_static(self, rel):
        safe = os.path.normpath(rel).replace("\\", "/").lstrip("/")
        full = os.path.abspath(os.path.join(WEB_DIR, safe))
        if not full.startswith(WEB_DIR + os.sep) or not os.path.isfile(full):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            self._send(200, fh.read(), types.get(ext, "application/octet-stream"))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/data":
            with CACHE_LOCK:
                payload = CACHE["data"]
            if payload is None:
                payload = {"ok": False, "error": "首次采集进行中，请稍候…", "ts": time.time()}
            self._send(200, json.dumps(payload), "application/json; charset=utf-8")
            return
        if path == "/api/history":
            self._serve_history()
            return
        if path == "/" or path == "/index.html":
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def _serve_history(self):
        qs = parse_qs(urlparse(self.path).query)
        port = qs.get("port", [""])[0]
        cfg = self.config
        try:
            seconds = int(qs.get("seconds", ["3600"])[0])
        except ValueError:
            seconds = 3600
        seconds = max(60, min(seconds, int(cfg.get("retain_days", 7) * 86400)))
        try:
            step = int(qs.get("step", ["0"])[0])
        except ValueError:
            step = 0
        if step > 0:
            step = max(1, min(step, seconds))
        else:
            step = max(60, seconds // MAX_POINTS)
        try:
            series = history.query(port, int(time.time()) - seconds, step)
            payload = {
                "ok": True,
                "port": port,
                "name": history.last_name(port),
                "step": step,
                "series": series,
            }
        except Exception as exc:
            payload = {"ok": False, "error": str(exc), "port": port, "step": step, "series": []}
        self._send(200, json.dumps(payload), "application/json; charset=utf-8")


def accumulate(ports):
    with ACC_LOCK:
        for p in ports:
            if p.get("oper") != "up":
                continue
            entry = ACC.setdefault(p["idx"], {"name": p.get("name"), "sum_rx": 0.0, "sum_tx": 0.0, "n": 0})
            entry["name"] = p.get("name")
            entry["sum_rx"] += p.get("rx_bps") or 0.0
            entry["sum_tx"] += p.get("tx_bps") or 0.0
            entry["n"] += 1


def flush(ts):
    with ACC_LOCK:
        rows = [
            {
                "idx": idx,
                "name": entry["name"],
                "oper": "up",
                "rx_bps": entry["sum_rx"] / entry["n"],
                "tx_bps": entry["sum_tx"] / entry["n"],
            }
            for idx, entry in ACC.items()
            if entry["n"]
        ]
        ACC.clear()
    if rows:
        try:
            history.record(ts, rows)
        except Exception:
            pass
    return len(rows)


def poll_loop(cfg):
    last_record = time.monotonic()
    last_prune = time.monotonic()
    while not STOP.is_set():
        start = time.monotonic()
        payload = build_payload(cfg["host"], cfg["community"], cfg["port"])
        with CACHE_LOCK:
            CACHE["data"] = payload
        if payload.get("ok"):
            accumulate(payload["ports"])
        if start - last_record >= cfg["record_interval"]:
            flush(time.time())
            last_record = start
        if start - last_prune >= 300:
            history.prune(int(time.time()) - int(cfg["retain_days"] * 86400))
            last_prune = start
        STOP.wait(max(0.1, cfg["interval"] - (time.monotonic() - start)))


def main():
    p = argparse.ArgumentParser(description="SNMP switch web monitor")
    p.add_argument("host", nargs="?", default="192.168.10.1")
    p.add_argument("-c", "--community", default="public")
    p.add_argument("-p", "--port", type=int, default=161, help="SNMP port")
    p.add_argument("-w", "--web-port", type=int, default=8000, help="HTTP port")
    p.add_argument("--bind", default="127.0.0.1", help="HTTP bind address")
    p.add_argument("-i", "--interval", type=float, default=2.0, help="poll interval seconds")
    p.add_argument("--record-interval", type=float, default=60.0, help="history sampling interval seconds")
    p.add_argument("--retain-days", type=float, default=7.0, help="history retention days")
    p.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "history.db"),
                   help="SQLite history database path")
    args = p.parse_args()

    cfg = {
        "host": args.host,
        "community": args.community,
        "port": args.port,
        "interval": args.interval,
        "record_interval": args.record_interval,
        "retain_days": args.retain_days,
    }
    Handler.config = cfg

    history.init(args.db)
    poll_thread = threading.Thread(target=poll_loop, args=(cfg,), daemon=True)
    poll_thread.start()

    server = ThreadingHTTPServer((args.bind, args.web_port), Handler)
    server.daemon_threads = True

    def shutdown(_signum, _frame):
        STOP.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (OSError, ValueError):
            pass

    print(f"SNMP target : {args.host} (community={args.community}, port={args.port})")
    print(f"History db  : {args.db} (retain {args.retain_days:g}d, every {args.record_interval:g}s)")
    print(f"Monitor page: http://{args.bind}:{args.web_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        STOP.set()
        poll_thread.join(timeout=5)
        flush(time.time())
        server.server_close()


if __name__ == "__main__":
    main()
