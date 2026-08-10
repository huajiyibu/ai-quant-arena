"""v0.8 当日行情补全测试（解决新浪历史接口滞后一天，实时接口补当日 K 线）"""
from datetime import date, datetime

import pandas as pd
import pytest

from aitrader.datasource import AkShareDataSource


class FakeResp:
    def __init__(self, text):
        self.text = text
        self.encoding = "utf-8"


# 新浪实时返回样本（hq.sinajs.cn 格式，需 Referer）
_SINA_RT = (
    'var hq_str_sh510300="沪深300ETF,4.755,4.751,4.759,4.772,4.720,'
    '4.758,4.759,561085163,2663755927.000,330800,4.758,196700,4.757,'
    '83900,4.756,95700,4.755,263200,4.754,250700,4.759,3620500,4.760,'
    '1150200,4.761,1029900,4.762,790700,4.763,2026-08-10,15:34:59,00,"'
)


def _hist_df(last_date):
    """构造截至 last_date 的历史 df（新浪 fund_etf_hist_sina 返回格式）"""
    dates = pd.date_range("2026-08-01", last_date).tolist()
    return pd.DataFrame(
        {
            "date": [d.date() for d in dates],
            "open": [4.7] * len(dates),
            "high": [4.8] * len(dates),
            "low": [4.6] * len(dates),
            "close": [4.75] * len(dates),
            "volume": [1000.0] * len(dates),
        }
    )


def test_fetch_realtime_parses_sina(monkeypatch):
    def fake_get(url, **kw):
        assert "hq.sinajs.cn" in url
        assert "Referer" in kw["headers"]  # 新浪反爬必须带 Referer
        return FakeResp(_SINA_RT)

    monkeypatch.setattr("requests.get", fake_get)
    ds = AkShareDataSource()
    bar = ds._fetch_realtime("510300", "SH")
    assert bar is not None
    assert bar.datetime.date() == date(2026, 8, 10)
    assert bar.open == pytest.approx(4.755)
    assert bar.close == pytest.approx(4.759)
    assert bar.high == pytest.approx(4.772)
    assert bar.low == pytest.approx(4.720)
    assert bar.volume == pytest.approx(561085163.0)


def test_fetch_daily_bars_completes_today(monkeypatch):
    """历史滞后到昨天 + end_date=今天 → 补当日实时 bar"""
    import akshare as ak

    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda symbol: _hist_df(date(2026, 8, 7)))
    monkeypatch.setattr("requests.get", lambda url, **kw: FakeResp(_SINA_RT))
    ds = AkShareDataSource()
    bars = ds.fetch_daily_bars("510300", 30, "SH", end_date=datetime(2026, 8, 10))
    assert bars[-1].datetime.date() == date(2026, 8, 10)  # 补全到今天
    assert bars[-1].close == pytest.approx(4.759)
    assert bars[-2].datetime.date() == date(2026, 8, 7)  # 历史最后一根仍保留


def test_fetch_daily_bars_no_complete_for_past(monkeypatch):
    """回测（end_date 是过去日）→ 不补实时"""
    import akshare as ak

    called = []
    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda symbol: _hist_df(date(2026, 8, 1)))
    monkeypatch.setattr(
        "requests.get", lambda url, **kw: (called.append(1), FakeResp(_SINA_RT))[1]
    )
    ds = AkShareDataSource()
    bars = ds.fetch_daily_bars("510300", 30, "SH", end_date=datetime(2026, 8, 1))
    assert called == []  # 未调用实时
    assert bars[-1].datetime.date() == date(2026, 8, 1)


def test_fetch_daily_bars_no_complete_when_fresh(monkeypatch):
    """历史最新已含目标日 → 不重复补"""
    import akshare as ak

    called = []
    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda symbol: _hist_df(date(2026, 8, 10)))
    monkeypatch.setattr(
        "requests.get", lambda url, **kw: (called.append(1), FakeResp(_SINA_RT))[1]
    )
    ds = AkShareDataSource()
    bars = ds.fetch_daily_bars("510300", 30, "SH", end_date=datetime(2026, 8, 10))
    assert called == []
    assert bars[-1].datetime.date() == date(2026, 8, 10)
