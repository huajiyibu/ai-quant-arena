"""可靠性改进测试：AI 语义校验、单实例文件锁、数据库迁移"""
import sqlite3
from datetime import datetime

from aitrader.database import Database
from aitrader.engines.base import DecisionContext
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.lock import FileLock
from aitrader.models import AccountState, Decision, Position

from helpers import make_bars

D = datetime(2024, 1, 1)
NAMES = {"510300": "沪深300ETF"}


def _ctx(positions=None):
    return DecisionContext(
        D,
        AccountState(100_000, 100_000, positions=positions or {}),
        {"510300": make_bars([1.0] * 25)},
        NAMES,
    )


def _engine():
    return DeepSeekEngine("sk-test")


# ---------------------------------------------------------------------------
# AI 语义校验
# ---------------------------------------------------------------------------
def test_validate_ok():
    d = Decision("510300", "buy", amount=30_000, reason="x")
    out = _engine()._validate([d], _ctx())
    assert out[0].valid is True
    assert out[0].validation == "ok"


def test_validate_invalid_symbol():
    d = Decision("999999", "buy", amount=30_000, reason="幻觉")
    out = _engine()._validate([d], _ctx())
    assert out[0].valid is False
    assert out[0].validation == "invalid_symbol:999999"


def test_validate_invalid_amount_zero():
    d = Decision("510300", "buy", amount=0, reason="x")
    out = _engine()._validate([d], _ctx())
    assert out[0].valid is False
    assert "invalid_amount" in out[0].validation


def test_validate_invalid_amount_exceeds_assets():
    d = Decision("510300", "buy", amount=1e18, reason="x")
    out = _engine()._validate([d], _ctx())
    assert out[0].valid is False
    assert "invalid_amount" in out[0].validation


def test_validate_too_many_buy():
    ctx = _ctx()
    ds = [
        Decision("510300", "buy", amount=10_000, reason="a"),
        Decision("510300", "buy", amount=10_000, reason="b"),
        Decision("510300", "buy", amount=10_000, reason="c"),
    ]
    out = _engine()._validate(ds, ctx)
    # 前两个合法，第三个超限
    assert all(d.valid for d in out[:2])
    assert out[2].valid is False
    assert out[2].validation == "too_many_buy"


def test_validate_sell_without_position():
    d = Decision("510300", "sell", reason="x")
    out = _engine()._validate([d], _ctx())  # 空仓
    assert out[0].valid is False
    assert out[0].validation == "sell_without_position"


def test_validate_sell_with_position_ok():
    pos = {"510300": Position("510300", "沪深300ETF", 100, 4.0, 4.0)}
    d = Decision("510300", "sell", reason="x")
    out = _engine()._validate([d], _ctx(pos))
    assert out[0].valid is True


def test_validate_hold_with_invalid_symbol():
    d = Decision("999999", "hold", reason="x")
    out = _engine()._validate([d], _ctx())
    assert out[0].valid is False
    assert out[0].validation == "invalid_symbol:999999"


def test_validate_max_buy_count_configurable():
    ctx = _ctx()
    engine = DeepSeekEngine("sk-test", max_buy_count=1)
    ds = [
        Decision("510300", "buy", amount=10_000, reason="a"),
        Decision("510300", "buy", amount=10_000, reason="b"),
    ]
    out = engine._validate(ds, ctx)
    assert out[0].valid is True
    assert out[1].valid is False
    assert out[1].validation == "too_many_buy"


# ---------------------------------------------------------------------------
# 单实例文件锁
# ---------------------------------------------------------------------------
def test_file_lock_exclusive(tmp_path):
    lock_path = tmp_path / "run.lock"
    l1 = FileLock(lock_path)
    l2 = FileLock(lock_path)
    assert l1.acquire() is True
    assert l2.acquire() is False  # 第二个拿不到锁
    l1.release()
    assert l2.acquire() is True   # 释放后可拿到
    l2.release()


# ---------------------------------------------------------------------------
# 数据库迁移：旧库补 validation 列
# ---------------------------------------------------------------------------
def test_db_migration_adds_validation_column(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL, date TEXT NOT NULL,
            engine_type TEXT NOT NULL, symbol TEXT NOT NULL,
            action TEXT NOT NULL, amount REAL, reason TEXT,
            fallback INTEGER DEFAULT 0, prompt_json TEXT,
            raw_output_json TEXT, created_at TEXT NOT NULL)"""
    )
    conn.commit()
    conn.close()

    db = Database(db_path)  # 打开旧库应触发补列
    with db._connect() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(decisions)").fetchall()}
    assert "validation" in cols
