"""配置模块：pydantic 模型 + .env 密钥 + config.json 业务配置。

- 业务配置（标的、资金、风控）存 config.json
- 密钥（DEEPSEEK_API_KEY）存 .env，两者分离，防止密钥入库/入仓/入日志
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

BASE_DIR: Path = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH: Path = BASE_DIR / "config.json"
DEFAULT_ENV_PATH: Path = BASE_DIR / ".env"
DEFAULT_DB_PATH: Path = BASE_DIR / "data" / "aitrader.db"


class SymbolConfig(BaseModel):
    """单个交易标的的配置"""

    name: str
    exchange: str = "SH"


class RiskConfig(BaseModel):
    """风控参数（写死上限，防止引擎乱来）"""

    max_position_pct: float = Field(default=0.3, gt=0, le=1)
    max_daily_buy_pct: float = Field(default=0.5, gt=0, le=1)
    commission_rate: float = Field(default=0.00025, ge=0, lt=0.01)
    min_confidence_buy: float = Field(default=0.0, ge=0, le=1)  # 买入最低置信度（0=关闭，PP-4）
    slippage_bps: float = Field(default=0.0, ge=0, le=100)  # 滑点（bps，买卖双边，P2-2）


class PolicyConfig(BaseModel):
    """宏观政策参考配置（FR12）"""

    enabled: bool = True
    max_items: int = Field(default=8, ge=1, le=30)
    keywords: list[str] = Field(default_factory=lambda: [
        "央行", "人民银行", "证监会", "金融监管", "降息", "降准", "利率",
        "LPR", "MLF", "逆回购", "财政", "国务院", "国常会", "发改委",
        "货币政策", "财政政策", "监管", "关税", "专项债", "IPO", "注册制",
        "政治局", "汇率", "外汇", "房地产",
    ])


class Settings(BaseModel):
    """全局配置"""

    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = Field(default=0.3, ge=0, le=2)
    system_prompt_extra: str = ""  # 追加到 DeepSeek system 的附加约束（PP-3）
    fill_mode: str = "close"  # 回测成交假设："close" 当日收盘 | "next_open" 次日开盘（PP-1）
    adjust: str = "none"  # 回测行情复权："none" 原始 | "hfq" 后复权（P2-1，默认走 config）
    feature_inject: bool = False  # 提示词注入技术特征（PP-2；建议配合 adjust=hfq 使用）
    market_env_inject: bool = False  # 提示词注入市场环境（B-3，探索性，默认关）
    initial_capital: float = Field(default=1_000_000, gt=0)
    lookback_days: int = Field(default=20, ge=5, le=120)
    max_buy_count: int = Field(default=2, ge=1, le=5)
    symbols: dict[str, SymbolConfig]
    risk: RiskConfig = RiskConfig()
    policy: PolicyConfig = PolicyConfig()
    db_path: Path = DEFAULT_DB_PATH
    api_key: str = ""

    @property
    def symbol_names(self) -> dict[str, str]:
        """symbol -> 中文名"""
        return {sym: cfg.name for sym, cfg in self.symbols.items()}


def load_settings(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    env_path: str | Path = DEFAULT_ENV_PATH,
) -> Settings:
    """加载配置：读取 config.json 合并 .env 中的密钥。

    Raises:
        FileNotFoundError: 配置文件不存在
        json.JSONDecodeError: 配置文件格式错误
    """
    load_dotenv(env_path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = json.load(f)

    # 密钥优先取环境变量，其次取配置文件（向后兼容）
    raw["api_key"] = os.getenv("DEEPSEEK_API_KEY", raw.get("api_key", ""))
    return Settings(**raw)
