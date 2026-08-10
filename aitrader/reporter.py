"""报表模块：多引擎资金曲线对比图 + 文本汇总（对应 SRS FR8）。"""
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .database import Database


def plot_compare(db: Database, settings: Settings, out_path: Path) -> Path:
    """绘制各账户资金曲线对比图，返回输出路径"""
    from datetime import datetime

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(12, 6))
    all_dates: list[datetime] = []
    for acc in db.get_accounts():
        snaps = db.get_snapshots(acc["id"])
        if not snaps:
            continue
        dates = [datetime.strptime(s["date"], "%Y-%m-%d") for s in snaps]
        assets = [s["total_assets"] for s in snaps]
        all_dates.extend(dates)
        ax.plot(
            dates, assets, marker="o", ms=3,
            label=f"{acc['name']} ({acc['engine_type']})",
        )
        ax.axhline(acc["initial_capital"], color="gray", ls="--", lw=0.8)

    ax.set_title("多引擎资金曲线对比")
    ax.set_ylabel("总资产")
    ax.legend()
    ax.grid(alpha=0.3)

    # X 轴：只在每月第一天显示刻度；每年 1 月显示年份，其余月份只显示两位月份
    if all_dates:
        first_days = sorted({d.replace(day=1) for d in all_dates})
        labels = [d.strftime("%Y") if d.month == 1 else d.strftime("%m") for d in first_days]
        ax.set_xticks(first_days)
        ax.set_xticklabels(labels)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def build_summary(db: Database, settings: Settings) -> str:
    """生成账户汇总文本（CLI 输出用）"""
    lines: list[str] = []
    for acc in db.get_accounts():
        snaps = db.get_snapshots(acc["id"])
        if not snaps:
            lines.append(f"[{acc['name']}] 暂无数据")
            continue
        last = snaps[-1]
        pct = last["pnl"] / acc["initial_capital"] * 100
        trades = db.get_trades(acc["id"])
        lines.append(
            f"[{acc['name']}] 总资产 {last['total_assets']:,.2f} "
            f"({last['pnl']:+,.2f} / {pct:+.2f}%) | 现金 {last['cash']:,.2f} | 累计成交 {len(trades)} 笔"
        )
    return "\n".join(lines)


