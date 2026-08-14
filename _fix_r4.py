"""v0.24d 第四批：合规红线（体检10）——
1. SRS.md：删除"真钱半自动建议接口/1000元真钱预算"，明确永不接入真实资金
2. reporter.py：日报头部加"模拟≠实盘"警示条 + 页脚免责增强
"""
from pathlib import Path

# ---- SRS.md ----
p = Path("docs/SRS.md")
src = p.read_text(encoding="utf-8")
repls = [
    (
        "- 用户为软件开发工程师，金融知识为零，真钱体验预算上限 1000 元（当前阶段未启用真钱）。",
        "- 用户为软件开发工程师，金融知识为零；**纯虚拟资金，无任何真实资金路径**。",
    ),
    (
        "- 系统当前阶段为**纯仿真**（虚拟资金），**预留真钱\"半自动建议\"模式接口**（不实现实盘自动下单）。",
        "- 系统为**纯仿真**（虚拟资金），**永不接入真实资金/实盘下单，也不向用户输出实盘操作建议**（合规红线，见 §6）。",
    ),
    (
        "- 不做实盘自动下单（仅预留接口）。",
        "- 不做实盘自动下单、不接入真实资金、不预留任何真实资金/荐股接口（避免触及证券投资咨询持牌监管红线）。",
    ),
]
for old, new in repls:
    n = src.count(old)
    assert n == 1, f"SRS: expected 1, got {n}: {old[:30]}"
    src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("SRS.md 合规改写 done")

# ---- reporter.py ----
p = Path("aitrader/reporter.py")
src = p.read_text(encoding="utf-8")
repls = [
    # 头部警示条（h1 之后）
    (
        '''<h1>📊 AI 交易日报</h1>
<p>生成时间：{datetime.now():%Y-%m-%d %H:%M} ｜ 数据截至：{data_disp}{data_note}</p>
''',
        '''<h1>📊 AI 交易日报</h1>
<p style="background:#fff3cd;border:1px solid #e67e22;color:#8a4b08;padding:10px 14px;border-radius:8px;font-size:14px;font-weight:bold">⚠️ 模拟≠实盘：本报告为虚拟资金仿真，按收盘价成交、默认低滑点；实盘还面临滑点/流动性/情绪等差异，模拟结果不可外推，请勿据此实盘操作。</p>
<p>生成时间：{datetime.now():%Y-%m-%d %H:%M} ｜ 数据截至：{data_disp}{data_note}</p>
''',
    ),
    # 页脚免责增强
    (
        '''<p class="foot">本报告为仿真（虚拟资金）结果，仅供学习体验，不构成投资建议。</p>''',
        '''<p class="foot">本报告为仿真（虚拟资金）结果，仅供学习体验，不构成投资建议。AI 输出（买卖理由/政策解读）仅为算法模拟，非专业投资建议；小样本下结论仅供过程观察，不具统计意义。</p>''',
    ),
]
for old, new in repls:
    n = src.count(old)
    assert n == 1, f"reporter: expected 1, got {n}: {old[:40]}"
    src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("reporter.py 免责增强 done")
print("ALL OK")
