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
CACHE = {"data": None}
CACHE_LOCK = threading.Lock()
HEALTH_INDEX = {}
HEALTH_RESOLVED = False
HEALTH_RETRY_AT = 0.0
HEALTH_VALUES = {}
HEALTH_AT = 0.0
HEALTH_INTERVAL = 60.0
NAMES = {}
STOP = threading.Event()
ACC = {}
ACC_LOCK = threading.Lock()
RT = {}
PORTS = []
PORTS_AT = 0.0
PORTS_INTERVAL = 300.0
UP_PORTS = set()
OPER_STATE = {}
DOWN_AT = 0.0
DOWN_INTERVAL = 10.0
COUNTERS = {}
GET_CHUNK = 16


async def get_many(engine, auth, target, context, oids):
    err, status, _, binds = await get_cmd(
        engine,
        auth,
        target,
        context,
        *[ObjectType(ObjectIdentity(oid)) for oid in oids],
        lookupMib=False,
    )
    if err or status:
        raise RuntimeError(str(err) if err else f"{status.prettyPrint()}")
    return {str(vb[0]): vb[1].prettyPrint() for vb in binds}


async def walk_column(engine, auth, target, context, oid):
    out = {}
    async for err, status, _index, varBinds in bulk_walk_cmd(
        engine, auth, target, context, 0, 25, ObjectType(ObjectIdentity(oid)), lookupMib=False
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


async def get_many_chunked(engine, auth, target, context, oids):
    values = {}
    ok = True
    for i in range(0, len(oids), GET_CHUNK):
        try:
            values.update(await get_many(engine, auth, target, context, oids[i : i + GET_CHUNK]))
        except Exception:
            ok = False
    return values, ok


def if_key(idx):
    return int(idx) if str(idx).isdigit() else 0


def targeted_oids(now):
    down_due = now - DOWN_AT >= DOWN_INTERVAL
    oper_idx = set(UP_PORTS)
    if down_due:
        oper_idx.update(PORTS)
    counter_idx = set(UP_PORTS)
    oids = [f"{COL['oper']}.{idx}" for idx in sorted(oper_idx, key=if_key)]
    for field in ("in", "out"):
        oids.extend(f"{COL[field]}.{idx}" for idx in sorted(counter_idx, key=if_key))
    return oper_idx, counter_idx, down_due, oids


def counter_rates(values, idxs, sample_at):
    rates = {}
    for idx in idxs:
        raw_in = values.get(f"{COL['in']}.{idx}")
        raw_out = values.get(f"{COL['out']}.{idx}")
        if raw_in is None or raw_out is None:
            rates[idx] = (0.0, 0.0)
            continue
        try:
            cur_in = int(raw_in)
            cur_out = int(raw_out)
        except (TypeError, ValueError):
            rates[idx] = (0.0, 0.0)
            continue
        rx = tx = 0.0
        prev = COUNTERS.get(idx)
        if prev:
            dt = sample_at - prev["ts"]
            if dt > 0:
                rx = max(0.0, (cur_in - prev["in"]) * 8 / dt)
                tx = max(0.0, (cur_out - prev["out"]) * 8 / dt)
        COUNTERS[idx] = {"in": cur_in, "out": cur_out, "ts": sample_at}
        rates[idx] = (rx, tx)
    return rates


def port_rows(rates):
    rows = []
    for idx in PORTS:
        oper_code = str(OPER_STATE.get(idx, ""))
        rx, tx = rates.get(idx, (0.0, 0.0))
        rows.append(
            {
                "idx": idx,
                "name": NAMES.get(idx, f"if{idx}"),
                "oper": OPER.get(oper_code, oper_code or "unknown"),
                "rx_bps": round(rx, 1),
                "tx_bps": round(tx, 1),
            }
        )
    return rows


def counter_oids(idxs):
    oids = []
    for field in ("in", "out"):
        oids.extend(f"{COL[field]}.{idx}" for idx in sorted(idxs, key=if_key))
    return oids


async def collect(engine, auth, context, target):
    global HEALTH_RESOLVED, HEALTH_RETRY_AT, HEALTH_AT, PORTS, PORTS_AT, UP_PORTS, DOWN_AT

    now = time.monotonic()
    resolving = (not HEALTH_RESOLVED) or (not HEALTH_INDEX and now >= HEALTH_RETRY_AT)
    refresh = (not PORTS) or (now - PORTS_AT >= PORTS_INTERVAL)
    health_due = resolving or (now - HEALTH_AT >= HEALTH_INTERVAL)

    scalar_oids = list(SYS.values())
    if health_due and not resolving:
        scalar_oids += list(HEALTH_INDEX.values())

    tasks = [get_many(engine, auth, target, context, scalar_oids)]
    if resolving:
        tasks.append(resolve_health(engine, auth, target, context))
    base = len(tasks)

    oper_idx = set()
    counter_idx = set()
    down_due = False
    if refresh:
        tasks.append(walk_column(engine, auth, target, context, COL["name"]))
        tasks.append(walk_column(engine, auth, target, context, COL["oper"]))
    else:
        oper_idx, counter_idx, down_due, oids = targeted_oids(now)
        tasks.append(get_many_chunked(engine, auth, target, context, oids))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    raw = results[0]
    if isinstance(raw, Exception):
        raise raw
    system = {label: raw.get(oid) for label, oid in SYS.items()}

    if resolving:
        health_res = results[1]
        if not isinstance(health_res, Exception):
            resolved, values = health_res
            HEALTH_INDEX.update(resolved)
            HEALTH_RESOLVED = True
            HEALTH_VALUES.update(values)
            HEALTH_AT = now
            if not resolved:
                HEALTH_RETRY_AT = time.monotonic() + 300
    elif health_due:
        for metric, oid in HEALTH_INDEX.items():
            num = to_number(raw.get(oid))
            if num is not None:
                HEALTH_VALUES[metric] = num
        HEALTH_AT = now

    if refresh:
        name_res = results[base]
        oper_res = results[base + 1]
        if isinstance(name_res, dict):
            NAMES.update(name_res)
        if isinstance(oper_res, dict) and oper_res:
            keep = [
                idx
                for idx in sorted(oper_res, key=if_key)
                if not EXCLUDE_PORTS.match(NAMES.get(idx, f"if{idx}"))
            ]
            PORTS = keep
            OPER_STATE.clear()
            for idx in keep:
                OPER_STATE[idx] = oper_res[idx]
            PORTS_AT = now
            DOWN_AT = now
        else:
            PORTS_AT = 0.0
        UP_PORTS = {idx for idx in PORTS if OPER_STATE.get(idx) == "1"}
        counter_idx = set(UP_PORTS)
        values = {}
        if counter_idx:
            values, _ = await get_many_chunked(
                engine, auth, target, context, counter_oids(counter_idx)
            )
        sample_at = time.monotonic()
    else:
        res = results[base]
        if isinstance(res, Exception):
            values, ok = {}, False
        else:
            values, ok = res
        sample_at = time.monotonic()
        for idx in oper_idx:
            value = values.get(f"{COL['oper']}.{idx}")
            if value is not None:
                OPER_STATE[idx] = value
        if down_due:
            DOWN_AT = now if ok else 0.0
        UP_PORTS = {idx for idx in PORTS if OPER_STATE.get(idx) == "1"}

    rates = counter_rates(values, counter_idx, sample_at)
    for idx in list(COUNTERS):
        if idx not in UP_PORTS:
            del COUNTERS[idx]

    return system, port_rows(rates), dict(HEALTH_VALUES)


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


def build_payload(rt, cfg):
    try:
        if rt.get("target") is None:
            rt["target"] = rt["loop"].run_until_complete(
                UdpTransportTarget.create((cfg["host"], cfg["port"]), timeout=2, retries=1)
            )
        system, ports, health = rt["loop"].run_until_complete(
            collect(rt["engine"], rt["auth"], rt["context"], rt["target"])
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "ts": time.time()}

    ports.sort(key=lambda r: if_key(r["idx"]))
    return {
        "ok": True,
        "ts": time.time(),
        "host": cfg["host"],
        "system": {**system, "sysUpTime": fmt_uptime(system.get("sysUpTime"))},
        "health": health,
        "ports": ports,
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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    RT.update(
        {
            "loop": loop,
            "engine": SnmpEngine(),
            "auth": CommunityData(cfg["community"], mpModel=1),
            "context": ContextData(),
            "target": None,
        }
    )

    last_record = time.monotonic()
    last_prune = time.monotonic()
    while not STOP.is_set():
        start = time.monotonic()
        payload = build_payload(RT, cfg)
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
        engine = RT.get("engine")
        if engine is not None:
            try:
                engine.close_dispatcher()
            except Exception:
                pass
        loop = RT.get("loop")
        if loop is not None:
            try:
                loop.close()
            except Exception:
                pass
        server.server_close()


if __name__ == "__main__":
    main()
