"""复权因子模块（IMPROVEMENTS P1-2 / PREDICTION PP-2 前置）。

akshare 无现成 ETF 前复权接口（东财 fund_etf_hist_em 本机不可达、新浪 fund_etf_hist_sina
无 adjust 参数），但新浪提供 `fund_etf_dividend_sina`（日期 + 每份累计分红）。

本模块采用"后复权式"调整：P_adj(t) = P(t) + 截至 t 的累计分红。选择理由：
- 只用截至当日的分红 → 无前视（回测安全）
- 消除除权日价格跳空（技术特征不失真，PP-2 前置）
- 实现简单、可单测；对比例型指标（均线/RSI/动量/波动率）无影响
"""
from __future__ import annotations

from datetime import date, datetime

from .models import Bar


def parse_dividends(rows) -> list[tuple[date, float]]:
    """把 (日期串, 累计分红) 行转成 [(date, 累计分红), ...]，按日期升序。"""
    out: list[tuple[date, float]] = []
    for r in rows:
        d = r[0] if isinstance(r[0], date) else datetime.strptime(str(r[0]), "%Y-%m-%d").date()
        out.append((d, float(r[1])))
    out.sort(key=lambda x: x[0])
    return out


def cumulative_dividend_at(dividends: list[tuple[date, float]], on: date) -> float:
    """截至 on 日的每份累计分红（后复权式加回金额）；无分红记录返回 0.0。"""
    total = 0.0
    for d, cum in dividends:
        if d <= on:
            total = cum
        else:
            break
    return total


def compute_adjusted_bars(
    bars: list[Bar], dividends: list[tuple[date, float]]
) -> list[Bar]:
    """后复权式调整：每根 bar 的 OHLC 加上截至其日期的累计分红（消除除权跳空）。

    无分红生效的 bar 原样返回；输入列表不被修改。
    """
    out: list[Bar] = []
    for b in bars:
        adj = cumulative_dividend_at(dividends, b.datetime.date())
        if adj:
            out.append(
                Bar(
                    symbol=b.symbol,
                    datetime=b.datetime,
                    open=b.open + adj,
                    high=b.high + adj,
                    low=b.low + adj,
                    close=b.close + adj,
                    volume=b.volume,
                )
            )
        else:
            out.append(b)
    return out


def fetch_dividends(symbol: str) -> list[tuple[date, float]]:
    """从新浪拉取 (日期, 每份累计分红) 列表（升序）。symbol 需带前缀，如 'sh510300'。

    进程内缓存（分红低频，同进程多次拉取去重）。失败向上抛，由调用方降级为原始行情。
    """
    if symbol in _DIVIDEND_CACHE:
        return _DIVIDEND_CACHE[symbol]
    from .util import retry_call

    import akshare as ak  # 延迟导入，便于 mock

    df = retry_call(
        lambda: ak.fund_etf_dividend_sina(symbol=symbol), label=f"分红{symbol}"
    )
    if df is None or df.empty:
        result: list[tuple[date, float]] = []
    else:
        result = parse_dividends(list(zip(df["日期"], df["累计分红"])))
    _DIVIDEND_CACHE[symbol] = result
    return result


_DIVIDEND_CACHE: dict[str, list] = {}  # 进程内分红缓存（仿 datasource 交易日历模式）
