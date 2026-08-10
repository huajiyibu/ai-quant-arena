"""v0.9 数据时效修复测试：政策去滞后/去前视（F-6/F-7）、行情时点硬校验（F-1/F-2）"""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from aitrader.batch import BatchRunner
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.datasource import AkSharePolicySource, FakeDataSource
from aitrader.engines.rule import RuleEngine


def _settings(db_path, symbols=None):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols=symbols or {"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )


# ---------- 政策过滤（F-6/F-7） ----------
def _policy_df():
    return pd.DataFrame(
        {
            "标题": ["昨日本央行", "今日早间央行", "今日盘后证监会", "今日盘中财政部"],
            "内容": ["旧闻", "早间消息", "盘后消息", "盘中消息"],
            "发布日期": [date(2026, 8, 7), date(2026, 8, 10), date(2026, 8, 10), date(2026, 8, 10)],
            "发布时间": ["09:00:00", "09:30:00", "16:05:00", "14:30:00"],
        }
    )


def test_policy_filters_date_and_cutoff(monkeypatch):
    """只取决策日当天、发布时间<=15:30 的消息（去滞后+去前视）"""
    import akshare as ak

    monkeypatch.setattr(ak, "stock_info_global_cls", lambda symbol: _policy_df())
    src = AkSharePolicySource()
    news = src.fetch_macro_news(
        ["央行", "证监会", "财政部"], 8, decision_date=date(2026, 8, 10), cutoff_time="15:30"
    )
    texts = "".join(news)
    assert "昨日" not in texts  # 非决策日被滤掉（治滞后）
    assert "盘后" not in texts  # 16:05 盘后消息被滤掉（治前视）
    assert "早间" in texts and "盘中" in texts  # 当日 <=15:30 的保留


def test_policy_latest_first(monkeypatch):
    """同日内倒序：最新匹配优先"""
    import akshare as ak

    monkeypatch.setattr(ak, "stock_info_global_cls", lambda symbol: _policy_df())
    src = AkSharePolicySource()
    news = src.fetch_macro_news(
        ["财政部", "央行"], 1, decision_date=date(2026, 8, 10), cutoff_time="15:30"
    )
    assert "盘中" in news[0]  # max_items=1 → 取最新匹配（14:30 财政部）


# ---------- 行情时点硬校验（F-1/F-2） ----------
def test_batch_rejects_non_decision_day_data(tmp_path):
    """数据最新早于决策日（实时失败滞后场景）→ 当日跳过 + _warning"""

    class LaggedDataSource:
        name = "lagged"
        calendar_ok = True

        def __init__(self):
            self.inner = FakeDataSource([1.0] * 25, base_date=datetime(2024, 1, 1))

        def fetch_daily_bars(self, symbol, days, exchange="SH", end_date=None, adjust="none"):
            if end_date is not None:
                end_date = end_date - timedelta(days=1)  # 模拟实时补全失败，滞后 1 天
            return self.inner.fetch_daily_bars(symbol, days, exchange, end_date, adjust)

        def is_trading_day(self, date):
            return True

    db = Database(tmp_path / "t.db")
    runner = BatchRunner(_settings(tmp_path / "t.db"), db, LaggedDataSource(), {"rule": RuleEngine()})
    results = runner.run(datetime(2024, 1, 26))
    assert "_warning" in results
    assert "stale_bars" in results["_warning"]  # 全量非决策日 → 跳过


def test_batch_excludes_only_stale_symbol(tmp_path):
    """部分标的非决策日 → 剔除该标的，其余正常交易（不整日跳过）"""

    class MixedDataSource:
        name = "mixed"
        calendar_ok = True

        def __init__(self):
            self.inner = FakeDataSource([1.0] * 25, base_date=datetime(2024, 1, 1))

        def fetch_daily_bars(self, symbol, days, exchange="SH", end_date=None, adjust="none"):
            if symbol == "510300":
                return self.inner.fetch_daily_bars(symbol, days, exchange, end_date, adjust)
            return self.inner.fetch_daily_bars(
                symbol, days, exchange, end_date - timedelta(days=1), adjust
            )

        def is_trading_day(self, date):
            return True

    db = Database(tmp_path / "t.db")
    settings = _settings(
        tmp_path / "t.db",
        symbols={
            "510300": {"name": "x", "exchange": "SH"},
            "588000": {"name": "y", "exchange": "SH"},
        },
    )
    runner = BatchRunner(settings, db, MixedDataSource(), {"rule": RuleEngine()})
    results = runner.run(datetime(2024, 1, 26))
    assert "_warning" not in results  # 部分陈旧 → 不整日跳过
    assert "rule" in results
    acc = db.get_account_by_engine("rule")
    assert db.get_snapshot(acc["id"], datetime(2024, 1, 26)) is not None
