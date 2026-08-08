"""DeepSeek 引擎测试（mock HTTP，不依赖网络）"""
import json
from datetime import datetime

import pytest

from aitrader.engines.base import DecisionContext
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.models import AccountState

from helpers import make_bars

D = datetime(2024, 1, 1)


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


class FakeHttp:
    def __init__(self, content: str) -> None:
        self.resp = FakeResponse(content)
        self.kw: dict = {}

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.url = url
        self.kw = kwargs
        return self.resp


def _ctx():
    bars = {"510300": make_bars([1.0] * 25)}
    return DecisionContext(D, AccountState(100_000, 100_000), bars, {"510300": "x"})


def test_parse_valid_decisions():
    content = json.dumps(
        {"decisions": [{"symbol": "510300", "action": "buy", "amount": 50000, "reason": "ok"}]}
    )
    http = FakeHttp(content)
    engine = DeepSeekEngine("sk-test", http_client=http)
    result = engine.decide(_ctx())

    assert len(result.decisions) == 1
    assert result.decisions[0].action == "buy"
    assert result.decisions[0].amount == 50000
    # 密钥正确传递
    assert http.kw["headers"]["Authorization"] == "Bearer sk-test"
    # 留痕数据完整
    assert "510300" in result.prompt
    assert result.raw_output == content


def test_parse_invalid_json_raises():
    engine = DeepSeekEngine("sk-test", http_client=FakeHttp("这不是JSON"))
    with pytest.raises(Exception):
        engine.decide(_ctx())


def test_parse_unknown_action_sanitized_to_hold():
    content = json.dumps(
        {"decisions": [{"symbol": "510300", "action": "梭哈", "reason": "x"}]}
    )
    engine = DeepSeekEngine("sk-test", http_client=FakeHttp(content))
    result = engine.decide(_ctx())
    assert result.decisions[0].action == "hold"


def test_policy_injected_when_enabled():
    """include_policy=True 时政策文本进入提示词"""
    http = FakeHttp(json.dumps({"decisions": []}))
    engine = DeepSeekEngine("sk-test", http_client=http, include_policy=True)
    ctx = DecisionContext(
        D, AccountState(100_000, 100_000),
        {"510300": make_bars([1.0] * 25)}, {"510300": "x"},
        policy_text="央行宣布降息",
    )
    engine.decide(ctx)
    assert "央行宣布降息" in http.kw["json"]["messages"][1]["content"]


def test_policy_not_injected_when_disabled():
    """include_policy=False（纯价格引擎）不注入政策"""
    http = FakeHttp(json.dumps({"decisions": []}))
    engine = DeepSeekEngine("sk-test", http_client=http, include_policy=False)
    ctx = DecisionContext(
        D, AccountState(100_000, 100_000),
        {"510300": make_bars([1.0] * 25)}, {"510300": "x"},
        policy_text="央行宣布降息",
    )
    engine.decide(ctx)
    assert "央行宣布降息" not in http.kw["json"]["messages"][1]["content"]
