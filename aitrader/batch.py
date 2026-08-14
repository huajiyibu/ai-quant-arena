"""每日批处理流程编排（对应 HLD §2 batch 与 §4 时序）。

职责：
1. 拉取行情并落库
2. 对每个引擎：加载账本 → 构造上下文 → 决策 → 留痕 → 风控执行 → 记账 → 快照
3. 异常降级：单个引擎失败不影响整体流程（HLD §6）
"""
from __future__ import annotations

import logging
from datetime import datetime, time

from .config import Settings
from .database import Database
from .datasource import DataSource, PolicySource
from .engines.base import DecisionContext, DecisionEngine, EngineResult
from .models import AccountState, Bar, Decision, EngineType
from .portfolio import apply_cash_interest, apply_stop_rules, execute_decisions, refresh_prices

logger = logging.getLogger(__name__)


class BatchRunner:
    """每日批处理执行器"""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        data_source: DataSource,
        engines: dict[EngineType, DecisionEngine],
        policy_source: PolicySource | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.data_source = data_source
        self.engines = engines
        self.policy_source = policy_source
        self._policy_text: str = ""

    def run(self, date: datetime | None = None, force: bool = False) -> dict[str, dict]:
        """执行一次完整批处理，返回各引擎摘要。

        - 非交易日直接跳过，不制造虚假快照（修复周末/节假日误跑）
        - 已处理过的日期默认幂等跳过；force=True 强制重跑（修复同日重复成交）
        """
        date = date or datetime.now()

        # 0. 交易日判断：非交易日跳过批处理
        if not self.data_source.is_trading_day(date):
            logger.info("非交易日 %s，跳过批处理", date.strftime("%Y-%m-%d"))
            return {}

        # 0.1 收盘后运行守卫（A-3）：盘中（<15:00）运行会把盘中价当收盘价 → 拒绝当日结算
        if (
            date.date() == datetime.now().date()
            and datetime.now().time() < time(15, 0)
            and not force
        ):
            logger.error(
                "当前未到收盘（<15:00），拒绝当日结算（避免用盘中价当收盘价）；--force 可强制"
            )
            return {"_warning": "before_close"}

        # 0.2 日历降级守卫：交易日历不可用时，工作日可能含节假日（清明/五一等），
        #     akshare 会返回上一交易日旧 K 线易制造"节假日"假快照 → 保守跳过（--force 可强制）
        if getattr(self.data_source, "calendar_ok", True) is False and not force:
            logger.error(
                "交易日历不可用（降级为仅跳周末），工作日可能含节假日，保守跳过交易；--force 可强制运行"
            )
            return {"_warning": "calendar_unavailable"}

        # 1. 拉宏观政策（仅当存在政策版引擎时；只跑 rule 时跳过，避免浪费与噪音）
        if any(getattr(e, "include_policy", False) for e in self.engines.values()):
            self._policy_text = self._fetch_policy(date)
        else:
            self._policy_text = ""

        # 2. 拉行情 + 落库（截至指定日期，回放无前视）
        bars_map, adjusted_map, failed = self._fetch_and_store(date)
        if failed:
            logger.error("行情拉取失败: %s，当日跳过交易（避免静默坏数据）", failed)
            return {"_warning": f"bar_fetch_failed:{','.join(failed)}"}

        # 2.1 数据时点硬校验（F-1/F-2）：参与标的最新 bar 必须截至决策日。
        #     实时补全失败/缺失 → 剔除该标的并告警，杜绝"用昨日价做今日决策+估值"的静默慢一拍。
        stale_symbols: list[str] = []
        fresh: dict[str, list] = {}
        for sym, bars in bars_map.items():
            if not bars or bars[-1].datetime.date() != date.date():
                stale_symbols.append(sym)
            else:
                fresh[sym] = bars
        if stale_symbols:
            logger.error("以下标的数据非决策日，已剔除不参与当日交易/估值：%s", stale_symbols)
        if not fresh:
            logger.error("全部标的数据非决策日，当日跳过交易")
            return {"_warning": f"stale_bars:{','.join(stale_symbols)}"}
        bars_map = fresh

        # 3. 各引擎独立运行（A-2：单引擎异常隔离，不拖垮其他引擎）
        results: dict[str, dict] = {}
        for engine_type, engine in self.engines.items():
            try:
                results[engine_type] = self._run_engine(
                    engine_type, engine, date, bars_map, adjusted_map, force
                )
            except Exception as exc:
                logger.exception("引擎 %s 运行异常，已隔离（不影响其他引擎）", engine_type)
                results[engine_type] = {"error": str(exc), "skipped": True}
        return results

    # ------------------------------------------------------------------
    def _fetch_policy(self, date: datetime) -> str:
        """拉取并过滤宏观政策快讯：只取决策日当天、不晚于 15:30 的消息（去滞后+去前视）。"""
        if not self.policy_source or not self.settings.policy.enabled:
            return ""
        try:
            news = self.policy_source.fetch_macro_news(
                self.settings.policy.keywords,
                self.settings.policy.max_items,
                decision_date=date.date(),
                cutoff_time="15:30",
            )
            if news:
                logger.info("已获取 %d 条宏观政策快讯", len(news))
            return "\n".join(news)
        except Exception:
            logger.exception("宏观政策快讯获取失败，本次不参考政策")
            return ""

    def _closed_trades(self, account_id: int) -> list[dict]:
        """PP-6：该账户最近已平仓交易明细（供引擎复盘；feedback_n=0 时为空）。"""
        if not self.settings.feedback_n:
            return []
        from .attribution import closed_trade_pairs

        return closed_trade_pairs(
            self.db.get_trades(account_id), max_items=self.settings.feedback_n
        )

    def _calibrate_forward_returns(
        self, account_id: int, date: datetime, bars_map: dict
    ) -> None:
        """真实盘 Rank IC 校准（B-1）：对已满 20 交易日窗口的 buy 决策回填 forward return。

        只回填 bars 已覆盖、且决策日后 20 交易日已存在的样本（无前视）；
        窗口未满的留待后续批处理累积。
        """
        import bisect

        close_by: dict = {}
        dates_by: dict = {}
        for sym, bars in bars_map.items():
            dates_by[sym] = sorted(b.datetime.date() for b in bars)
            close_by[sym] = {b.datetime.date(): b.close for b in bars}
        for rec in self.db.get_uncalibrated_buys(account_id):
            try:
                d = datetime.strptime(rec["date"], "%Y-%m-%d").date()
                dec_date = datetime.strptime(rec["date"], "%Y-%m-%d")
            except ValueError:
                continue
            ds = dates_by.get(rec["symbol"], [])
            idx = bisect.bisect_right(ds, d)
            entry_pos, exit_pos = idx - 1, idx + 20 - 1
            if entry_pos < 0 or exit_pos >= len(ds):
                continue  # 窗口未满，等后续
            entry = close_by[rec["symbol"]].get(ds[entry_pos])
            exit_ = close_by[rec["symbol"]].get(ds[exit_pos])
            if not entry or not exit_ or entry <= 0:
                continue
            self.db.update_decision_forward_return(
                account_id, dec_date, rec["symbol"], exit_ / entry - 1
            )

    def _fetch_and_store(
        self, end_date: datetime | None = None
    ) -> tuple[dict[str, list], dict[str, list], list[str]]:
        """拉取行情并落库；返回 (bars_map, adjusted_map, 拉取失败的标的列表)。

        N-1 双 bars：bars_map 为原始行情（估值/成交/价格展示用）；adjusted_map 为本地生成的
        复权行情（特征计算专用，消除除权跳空失真，与回测 adjust=hfq 口径一致）。
        """
        bars_map: dict[str, list] = {}
        all_bars: list = []
        failed: list[str] = []
        for symbol, cfg in self.settings.symbols.items():
            try:
                bars = self.data_source.fetch_daily_bars(
                    symbol,
                    self.settings.lookback_days + 45,  # 覆盖 B-1 20 日回填窗口
                    cfg.exchange,
                    end_date=end_date,
                )
            except Exception:
                logger.exception("行情获取失败: %s", symbol)
                bars = []
            if not bars:
                # N-11：主源失败 → 尝试 bars 表缓存兜底（容灾而非提速，避免时效风险；
                #      当日新鲜度仍由后续 bar_date 硬校验把关，陈旧则剔除不参与交易）
                cached = self.db.get_bars(symbol, self.settings.lookback_days + 45)
                if cached:
                    bars = [
                        Bar(
                            symbol,
                            datetime.strptime(c["date"], "%Y-%m-%d"),
                            c["open"], c["high"], c["low"], c["close"], c["volume"],
                        )
                        for c in cached
                    ]
                    bars.sort(key=lambda b: b.datetime)
                    logger.warning(
                        "行情源失败，%s 使用 bars 缓存 %d 根（新鲜度由硬校验把关）",
                        symbol, len(bars),
                    )
            if not bars:
                failed.append(symbol)
            bars_map[symbol] = bars
            all_bars.extend(bars)
        adjusted_map = self._adjusted_bars_map(bars_map)
        self.db.save_bars(all_bars)
        return bars_map, adjusted_map, failed

    def _adjusted_bars_map(self, bars_map: dict[str, list]) -> dict[str, list]:
        """本地生成复权 K 线（N-1）：P_adj(t)=P(t)+截至 t 累计分红。

        复用 adjfactor（与回测 hfq 同一实现，零额外网络，分红进程内缓存）；
        分红拉取失败降级为原始行情，不中断当日批处理。
        """
        from .adjfactor import compute_adjusted_bars, fetch_dividends

        adjusted: dict[str, list] = {}
        for sym, bars in bars_map.items():
            cfg = self.settings.symbols[sym]
            prefix = "sh" if cfg.exchange.upper() == "SH" else "sz"
            try:
                adjusted[sym] = compute_adjusted_bars(
                    bars, fetch_dividends(f"{prefix}{sym}")
                )
            except Exception:
                logger.warning("复权因子获取失败，%s 特征回退原始行情", sym)
                adjusted[sym] = bars
        return adjusted

    def _run_engine(
        self,
        engine_type: str,
        engine: DecisionEngine,
        date: datetime,
        bars_map: dict[str, list],
        adjusted_bars_map: dict[str, list] | None = None,
        force: bool = False,
    ) -> dict:
        """运行单个引擎，返回该引擎当日摘要"""
        # 账户（不存在则创建）
        account = self.db.get_account_by_engine(engine_type)
        if account is None:
            account_id = self.db.create_account(
                f"{engine.name}引擎", engine_type, self.settings.initial_capital
            )
        else:
            account_id = account["id"]

        # 幂等：该账户该日已有快照或批处理标记则跳过（定时任务 + 启动项兜底可能同日跑两次；
        #     崩溃后重跑也据此跳过，避免重复成交）
        if not force and (self.db.has_snapshot(account_id, date) or self.db.has_batch_run(account_id, date)):
            snap = self.db.get_snapshot(account_id, date)
            logger.info(
                "账户 %s 已于 %s 处理，本次跳过（--force 可强制重跑）",
                engine.name,
                date.strftime("%Y-%m-%d"),
            )
            return {
                "account_id": account_id,
                "trades": 0,
                "skipped": True,
                "total_assets": snap["total_assets"] if snap else self.settings.initial_capital,
                "pnl": snap["pnl"] if snap else 0.0,
            }

        # 加载状态并刷新现价
        state = self.db.load_state(account_id)
        assert state is not None, "账本状态缺失"
        prices = {
            sym: bars[-1].close
            for sym, bars in bars_map.items()
            if bars
        }
        state = refresh_prices(state, prices)
        # 现金生息（P0-1 幂等）：仅当日未计息过才计，防 --force/崩溃重跑双计息
        interest_today = 0.0
        last_int = self.db.get_last_interest_date(account_id)
        today_str = date.strftime("%Y-%m-%d")
        if last_int is None or last_int < today_str:
            daily_rate = self.settings.cash_interest_rate / 252
            interest_today = round(state.cash * daily_rate, 2)
            state = apply_cash_interest(state, daily_rate)
            # 计息 marker 后移到 save_state 之后（防"marker 已写但状态未入账"的漏计窗口）

        # 决策（异常降级）
        policy_text = self._policy_text if getattr(engine, "include_policy", False) else ""
        ctx = DecisionContext(
            date=date,
            account=state,
            bars=bars_map,
            symbol_names=self.settings.symbol_names,
            lookback=self.settings.lookback_days,
            policy_text=policy_text,
            adjusted_bars=adjusted_bars_map,
            recent_closed_trades=self._closed_trades(account_id),
            feedback_n=self.settings.feedback_n,
        )
        result: EngineResult = EngineResult()
        try:
            result = engine.decide(ctx)
        except Exception as exc:
            logger.warning("引擎 %s 决策失败，本次降级为空仓决策: %s", engine.name, exc)
            fallback = Decision(
                symbol="", action="hold", reason=f"引擎异常降级: {exc}", fallback=True
            )
            self.db.add_decision(
                account_id, date, engine_type, fallback, prompt="", raw_output=str(exc)
            )

        # 决策留痕
        for d in result.decisions:
            self.db.add_decision(
                account_id, date, engine_type, d,
                prompt=result.prompt, raw_output=result.raw_output,
            )

        # 风控 + 执行 + 记账（先标记"运行中"，崩溃后重跑不重复成交）
        self.db.begin_batch_run(account_id, date)
        names = self.settings.symbol_names
        # PP-5：止损/止盈强制卖出（先于模型决策执行，reason 带 [stop_loss]/[take_profit] 标签）
        forced = apply_stop_rules(state, prices, self.settings.risk)
        new_state, trades, exec_results = execute_decisions(
            state, forced + result.decisions, prices, names, self.settings.risk, date
        )
        for trade in trades:
            self.db.add_trade(account_id, trade)
        # 决策执行结果回填（风控拒绝 / 价格缺失 / 截断金额可统计）
        for sym, res in exec_results.items():
            self.db.update_decision_execution(account_id, date, sym, res)

        # 保存状态 + 快照（记录实际 bar_date 供审计；正常情况下=决策日）
        actual_bar_date = max(
            (b[-1].datetime.date() for b in bars_map.values() if b),
            default=date.date(),
        )
        self.db.save_state(account_id, new_state)
        # P0-1：计息标记与状态同点落库（漏计窗口修复：marker 不再早于状态提交）
        if last_int is None or last_int < today_str:
            self.db.set_last_interest_date(account_id, today_str)
        source = "real" if date.date() == datetime.now().date() else "replay"
        self.db.add_snapshot(
            account_id,
            date,
            new_state,
            bar_date=actual_bar_date.strftime("%Y-%m-%d"),
            source=source,
            interest=interest_today,
        )
        # B-1：回填已满 20 交易日窗口的 buy 决策 forward return（真实盘 Rank IC 校准）。
        # N-1：用复权行情算收益（与回测口径一致，含分红）。
        self._calibrate_forward_returns(account_id, date, adjusted_bars_map or bars_map)
        self.db.complete_batch_run(account_id, date)

        return {
            "account_id": account_id,
            "trades": len(trades),
            "total_assets": new_state.total_assets,
            "pnl": new_state.total_pnl,
        }
