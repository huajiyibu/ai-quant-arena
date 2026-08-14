"""v0.24 第一批：口径漂移(P0-2)——
1. config.json + config.example.json: risk 块补全 slippage/stop_loss/take_profit/min_confidence（真实盘风控可生效）
2. run.py _run 批处理路径应用 --stop-loss/--take-profit/--slippage/--min-confidence（原只回测生效）
3. run.py --print-config（A-2：一键核对最终生效配置，脱敏 api_key）
"""
import json
from pathlib import Path


# ---- config.json / config.example.json：risk 补全 ----
def _patch_risk(path: Path) -> None:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    risk = cfg.setdefault("risk", {})
    # 补全真实盘可生效的风控字段（值：滑点 10bps 合理成本假设；止损/止盈/置信度尊重研究结论默认关，字段暴露可配）
    risk.setdefault("min_confidence_buy", 0.0)
    risk.setdefault("slippage_bps", 10)
    risk.setdefault("stop_loss_pct", 0.0)
    risk.setdefault("take_profit_pct", 0.0)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_patch_risk(Path("config.json"))
print("config.json risk 补全 done")

# config.example.json：risk 补全 + 补齐缺失的顶层字段（诊断11 P2-1：example 与 config 对齐）
p = Path("config.example.json")
cfg = json.loads(p.read_text(encoding="utf-8"))
risk = cfg.setdefault("risk", {})
risk.setdefault("min_confidence_buy", 0.0)
risk.setdefault("slippage_bps", 10)
risk.setdefault("stop_loss_pct", 0.0)
risk.setdefault("take_profit_pct", 0.0)
# 补齐 example 缺失字段（避免复制后新功能缩水）
cfg.setdefault("feature_inject", False)
cfg.setdefault("market_env_inject", False)
cfg.setdefault("cash_interest_rate", 0.017)
cfg.setdefault("fill_mode", "close")
cfg.setdefault("adjust", "none")
cfg.setdefault("notify", {"enabled": False, "webhook_url": ""})
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("config.example.json 补全 done")


# ---- run.py ----
p = Path("run.py")
src = p.read_text(encoding="utf-8")

repls = [
    # a. argparse 加 --print-config（--take-profit 块之后）
    (
        '''    parser.add_argument(
        "--take-profit",
        type=float,
        default=None,
        help="止盈阈值（0~1.0，如 0.2=涨20%%强制卖出；默认0关，PP-5）",
    )
''',
        '''    parser.add_argument(
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
''',
    ),
    # b. _run 批处理路径应用风控参数（N-6 market 块之后）
    (
        '''    # N-6：--market-env 对批处理也生效（默认关，A/B 验证前不建议开）
    if args.market_env:
        settings.market_env_inject = True

    # 回测模式（独立数据库，不走每日账本）
    if args.backtest:
''',
        '''    # N-6：--market-env 对批处理也生效（默认关，A/B 验证前不建议开）
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
''',
    ),
    # c. print_config 函数（def _run 之前）
    (
        '''def _run(args) -> int:
    """批处理 / 回测 / 报表 的实际执行（main 已持有单实例锁）"""
    settings = load_settings()
    if args.db:
        settings.db_path = Path(args.db)
''',
        '''def print_config(settings) -> None:
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
''',
    ),
]

for i, (old, new) in enumerate(repls, 1):
    n = src.count(old)
    assert n == 1, f"run.py repl #{i}: expected 1, got {n}"
    src = src.replace(old, new)

p.write_text(src, encoding="utf-8")
print("run.py: --print-config + 批处理风控应用 done")
print("ALL OK")
