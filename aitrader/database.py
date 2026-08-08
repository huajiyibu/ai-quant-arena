"""数据持久化层：SQLite 仓储。

职责：账户、账本状态、成交流水、决策留痕、每日净值快照、行情缓存。
所有写操作集中在此层，保证状态一致性（对应 HLD §2 database 模块）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import AccountState, Bar, Decision, EngineType, Position, Trade

_DT_FMT = "%Y-%m-%d %H:%M:%S"
_DATE_FMT = "%Y-%m-%d"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    engine_type     TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_states (
    account_id INTEGER PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    date       TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    name       TEXT NOT NULL,
    action     TEXT NOT NULL,
    price      REAL NOT NULL,
    volume     INTEGER NOT NULL,
    amount     REAL NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL,
    date            TEXT NOT NULL,
    engine_type     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,
    amount          REAL,
    reason          TEXT,
    fallback        INTEGER DEFAULT 0,
    validation      TEXT,
    prompt_json     TEXT,
    raw_output_json TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL,
    date         TEXT NOT NULL,
    cash         REAL NOT NULL,
    total_assets REAL NOT NULL,
    pnl          REAL NOT NULL,
    UNIQUE(account_id, date)
);
CREATE TABLE IF NOT EXISTS bars (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume REAL,
    UNIQUE(symbol, date)
);
"""


# ---------------------------------------------------------------------------
# AccountState <-> dict 序列化
# ---------------------------------------------------------------------------
def state_to_dict(state: AccountState) -> dict:
    return {
        "initial_capital": state.initial_capital,
        "cash": state.cash,
        "positions": {
            sym: {
                "name": p.name,
                "volume": p.volume,
                "cost_price": p.cost_price,
                "last_price": p.last_price,
            }
            for sym, p in state.positions.items()
        },
    }


