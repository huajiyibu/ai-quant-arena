"""v0.24g 第七批：回测 run ledger（体检08/12 P1-1/A-1）——
1. database.py: backtest_runs 表 + add_backtest_run/list_backtest_runs
2. run.py: run_backtest 落库（config/metrics/bench/rank_ic/api_calls）
3. run.py: --list-runs 查看台账
"""
from pathlib import Path

# ---- database.py ----
p = Path("aitrader/database.py")
src = p.read_text(encoding="utf-8")
repls = [
    # 表
    (
        '''CREATE TABLE IF NOT EXISTS batch_runs (
    account_id INTEGER NOT NULL,
    date       TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, date)
);
"""
''',
        '''CREATE TABLE IF NOT EXISTS batch_runs (
    account_id INTEGER NOT NULL,
    date       TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, date)
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT NOT NULL,
    engine_type  TEXT,
    start_date   TEXT,
    end_date     TEXT,
    config_json  TEXT,
    metrics_json TEXT,
    bench_json   TEXT,
    rank_ic      REAL,
    api_calls    INTEGER DEFAULT 0,
    cache_hits   INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL
);
"""
''',
    ),
    # 方法（get_bars 之后）
    (
        '''    def get_bars(self, symbol: str, limit: int = 120) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bars WHERE symbol=? ORDER BY date DESC LIMIT ?", (symbol, limit)
            ).fetchall()
        return [dict(r) for r in rows]
''',
        '''    def get_bars(self, symbol: str, limit: int = 120) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bars WHERE symbol=? ORDER BY date DESC LIMIT ?", (symbol, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- backtest_runs（体检P1-1：回测台账，可追溯/可对比） ----
    def add_backtest_run(
        self,
        run_at: str,
        engine_type: str | None,
        start_date: str,
        end_date: str,
        config_json: str,
        metrics_json: str,
        bench_json: str,
        rank_ic: float | None,
        api_calls: int,
        cache_hits: int,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO backtest_runs(run_at, engine_type, start_date, end_date,
                     config_json, metrics_json, bench_json, rank_ic, api_calls, cache_hits, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_at, engine_type, start_date, end_date, config_json, metrics_json,
                    bench_json, rank_ic, api_calls, cache_hits,
                    datetime.now().strftime(_DT_FMT),
                ),
            )
            return cur.lastrowid

    def list_backtest_runs(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, run_at, engine_type, start_date, end_date, metrics_json, "
                "bench_json, rank_ic, api_calls, cache_hits "
                "FROM backtest_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
''',
    ),
]
for i, (old, new) in enumerate(repls, 1):
    n = src.count(old)
    assert n == 1, f"database repl #{i}: expected 1, got {n}"
    src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("database.py: backtest_runs 表+方法 done")

