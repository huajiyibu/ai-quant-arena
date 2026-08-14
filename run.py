"""AI 自动交易体验机 · CLI 入口（对应 SRS FR10）。

用法：
    python run.py                     # 三引擎跑今日批处理
    python run.py --engine rule       # 仅规则引擎
    python run.py --engine ai         # 仅 AI 引擎（需配置 DEEPSEEK_API_KEY）
    python run.py --date 2026-08-06   # 指定交易日（回放）
    python run.py --report-only       # 只出报表，不交易
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

# pythonw（无控制台）下 stdout/stderr 为 None，print/logging 会崩溃；
# 重定向到空设备，保证静默模式下也能完整执行到生成日报
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from aitrader.batch import BatchRunner
from aitrader.config import load_settings
from aitrader.database import Database
from aitrader.datasource import AkShareDataSource, AkSharePolicySource
from aitrader.engines.base import DecisionEngine
from aitrader.engines.deepseek import DeepSeekEngine
from aitrader.engines.rule import RuleEngine
from aitrader.lock import FileLock
from aitrader.reporter import build_daily_report, build_summary, plot_compare

ROOT = Path(__file__).resolve().parent
REPORT_PATH = ROOT / "reports" / "compare.png"
DAILY_REPORT_PATH = ROOT / "reports" / "daily_report.html"
BACKTEST_CHART_PATH = ROOT / "reports" / "backtest_compare.png"
BACKTEST_REPORT_PATH = ROOT / "reports" / "backtest_report.html"
AI_CACHE_PATH = ROOT / "data" / "ai_response_cache.json"


def setup_logging() -> None:
    """日志同时落盘（data/logs/app.log，5MB×5 轮转）与控制台（有则），保证定时任务下可留证"""
    import logging.handlers

    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list = []
    if sys.stderr is not None and hasattr(sys.stderr, "write"):
        handlers.append(logging.StreamHandler(sys.stderr))
    handlers.append(
        logging.handlers.RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def write_last_run(meta: dict) -> None:
    """写 data/last_run.json：运行时间 / 模式 / 各引擎结果 / 异常，供快速核对"今天跑没跑成"""
    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "mode": meta.get("mode", ""),
        "date": meta.get("date", ""),
        "engine_results": meta.get("engine_results", {}),
        "api_stats": meta.get("api_stats", {}),
        # N-3：成交口径说明（真实盘=收盘价成交；回测 next_open+slippage 是更严苛口径）
        "fill_note": meta.get(
            "fill_note",
            "真实盘按决策日收盘价成交（滑点默认0）；回测 fill_mode=next_open + slippage "
            "是更严苛假设，真实盘表现不应优于回测",
        ),
        "ok": meta.get("ok", True),
        "error": meta.get("error", ""),
    }
    # P1-6：失败路径保留上次成功的 engine_results（便于对比"上次正常 vs 这次失败"）
    if not meta.get("ok") and not payload["engine_results"]:
        last_run_path = ROOT / "data" / "last_run.json"
        prev = {}
        if last_run_path.exists():
            try:
                prev = json.loads(last_run_path.read_text(encoding="utf-8"))
            except Exception:
                prev = {}
        if prev.get("engine_results"):
            payload["engine_results"] = prev["engine_results"]
            payload["prev_ok"] = prev.get("ok", True)
            payload["prev_date"] = prev.get("date", "")
    last_run_path = ROOT / "data" / "last_run.json"
    last_run_path.parent.mkdir(parents=True, exist_ok=True)
    last_run_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_engines(settings, response_cache: dict | None = None) -> dict[str, DecisionEngine]:
    """按配置构建引擎：规则恒有；AI 系列需配置 key"""
    engines: dict[str, DecisionEngine] = {"rule": RuleEngine()}
    if settings.api_key:
        engines["ai"] = DeepSeekEngine(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            lookback=settings.lookback_days,
            max_buy_count=settings.max_buy_count,
            temperature=settings.temperature,
            system_prompt_extra=settings.system_prompt_extra,
            feature_inject=settings.feature_inject,
            market_env_inject=settings.market_env_inject,
            feedback_n=settings.feedback_n,
            response_cache=response_cache,
        )
        engines["ai_policy"] = DeepSeekEngine(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            lookback=settings.lookback_days,
            include_policy=True,
            name="AI·政策版",
            max_buy_count=settings.max_buy_count,
            temperature=settings.temperature,
            system_prompt_extra=settings.system_prompt_extra,
            feature_inject=settings.feature_inject,
            market_env_inject=settings.market_env_inject,
            feedback_n=settings.feedback_n,
            response_cache=response_cache,
        )
    return engines


