"""行情数据源：抽象接口 + akshare 实现。

设计要点（对应 HLD §2 datasource）：统一接口可注入，测试用 Fake 替代网络数据源。
"""
from __future__ import annotations

import logging
import socket
from datetime import datetime, time, timedelta
from typing import Protocol

from .models import Bar

logger = logging.getLogger(__name__)

# 网络硬超时（P1-9）：akshare 内部 requests.get 不传 timeout，网络半死/被代理干扰时
# 会无限挂起（实测新浪 WinError 10060 需手动 Ctrl+C）。设 socket 级默认超时，快速失败。
# 对显式传 timeout 的请求（如 DeepSeek 90s）无影响；上层 retry_call 再负责重试。
socket.setdefaulttimeout(15)

# IPv6 无路由修复：本机无 IPv6 路由，而 DNS 返回的新浪地址前几条全是 IPv6，
# urllib3 逐条试 IPv6 失败后才轮到 IPv4 → 拉取从 0.5s 恶化到 60s（实测）。
# 对国内行情域名过滤 IPv6，只留 IPv4（不影响 DeepSeek 等其他域名）。
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_first_getaddrinfo(host, *args, **kwargs):
    result = _orig_getaddrinfo(host, *args, **kwargs)
    if isinstance(host, str) and ("sina" in host or "sinajs" in host):
        ipv4 = [r for r in result if r[0] == socket.AF_INET]
        if ipv4:
            return ipv4
    return result


socket.getaddrinfo = _ipv4_first_getaddrinfo


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
        # 当日行情补全：新浪历史接口滞后约 1 个交易日。历史最新早于目标日时尝试补实时；
        # 实时接口只返回"今天"，日期不匹配（如回测 end_date 为过去日）则自然不补，安全。
        ref_date = end_date.date() if end_date is not None else datetime.now().date()
        if bars and bars[-1].datetime.date() < ref_date:
            rt = self._fetch_realtime(symbol, exchange)
            if rt is not None and rt.datetime.date() == ref_date:
                bars.append(rt)
        # 可选后复权式调整（消除除权跳空；拉取失败降级为原始行情）
        if adjust == "hfq":
            from .adjfactor import compute_adjusted_bars, fetch_dividends

            try:
                dividends = fetch_dividends(f"{prefix}{symbol}")
                bars = compute_adjusted_bars(bars, dividends)
            except Exception:
                logger.warning("复权因子获取失败，返回原始行情: %s", symbol)
        return bars

    def _fetch_realtime(self, symbol: str, exchange: str) -> Bar | None:
        """拉当日实时行情（新浪 hq.sinajs.cn，需 Referer），构造一根日 K 线。

        新浪历史接口 fund_etf_hist_sina 滞后约 1 个交易日（实测周一仍停在上周五），
        真实运行时用实时接口补当日 K 线（收盘后调用 close≈当日收盘价）。失败返回 None，
        上层降级为仅用历史数据。
        """
        import requests
        from .util import retry_call

        prefix = self._EXCHANGE_PREFIX.get(exchange.upper(), "sh")
        try:
            # P1-3：接入重试（指数退避），偶发抖动不再整日剔除该标的
            r = retry_call(
                lambda: requests.get(
                    f"https://hq.sinajs.cn/list={prefix}{symbol}",
                    headers={"Referer": "https://finance.sina.com.cn/"},
                    timeout=10,
                ),
                label=f"实时{symbol}",
            )
            r.encoding = "gbk"
            text = r.text
            if '"' not in text:
                return None
            fields = text.split('"')[1].split(",")
            # 0名称 1今开 2昨收 3当前 4最高 5最低 8成交量(股) 30日期 31时间
            if len(fields) < 32 or not fields[30].strip():
                return None
            date = datetime.strptime(fields[30].strip(), "%Y-%m-%d")
            open_, high, low, close = (float(fields[i]) for i in (1, 4, 5, 3))
            volume = float(fields[8])
            if close <= 0:
                return None
            return Bar(symbol, date, open_, high, low, close, volume)
        except Exception:
            logger.warning("实时行情获取失败: %s", symbol)
            return None

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

    def fetch_macro_news(
        self,
        keywords: list[str],
        max_items: int,
        decision_date=None,
        cutoff_time: str = "15:30",
    ) -> list[str]:
        """拉取宏观政策快讯：只取决策日当天、不晚于决策时点(cutoff)的，按关键词过滤，最新在前。"""
        ...