def state_from_dict(data: dict) -> AccountState:
    positions = {
        sym: Position(
            symbol=sym,
            name=p["name"],
            volume=int(p["volume"]),
            cost_price=float(p["cost_price"]),
            last_price=float(p["last_price"]),
        )
        for sym, p in data.get("positions", {}).items()
    }
    return AccountState(
        initial_capital=float(data["initial_capital"]),
        cash=float(data["cash"]),
        positions=positions,
    )


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class Database:
    """SQLite 仓储（非线程安全，批处理单线程使用）"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # 迁移：旧库补齐 decisions.validation 列
            cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
            if "validation" not in cols:
                conn.execute("ALTER TABLE decisions ADD COLUMN validation TEXT")

    # ---- accounts ----
    def create_account(self, name: str, engine_type: EngineType, initial_capital: float) -> int:
        now = datetime.now().strftime(_DT_FMT)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO accounts(name, engine_type, initial_capital, created_at) VALUES(?,?,?,?)",
                (name, engine_type, initial_capital, now),
            )
            account_id = int(cur.lastrowid)
        # 初始化空账本
        self.save_state(account_id, AccountState(initial_capital=initial_capital, cash=initial_capital))
        return account_id

    def get_accounts(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, engine_type, initial_capital FROM accounts ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_account(self, account_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, engine_type, initial_capital FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_account_by_engine(self, engine_type: str) -> dict | None:
        """按引擎类型查询账户（多引擎各一个账户）"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, engine_type, initial_capital FROM accounts WHERE engine_type=?",
                (engine_type,),
            ).fetchone()
        return dict(row) if row else None

    # ---- account state ----
    def save_state(self, account_id: int, state: AccountState) -> None:
        now = datetime.now().strftime(_DT_FMT)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO account_states(account_id, state_json, updated_at) VALUES(?,?,?)
                   ON CONFLICT(account_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (account_id, json.dumps(state_to_dict(state), ensure_ascii=False), now),
            )

    def load_state(self, account_id: int) -> AccountState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM account_states WHERE account_id=?", (account_id,)
            ).fetchone()
        if not row:
            return None
        return state_from_dict(json.loads(row["state_json"]))

    # ---- trades ----
    def add_trade(self, account_id: int, trade: Trade) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO trades(account_id, date, symbol, name, action, price, volume, amount, reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    account_id,
                    trade.date.strftime(_DATE_FMT),
                    trade.symbol,
                    trade.name,
                    trade.action,
                    trade.price,
                    trade.volume,
                    trade.amount,
                    trade.reason,
                    datetime.now().strftime(_DT_FMT),
                ),
            )

    def get_trades(self, account_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE account_id=? ORDER BY date", (account_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- decisions ----
    def add_decision(
        self,
        account_id: int,
        date: datetime,
        engine_type: str,
        decision: Decision,
        prompt: str = "",
        raw_output: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO decisions(account_id, date, engine_type, symbol, action, amount, reason, fallback,
                                         validation, prompt_json, raw_output_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    account_id,
                    date.strftime(_DATE_FMT),
                    engine_type,
                    decision.symbol,
                    decision.action,
                    decision.amount,
                    decision.reason,
                    1 if decision.fallback else 0,
                    decision.validation or None,
                    prompt or None,
                    raw_output or None,
                    datetime.now().strftime(_DT_FMT),
                ),
            )

    def get_decisions(self, account_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE account_id=? ORDER BY date", (account_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- snapshots ----
    def add_snapshot(self, account_id: int, date: datetime, state: AccountState) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO daily_snapshots(account_id, date, cash, total_assets, pnl) VALUES(?,?,?,?,?)
                   ON CONFLICT(account_id, date) DO UPDATE SET
                     cash=excluded.cash, total_assets=excluded.total_assets, pnl=excluded.pnl""",
                (
                    account_id,
                    date.strftime(_DATE_FMT),
                    state.cash,
                    state.total_assets,
                    state.total_pnl,
                ),
            )

    def get_snapshots(self, account_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date, cash, total_assets, pnl FROM daily_snapshots WHERE account_id=? ORDER BY date",
                (account_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def reset_account(self, account_id: int) -> None:
        """清空该账户成交/决策/快照并重置账本为初始资金（回测重新开始用）"""
        row = self.get_account(account_id)
        initial = row["initial_capital"] if row else 0.0
        with self._connect() as conn:
            conn.execute("DELETE FROM trades WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM decisions WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM daily_snapshots WHERE account_id=?", (account_id,))
        self.save_state(
            account_id, AccountState(initial_capital=initial, cash=initial)
        )

    def has_snapshot(self, account_id: int, date: datetime) -> bool:
        """该账户在指定日期是否已有净值快照（同日幂等判断用）"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM daily_snapshots WHERE account_id=? AND date=?",
                (account_id, date.strftime(_DATE_FMT)),
            ).fetchone()
        return row is not None

    def get_snapshot(self, account_id: int, date: datetime) -> dict | None:
        """查询指定日期的净值快照"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT date, cash, total_assets, pnl FROM daily_snapshots WHERE account_id=? AND date=?",
                (account_id, date.strftime(_DATE_FMT)),
            ).fetchone()
        return dict(row) if row else None

    # ---- bars ----
    def save_bars(self, bars: list[Bar]) -> None:
        with self._connect() as conn:
            for bar in bars:
                conn.execute(
                    """INSERT INTO bars(symbol, date, open, high, low, close, volume) VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(symbol, date) DO UPDATE SET
                         open=excluded.open, high=excluded.high, low=excluded.low,
                         close=excluded.close, volume=excluded.volume""",
                    (
                        bar.symbol,
                        bar.datetime.strftime(_DATE_FMT),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                    ),
                )

    def get_bars(self, symbol: str, limit: int = 120) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bars WHERE symbol=? ORDER BY date DESC LIMIT ?", (symbol, limit)
            ).fetchall()
        return [dict(r) for r in rows]