def build_daily_report(
    db: Database, settings: Settings, chart_path: Path, out_path: Path
) -> Path:
    """生成小白友好的 HTML 日报：收益概况 + 今日决策 + 基准对照 + AI 信心校准（B-1/B-2）"""
    import base64
    from datetime import datetime

    from .backtest import rank_ic
    from .datasource import AkShareDataSource

    cards: list[str] = []
    data_until = ""
    for acc in db.get_accounts():
        snaps = db.get_snapshots(acc["id"])
        if not snaps:
            continue
        last = snaps[-1]
        pct = last["pnl"] / acc["initial_capital"] * 100
        state = db.load_state(acc["id"])
        if state and state.positions:
            parts = [
                f"{p.name} {p.volume}股（成本{p.cost_price:.3f}）"
                for p in state.positions.values()
            ]
            holdings = "，".join(parts)
        else:
            holdings = "空仓"
        trades = db.get_trades(acc["id"])
        css = "green" if last["pnl"] >= 0 else "red"

        # B-1：真实盘 Rank IC 校准（AI 引擎信心是否有预测力）
        ic_line = ""
        bucket_html = ""
        if acc["engine_type"] in ("ai", "ai_policy"):
            cal = db.get_calibrated_decisions(acc["id"])
            if len(cal) >= 5:
                ic = rank_ic(
                    [c["confidence"] for c in cal], [c["forward_return"] for c in cal]
                )
                ic_line = f"<p>AI 信心校准 Rank IC：{ic:+.3f}（已校准 {len(cal)} 笔）</p>"
            # N-4：按信心分桶胜率（高信心是否真的更准；独立于 IC 门槛，有样本即渲染）
            buckets = {"0~0.6": [], "0.6~0.7": [], "0.7+": []}
            for c in cal:
                cf = c["confidence"]
                if cf < 0.6:
                    buckets["0~0.6"].append(c["forward_return"])
                elif cf <= 0.7:
                    buckets["0.6~0.7"].append(c["forward_return"])
                else:
                    buckets["0.7+"].append(c["forward_return"])
            bucket_rows = []
            for name, vals in buckets.items():
                if not vals:
                    continue
                n = len(vals)
                win = sum(1 for v in vals if v > 0) / n
                avg = sum(vals) / n
                bucket_rows.append(
                    f"<tr><td>{name}</td><td>{n}</td><td>{win:.0%}</td><td>{avg:+.2%}</td></tr>"
                )
            if bucket_rows:
                bucket_html = (
                    "<p>按信心分桶（正收益占比）：</p>"
                    "<table border=1 cellpadding=4 cellspacing=0>"
                    "<tr><th>信心</th><th>样本</th><th>胜率</th><th>均收益</th></tr>"
                    + "".join(bucket_rows)
                    + "</table>"
                )

        # B-2：今日决策明细
        today = last["date"]
        today_dec = [d for d in db.get_decisions(acc["id"]) if d["date"] == today]
        if today_dec:
            dec_items = "，".join(
                f"{d['symbol']} {d['action']} {d['amount']:,.0f}元(conf {d.get('confidence', '-')}) "
                f"{str(d['reason'])[:26]}"
                for d in today_dec[:6]
            )
            dec_line = f"<p>今日决策：{dec_items}</p>"
        else:
            dec_line = ""

        # B-2/F-11：数据截至日期（异常时高亮）
        bar_date = last.get("bar_date") or today
        bar_note = f"（估值截至 {bar_date}）" if bar_date != today else ""
        if bar_date > today:
            data_until = max(data_until, bar_date)

        cards.append(
            f"""<div class="card {css}">
            <h2>{acc['name']}（{acc['engine_type']}）</h2>
            <p class="big">累计盈亏 <b>{last['pnl']:+,.2f} 元</b>（{pct:+.2f}%）</p>
            <p>总资产 {last['total_assets']:,.2f} 元 ｜ 现金 {last['cash']:,.2f} 元 {bar_note}</p>
            <p>持仓：{holdings}</p>
            <p>累计成交 {len(trades)} 笔</p>
            {dec_line}
            {ic_line}
            {bucket_html}
            </div>"""
        )
    cards_html = "\n".join(cards)

    # B-2：基准对照（配置首个标的买入持有，覆盖首个快照至今；失败则留空不阻塞）
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

    img_b64 = ""
    if chart_path.exists():
        img_b64 = base64.b64encode(chart_path.read_bytes()).decode()

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AI 交易日报</title>
<style>
  body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px; background: #f5f6fa; }}
  h1 {{ color: #2c3e50; }}
  .card {{ background: #fff; border-radius: 10px; padding: 16px 20px; margin: 12px 0;
          box-shadow: 0 1px 4px rgba(0,0,0,.1); border-left: 6px solid #ccc; }}
  .card.green {{ border-left-color: #27ae60; }}
  .card.red {{ border-left-color: #e74c3c; }}
  .big {{ font-size: 22px; margin: 8px 0; }}
  .big b {{ font-size: 26px; }}
  img {{ max-width: 100%; border-radius: 10px; margin-top: 12px; }}
  .foot {{ color: #95a5a6; font-size: 12px; margin-top: 20px; }}
</style></head><body>
<h1>📊 AI 交易日报</h1>
<p>生成时间：{datetime.now():%Y-%m-%d %H:%M} ｜ 数据截至：{data_until or '—'}</p>
{bench_line}
{cards_html}
<h2>多引擎资金曲线对比</h2>
<img src="data:image/png;base64,{img_b64}" alt="资金曲线"/>
<p class="foot">本报告为仿真（虚拟资金）结果，仅供学习体验，不构成投资建议。</p>
</body></html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def plot_backtest_curves(
    db: Database, settings: Settings, benchmark: list[dict] | None, out_path: Path,
    engine_types: set[str] | None = None,
) -> Path:
    """回测净值曲线对比（起点=1，避免 1e6 轴问题）+ 回撤面板 + 基准线。

    engine_types 非空时只渲染指定引擎的账户（避免混入历史残留曲线）。
    """
    from datetime import datetime

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, axes = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax1, ax2 = axes

    all_dates: list[datetime] = []
    for acc in db.get_accounts():
        if engine_types is not None and acc["engine_type"] not in engine_types:
            continue
        snaps = db.get_snapshots(acc["id"])
        if not snaps:
            continue
        dates = [datetime.strptime(s["date"], "%Y-%m-%d") for s in snaps]
        nav = [s["total_assets"] / acc["initial_capital"] for s in snaps]
        all_dates.extend(dates)
        ax1.plot(dates, nav, marker="o", ms=2, label=f"{acc['name']} ({acc['engine_type']})")

        # 回撤面板
        peak = float("-inf")
        dds: list[float] = []
        for s in snaps:
            peak = max(peak, s["total_assets"])
            dds.append((peak - s["total_assets"]) / peak * 100 if peak else 0.0)
        ax2.plot(dates, dds, lw=1, label=acc["engine_type"])

    if benchmark:
        dates = [datetime.strptime(b["date"], "%Y-%m-%d") for b in benchmark]
        nav = [b["assets"] / benchmark[0]["assets"] for b in benchmark]
        ax1.plot(dates, nav, "--", color="gray", lw=1.2, label="基准(买入持有)")
        all_dates.extend(dates)

    ax1.axhline(1.0, color="gray", ls=":", lw=0.8)
    ax1.set_title("回测净值曲线对比（起点=1.0）")
    ax1.set_ylabel("净值")
    ax1.legend()
    ax1.grid(alpha=0.3)

    ax2.axhline(0, color="gray", lw=0.8)
    ax2.set_ylabel("回撤 %")
    ax2.grid(alpha=0.3)

    # X 轴：只在每月第一天显示刻度；每年 1 月显示年份，其余月份只显示两位月份
    if all_dates:
        first_days = sorted({d.replace(day=1) for d in all_dates})
        labels = [d.strftime("%Y") if d.month == 1 else d.strftime("%m") for d in first_days]
        ax1.set_xticks(first_days)
        ax1.set_xticklabels(labels)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def build_backtest_report(
    db: Database, settings: Settings, chart_path: Path, out_path: Path,
    engine_types: set[str] | None = None,
) -> Path:
    """回测结果 HTML 报告：每账户指标表格 + 净值曲线图（engine_types 非空时只渲染指定引擎）"""
    import base64
    from datetime import datetime

    from .backtest import compute_metrics

    cards: list[str] = []
    for acc in db.get_accounts():
        if engine_types is not None and acc["engine_type"] not in engine_types:
            continue
        snaps = db.get_snapshots(acc["id"])
        if not snaps:
            continue
        trades = db.get_trades(acc["id"])
        m = compute_metrics(snaps, trades, acc["initial_capital"])
        ret_cls = "green" if m.total_return >= 0 else "red"
        cards.append(
            f"""<div class="card">
            <h2>{acc['name']}（{acc['engine_type']}）</h2>
            <table class="mt">
              <tr><td>总收益</td><td class="{ret_cls}">{m.total_return:+.2%}</td>
                  <td>年化收益</td><td class="{ret_cls}">{m.annual_return:+.2%}</td></tr>
              <tr><td>最大回撤</td><td class="red">{m.max_drawdown:.2%}</td>
                  <td>夏普比率</td><td>{m.sharpe:.2f}</td></tr>
              <tr><td>胜率</td><td>{m.win_rate:.1%}</td>
                  <td>盈亏比</td><td>{m.profit_factor:.2f}</td></tr>
              <tr><td>换手率</td><td>{m.turnover:.2f}</td>
                  <td>成交笔数</td><td>{m.trade_count}（买{m.buy_count}/卖{m.sell_count}）</td></tr>
            </table>
            </div>"""
        )
    cards_html = "\n".join(cards)

    img_b64 = ""
    if chart_path.exists():
        img_b64 = base64.b64encode(chart_path.read_bytes()).decode()

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AI 交易 · 回测报告</title>
<style>
  body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px; background: #f5f6fa; }}
  h1 {{ color: #2c3e50; }}
  .card {{ background: #fff; border-radius: 10px; padding: 16px 20px; margin: 12px 0;
          box-shadow: 0 1px 4px rgba(0,0,0,.1); border-left: 6px solid #3498db; }}
  table.mt {{ border-collapse: collapse; width: 100%; max-width: 640px; }}
  table.mt td {{ padding: 6px 12px; border-bottom: 1px solid #eee; }}
  .green {{ color: #27ae60; font-weight: bold; }}
  .red {{ color: #e74c3c; font-weight: bold; }}
  img {{ max-width: 100%; border-radius: 10px; margin-top: 12px; }}
  .foot {{ color: #95a5a6; font-size: 12px; margin-top: 20px; }}
</style></head><body>
<h1>📊 AI 交易 · 回测报告</h1>
<p>生成时间：{datetime.now():%Y-%m-%d %H:%M}｜区间内为独立回测账户（不污染每日仿真账本）</p>
{cards_html}
<h2>净值曲线与回撤</h2>
<img src="data:image/png;base64,{img_b64}" alt="回测净值曲线"/>
<p class="foot">回测为仿真结果，仅供学习与策略研究，不构成投资建议。</p>
</body></html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
