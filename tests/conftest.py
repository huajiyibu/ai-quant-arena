"""pytest 公共夹具"""
import pytest

from aitrader.config import RiskConfig, Settings
from aitrader.database import Database
from aitrader.models import AccountState


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def settings(db_path):
    return Settings(
        initial_capital=100_000,
        lookback_days=20,
        symbols={"510300": {"name": "沪深300ETF", "exchange": "SH"}},
        risk=RiskConfig(max_position_pct=0.3, max_daily_buy_pct=0.5, commission_rate=0.00025),
        db_path=db_path,
    )


@pytest.fixture
def db(db_path):
    return Database(db_path)


@pytest.fixture
def empty_state():
    return AccountState(initial_capital=100_000, cash=100_000)


@pytest.fixture(autouse=True)
def _clean_deepseek_env(monkeypatch):
    """清理 DEEPSEEK_API_KEY 环境变量残留，保证配置测试相互隔离"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