def select_backtest_engines(engine_arg: str, engines: dict) -> tuple[dict, list[str]]:
    """按 --engine 过滤回测引擎；回测无历史政策源，默认跳过政策版（避免重复结果误导）。

    Returns:
        (过滤后的引擎, 提示信息列表)
    """
    warnings: list[str] = []
    had_policy = "ai_policy" in engines
    if engine_arg == "ai":
        engines = {k: v for k, v in engines.items() if k == "ai"}
    elif engine_arg == "rule":
        engines = {k: v for k, v in engines.items() if k == "rule"}
    elif engine_arg == "ai_policy":
        engines = {k: v for k, v in engines.items() if k == "ai_policy"}
        if engines:
            warnings.append("回测无历史政策源，AI·政策版退化为纯价格版，结果与 ai 引擎一致")
    else:  # both（默认）
        engines = {k: v for k, v in engines.items() if k != "ai_policy"}
        if had_policy:
            warnings.append(
                "回测默认跳过 AI·政策版（无历史政策源，避免重复结果误导）；"
                "如确需回测请用 --engine ai_policy（退化为纯价格版）"
            )
    return engines, warnings


def select_daily_engines(engine_arg: str, engines: dict) -> tuple[dict, list[str]]:
    """按 --engine 过滤每日批处理引擎；缺 key 时给出提示并回退规则引擎。

    Returns:
        (过滤后的引擎, 提示信息列表)
    """
    warnings: list[str] = []
    if engine_arg == "ai":
        engines = {k: v for k, v in engines.items() if k == "ai"}
    elif engine_arg == "rule":
        engines = {k: v for k, v in engines.items() if k == "rule"}
    elif engine_arg == "ai_policy":
        engines = {k: v for k, v in engines.items() if k == "ai_policy"}
        if not engines:
            warnings.append("未配置 DEEPSEEK_API_KEY，AI·政策版不可用")
    if not engines:
        warnings.append("未配置 DEEPSEEK_API_KEY，仅规则引擎可用")
        engines = {"rule": RuleEngine()}
    return engines, warnings


def _date_type(s: str) -> datetime:
    """argparse 日期类型校验：非法/未来日期给出友好错误（A-4）"""
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD，收到: {s!r}")
    if d.date() > datetime.now().date():
        raise argparse.ArgumentTypeError(f"日期不能晚于今天，收到: {s!r}")
    return d


def _last_closed_trading_day() -> datetime:
    """最近一个已收盘交易日：今天 15:30 前 → 今天未收盘，取上一个交易日（A-5）。"""
    from aitrader.datasource import AkShareDataSource

    ds = AkShareDataSource()
    d = datetime.now()
    if d.time() < time(15, 30):
        d = d - timedelta(days=1)
    while not ds.is_trading_day(d):
        d = d - timedelta(days=1)
    return datetime.combine(d.date(), datetime.min.time())


