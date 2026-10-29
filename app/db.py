"""SQLite 数据层：六张业务表 + 基础访问。

表设计（对齐方案第六章）：
- watchlist     自选即监控范围（持仓标的强制保留）
- signal        信号生命周期（待处理/已跟随/已忽略）
- trade         成交流水（唯一事实源，持仓与现金由此推导）
- account       账户单行表（现金余额）
- asset_snapshot 每日资产快照（现金/持仓市值/总资产）
- push_log      推送审计
"""
import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("QUANT_DB_PATH") or os.path.join("data", "quant.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT,
    quant_enabled INTEGER DEFAULT 1,
    strategies TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS signal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT,
    side TEXT NOT NULL,
    strategy TEXT NOT NULL,
    reason TEXT,
    ref_price REAL,
    suggest_shares INTEGER,
    status TEXT DEFAULT 'pending',   -- pending / followed / ignored
    created_at TEXT DEFAULT (datetime('now','localtime')),
    followed_at TEXT
);
CREATE TABLE IF NOT EXISTS trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    shares INTEGER NOT NULL,
    fee REAL DEFAULT 0,
    traded_at TEXT DEFAULT (datetime('now','localtime')),
    note TEXT
);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL DEFAULT 0,
    initial_cash REAL
);
CREATE TABLE IF NOT EXISTS asset_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_date TEXT NOT NULL,
    cash REAL,
    market_value REAL,
    total REAL,
    UNIQUE(snap_date)
);
CREATE TABLE IF NOT EXISTS push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT,
    title TEXT,
    content TEXT,
    status TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_signal_status ON signal(status);
