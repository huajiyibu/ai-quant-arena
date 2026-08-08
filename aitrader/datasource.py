"""行情数据源：抽象接口 + akshare 实现。

设计要点（对应 HLD §2 datasource）：统一接口可注入，测试用 Fake 替代网络数据源。
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Protocol

from .models import Bar

logger = logging.getLogger(__name__)


class DataSource(Protocol):
    """行情数据源协议"""

    name: str

    def fetch_daily_bars(
        self, symbol: str, days: int, exchange: str = "SH", end_date: datetime | None = None
    ) -> list[Bar]:
        """获取某标的最近 days 根日线（升序）；end_date 指定时仅返回截至该日的 K 线"""
        ...

    def is_trading_day(self, date: datetime) -> bool:
        """判断某日是否为 A 股交易日（非交易日不产生快照）"""
        ...


class AkShareDataSource:
    """基于 akshare（新浪财经接口）的实现，免费、无需注册"""

    name = "akshare"

    _EXCHANGE_PREFIX: dict[str, str] = {"SH": "sh", "SZ": "sz"}

    def fetch_daily_bars(
        self,
        symbol: str,
        days: int,
        exchange: str = "SH",
        end_date: datetime | None = None,
        adjust: str = "none",
    ) -> list[Bar]:
        from .util import retry_call

        import akshare as ak  # 延迟导入，便于 mock

        prefix = self._EXCHANGE_PREFIX.get(exchange.upper(), "sh")
        df = retry_call(
            lambda: ak.fund_etf_hist_sina(symbol=f"{prefix}{symbol}"),
            label=f"行情{symbol}",
        )
        if df is None or df.empty:
            return []

        df = df.sort_values("date")
        # 指定 end_date 时只保留截至该日的 K 线（修复 --date 回放前视偏差）
        if end_date is not None:
            end_str = end_date.strftime("%Y-%m-%d")
            df = df[df["date"].astype(str) <= end_str]
        bars: list[Bar] = []
        for row in df.tail(days).itertuples():
            d = datetime.strptime(str(row.date), "%Y-%m-%d")
            bars.append(
                Bar(
                    symbol=symbol,
                    datetime=d,
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(row.volume),
                )
            )
        # 可选后复权式调整（消除除权跳空；拉取失败降级为原始行情）
        if adjust == "hfq":
            from .adjfactor import compute_adjusted_bars, fetch_dividends

            try:
                dividends = fetch_dividends(f"{prefix}{symbol}")
                bars = compute_adjusted_bars(bars, dividends)
            except Exception:
                logger.warning("复权因子获取失败，返回原始行情: %s", symbol)
        return bars

    def is_trading_day(self, date: datetime) -> bool:
        """判断某日是否为 A 股交易日；交易日历不可用（无网络）时降级为仅跳过周末"""
        ds = date.strftime("%Y-%m-%d")
        cal = _load_trade_calendar()
        if cal:
            return ds in cal
        return date.weekday() < 5

    @property
    def calendar_ok(self) -> bool:
        """交易日历是否加载成功（供 batch 在降级时显著告警）"""
        return _TRADE_CALENDAR_OK


# A 股交易日历缓存（YYYY-MM-DD 集合）；获取失败时为空集
_TRADE_CALENDAR: set[str] | None = None
_TRADE_CALENDAR_OK: bool = False


def _load_trade_calendar() -> set[str]:
    """加载 A 股交易日历（akshare），失败返回空集（调用方降级为周末判断）"""
    global _TRADE_CALENDAR, _TRADE_CALENDAR_OK
    if _TRADE_CALENDAR is not None:
        return _TRADE_CALENDAR
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        _TRADE_CALENDAR = {str(d) for d in df["trade_date"]}
        _TRADE_CALENDAR_OK = True
    except Exception:
        _TRADE_CALENDAR = set()
        _TRADE_CALENDAR_OK = False
    return _TRADE_CALENDAR


class FakeDataSource:
    """测试用假数据源：按固定序列循环生成行情"""

    name = "fake"

    def __init__(
        self,
        closes: list[float],
        base_date: datetime | None = None,
        trading_days: set[str] | None = None,
    ) -> None:
        """trading_days: 视为交易日的日期集合（YYYY-MM-DD）；None 表示每天都算交易日"""
        self.closes = closes
        self.base_date = base_date or datetime(2024, 1, 1)
        self._trading_days = trading_days

    def fetch_daily_bars(
        self,
        symbol: str,
        days: int,
        exchange: str = "SH",
        end_date: datetime | None = None,
        adjust: str = "none",
    ) -> list[Bar]:
        # 语义与真实数据源对齐：返回截至 end_date 的最近 days 根（最后一根 = end_date，不含未来）
        start = self.base_date if end_date is None else end_date - timedelta(days=days - 1)
        bars: list[Bar] = []
        n = len(self.closes)
        for i in range(days):
            d = start + timedelta(days=i)
            if end_date is not None and d > end_date:
                break
            c = self.closes[i % n]
            bars.append(
                Bar(
                    symbol=symbol,
                    datetime=d,
                    open=c,
                    high=c * 1.01,
                    low=c * 0.99,
                    close=c,
                    volume=1000.0,
                )
            )
        return bars

    def is_trading_day(self, date: datetime) -> bool:
        if self._trading_days is None:
            return True
        return date.strftime("%Y-%m-%d") in self._trading_days


class PolicySource(Protocol):
    """宏观政策/新闻数据源协议"""

    name: str

    def fetch_macro_news(self, keywords: list[str], max_items: int) -> list[str]:
        """拉取宏观政策快讯，按关键词过滤，返回文本列表（最新在前）"""
        ...


class AkSharePolicySource:
    """基于 akshare 财联社电报接口的政策源，免费、无需注册"""

    name = "akshare_cls"

    def fetch_macro_news(self, keywords: list[str], max_items: int) -> list[str]:
        from .util import retry_call

        import akshare as ak  # 延迟导入，便于 mock

        df = retry_call(lambda: ak.stock_info_global_cls(symbol="全部"), label="政策快讯")
        if df is None or df.empty:
            return []

        items: list[str] = []
        for row in df.itertuples():
            title = str(row.标题)
            content = str(row.内容)
            text = f"{title}｜{content}"
            if any(kw in title or kw in content for kw in keywords):
                items.append(text)
            if len(items) >= max_items:
                break
        return items


class FakePolicySource:
    """测试用假政策源"""

    name = "fake_policy"

    def __init__(self, news: list[str]) -> None:
        self.news = news

    def fetch_macro_news(self, keywords: list[str], max_items: int) -> list[str]:
        return [n for n in self.news if any(k in n for k in keywords)][:max_items]
