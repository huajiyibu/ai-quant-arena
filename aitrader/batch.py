"""每日批处理流程编排（对应 HLD §2 batch 与 §4 时序）。

职责：
1. 拉取行情并落库
2. 对每个引擎：加载账本 → 构造上下文 → 决策 → 留痕 → 风控执行 → 记账 → 快照
3. 异常降级：单个引擎失败不影响整体流程（HLD §6）
"""
from __future__ import annotations

import logging
from datetime import datetime

from .config import Settings
from .database import Database
from .datasource import DataSource, PolicySource
from .engines.base import DecisionContext, DecisionEngine, EngineResult
from .models import AccountState, Decision, EngineType
from .portfolio import execute_decisions, refresh_prices

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

        # 0.1 日历降级守卫：交易日历不可用时，工作日可能含节假日（清明/五一等），
        #     akshare 会返回上一交易日旧 K 线易制造"节假日"假快照 → 保守跳过（--force 可强制）
        if getattr(self.data_source, "calendar_ok", True) is False and not force:
            logger.error(
                "交易日历不可用（降级为仅跳周末），工作日可能含节假日，保守跳过交易；--force 可强制运行"
            )
            return {"_warning": "calendar_unavailable"}

        # 1. 拉宏观政策（仅当存在政策版引擎时；只跑 rule 时跳过，避免浪费与噪音）
        if any(getattr(e, "include_policy", False) for e in self.engines.values()):
            self._policy_text = self._fetch_policy()
        else:
            self._policy_text = ""

        # 2. 拉行情 + 落库（截至指定日期，回放无前视）
        bars_map, failed = self._fetch_and_store(date)
        if failed:
            logger.error("行情拉取失败: %s，当日跳过交易（避免静默坏数据）", failed)
            return {"_warning": f"bar_fetch_failed:{','.join(failed)}"}

        # 2.1 数据新鲜度守卫：逐标的检查——严重陈旧的标的不参与当日成交/估值（剔除并告警）；
        #     全部陈旧则当日跳过，避免用旧价制造"当日"假快照（容忍 1~2 个自然日的 T+1 滞后）
        STALE_DAYS = 5
        fresh: dict[str, list] = {}
        stale_symbols: list[str] = []
        for sym, bars in bars_map.items():
            if not bars:
                continue
            d = bars[-1].datetime.date()
            if (date.date() - d).days > STALE_DAYS:
                stale_symbols.append(sym)
            else:
                fresh[sym] = bars
        if stale_symbols:
            logger.error("以下标的数据陈旧已剔除（>%d 天）：%s", STALE_DAYS, stale_symbols)
        if not fresh:
            logger.error("全部标的数据陈旧，当日跳过交易")
            return {"_warning": f"stale_bars:{','.join(stale_symbols)}"}
        bars_map = fresh

        # 3. 各引擎独立运行
        results: dict[str, dict] = {}
        for engine_type, engine in self.engines.items():
            results[engine_type] = self._run_engine(engine_type, engine, date, bars_map, force)
        return results

    # ------------------------------------------------------------------
    def _fetch_policy(self) -> str:
        """拉取并过滤宏观政策快讯（异常时降级为空）"""
        if not self.policy_source or not self.settings.policy.enabled:
            return ""
        try:
            news = self.policy_source.fetch_macro_news(
                self.settings.policy.keywords,
                self.settings.policy.max_items,
            )
            if news:
                logger.info("已获取 %d 条宏观政策快讯", len(news))
            return "\n".join(news)
        except Exception:
            logger.exception("宏观政策快讯获取失败，本次不参考政策")
            return ""

    def _fetch_and_store(self, end_date: datetime | None = None) -> tuple[dict[str, list], list[str]]:
        """拉取行情并落库；返回 (bars_map, 拉取失败的标的列表)"""
        bars_map: dict[str, list] = {}
        all_bars: list = []
        failed: list[str] = []
        for symbol, cfg in self.settings.symbols.items():
            try:
                bars = self.data_source.fetch_daily_bars(
                    symbol, self.settings.lookback_days + 5, cfg.exchange, end_date=end_date
                )
            except Exception:
                logger.exception("行情获取失败: %s", symbol)
                bars = []
            if not bars:
                failed.append(symbol)
            bars_map[symbol] = bars
            all_bars.extend(bars)
        self.db.save_bars(all_bars)
        return bars_map, failed

    def _run_engine(
        self,
        engine_type: str,
        engine: DecisionEngine,
        date: datetime,
        bars_map: dict[str, list],
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

        # 决策（异常降级）
        policy_text = self._policy_text if getattr(engine, "include_policy", False) else ""
        ctx = DecisionContext(
            date=date,
            account=state,
            bars=bars_map,
            symbol_names=self.settings.symbol_names,
            lookback=self.settings.lookback_days,
            policy_text=policy_text,
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
        new_state, trades, exec_results = execute_decisions(
            state, result.decisions, prices, names, self.settings.risk, date
        )
        for trade in trades:
            self.db.add_trade(account_id, trade)
        # 决策执行结果回填（风控拒绝 / 价格缺失 / 截断金额可统计）
        for sym, res in exec_results.items():
            self.db.update_decision_execution(account_id, date, sym, res)

        # 保存状态 + 快照（记录 bar_date 供审计）
        self.db.save_state(account_id, new_state)
        self.db.add_snapshot(account_id, date, new_state, bar_date=date.strftime("%Y-%m-%d"))
        self.db.complete_batch_run(account_id, date)

        return {
            "account_id": account_id,
            "trades": len(trades),
            "total_assets": new_state.total_assets,
            "pnl": new_state.total_pnl,
        }
