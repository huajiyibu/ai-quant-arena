"""v0.24b 第二批：真实盘除权日分红现金入账（P0-2 口径漂移之一）。
除权日原始价跳空 → 按持仓贷记分红现金（复用 fetch_dividends 进程缓存，仅当日单份分红，无前视）。
"""
from pathlib import Path

p = Path("aitrader/batch.py")
src = p.read_text(encoding="utf-8")

repls = [
    # 1. _run_engine 估值后调用分红入账
    (
        '''        state = refresh_prices(state, prices)
        # 现金生息（P0-1 幂等）：仅当日未计息过才计，防 --force/崩溃重跑双计息
        interest_today = 0.0
''',
        '''        state = refresh_prices(state, prices)
        # 体检P0-2：除权日分红现金入账（复用分红缓存，仅当日分红，无前视；分红现金再参与计息）
        state = self._apply_dividend_cash(state, date)
        # 现金生息（P0-1 幂等）：仅当日未计息过才计，防 --force/崩溃重跑双计息
        interest_today = 0.0
''',
    ),
    # 2. 新增 _apply_dividend_cash 方法（_adjusted_bars_map 之后、_run_engine 之前）
    (
        '''        return adjusted

    def _run_engine(
''',
        '''        return adjusted

    def _apply_dividend_cash(self, state: AccountState, date: datetime) -> AccountState:
        """体检P0-2：除权日按持仓贷记分红现金（复用 fetch_dividends 进程内缓存，
        仅当日单份分红 → 无前视）。分红现金再参与后续货基计息。失败跳过不中断。"""
        from dataclasses import replace
        from datetime import timedelta

        from .adjfactor import cumulative_dividend_at, fetch_dividends

        for sym, pos in (state.positions or {}).items():
            cfg = self.settings.symbols[sym]
            prefix = "sh" if cfg.exchange.upper() == "SH" else "sz"
            try:
                divs = fetch_dividends(f"{prefix}{sym}")
                cum_today = cumulative_dividend_at(divs, date.date())
                cum_prev = cumulative_dividend_at(divs, date.date() - timedelta(days=1))
                per_share = round(cum_today - cum_prev, 6)
                if per_share > 0:
                    amount = round(per_share * pos.volume, 2)
                    state = replace(state, cash=round(state.cash + amount, 2))
                    logger.info(
                        "除权日分红入账 %s %d股×%.4f=%.2f", sym, pos.volume, per_share, amount
                    )
            except Exception as exc:
                logger.warning("分红入账失败 %s（跳过，不中断）: %s", sym, exc)
        return state

    def _run_engine(
''',
    ),
]

for i, (old, new) in enumerate(repls, 1):
    n = src.count(old)
    assert n == 1, f"batch repl #{i}: expected 1, got {n}"
    src = src.replace(old, new)

p.write_text(src, encoding="utf-8")
print("batch.py: 分红入账 done")
