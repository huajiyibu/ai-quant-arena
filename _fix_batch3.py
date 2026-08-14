"""v0.23c 第三批修复（reporter.py）：
1. 卡片样本不足水印（成交<20 笔，结论仅供过程展示）
2. ai_policy 政策注入自检（最近决策无政策段 → 标注不可信）
3. 日报基准改用本地 bars 表 + 扣单边佣金 + 失败可见（P1-10）
"""
from pathlib import Path

p = Path("aitrader/reporter.py")
src = p.read_text(encoding="utf-8")

repls = [
    # 1+2: 样本水印 + 政策注入自检计算（加在 interest_total 之后）
    (
        '''        trades = db.get_trades(acc["id"])
        css = "green" if t_amt >= 0 else "red"
        interest_total = sum(s.get("interest") or 0.0 for s in snaps)
''',
        '''        trades = db.get_trades(acc["id"])
        css = "green" if t_amt >= 0 else "red"
        interest_total = sum(s.get("interest") or 0.0 for s in snaps)

        # 体检P0：样本不足水印（成交 <20 笔时结论不具统计意义，仅过程展示）
        sample_warn = ""
        if len(trades) < 20:
            sample_warn = (
                f"<p style='color:#e67e22;font-weight:bold'>⚠️ 仅 {len(trades)} 笔成交，"
                "样本不足，当前结论仅供过程展示、不具统计意义</p>"
            )
        # 体检P0-3：政策注入自检（ai_policy 最近决策未收到政策 → 标注本轮不可信）
        inject_warn = ""
        if acc["engine_type"] == "ai_policy":
            recent = db.get_decisions(acc["id"])
            if recent and "宏观政策" not in (recent[-1].get("prompt_json") or ""):
                inject_warn = (
                    "<p style='color:#c0392b;font-weight:bold'>⚠️ 政策注入自检未通过"
                    "（最近决策未含政策段），政策版本轮结论不可信</p>"
                )
''',
    ),
    # 2b: 卡片渲染 sample_warn / inject_warn
    (
        '''            <p>持仓：{holdings}</p>
            {fair_line}
            <p>累计成交 {len(trades)} 笔 ｜ 货基利息累计 {interest_total:+,.2f} 元</p>
''',
        '''            <p>持仓：{holdings}</p>
            {sample_warn}
            {inject_warn}
            {fair_line}
            <p>累计成交 {len(trades)} 笔 ｜ 货基利息累计 {interest_total:+,.2f} 元</p>
''',
    ),
    # 3: 基准改用本地 bars + 扣单边佣金 + 失败可见
    (
        '''    # B-2：基准对照（配置首个标的买入持有，覆盖首个快照至今；失败则留空不阻塞）
    bench_line = ""
    try:
        ds = AkShareDataSource()
        first_date = datetime.now().replace(year=2020)
        for acc in db.get_accounts():
            snaps = db.get_snapshots(acc["id"])
            if snaps:
                d = datetime.strptime(snaps[0]["date"], "%Y-%m-%d")
                if d < first_date:
                    first_date = d
        first_sym = next(iter(settings.symbols))
        cfg = settings.symbols[first_sym]
        bars = ds.fetch_daily_bars(first_sym, 500, cfg.exchange, end_date=datetime.now())
        bars = [b for b in bars if b.datetime >= first_date]
        if len(bars) >= 2:
            bench_ret = bars[-1].close / bars[0].close - 1
            bench_line = (
                f"<p>基准对照（{first_sym} 买入持有，{bars[0].datetime.date()}~"
                f"{bars[-1].datetime.date()}）：{bench_ret:+.2%}</p>"
            )
    except Exception:
        bench_line = ""
''',
        '''    # 体检P1-10：基准改用本地 bars 表（不联网、与账户原始价同口径）+ 扣单边买入佣金；失败可见不静默
    bench_line = ""
    try:
        first_date = None
        for acc in db.get_accounts():
            snaps = db.get_snapshots(acc["id"])
            if snaps:
                d = datetime.strptime(snaps[0]["date"], "%Y-%m-%d")
                if first_date is None or d < first_date:
                    first_date = d
        first_sym = next(iter(settings.symbols))
        rows = db.get_bars(first_sym, limit=1200)
        rows = [
            r for r in rows
            if first_date is None or r["date"] >= first_date.strftime("%Y-%m-%d")
        ]
        rows.sort(key=lambda r: r["date"])
        if len(rows) >= 2:
            c = settings.risk.commission_rate
            bench_ret = (rows[-1]["close"] * (1 - c)) / (rows[0]["close"] * (1 + c)) - 1
            bench_line = (
                f"<p>基准对照（{first_sym} 买入持有，{rows[0]['date']}~"
                f"{rows[-1]['date']}，含单边佣金）：{bench_ret:+.2%}</p>"
            )
        elif not rows:
            bench_line = "<p>基准对照：本地无行情数据（数据源尚未拉取）</p>"
    except Exception as exc:
        bench_line = f"<p>基准对照：获取失败（{type(exc).__name__}: {exc}）</p>"
''',
    ),
]

for i, (old, new) in enumerate(repls, 1):
    n = src.count(old)
    assert n == 1, f"repl #{i}: expected 1 occurrence, got {n}: {old[:50]}"
    src = src.replace(old, new)

p.write_text(src, encoding="utf-8")
print("reporter.py: 样本水印 + 政策自检 + 基准本地化 done")