class AkSharePolicySource:
    """基于 akshare 财联社电报接口的政策源，免费、无需注册"""

    name = "akshare_cls"

    def fetch_macro_news(
        self,
        keywords: list[str],
        max_items: int,
        decision_date=None,
        cutoff_time: str = "15:30",
    ) -> list[str]:
        """拉取宏观政策快讯：只取决策日当天、且不晚于决策时点（cutoff）的消息。

        - 过滤 发布日期==decision_date：避免周一把上周旧闻当"今日政策"（治滞后 F-6）
        - 过滤 发布时间<=cutoff：避免用收盘后消息 justify 收盘价成交（治前视 F-7）
        - 倒序遍历取最新匹配
        """
        from .util import retry_call

        import akshare as ak  # 延迟导入，便于 mock

        df = retry_call(lambda: ak.stock_info_global_cls(symbol="全部"), label="政策快讯")
        if df is None or df.empty:
            return []

        # F-6/F-7：按决策日 + 决策时点过滤（接口自带 发布日期/发布时间 两列）
        # 注意：发布日期列是 datetime.date 对象，需先转字符串再比较（date == str 永远 False）
        if {"发布日期", "发布时间"}.issubset(df.columns):
            if decision_date is not None:
                # decision_date 可能是 str 或 datetime.date，统一转 "YYYY-MM-DD"
                dd = (
                    decision_date.strftime("%Y-%m-%d")
                    if hasattr(decision_date, "strftime")
                    else str(decision_date)
                )
                df = df[df["发布日期"].astype(str).str[:10] == dd]
            df = df[df["发布时间"].astype(str).str[:5] <= cutoff_time]
        if df.empty:
            return []

        items: list[str] = []
        seen: set[str] = set()
        # 体检：过滤海外邻国政策噪音（澳/印/日/韩等非 A 股政策，易被宽松关键词误命中）
        FOREIGN = ("澳大利亚", "印度", "日本", "韩国", "越南", "泰国", "马来西亚", "印尼", "新加坡")
        DOMESTIC = ("中国", "央行", "证监会", "国务院", "发改委", "国常会", "人民币", "上证", "深证", "沪深", "A股", "国内")
        # 接口内部按发布时间升序 → 倒序遍历取最新匹配优先
        for row in df.iloc[::-1].itertuples():
            title = str(row.标题).strip()
            content = str(row.内容).strip()
            if not title or title == "nan":
                continue  # 标题为空/NaN 跳过（避免裸"｜内容"残文）
            text = f"{title}｜{content}"
            if not any(kw in title or kw in content for kw in keywords):
                continue
            # 海外邻国噪音：含外国名且不含国内词 → 跳过
            if any(f in text for f in FOREIGN) and not any(d in text for d in DOMESTIC):
                continue
            key = text[:80]
            if key in seen:
                continue  # 去重（财联社标题/内容常重复）
            seen.add(key)
            items.append(text[:150])  # 限长，避免长文挤占 prompt
            if len(items) >= max_items:
                break
        return items


class FakePolicySource:
    """测试用假政策源"""

    name = "fake_policy"

    def __init__(self, news: list[str]) -> None:
        self.news = news

    def fetch_macro_news(
        self,
        keywords: list[str],
        max_items: int,
        decision_date=None,
        cutoff_time: str = "15:30",
    ) -> list[str]:
        return [n for n in self.news if any(k in n for k in keywords)][:max_items]
