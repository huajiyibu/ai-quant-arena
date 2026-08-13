"""v0.20 测试：P0-1 trades 唯一约束(INSERT OR IGNORE 幂等) + P1-9 内存库单连接 + 迁移去重"""
from datetime import datetime

import pytest

from aitrader.database import Database
from aitrader.models import Trade

D = datetime(2024, 1, 1)


def _trade(sym="510300", action="buy", price=4.0):
    return Trade(D, sym, "沪深300ETF", action, price, 1000, price * 1000, "x")


def test_add_trade_idempotent_on_duplicate(tmp_path):
    """同 account/date/symbol/action 重复 add_trade 只保留一条（INSERT OR IGNORE + 唯一索引）"""
    db = Database(tmp_path / "t.db")
    acc = db.create_account("t", "rule", 100_000)
    db.add_trade(acc, _trade())
    db.add_trade(acc, _trade())  # 完全相同的成交（force/崩溃重跑场景）
    trades = db.get_trades(acc)
    assert len(trades) == 1


def test_memory_database_works():
    """P1-9：Database(':memory:') 应可用（schema 建在复用连接上，save_bars 不报 no such table）"""
    db = Database(":memory:")
    acc = db.create_account("t", "rule", 100_000)
    bar = type("B", (), {"symbol": "510300", "datetime": D, "open": 1.0,
                         "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1000})()
    db.save_bars([bar])  # 旧版会报 no such table: bars
    assert len(db.get_bars("510300")) == 1
    db.add_snapshot(acc, D, type("S", (), {"cash": 100000.0, "total_assets": 100000.0, "total_pnl": 0.0})())
    assert len(db.get_snapshots(acc)) == 1


def test_migration_dedups_trades(tmp_path):
    """迁移：已有重复 trades 的旧库 → 清理保留最早 + 建唯一索引后不报错"""
    db_path = tmp_path / "t.db"
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, "
        "date TEXT, symbol TEXT, name TEXT, action TEXT, price REAL, volume INTEGER, "
        "amount REAL, reason TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO trades(account_id,date,symbol,name,action,price,volume,amount,reason,created_at) VALUES "
        "(1,'2024-01-02','510300','x','buy',4.0,1000,4000,'旧',''),"
        "(1,'2024-01-02','510300','x','buy',4.1,2000,8200,'新','')"
    )
    conn.commit()
    conn.close()
    db = Database(db_path)  # 触发 _init_schema 迁移（清理重复 + 建唯一索引）
    rows = db.get_trades(1)
    assert len(rows) == 1
    assert rows[0]["reason"] == "旧"  # 保留最早
