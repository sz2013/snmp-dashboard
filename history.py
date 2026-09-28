import os
import sqlite3
import threading

_LOCAL = threading.local()
DB_PATH = "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts       INTEGER NOT NULL,
    port_idx TEXT    NOT NULL,
    name     TEXT,
    oper     TEXT,
    rx_bps   REAL,
    tx_bps   REAL
);
CREATE INDEX IF NOT EXISTS ix_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS ix_samples_port_ts ON samples(port_idx, ts);
"""


def _conn():
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _LOCAL.conn = conn
    return conn


def init(path):
    global DB_PATH
    DB_PATH = path
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = _conn()
    conn.executescript(SCHEMA)
    conn.commit()


def record(ts, ports):
    rows = [
        (int(ts), str(p["idx"]), p.get("name"), p.get("oper"), p.get("rx_bps"), p.get("tx_bps"))
        for p in ports
        if p.get("oper") == "up"
    ]
    if not rows:
        return 0
    conn = _conn()
    conn.executemany(
        "INSERT INTO samples (ts, port_idx, name, oper, rx_bps, tx_bps) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def prune(cutoff):
    conn = _conn()
    conn.execute("DELETE FROM samples WHERE ts < ?", (int(cutoff),))
    conn.commit()


def last_name(port_idx):
    row = _conn().execute(
        "SELECT name FROM samples WHERE port_idx = ? ORDER BY ts DESC LIMIT 1",
        (str(port_idx),),
    ).fetchone()
    return row[0] if row else None


def query(port_idx, since, step=60):
    rows = _conn().execute(
        """
        SELECT (ts / ?) * ? AS bucket, AVG(rx_bps), AVG(tx_bps)
        FROM samples
        WHERE port_idx = ? AND ts >= ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        (step, step, str(port_idx), int(since)),
    ).fetchall()
    series = [
        {"t": int(bucket), "rx": round(rx or 0.0, 1), "tx": round(tx or 0.0, 1)}
        for bucket, rx, tx in rows
    ]
    return series