def _catch_up_dates(settings, db, engines, data_source, today: datetime) -> list[datetime]:
    """计算需要补跑的缺失交易日（N-9）：目标引擎账户最近快照日的次日 → 昨天，仅交易日。

    无任何快照时返回空（从今天开始正常跑即可，无需历史补跑）。
    """
    last_dates: list[datetime] = []
    for engine_type in engines:
        acc = db.get_account_by_engine(engine_type)
        if acc:
            snaps = db.get_snapshots(acc["id"])
            if snaps:
                last_dates.append(datetime.strptime(snaps[-1]["date"], "%Y-%m-%d"))
    if not last_dates:
        return []
    anchor = max(last_dates)
    end = today - timedelta(days=1)  # 今天由当日任务处理（避免 A-3 收盘守卫）
    if anchor.date() >= end.date():
        return []
    out: list[datetime] = []
    d = anchor + timedelta(days=1)
    while d.date() <= end.date():
        if data_source.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def run_backtest(args, settings) -> int:
    """walk-forward 回测：独立数据库逐日回放引擎，输出指标 + 基准对比报表"""
    from aitrader.backtest import Backtester, compute_benchmark
    from aitrader.datasource import AkShareDataSource
    from aitrader.reporter import build_backtest_report, plot_backtest_curves

    # v0.5：覆盖成交假设 / 佣金倍率 / 采样温度 / 模型（供 A/B 实验，缓存键含这些 → 不误用旧缓存）
    if args.fill:
        settings.fill_mode = args.fill
    if args.commission_mult:
        settings.risk.commission_rate = round(
            settings.risk.commission_rate * args.commission_mult, 6
        )
    if args.temperature is not None:
        settings.temperature = args.temperature
    if args.model:
        settings.model = args.model
    if args.min_confidence is not None:
        settings.risk.min_confidence_buy = args.min_confidence
    if args.slippage is not None:
        settings.risk.slippage_bps = args.slippage
    if args.feature_inject:
        settings.feature_inject = True
    if args.market_env:
        settings.market_env_inject = True
    if args.feedback is not None:
        settings.feedback_n = args.feedback
    if args.stop_loss is not None:
        settings.risk.stop_loss_pct = args.stop_loss
    if args.take_profit is not None:
        settings.risk.take_profit_pct = args.take_profit

    # 明确请求 AI 引擎但未配置 key：直接报错，避免静默降级 rule 导致误读结果
    if args.engine in ("ai", "ai_policy") and not settings.api_key:
        print("[错误] 未配置 DEEPSEEK_API_KEY，无法回测 AI 引擎。请在 .env 配置 Key 后重试。")
        return 1

    bt_db_path = Path(args.db) if args.db else ROOT / "data" / "backtest.db"
    db = Database(bt_db_path)

    # 加载/持久化 AI 响应缓存（回测重跑不重复计费；截断到上限防膨胀）
    MAX_CACHE_ITEMS = 2000
    response_cache: dict = {}
    if AI_CACHE_PATH.exists():
        try:
            loaded = json.loads(AI_CACHE_PATH.read_text(encoding="utf-8"))
            response_cache = dict(list(loaded.items())[-MAX_CACHE_ITEMS:])
        except (json.JSONDecodeError, ValueError):
            response_cache = {}

    engines = build_engines(settings, response_cache)
    engines, warnings = select_backtest_engines(args.engine, engines)
    for w in warnings:
        print(f"[提示] {w}")
    if not engines:
        print("[提示] 未配置 DEEPSEEK_API_KEY，仅回测规则引擎。")
        engines = {"rule": RuleEngine()}

    end = args.end if args.end else _last_closed_trading_day()
    start = args.start if args.start else end - timedelta(days=120)

    # 提示 AI 回测预计调用次数（防误操作烧 API 额度；缓存命中不重复计费）
    ai_engines = [k for k in engines if k in ("ai", "ai_policy")]
    if ai_engines:
        est_days = max(int((end - start).days * 0.7), 1)
        print(
            f"[提示] 本轮回测含 AI 引擎 {ai_engines}，预计首次逐日调用 DeepSeek 约 "
            f"{est_days * len(ai_engines)} 次；已缓存区间不重复计费。"
        )

    ds = AkShareDataSource()
    results: dict[str, dict] = {}
    for engine_type, engine in engines.items():
        bt = Backtester(
            settings,
            db,
            ds,
            engine,
            engine_type,
            start,
            end,
            record_decisions=args.record_decisions,
            fill_mode=settings.fill_mode,
            adjust=args.adjust or settings.adjust,
        )
        results[engine_type] = bt.run()

    # 基准（买入持有）；未知基准代码直接报错
    bench_symbol = args.benchmark or next(iter(settings.symbols))
    bench_cfg = settings.symbols.get(bench_symbol)
    if bench_cfg is None:
        print(f"[错误] 未知基准代码: {bench_symbol}，可选: {list(settings.symbols)}")
        return 1
    bench_bars: list = []
    span_days = (end - start).days
    fetch_days = min(
        max(int(span_days * 1.5) + settings.lookback_days + 20, settings.lookback_days + 20),
        5000,
    )
    all_bars = ds.fetch_daily_bars(
        bench_symbol, fetch_days, bench_cfg.exchange, end_date=end, adjust=args.adjust or settings.adjust
    )
    bench_bars = [
        b for b in all_bars if start.date() <= b.datetime.date() <= end.date()
    ]
    benchmark = compute_benchmark(
        bench_bars, settings.initial_capital, commission_rate=settings.risk.commission_rate
    )

    # 打印指标
    print(f"\n===== 回测结果 {start.date()} ~ {end.date()} =====")
    for et, r in results.items():
        m = r["metrics"]
        if m is None:
            print(f"  [{et}] 区间内无行情")
            continue
        print(
            f"  [{et}] 总收益 {m.total_return:+.2%} | 年化 {m.annual_return:+.2%} | "
            f"最大回撤 {m.max_drawdown:.2%} | 夏普 {m.sharpe:.2f} | "
            f"胜率 {m.win_rate:.1%} | 盈亏比 {m.profit_factor:.2f} | "
            f"换手 {m.turnover:.2f} | 成交 {m.trade_count} 笔"
        )
        ric = r.get("rank_ic") or {}
        ric_n = ric.get("n", 0)
        if ric_n >= 15:
            print(f"  [{et}] Rank IC(20日): {ric['ic']:+.3f} (n={ric_n})")
        elif ric_n:
            print(f"  [{et}] Rank IC: 样本不足 n={ric_n}（<15 不显著）")
    if benchmark:
        bench_ret = benchmark[-1]["assets"] / settings.initial_capital - 1
        print(f"  基准({bench_symbol} 买入持有): {bench_ret:+.2%}")

    # 报表（只渲染本轮引擎账户，避免混入历史残留曲线误导对比）
    engine_types = set(results.keys())
    chart = plot_backtest_curves(db, settings, benchmark, BACKTEST_CHART_PATH, engine_types=engine_types)
    report = build_backtest_report(db, settings, chart, BACKTEST_REPORT_PATH, engine_types=engine_types)
    print(f"回测资金曲线: {chart}")
    print(f"回测报告: {report}")

    # 保存 AI 响应缓存（截断到上限 + 原子替换，防崩溃损坏）
    if response_cache:
        response_cache = dict(list(response_cache.items())[-MAX_CACHE_ITEMS:])
        AI_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = AI_CACHE_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(response_cache, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp_path, AI_CACHE_PATH)
        print(f"AI 响应缓存已更新: {AI_CACHE_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：单实例锁 + 统一异常处理 + 写 last_run.json"""
    parser = argparse.ArgumentParser(description="AI 自动交易体验机")
    parser.add_argument("--engine", choices=["ai", "rule", "ai_policy", "both"], default="both")
    parser.add_argument("--date", type=_date_type, help="指定交易日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--report-only", action="store_true", help="只出报表不交易")
    parser.add_argument("--force", action="store_true", help="强制重跑（跳过同日幂等检查）")
    parser.add_argument(
        "--catch-up",
        nargs="?",
        const=5,
        type=int,
        metavar="N",
        help="补跑缺失交易日（最近快照次日→昨天，最多 N 个交易日；无参数默认 5，N-9）",
    )
    parser.add_argument("--db", default=None, help="覆盖数据库路径")
    parser.add_argument("--backtest", action="store_true", help="walk-forward 回测模式（独立数据库）")
    parser.add_argument("--start", type=_date_type, help="回测起始日 YYYY-MM-DD（--backtest）")
    parser.add_argument("--end", type=_date_type, help="回测结束日 YYYY-MM-DD，默认今天")
    parser.add_argument("--benchmark", default=None, help="基准标的代码（默认取配置第一个标的）")
    parser.add_argument("--record-decisions", action="store_true", help="回测同时落库决策留痕")
    parser.add_argument(
        "--fill",
        choices=["close", "next_open"],
        default=None,
        help="回测成交假设：close 当日收盘（默认）| next_open 次日开盘（PP-1）",
    )
    parser.add_argument(
        "--commission-mult",
        type=float,
        default=None,
        help="回测佣金倍率（如 0.5/1/2），同口径校验结论稳定性（PP-1）",
    )
    parser.add_argument(
        "--temperature", type=float, default=None, help="覆盖 DeepSeek 采样温度（默认 0.3）"
    )
    parser.add_argument(
        "--model", default=None, help="覆盖 DeepSeek 模型（如 deepseek-reasoner）"
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=None,
        help="买入最低置信度门槛（0~1，默认关闭；A/B 实验用，PP-4）",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=None,
        help="滑点（bps，买卖双边；默认 0，A/B 用，P2-2）",
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=None,
        help="止损阈值（0~0.5，如 0.08=跌8%%强制卖出；默认0关，PP-5）",
    )
    parser.add_argument(
        "--take-profit",
        type=float,
        default=None,
        help="止盈阈值（0~1.0，如 0.2=涨20%%强制卖出；默认0关，PP-5）",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="打印最终生效配置（脱敏，不输出 api_key）后退出（A-2）",
    )
    parser.add_argument(
        "--adjust",
        choices=["none", "hfq"],
        default=None,
        help="回测行情复权：none 原始（默认）| hfq 后复权式（分红加回，PP-1/P1-2）",
    )
    parser.add_argument(
        "--feature-inject",
        action="store_true",
        help="提示词注入技术特征（PP-2；建议配合 --adjust hfq）",
    )
    parser.add_argument(
        "--market-env",
        action="store_true",
        help="提示词注入市场环境（B-3，探索性，默认关）",
    )
    parser.add_argument(
        "--feedback",
        type=int,
        default=None,
        help="历史盈亏反馈笔数（0=关，N=最近N笔已平仓交易，PP-6；A/B 实验用）",
    )
    args = parser.parse_args(argv)

    setup_logging()

    # 单实例锁：定时任务 + 登录启动项可能并发，后到者直接退出
    lock = FileLock(ROOT / "data" / "run.lock")
    if not lock.acquire():
        print("检测到另一实例正在运行，本次退出（单实例锁）")
        write_last_run({"mode": "locked", "engine_results": {}, "ok": True, "error": ""})
        return 0

    try:
        code = _run(args)
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("运行异常")
        write_last_run({"mode": "error", "engine_results": {}, "ok": False, "error": str(exc)})
        code = 1
    finally:
        lock.release()
    return code


def print_config(settings) -> None:
    """体检A-2：打印最终生效配置（脱敏），一键核对真实盘/回测口径。"""
    s = settings
    print("== 生效配置 ==")
    print(f"model={s.model} | temperature={s.temperature} | initial_capital={s.initial_capital}")
    print(f"lookback={s.lookback_days} | max_buy_count={s.max_buy_count} | fill_mode={s.fill_mode} | adjust={s.adjust}")
    print(
        f"feature_inject={s.feature_inject} | market_env_inject={s.market_env_inject} | "
        f"feedback_n={s.feedback_n} | cash_interest_rate={s.cash_interest_rate}"
    )
    r = s.risk
    print(
        f"risk: max_position_pct={r.max_position_pct} | max_daily_buy_pct={r.max_daily_buy_pct} | "
        f"commission_rate={r.commission_rate} | slippage_bps={r.slippage_bps} | "
        f"stop_loss_pct={r.stop_loss_pct} | take_profit_pct={r.take_profit_pct} | "
        f"min_confidence_buy={r.min_confidence_buy}"
    )
    print(f"policy.enabled={s.policy.enabled} | max_items={s.policy.max_items}")
    print(
        f"notify.enabled={s.notify.enabled} | webhook={'已配置' if s.notify.webhook_url else '未配置'}"
    )
    print(f"db_path={s.db_path} | symbols={list(s.symbols)}")
    print(f"api_key={'已配置' if s.api_key else '未配置'}（脱敏，不输出原文）")


def _run(args) -> int:
    """批处理 / 回测 / 报表 的实际执行（main 已持有单实例锁）"""
    settings = load_settings()
    if getattr(args, "print_config", False):
        print_config(settings)
        return 0
    if args.db:
        settings.db_path = Path(args.db)
    # A-1：--feature-inject 对批处理也生效（真实盘接入最优配置）
    if args.feature_inject:
        settings.feature_inject = True
    # N-6：--market-env 对批处理也生效（默认关，A/B 验证前不建议开）
    if args.market_env:
        settings.market_env_inject = True
    # 体检P0-2：真实盘也应用风控/成本参数（原只回测生效 → 真实盘恒无滑点止损）
    if args.min_confidence is not None:
        settings.risk.min_confidence_buy = args.min_confidence
    if args.slippage is not None:
        settings.risk.slippage_bps = args.slippage
    if args.stop_loss is not None:
        settings.risk.stop_loss_pct = args.stop_loss
    if args.take_profit is not None:
        settings.risk.take_profit_pct = args.take_profit

    # 回测模式（独立数据库，不走每日账本）
    if args.backtest:
        code = run_backtest(args, settings)
        write_last_run({"mode": "backtest", "engine_results": {}, "ok": code == 0})
        return code

    db = Database(settings.db_path)

    # 只出报表
    if args.report_only:
        print(build_summary(db, settings))
        out = plot_compare(db, settings, REPORT_PATH)
        report = build_daily_report(db, settings, out, DAILY_REPORT_PATH)
        print(f"资金曲线已更新: {out}")
        print(f"日报已生成: {report}")
        write_last_run({"mode": "report", "engine_results": {}, "ok": True})
        return 0

    # 构建并按参数过滤引擎
    engines, warnings = select_daily_engines(args.engine, build_engines(settings))
    for w in warnings:
        print(f"[提示] {w}")

    policy_source = AkSharePolicySource() if settings.policy.enabled else None
    runner = BatchRunner(settings, db, AkShareDataSource(), engines, policy_source=policy_source)

    # N-9：--catch-up 补跑缺失交易日（幂等：已有快照的日期自动跳过，不重复成交）
    if args.catch_up:
        catch_dates = _catch_up_dates(
            settings, db, engines, runner.data_source, datetime.now()
        )
        catch_dates = catch_dates[: max(args.catch_up, 1)]
        for cd in catch_dates:
            runner.run(cd)
        if catch_dates:
            print(
                f"[catch-up] 补跑 {len(catch_dates)} 个缺失交易日: "
                f"{[c.strftime('%Y-%m-%d') for c in catch_dates]}"
            )
        else:
            print("[catch-up] 无缺失交易日，跳过")

    # 跑批处理
    date = args.date if args.date else datetime.now()
    results = runner.run(date, force=args.force)

    # 输出汇总 + 报表
    print("\n===== 收盘汇总 =====")
    if not results:
        print("  非交易日，已跳过交易（仅刷新报表）")
    elif "_warning" in results:
        print(f"  [告警] {results['_warning']}，当日跳过交易（数据完整性保护）")
        # 仍刷新报表（用既有数据），并把告警写进 last_run
        print(build_summary(db, settings))
        out = plot_compare(db, settings, REPORT_PATH)
        report = build_daily_report(db, settings, out, DAILY_REPORT_PATH)
        print(f"资金曲线已更新: {out}")
        print(f"日报已生成: {report}")
        write_last_run({"mode": "batch", "engine_results": {}, "ok": False, "error": results["_warning"]})
        return 0
    for engine_type, r in results.items():
        if r.get("skipped"):
            print(f"  [{engine_type}] 该日已处理过，跳过（--force 可强制重跑）")
            continue
        print(
            f"  [{engine_type}] 成交 {r['trades']} 笔 | "
            f"总资产 {r['total_assets']:,.2f} | 累计盈亏 {r['pnl']:+,.2f}"
        )
    print(build_summary(db, settings))
    out = plot_compare(db, settings, REPORT_PATH)
    report = build_daily_report(db, settings, out, DAILY_REPORT_PATH)
    print(f"资金曲线已更新: {out}")
    print(f"日报已生成: {report}")

    # N-8：API 调用量记账（成本漂移可察觉）
    api_stats = {
        k: {"calls": getattr(v, "api_calls", 0), "cache_hits": getattr(v, "cache_hits", 0)}
        for k, v in engines.items()
        if hasattr(v, "api_calls")
    }

    # 写 last_run：供快速核对"今天跑没跑成"
    write_last_run(
        {
            "mode": "batch",
            "date": date.strftime("%Y-%m-%d"),
            "engine_results": {
                k: {
                    "trades": v.get("trades", 0),
                    "total_assets": v.get("total_assets"),
                    "pnl": v.get("pnl"),
                }
                for k, v in results.items()
            },
            "api_stats": api_stats,
            "ok": True,
            "error": "",
        }
    )

    # N-10：告警通知（默认关；推送失败不阻塞）
    _maybe_notify(settings, db, engines, results, date)
    return 0


def _maybe_notify(settings, db, engines, results, date) -> None:
    """N-10：批处理结束后按告警条件推送；默认关，推送失败静默。"""
    if not settings.notify.enabled or not settings.notify.webhook_url:
        return
    from aitrader.notify import check_alerts, send_notify

    ok = "_warning" not in results and all(
        not r.get("error") for r in results.values()
    )
    error = results.get("_warning", "") or next(
        (r.get("error", "") for r in results.values() if r.get("error")), ""
    )
    # P0-2：跨全部引擎账户独立判定（AI 空转/回撤也要能触发告警）
    alerts_all: list[str] = []
    n = settings.notify.idle_days
    cutoff = datetime.combine(date.date() - timedelta(days=n * 2), datetime.min.time())
    for et in engines:
        a = db.get_account_by_engine(et)
        if not a:
            continue
        snaps = db.get_snapshots(a["id"])
        if not snaps:
            continue
        recent_days = {
            t["date"] for t in db.get_trades(a["id"])
            if datetime.strptime(t["date"], "%Y-%m-%d") >= cutoff
        }
        alerts_all.extend(
            check_alerts(
                ok, error, snaps, len(recent_days),
                idle_days=n, max_drawdown=settings.notify.max_drawdown_alert,
            )
        )
    alerts = list(dict.fromkeys(alerts_all))  # 去重保序
    if alerts:
        send_notify("\n".join(alerts), settings.notify.webhook_url)


if __name__ == "__main__":
    sys.exit(main())
