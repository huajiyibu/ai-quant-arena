"""政策数据源过滤测试"""
from aitrader.datasource import FakePolicySource

KEYWORDS = ["央行", "证监会", "降息"]


def test_filters_macro_policy_news():
    news = [
        "央行宣布降息0.25个百分点",
        "某公司发布季度财报",
        "证监会召开会议强调监管",
        "阳光电源回应FCC政策",
    ]
    src = FakePolicySource(news)
    result = src.fetch_macro_news(KEYWORDS, max_items=10)
    assert "央行宣布降息0.25个百分点" in result
    assert "证监会召开会议强调监管" in result
    # 公司财报属于噪音，不应命中宏观政策关键词
    assert "某公司发布季度财报" not in result
    assert len(result) == 2


def test_max_items_limit():
    news = ["央行a", "证监会b", "降息c", "央行d", "央行e"]
    src = FakePolicySource(news)
    result = src.fetch_macro_news(KEYWORDS, max_items=2)
    assert len(result) == 2


def test_no_match_returns_empty():
    src = FakePolicySource(["某公司公告", "行业展会新闻"])
    assert src.fetch_macro_news(KEYWORDS, max_items=10) == []