# ---- run.py ----
p = Path("run.py")
src = p.read_text(encoding="utf-8")
repls = [
    # 1. run_backtest 打印后落库
    (
        '''    if benchmark:
        bench_ret = benchmark[-1]["assets"] / settings.initial_capital - 1
        print(f"  基准({bench_symbol} 买入持有): {bench_ret:+.2%}")

    # 报表（只渲染本轮引擎账户，避免混入历史残留曲线误导对比）
''',
        '''    if benchmark:
        bench_ret = benchmark[-1]["assets"] / settings.initial_capital - 1
        print(f"  基准({bench_symbol} 买入持有): {bench_ret:+.2%}")

    # 体检P1-1：回测 run ledger 落库（可追溯、可 --list-runs 对比）
    try:
        api_calls = sum(getattr(e, "api_calls", 0) for e in engines.values())
        cache_hits = sum(getattr(e, "cache_hits", 0) for e in engines.values())
        for et, r in results.items():
            m = r.get("metrics")
            if m is None:
                continue
            metrics_json = json.dumps(
                {
                    "total_return": getattr(m, "total_return", None),
                    "annual_return": getattr(m, "annual_return", None),
                    "max_drawdown": getattr(m, "max_drawdown", None),
                    "sharpe": getattr(m, "sharpe", None),
                    "win_rate": getattr(m, "win_rate", None),
                    "profit_factor": getattr(m, "profit_factor", None),
                    "turnover": getattr(m, "turnover", None),
                    "trade_count": getattr(m, "trade_count", 0),
                },
                ensure_ascii=False,
            )
            ric = r.get("rank_ic") or {}
            bench_json = json.dumps(
                {
                    "symbol": bench_symbol,
                    "start": bench_bars[0].datetime.date().isoformat() if bench_bars else None,
                    "end": bench_bars[-1].datetime.date().isoformat() if bench_bars else None,
                    "ret": (benchmark[-1]["assets"] / settings.initial_capital - 1) if benchmark else None,
                },
                ensure_ascii=False,
            )
            db.add_backtest_run(
                run_at=datetime.now().isoformat(timespec="seconds"),
                engine_type=et,
                start_date=start.date().isoformat(),
                end_date=end.date().isoformat(),
                config_json=json.dumps(
                    {
                        "fill_mode": settings.fill_mode,
                        "adjust": args.adjust or settings.adjust,
                        "feature_inject": settings.feature_inject,
                        "market_env_inject": settings.market_env_inject,
                        "feedback_n": settings.feedback_n,
                        "temperature": settings.temperature,
                        "model": settings.model,
                        "commission_rate": settings.risk.commission_rate,
                        "slippage_bps": settings.risk.slippage_bps,
                        "stop_loss_pct": settings.risk.stop_loss_pct,
                    },
                    ensure_ascii=False,
                ),
                metrics_json=metrics_json,
                bench_json=bench_json,
                rank_ic=ric.get("ic"),
                api_calls=api_calls,
                cache_hits=cache_hits,
            )
        print(f"[run-ledger] 已落库 {len(results)} 条回测记录，可用 --list-runs 查看")
    except Exception as exc:
        print(f"[run-ledger] 落库失败（不影响回测结果）: {type(exc).__name__}: {exc}")

    # 报表（只渲染本轮引擎账户，避免混入历史残留曲线误导对比）
''',
    ),
    # 2. argparse 加 --list-runs（--health 之后）
    (
        '''    parser.add_argument(
        "--health",
        action="store_true",
        help="健康自检（key/交易日历/bars新鲜度/账户快照/last_run），有问题返回非0退出码（P1-2）",
    )
''',
        '''    parser.add_argument(
        "--health",
        action="store_true",
        help="健康自检（key/交易日历/bars新鲜度/账户快照/last_run），有问题返回非0退出码（P1-2）",
    )
    parser.add_argument(
        "--list-runs",
        action="store_true",
        help="列出最近回测台账（run ledger，P1-1）后退出",
    )
''',
    ),
    # 3. _run 里 db 创建后处理 --list-runs（health 之后）
    (
        '''    # 体检P1-2：--health 自检
    if getattr(args, "health", False):
        return run_health(settings, db)

    # 只出报表
    if args.report_only:
''',
        '''    # 体检P1-2：--health 自检
    if getattr(args, "health", False):
        return run_health(settings, db)

    # 体检P1-1：--list-runs 回测台账
    if getattr(args, "list_runs", False):
        runs = db.list_backtest_runs(20)
        if not runs:
            print("回测台账为空（尚未跑过 --backtest 或未落库）")
        for r in runs:
            print(
                f"#{r['id']} {r['run_at']} {r['engine_type'] or '-'} "
                f"{r['start_date']}~{r['end_date']} ic={r['rank_ic']} "
                f"calls={r['api_calls']}"
            )
        return 0

    # 只出报表
    if args.report_only:
''',
    ),
]
for i, (old, new) in enumerate(repls, 1):
    n = src.count(old)
    assert n == 1, f"run.py repl #{i}: expected 1, got {n}"
    src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("run.py: run ledger 落库 + --list-runs done")
print("ALL OK")