CREATE INDEX IF NOT EXISTS idx_signal_code ON signal(code);
"""

# 信号质量结算列（ALTER TABLE 迁移添加）
_OUTCOME_COLS = ["ret_1d", "ret_5d", "ret_20d", "settled_at"]


@contextmanager
def db():
    """连接上下文：自动提交。"""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """建表（幂等）+ 旧库迁移（补列）。"""
    with db() as conn:
        conn.executescript(SCHEMA)
        # 旧库迁移：account 表补 initial_cash 列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(account)")}
        if "initial_cash" not in cols:
            conn.execute("ALTER TABLE account ADD COLUMN initial_cash REAL")
            # 由现有现金反推初始资金（cash = 初始 + 卖出净额 - 买入净额）
            conn.execute("""
                UPDATE account SET initial_cash = cash - (
                    SELECT COALESCE(SUM(CASE WHEN side='sell' THEN price*shares - fee ELSE 0 END), 0)
                         - COALESCE(SUM(CASE WHEN side='buy' THEN price*shares + fee ELSE 0 END), 0)
                    FROM trade
                ) WHERE id=1 AND initial_cash IS NULL
            """)
        # 旧库迁移：signal 表补信号质量结算列
        sig_cols = {r[1] for r in conn.execute("PRAGMA table_info(signal)")}
        for col in _OUTCOME_COLS:
            if col not in sig_cols:
                conn.execute(f"ALTER TABLE signal ADD COLUMN {col} REAL")


# ---------- watchlist ----------

def seed_watchlist(items: list[dict]) -> None:
    """首次启动从 config.yaml 预置自选（幂等：已存在的跳过）。"""
    with db() as conn:
        for it in items:
            conn.execute(
                "INSERT OR IGNORE INTO watchlist(code, name, quant_enabled, strategies) VALUES(?,?,?,?)",
                (it["code"], it.get("name", ""), int(it.get("quant_enabled", 1)),
                 json.dumps(it.get("strategies", []), ensure_ascii=False)),
            )


def list_watchlist() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM watchlist ORDER BY id").fetchall()


def add_watchlist(code: str, name: str, strategies: list[str], enabled: int = 1) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO watchlist(code, name, strategies, quant_enabled) VALUES(?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, strategies=excluded.strategies",
            (code, name, json.dumps(strategies, ensure_ascii=False), enabled),
        )


def update_watchlist(wid: int, **fields) -> None:
    """fields: quant_enabled / strategies / name"""
    with db() as conn:
        for k, v in fields.items():
            if k == "strategies":
                v = json.dumps(v, ensure_ascii=False)
            conn.execute(f"UPDATE watchlist SET {k}=? WHERE id=?", (v, wid))


def remove_watchlist(wid: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))


# ---------- signal ----------

def add_signal(code, name, side, strategy, reason, ref_price, suggest_shares) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO signal(code,name,side,strategy,reason,ref_price,suggest_shares) VALUES(?,?,?,?,?,?,?)",
            (code, name, side, strategy, reason, ref_price, suggest_shares),
        )
        return cur.lastrowid


def list_signals(status: str | None = None) -> list[sqlite3.Row]:
    with db() as conn:
        if status:
            return conn.execute("SELECT * FROM signal WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        return conn.execute("SELECT * FROM signal ORDER BY id DESC").fetchall()


def get_signal(sid: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM signal WHERE id=?", (sid,)).fetchone()


def duplicate_today(code: str, strategy: str) -> bool:
    """同一标的 × 同一策略当日是否已有信号（去重闸门）。

    包含「已忽略」的信号：用户当天忽略后不再重复推送（忽略即当日安静），
    避免同一触发条件在盘中轮询/尾盘扫描中被反复打扰。
    """
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM signal WHERE code=? AND strategy=? "
            "AND date(created_at)=date('now','localtime') LIMIT 1",
            (code, strategy),
        ).fetchone()
        return row is not None


def set_signal_status(sid: int, status: str) -> None:
    with db() as conn:
        conn.execute("UPDATE signal SET status=?, followed_at=datetime('now','localtime') WHERE id=?",
                     (status, sid))


def set_signal_outcomes(sid: int, ret_1d: float | None, ret_5d: float | None,
                        ret_20d: float | None, settled_at: str | None) -> None:
    with db() as conn:
        conn.execute("UPDATE signal SET ret_1d=?, ret_5d=?, ret_20d=?, settled_at=? WHERE id=?",
                     (ret_1d, ret_5d, ret_20d, settled_at, sid))


def unsettled_signals() -> list[sqlite3.Row]:
    """已到期但尚未结算质量的信号（settled_at 为空）。"""
    with db() as conn:
        return conn.execute("SELECT * FROM signal WHERE settled_at IS NULL").fetchall()


# ---------- trade / account ----------

def add_trade(signal_id: int | None, code: str, side: str, price: float, shares: int, fee: float, note: str = "") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO trade(signal_id,code,side,price,shares,fee,note) VALUES(?,?,?,?,?,?,?)",
            (signal_id, code, side, price, shares, fee, note),
        )


def get_cash() -> float:
    with db() as conn:
        row = conn.execute("SELECT cash FROM account WHERE id=1").fetchone()
        return float(row["cash"]) if row else 0.0


def set_cash(cash: float) -> None:
    with db() as conn:
        conn.execute("INSERT INTO account(id,cash) VALUES(1,?) "
                     "ON CONFLICT(id) DO UPDATE SET cash=excluded.cash", (cash,))


def init_cash(cash: float) -> None:
    """首次使用初始化现金与初始资金（幂等）。"""
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO account(id,cash,initial_cash) VALUES(1,?,?)", (cash, cash))


def get_initial_cash() -> float:
    with db() as conn:
        row = conn.execute("SELECT initial_cash FROM account WHERE id=1").fetchone()
        return float(row["initial_cash"]) if row and row["initial_cash"] is not None else 0.0


def list_trades() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM trade ORDER BY id DESC").fetchall()


def get_trade(tid: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM trade WHERE id=?", (tid,)).fetchone()


def update_trade(tid: int, price: float, shares: int, fee: float, note: str | None = None) -> None:
    with db() as conn:
        if note is None:
            conn.execute("UPDATE trade SET price=?, shares=?, fee=? WHERE id=?",
                         (price, shares, fee, tid))
        else:
            conn.execute("UPDATE trade SET price=?, shares=?, fee=?, note=? WHERE id=?",
                         (price, shares, fee, note, tid))


def last_trade_price(code: str) -> float | None:
    """某标的最近一次成交价（网格锚点用；从未成交返回 None）。"""
    with db() as conn:
        row = conn.execute("SELECT price FROM trade WHERE code=? ORDER BY id DESC LIMIT 1",
                           (code,)).fetchone()
        return float(row["price"]) if row else None


def save_snapshot(snap_date: str, cash: float, market_value: float, total: float) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO asset_snapshot(snap_date,cash,market_value,total) VALUES(?,?,?,?) "
            "ON CONFLICT(snap_date) DO UPDATE SET cash=excluded.cash, market_value=excluded.market_value, total=excluded.total",
            (snap_date, cash, market_value, total),
        )


def list_snapshots(limit: int = 90) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM asset_snapshot ORDER BY snap_date DESC LIMIT ?", (limit,)).fetchall()


# ---------- push_log ----------

def log_push(channel: str, title: str, content: str, status: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO push_log(channel,title,content,status) VALUES(?,?,?,?)",
                     (channel, title, content, status))