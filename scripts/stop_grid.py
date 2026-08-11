"""PP-5 止损网格实验：rule 引擎全组合回测（免费），输出指标表供人审阅而非自动采纳。

用法（veighna python）：python scripts/stop_grid.py [--start 2021-01-01] [--end 2024-12-31]
判定：某组合样本外 Sharpe ≥ 无止损基线+0.1 且 最大回撤下降 ≥3pp 才算有效；
若所有档都不优于无止损，如实保留"无止损"为默认。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aitrader.backtest import Backtester
from aitrader.config import RiskConfig, load_settings
from aitrader.database import Database
from aitrader.datasource import AkShareDataSource
from aitrader.engines.rule import RuleEngine

STOP_GRID = [0.0, 0.05, 0.08, 0.12]
TAKE_GRID = [0.0, 0.10, 0.20]


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--engine", default="rule", choices=["rule", "ai", "ai_policy"])
    ap.add_argument("--db", default="data/stop_grid.db", help="独立回测库（避免与基线冲突）")
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")

    base = load_settings()
    ds = AkShareDataSource()
    rows: list[tuple] = []

    def _make_risk(sl: float, tp: float) -> RiskConfig:
        return RiskConfig(
            max_position_pct=base.risk.max_position_pct,
            max_daily_buy_pct=base.risk.max_daily_buy_pct,
            commission_rate=base.risk.commission_rate,
            slippage_bps=base.risk.slippage_bps,
            stop_loss_pct=sl,
            take_profit_pct=tp,
        )

    engines: dict[str, object] = {"rule": RuleEngine()}
    if args.engine != "rule":
        from aitrader.engines.deepseek import DeepSeekEngine

        engines = {
            args.engine: DeepSeekEngine(
                api_key=base.api_key,
                base_url=base.base_url,
                model=base.model,
                lookback=base.lookback_days,
                max_buy_count=base.max_buy_count,
                temperature=base.temperature,
                feature_inject=base.feature_inject,
                market_env_inject=base.market_env_inject,
                feedback_n=base.feedback_n,
            )
        }

    for sl in STOP_GRID:
        for tp in TAKE_GRID:
            settings = base.model_copy(deep=True)
            settings.risk = _make_risk(sl, tp)
            db = Database(Path(args.db))
            engine = engines[args.engine]
            bt = Backtester(
                settings, db, ds, engine, args.engine, start, end,
                fill_mode=settings.fill_mode, adjust=settings.adjust,
            )
            r = bt.run()
            m = r["metrics"]
            if m is None:
                rows.append((sl, tp, float("nan"), float("nan"), float("nan"), 0))
                continue
            rows.append((sl, tp, m.total_return, m.sharpe, m.max_drawdown, m.trade_count))
            print(
                f"  stop={sl:.0%} tp={tp:.0%} → 收益 {m.total_return:+.2%} | "
                f"夏普 {m.sharpe:.2f} | 回撤 {m.max_drawdown:.2%} | 成交 {m.trade_count}"
            )

    print(f"\n===== {args.engine} 止损网格 {start.date()} ~ {end.date()} =====")
    print("stop_loss x take_profit → 总收益 / 夏普 / 最大回撤 / 成交数")
    header = "        " + "".join(f"tp={tp:.0%}".rjust(16) for tp in TAKE_GRID)
    print(header)
    base_row = next((r for r in rows if r[0] == 0.0 and r[1] == 0.0), None)
    base_metrics = (base_row[2], base_row[3], base_row[4]) if base_row else (0, 0, 0)
    for sl in STOP_GRID:
        line = f"sl={sl:.0%} "
        for tp in TAKE_GRID:
            r = next((x for x in rows if x[0] == sl and x[1] == tp), None)
            if r:
                line += f"{r[2]:+.1%}/{r[3]:.2f}/{r[4]:.1%}".rjust(16)
        print(line)
    print(
        f"\n基线(无止损) 收益{base_metrics[0]:+.2%} 夏普{base_metrics[1]:.2f} "
        f"回撤{base_metrics[2]:.2%}"
    )
    print("判定：夏普≥基线+0.1 且 回撤下降≥3pp 才有效；否则保留'无止损'为默认。")


if __name__ == "__main__":
    main()
