"""v0.24c 第三批：AI 只买不卖（体检07 P1-4）——JSON 示例给 sell 对称例子 + 明确卖出纪律。"""
from pathlib import Path

p = Path("aitrader/engines/deepseek.py")
src = p.read_text(encoding="utf-8")

old = '''            '{"decisions":[{"symbol":"510300","action":"buy","amount":50000,'
            '"confidence":0.7,"reason":"[趋势] 放量突破20日线"}]}\\n'
            "规则: 1) action 仅限 buy/sell/hold; 2) buy 必带 amount(元), sell=清仓该标的; "
            f"3) 最多对{self.max_buy_count}个标的下buy; "
            "4) confidence 是 0~1 的数值，表示你对这个信号带来正收益的信心（不是市场必然性），"
            "信号足够明确时才给高 confidence；5) 谨慎、保守、不追高、不接飞刀；"
            "6) reason 以 [趋势]/[回调]/[政策]/[超买]/[超卖]/[其他] 之一开头，再接一句话理由。"
'''
new = '''            '{"decisions":[{"symbol":"510300","action":"buy","amount":50000,'
            '"confidence":0.7,"reason":"[趋势] 放量突破20日线"},'
            '{"symbol":"588000","action":"sell","amount":0,'
            '"confidence":0.8,"reason":"[趋势] 跌破20日线，止盈离场"}]}\\n'
            "规则: 1) action 仅限 buy/sell/hold; 2) buy 必带 amount(元), sell=清仓该标的; "
            f"3) 最多对{self.max_buy_count}个标的下buy; "
            "4) confidence 是 0~1 的数值，表示你对这个信号带来正收益的信心（不是市场必然性），"
            "信号足够明确时才给高 confidence；5) 谨慎、保守、不追高、不接飞刀；"
            "6) 持仓标的若破位（跌破关键均线/止损位）、达到目标收益、或风险明显释放，"
            "应主动 sell 落袋/止损，不要只买不卖（卖出纪律与买入同等重要）；"
            "7) reason 以 [趋势]/[回调]/[政策]/[超买]/[超卖]/[其他] 之一开头，再接一句话理由。"
'''
n = src.count(old)
assert n == 1, f"deepseek: expected 1, got {n}"
src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("deepseek.py: sell 对称示例 + 卖出纪律 done")
