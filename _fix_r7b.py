"""修复 --list-runs 查回测库 backtest.db（回测台账写在 backtest.db）。"""
from pathlib import Path

p = Path("run.py")
src = p.read_text(encoding="utf-8")

old = '''    # 体检P1-1：--list-runs 回测台账
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
'''
new = '''    # 体检P1-1：--list-runs 回测台账（查回测库 backtest.db）
    if getattr(args, "list_runs", False):
        bt_path = Path(args.db) if args.db else ROOT / "data" / "backtest.db"
        bdb = Database(bt_path)
        runs = bdb.list_backtest_runs(20)
        if not runs:
            print("回测台账为空（尚未跑过 --backtest 或未落库）")
        for r in runs:
            print(
                f"#{r['id']} {r['run_at']} {r['engine_type'] or '-'} "
                f"{r['start_date']}~{r['end_date']} ic={r['rank_ic']} "
                f"calls={r['api_calls']}"
            )
        return 0
'''

n = src.count(old)
assert n == 1, f"expected 1, got {n}"
p.write_text(src.replace(old, new), encoding="utf-8")
print("list-runs 查回测库 fix done")
