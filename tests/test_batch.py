"""批处理集成测试：临时 SQLite + 假数据源，验证完整链路"""
from datetime import datetime

from aitrader.batch import BatchRunner
from aitrader.config import Settings
from aitrader.database import Database
from aitrader.datasource import FakeDataSource, FakePolicySource
from aitrader.engines.base import DecisionEngine, EngineResult
from aitrader.engines.rule import RuleEngine
from aitrader.models import Decision

D = datetime(2024, 1, 1)


def _make_runner(tmp_path, closes, engines=None, symbols=None, policy_source=None):
    db_path = tmp_path / "t.db"
    settings = Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols=symbols or {"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )
    db = Database(db_path)
    ds = FakeDataSource(closes)
    engines = engines or {"rule": RuleEngine()}
    return BatchRunner(settings, db, ds, engines, policy_source=policy_source), db


def test_rule_engine_creates_account_and_snapshot(tmp_path):
    runner, db = _make_runner(tmp_path, [1.0] * 25)
    results = runner.run(D)
    assert "rule" in results
    acc = db.get_account_by_engine("rule")
    assert acc is not None
    assert len(db.get_snapshots(acc["id"])) == 1


def test_uptrend_triggers_buy_and_records_trade(tmp_path):
    closes = list(range(100, 125))
    runner, db = _make_runner(tmp_path, closes)
    results = runner.run(D)
    acc = db.get_account_by_engine("rule")
    trades = db.get_trades(acc["id"])
    assert len(trades) == 1
    assert trades[0]["action"] == "buy"
    # 决策留痕
    decisions = db.get_decisions(acc["id"])
    assert any(d["action"] == "buy" for d in decisions)


def test_ai_engine_failure_falls_back(tmp_path):
    class ThrowingEngine(DecisionEngine):
        name = "ai"

        def decide(self, ctx):
            raise RuntimeError("boom")

    runner, db = _make_runner(tmp_path, list(range(100, 125)), engines={"ai": ThrowingEngine()})
    results = runner.run(D)
    acc = db.get_account_by_engine("ai")
    decisions = db.get_decisions(acc["id"])
    # 降级：留一条 fallback 决策
    assert len(decisions) == 1
    assert decisions[0]["fallback"] == 1
    # 流程不中断：快照正常生成
    assert len(db.get_snapshots(acc["id"])) == 1


def test_dual_engines_run_independently(tmp_path):
    """多引擎并行，两个独立账户"""
    closes = list(range(100, 125))
    engines = {"rule": RuleEngine(), "ai": RuleEngine()}
    runner, db = _make_runner(tmp_path, closes, engines=engines)
    results = runner.run(D)
    assert set(results) == {"rule", "ai"}
    acc_rule = db.get_account_by_engine("rule")
    acc_ai = db.get_account_by_engine("ai")
    assert acc_rule["id"] != acc_ai["id"]
    assert len(db.get_snapshots(acc_rule["id"])) == 1
    assert len(db.get_snapshots(acc_ai["id"])) == 1


class RecordingEngine(DecisionEngine):
    """记录 ctx.policy_text 的假引擎，用于验证政策注入链路"""

    name = "ai_policy"
    include_policy = True

    def __init__(self) -> None:
        self.seen_policy: str = ""

    def decide(self, ctx):
        self.seen_policy = ctx.policy_text
        return EngineResult()


def test_policy_text_passed_to_policy_engine(tmp_path):
    """policy 文本只注入 include_policy=True 的引擎，且被关键词过滤"""
    engines = {"ai_policy": RecordingEngine()}
    runner, db = _make_runner(
        tmp_path, list(range(100, 125)), engines=engines,
        policy_source=FakePolicySource(["央行宣布降息", "某公司发布季度财报"]),
    )
    runner.run(D)
    assert "央行宣布降息" in engines["ai_policy"].seen_policy
    assert "季度财报" not in engines["ai_policy"].seen_policy


def test_three_engines_independent_accounts(tmp_path):
    """三方引擎（rule/ai/ai_policy）各有独立账户"""
    engines = {
        "rule": RuleEngine(),
        "ai": RuleEngine(),
        "ai_policy": RuleEngine(),
    }
    runner, db = _make_runner(tmp_path, list(range(100, 125)), engines=engines)
    results = runner.run(D)
    assert set(results) == {"rule", "ai", "ai_policy"}
    ids = {e: db.get_account_by_engine(e)["id"] for e in ("rule", "ai", "ai_policy")}
    assert len(set(ids.values())) == 3


def test_replay_respects_end_date(tmp_path):
    """回放无前视：数据源返回截至回放日的最近 K 线，最后一根即回放日，不含未来"""
    ds = FakeDataSource([1.0] * 30, base_date=datetime(2024, 1, 1))
    bars = ds.fetch_daily_bars("510300", 20, end_date=datetime(2024, 1, 10))
    assert len(bars) == 20
    assert bars[-1].datetime == datetime(2024, 1, 10)
    assert bars[0].datetime == datetime(2023, 12, 22)


def test_non_trading_day_skips_batch(tmp_path):
    """非交易日：跳过批处理，不产生账户/快照（修复周末节假日误跑）"""
    ds = FakeDataSource(list(range(100, 125)), trading_days={"2024-01-02"})
    db_path = tmp_path / "t.db"
    settings = Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )
    db = Database(db_path)
    runner = BatchRunner(settings, db, ds, {"rule": RuleEngine()})
    results = runner.run(datetime(2024, 1, 1))  # 2024-01-01 不在 trading_days 内
    assert results == {}
    assert db.get_account_by_engine("rule") is None


class AlwaysBuyEngine(DecisionEngine):
    """每次决策都请求买入，用于验证幂等与 --force 行为"""

    name = "ai"

    def decide(self, ctx):
        # 金额需 ≥ 1 手市值（510300 约 124 元/股 × 100 股 = 12400 元），且 ≤ 单笔上限 30%
        return EngineResult(
            decisions=[Decision(symbol="510300", action="buy", amount=30000, reason="always")]
        )


def _make_always_buy_runner(tmp_path):
    db_path = tmp_path / "t.db"
    settings = Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        db_path=db_path,
    )
    db = Database(db_path)
    engine = AlwaysBuyEngine()
    runner = BatchRunner(settings, db, FakeDataSource(list(range(100, 125))), {"ai": engine})
    return runner, db


def test_same_day_run_is_idempotent(tmp_path):
    """同日跑两次：默认幂等跳过，不产生重复成交/重复决策"""
    runner, db = _make_always_buy_runner(tmp_path)
    first = runner.run(D)
    second = runner.run(D)
    acc = db.get_account_by_engine("ai")
    assert first["ai"]["trades"] == 1
    assert second["ai"]["skipped"] is True
    assert len(db.get_trades(acc["id"])) == 1
    assert len(db.get_decisions(acc["id"])) == 1


def test_force_reruns_same_day(tmp_path):
    """--force：跳过幂等检查，同日可重跑；决策留痕幂等（唯一约束+INSERT OR IGNORE，不重复留痕）"""
    runner, db = _make_always_buy_runner(tmp_path)
    runner.run(D)
    runner.run(D, force=True)
    acc = db.get_account_by_engine("ai")
    # 第二次 force 重新决策但留痕幂等（decisions 唯一约束）；已持仓，买入被风控拒绝（成交仍 1 笔）
    assert len(db.get_decisions(acc["id"])) == 1
    assert len(db.get_trades(acc["id"])) == 1
