"""v0.10 测试：真实盘接入最优配置（A-1）+ 未来日期拒绝（A-4）"""
import argparse
from datetime import datetime

import pytest

from aitrader.config import Settings

from run import _date_type, build_engines


def _settings(db_path, **kw):
    base = dict(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "x", "exchange": "SH"}},
        db_path=db_path,
    )
    base.update(kw)
    return Settings(**base)


def test_date_type_rejects_future():
    with pytest.raises(argparse.ArgumentTypeError):
        _date_type("2030-01-01")


def test_date_type_accepts_past_and_today():
    assert _date_type("2026-08-01") == datetime(2026, 8, 1)
    assert _date_type(datetime.now().strftime("%Y-%m-%d")).date() == datetime.now().date()


def test_daily_engines_use_feature_inject_when_enabled():
    """批处理路径：build_engines 把 settings.feature_inject 传给引擎（A-1）"""
    settings = _settings("t.db", api_key="sk-test", feature_inject=True)
    engines = build_engines(settings)
    assert engines["ai"].feature_inject is True
    assert engines["ai_policy"].feature_inject is True


def test_daily_engines_feature_inject_off_by_default():
    settings = _settings("t.db", api_key="sk-test")  # 默认 False
    engines = build_engines(settings)
    assert engines["ai"].feature_inject is False
