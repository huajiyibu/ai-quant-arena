"""v0.6 置信度门槛（PP-4）测试：解析/校验/执行门槛/落库/Rank IC 评测"""
import json
from datetime import datetime

import pytest

from aitrader.backtest import rank_ic
from aitrader.config import RiskConfig
from aitrader.database import Database
from aitrader.engines.base import DecisionContext
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.models import AccountState, Decision
from aitrader.portfolio import execute_decisions

from helpers import make_bars

D = datetime(2024, 1, 1)


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


def _engine(content, **kw):
    return DeepSeekEngine("sk", http_client=FakeHttp(content), **kw)


# ---------- 解析 ----------
def test_confidence_parsed_from_json():
    content = json.dumps(
        {"decisions": [{"symbol": "510300", "action": "buy", "amount": 50000, "confidence": 0.7, "reason": "x"}]}
    )
    res = _engine(content).decide(_ctx())
    assert res.decisions[0].confidence == pytest.approx(0.7)


def test_confidence_default_when_missing():
    content = json.dumps(
        {"decisions": [{"symbol": "510300", "action": "buy", "amount": 50000, "reason": "x"}]}
    )
    res = _engine(content).decide(_ctx())
    assert res.decisions[0].confidence == pytest.approx(0.5)


def test_confidence_out_of_range_invalid():
    content = json.dumps(
        {"decisions": [{"symbol": "510300", "action": "buy", "amount": 50000, "confidence": 1.5, "reason": "x"}]}
    )
    res = _engine(content).decide(_ctx())
    d = res.decisions[0]
    assert d.valid is False
    assert "invalid_confidence" in d.validation


def test_prompt_mentions_confidence():
    content = json.dumps({"decisions": []})
    engine = _engine(content)
    engine.decide(_ctx())
    prompt = engine._build_prompt(_ctx())
    assert "confidence" in prompt
    assert "正收益的信心" in prompt


# ---------- 执行门槛 ----------
def test_min_confidence_rejects_low_confidence_buy():
    state = AccountState(100_000, 100_000)
    risk = RiskConfig(min_confidence_buy=0.6)
    dec = Decision("510300", "buy", amount=30_000, reason="x", confidence=0.4)
    new_state, trades, results = execute_decisions(
        state, [dec], {"510300": 4.0}, {"510300": "x"}, risk, D
    )
    assert len(trades) == 0
    assert results["510300"].startswith("risk_rejected:low_confidence")


def test_min_confidence_zero_disabled_by_default():
    state = AccountState(100_000, 100_000)
    risk = RiskConfig()  # 默认 0.0，关闭门槛（向后兼容）
    dec = Decision("510300", "buy", amount=30_000, reason="x", confidence=0.4)
    _, trades, results = execute_decisions(
        state, [dec], {"510300": 4.0}, {"510300": "x"}, risk, D
    )
    assert len(trades) == 1
    assert results["510300"].startswith("executed:buy")


def test_high_confidence_passes_threshold():
    state = AccountState(100_000, 100_000)
    risk = RiskConfig(min_confidence_buy=0.6)
    dec = Decision("510300", "buy", amount=30_000, reason="x", confidence=0.8)
    _, trades, results = execute_decisions(
        state, [dec], {"510300": 4.0}, {"510300": "x"}, risk, D
    )
    assert len(trades) == 1
    assert results["510300"].startswith("executed:buy")


# ---------- 落库 ----------
def test_confidence_persisted_for_all_actions(tmp_path):
    """体检P1-2：hold/sell 也存置信度（扩大校准样本，成本≈0）"""
    db = Database(tmp_path / "t.db")
    account_id = db.create_account("t", "ai", 100_000)
    db.add_decision(
        account_id, D, "ai", Decision("510300", "buy", amount=50000, reason="x", confidence=0.8)
    )
    db.add_decision(account_id, D, "ai", Decision("510300", "sell", reason="y", confidence=0.9))
    rows = db.get_decisions(account_id)
    by_action = {r["action"]: r for r in rows}
    assert by_action["buy"]["confidence"] == pytest.approx(0.8)
    assert by_action["sell"]["confidence"] == pytest.approx(0.9)  # 不再丢弃 sell/hold 的置信度


# ---------- Rank IC ----------
def test_rank_ic_positive_monotonic():
    conf = [0.5, 0.6, 0.7, 0.8, 0.9]
    ret = [0.01, 0.02, 0.03, 0.04, 0.05]
    assert rank_ic(conf, ret) == pytest.approx(1.0)


def test_rank_ic_negative():
    conf = [0.5, 0.6, 0.7, 0.8, 0.9]
    ret = [0.05, 0.04, 0.03, 0.02, 0.01]
    assert rank_ic(conf, ret) == pytest.approx(-1.0)


def test_rank_ic_zero_for_constant_or_invalid():
    assert rank_ic([0.5] * 5, [0.1, 0.2, 0.3, 0.4, 0.5]) == 0.0  # 常数
    assert rank_ic([], []) == 0.0  # 空
    assert rank_ic([0.5, 0.6], [0.1]) == 0.0  # 长度不等
