"""v0.5 预测性能地基测试：PP-1 成交假设/佣金、PP-3 system/温度/缓存键"""
import json
from datetime import datetime

import pytest

from aitrader.backtest import Backtester, compute_benchmark
from aitrader.config import RiskConfig, Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource
from aitrader.engines.base import DecisionContext, EngineResult
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.models import AccountState, Bar, Decision
from aitrader.portfolio import execute_decisions

from helpers import make_bars

D = datetime(2024, 1, 1)


def _settings(db_path):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )


class AlwaysBuyEngine:
    """每个决策日都尝试买入，便于验证成交价假设"""

    name = "alwaysbuy"

    def decide(self, ctx):
        return EngineResult(
            decisions=[Decision("510300", "buy", amount=50_000, reason="always")],
            prompt="",
            raw_output="",
        )


class OpenShiftDataSource(FakeDataSource):
    """open = close + 10，让 next_open 与 close 成交价明显可分"""

    def fetch_daily_bars(self, symbol, days, exchange="SH", end_date=None):
        bars = super().fetch_daily_bars(symbol, days, exchange, end_date)
        return [
            Bar(b.symbol, b.datetime, b.open + 10, b.high, b.low, b.close, b.volume)
            for b in bars
        ]


# ---------------- PP-1 成交假设 ----------------
def test_execute_decisions_fill_prices_used_for_fill():
    state = AccountState(100_000, 100_000)
    dec = Decision("510300", "buy", amount=30_000, reason="x")
    _, trades, _ = execute_decisions(
        state, [dec], {"510300": 4.0}, {"510300": "x"}, RiskConfig(), D,
        fill_prices={"510300": 4.5},
    )
    assert trades[0].price == 4.5  # 成交价用次日开盘 4.5，参考价 4.0 仍用于现价


def test_execute_decisions_default_fill_is_close():
    state = AccountState(100_000, 100_000)
    dec = Decision("510300", "buy", amount=30_000, reason="x")
    _, trades, _ = execute_decisions(
        state, [dec], {"510300": 4.0}, {"510300": "x"}, RiskConfig(), D
    )
    assert trades[0].price == 4.0  # 默认成交价 == 参考价（当日收盘，真实盘行为不变）


def test_backtester_next_open_uses_next_bar_open(tmp_path):
    closes = list(range(100, 200))
    ds = OpenShiftDataSource(closes, base_date=datetime(2023, 1, 1))
    db_path = tmp_path / "bt.db"
    db = Database(db_path)
    settings = _settings(db_path)
    start, end = datetime(2024, 1, 1), datetime(2024, 1, 31)

    r_close = Backtester(
        settings, db, ds, AlwaysBuyEngine(), "rule", start, end, fill_mode="close"
    ).run()
    r_next = Backtester(
        settings, db, ds, AlwaysBuyEngine(), "rule", start, end, fill_mode="next_open"
    ).run()

    p_close = r_close["trades"][0]["price"]
    p_next = r_next["trades"][0]["price"]
    assert p_close > 0 and p_next > 0
    # 下一根 close 比当日高 1（closes 递增），open = close+10 → 两者差 11
    assert p_next == pytest.approx(p_close + 11)


def test_benchmark_includes_commission():
    bars = [
        Bar("510300", datetime(2024, 1, 1), 10, 10, 10, 10, 1000),
        Bar("510300", datetime(2024, 1, 2), 10, 10, 10, 12, 1000),
    ]
    no_fee = compute_benchmark(bars, 100_000)
    with_fee = compute_benchmark(bars, 100_000, commission_rate=0.001)
    assert no_fee[0]["assets"] == 100_000
    assert with_fee[0]["assets"] == pytest.approx(100_000 * 0.999)
    assert no_fee[-1]["assets"] == pytest.approx(120_000)
    assert with_fee[-1]["assets"] == pytest.approx(120_000 * 0.999)


# ---------------- PP-3 system / 温度 / 缓存键 ----------------
class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class FakeHttp:
    def __init__(self, content):
        self.resp = FakeResponse(content)
        self.kw = {}

    def post(self, url, **kwargs):
        self.kw = kwargs
        return self.resp


def _ctx():
    bars = {"510300": make_bars([1.0] * 25)}
    return DecisionContext(D, AccountState(100_000, 100_000), bars, {"510300": "x"})


def test_system_prompt_three_sections():
    http = FakeHttp(json.dumps({"decisions": []}))
    engine = DeepSeekEngine("sk", http_client=http)
    engine.decide(_ctx())
    sys_msg = http.kw["json"]["messages"][0]["content"]
    assert "长期正期望" in sys_msg
    assert "决策框架" in sys_msg
    assert "输出契约" in sys_msg
    assert http.kw["json"]["temperature"] == 0.3  # 默认温度


def test_temperature_parameter_forwarded():
    http = FakeHttp(json.dumps({"decisions": []}))
    engine = DeepSeekEngine("sk", http_client=http, temperature=0.7)
    engine.decide(_ctx())
    assert http.kw["json"]["temperature"] == 0.7


def test_cache_key_includes_temperature_and_system():
    content = json.dumps({"decisions": []})
    cache: dict = {}
    e1 = DeepSeekEngine("sk", http_client=FakeHttp(content), temperature=0.3, response_cache=cache)
    e1.decide(_ctx())
    e2 = DeepSeekEngine("sk", http_client=FakeHttp(content), temperature=0.7, response_cache=cache)
    e2.decide(_ctx())
    assert len(cache) == 2  # 不同温度 → 不同缓存键，不误用旧缓存
    e3 = DeepSeekEngine(
        "sk", http_client=FakeHttp(content), temperature=0.3,
        system_prompt_extra="附加约束", response_cache=cache,
    )
    e3.decide(_ctx())
    assert len(cache) == 3  # 不同 system → 也不共享


def test_cache_shared_when_system_and_temperature_same():
    """ai 与 ai_policy 提示词相同时仍共享缓存（v0.3 行为保持）"""
    content = json.dumps({"decisions": []})
    cache: dict = {}
    e1 = DeepSeekEngine("sk", http_client=FakeHttp(content), temperature=0.3, response_cache=cache)
    e1.decide(_ctx())
    e2 = DeepSeekEngine(
        "sk", http_client=FakeHttp(content), temperature=0.3,
        include_policy=True, name="AI·政策版", response_cache=cache,
    )
    e2.decide(_ctx())
    assert len(cache) == 1  # prompt 相同且 system/temperature 相同 → 共享
