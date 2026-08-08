"""配置模块测试"""
import json

import pytest
from pydantic import ValidationError

from aitrader.config import Settings, load_settings


def _write_config(path, **extra):
    cfg = {
        "base_url": "https://x",
        "model": "m",
        "initial_capital": 50000,
        "lookback_days": 20,
        "symbols": {"510300": {"name": "沪深300ETF", "exchange": "SH"}},
    }
    cfg.update(extra)
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def test_load_valid(tmp_path):
    p = tmp_path / "config.json"
    _write_config(p)
    s = load_settings(p, tmp_path / ".env")
    assert s.initial_capital == 50000
    assert s.symbol_names["510300"] == "沪深300ETF"
    assert s.risk.max_position_pct == 0.3


def test_key_from_env(tmp_path):
    p = tmp_path / "config.json"
    _write_config(p)
    e = tmp_path / ".env"
    e.write_text("DEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    assert load_settings(p, e).api_key == "sk-test"


def test_key_default_empty(tmp_path):
    p = tmp_path / "config.json"
    _write_config(p)
    assert load_settings(p, tmp_path / ".env").api_key == ""


def test_key_env_priority_over_config(tmp_path):
    """密钥优先取 .env，其次取 config.json（向后兼容）"""
    p = tmp_path / "config.json"
    _write_config(p, api_key="sk-from-json")
    e = tmp_path / ".env"
    e.write_text("DEEPSEEK_API_KEY=sk-from-env\n", encoding="utf-8")
    assert load_settings(p, e).api_key == "sk-from-env"


def test_invalid_position_pct_rejected():
    with pytest.raises(ValidationError):
        Settings(
            initial_capital=100000,
            symbols={"a": {"name": "x"}},
            risk={"max_position_pct": 1.5},
        )


def test_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.json", tmp_path / ".env")
