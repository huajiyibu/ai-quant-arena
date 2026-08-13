"""v0.22: 政策源发布日期过滤修复（datetime.date 对象 vs str）回归测试。"""
import datetime as dt
from unittest import mock

import pandas as pd

from aitrader.datasource import AkSharePolicySource


def _df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "标题": ["央行宣布降息", "某公司发布季度财报", "央行呵护流动性"],
            "内容": ["A", "B", "C"],
            "发布日期": [dt.date(2026, 8, 13)] * 3,
            "发布时间": [dt.time(9, 30), dt.time(16, 0), dt.time(10, 0)],
        }
    )


def test_policy_source_filters_by_date_and_cutoff():
    """F-6 回归：发布日期为 date 对象时能按决策日过滤（date==str 永远 False 的 bug）"""
    with mock.patch("akshare.stock_info_global_cls", return_value=_df()):
        src = AkSharePolicySource()
        news = src.fetch_macro_news(
            ["央行"], 5, decision_date="2026-08-13", cutoff_time="15:30"
        )
    # 16:00 那条被 cutoff 过滤；央行命中 09:30 / 10:00 两条
    assert len(news) == 2
    assert all("央行" in n for n in news)


def test_policy_source_wrong_date_returns_empty():
    """决策日与数据日期不一致时返回空（避免把旧闻当今日政策）"""
    with mock.patch("akshare.stock_info_global_cls", return_value=_df()):
        src = AkSharePolicySource()
        news = src.fetch_macro_news(
            ["央行"], 5, decision_date="2026-08-12", cutoff_time="15:30"
        )
    assert news == []
